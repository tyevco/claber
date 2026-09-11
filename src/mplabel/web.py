"""
web.py - the phone app's server.

A small stdlib HTTP server so she can work the queue from her phone
instead of asking for an SSH session. No framework: the Pi dependency
list is deliberately short, and `http.server` plus a routing table is
genuinely enough for six endpoints and one user.

**This runs as a second process against the same SQLite file and the same
printer.** Both of those assumptions used to belong to the poll loop
alone, and both are handled in `cli`: `connect_db` turns on WAL and a
busy timeout, and `print_label` takes an flock. Do not reach around
either of them from here - call the same functions the CLI calls.

Two layers of authentication, because the intended deployment puts this
on the open internet through a Cloudflare tunnel and the database holds
buyers' real names and home addresses:

  outer   Cloudflare Access in front of the hostname. Does the real work.
  inner   the password and signed cookie below, so the Pi is not naked if
          the tunnel is misconfigured or someone is already on the LAN.

The inner layer is stdlib only - `hashlib.scrypt` for the password,
`hmac` for the cookie signature. Sessions are stateless signed tokens
rather than a server-side table, so a `systemctl restart` does not log
her out mid-parcel.
"""

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

# For EX_CONFIG only. printd owns that constant because it learned
# why it is needed; duplicating the number here would let the two
# drift apart from the unit files that honour it.
from . import printd as printd_mod

from . import label
from . import listings as listings_mod
from . import shopping as shopping_mod
from . import build as build_mod
from . import printers as printers_mod

log = logging.getLogger("mplabel.web")


class PrintError(Exception):
    """A print failed for a reason she can act on: printer off, out of
    paper, label does not match the sale. Mapped to 502 with the message
    intact, rather than the blanket 500 that read as "internal error" on
    the one screen where the cause matters."""


STATIC = Path(__file__).parent / "static"
COOKIE_NAME = "mplabel_session"

# The versioned alias for every /api route. Bump this and keep the
# old prefix working when something actually changes shape; today
# it is one string because nothing has.
API_VERSION = 1
API_PREFIX = f"/api/v{API_VERSION}"

# JSON bodies stay small; anything larger than this is a mistake or an
# attack, and reading it into memory on a Pi is how you get the OOM
# killer to stop the label printer.
MAX_BODY = 2 * 1024 * 1024

# Photographs are the one thing that is legitimately bigger, so the
# allowance is raised on that route alone rather than globally. An iPhone
# HEIC is a couple of megabytes and a JPEG from the same camera can be
# eight; twelve leaves room without letting any other endpoint become a
# way to hand the Pi a hundred megabytes.
MAX_PHOTO = 12 * 1024 * 1024

# A label bought anywhere else. Same reasoning as MAX_PHOTO and the same
# per-route treatment: a 4x6 of vector text is a few kilobytes, but a
# carrier that flattens its label to a 300dpi image lands a couple of
# megabytes, and a US Letter page carrying a packing slip as well can be
# more. Four is comfortably above anything real and far below the number
# that stops the printer.
MAX_LABEL = 4 * 1024 * 1024

# What the camera roll actually produces, and nothing else. The extension
# is chosen here rather than taken from the request: a filename from a
# client is an attacker-controlled string, and this way the stored name
# cannot be one.
PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heic",
}

# scrypt cost. n=2**14 with r=8 needs ~16MB, which a Pi has and an
# attacker has to spend per guess.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

# Failed logins per client before a lockout, and how long it lasts.
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 300


# ------------------------------------------------------------- passwords

def hash_password(password, salt=None):
    """`scrypt$n$r$p$salt$hash`, for pasting into mplabel.conf."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password, stored):
    """Constant-time check against a stored hash. False on anything
    malformed rather than raising - a corrupted config line should lock
    her out, not crash the service."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                            r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# -------------------------------------------------------------- sessions

def _b64d(s):
    """urlsafe b64 decode that tolerates the stripped padding."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _session_secret(cfg):
    """Derived from the password hash rather than stored separately.

    Two things fall out of that and both are wanted: there is no extra
    secret to generate, chmod and lose, and changing the password
    invalidates every outstanding session."""
    return hashlib.sha256(
        b"mplabel-session-v1" + (cfg.get("web_password_hash") or "").encode()
    ).digest()


def issue_token(cfg, days=30, now=None):
    now = int(now if now is not None else time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": now + int(days) * 86400}).encode()).rstrip(b"=")
    sig = hmac.new(_session_secret(cfg), payload, hashlib.sha256).digest()
    return "{}.{}".format(
        payload.decode(), base64.urlsafe_b64encode(sig).rstrip(b"=").decode())


def valid_token(cfg, token, now=None):
    now = now if now is not None else time.time()
    try:
        payload_s, sig_s = (token or "").split(".")
        expected = hmac.new(_session_secret(cfg), payload_s.encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(sig_s)):
            return False
        return json.loads(_b64d(payload_s)).get("exp", 0) > now
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


class Throttle:
    """Per-client failed-login counter.

    Cloudflare Access is the real gate, but this endpoint takes a single
    shared password and is reachable from the internet, so an unlimited
    guess rate would be the weakest thing in the system."""

    def __init__(self, limit=LOCKOUT_AFTER, window=LOCKOUT_SECONDS):
        self.limit, self.window = limit, window
        self._fails = {}
        self._lock = threading.Lock()

    def locked(self, who, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            count, until = self._fails.get(who, (0, 0))
            return count >= self.limit and now < until

    def record_failure(self, who, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            count, until = self._fails.get(who, (0, 0))
            if now >= until:
                count = 0
            self._fails[who] = (count + 1, now + self.window)

    def clear(self, who):
        with self._lock:
            self._fails.pop(who, None)


# -------------------------------------------------- the static frontends

# Every script and stylesheet either shell loads. A file missing from
# here ships to a client that goes on using its cached copy - the change
# is deployed, the service is restarted, and it is simply not there,
# which reads as the deploy having failed rather than as a stale asset.
#
# One tuple, read by both `asset_stamp` and `shell_html`. It used to be
# written out twice, which was two places to forget a file.
STAMPED_ASSETS = ("app.js", "app.css", "marker.js",
                  "tokens.css", "common.js", "desk.js", "desk.css")

# The HTML that gets stamped and served no-store. Two front ends now: the
# phone app at `/` and the desk portal at `/desk`.
SHELLS = ("index.html", "desk.html")

# What a bare entry point means. `/desk` is an alias resolved here rather
# than a branch in the dispatcher, so it goes through the same
# containment check as everything else under `static/`.
SHELL_ALIASES = {"": "index.html", "desk": "desk.html", "desk/": "desk.html"}

# Which front end a browser gets at `/`, when nobody has said.
#
# **"Request Desktop Site" is not a separate thing to honour.** That
# button works by rewriting the User-Agent: iOS Safari starts sending a
# macOS one, and Android Chrome drops its `Mobi` token. So a sniff that
# looks at those tokens respects the button for free, and a sniff that
# looks at anything else (screen size guesses, a vendor header) actively
# fights it. This list is short on purpose for that reason - it is
# exactly the set of tokens the button manipulates.
#
# `ipad` is in it for iPadOS 12 and earlier. From 13 on, Safari on an
# iPad reports itself as a Mac and there is nothing in the request that
# says otherwise, so an iPad gets the desk. That is a real limitation
# rather than an oversight: the preference cookie below is the answer,
# and the phone app's manifest pins itself so an installed copy can
# never be redirected away.
MOBILE_TOKENS = ("iphone", "ipod", "ipad", "android", "mobi")

# Set by `?ui=phone` / `?ui=desk`, and it beats the sniff. Every
# auto-route needs a way to say "no, this one" that survives the next
# visit, or the answer to guessing wrong is to stop using the thing.
UI_COOKIE = "mplabel_ui"
UI_CHOICES = ("phone", "desk")


def prefers_desk(user_agent, sec_ch_ua_mobile=None):
    """Would this browser rather have the desk portal?

    `Sec-CH-UA-Mobile` first where it exists. It is the designed
    replacement for reading the UA string, Chrome and Edge send it
    unasked, and it flips with "Request Desktop Site" like everything
    else does. Safari does not send it, which is what the token list is
    for."""
    hint = (sec_ch_ua_mobile or "").strip()
    if hint in ("?0", "?1"):
        return hint == "?0"
    ua = (user_agent or "").lower()
    return not any(token in ua for token in MOBILE_TOKENS)


def safe_static_path(rel):
    """Resolve `rel` under STATIC, or None if it escapes.

    Serving files by name off a user-supplied path is the classic
    traversal hole, and it reads as fine right up until someone asks for
    ../../../etc/passwd. Resolve first, then check containment - string
    prefix checks miss symlinks."""
    rel = unquote(rel or "").lstrip("/")
    rel = SHELL_ALIASES.get(rel, rel)
    try:
        target = (STATIC / rel).resolve()
        target.relative_to(STATIC.resolve())
    except (ValueError, OSError):
        return None
    return target


def asset_stamp():
    """A short hex stamp that moves whenever a served asset does."""
    newest = 0
    for name in STAMPED_ASSETS:
        try:
            newest = max(newest, int((STATIC / name).stat().st_mtime))
        except OSError:
            pass
    return format(newest, "x")


def shell_html(path):
    """A shell with its asset URLs version-stamped."""
    stamp = asset_stamp()
    html = path.read_text(encoding="utf-8")
    for name in STAMPED_ASSETS:
        html = html.replace(f'"/{name}"', f'"/{name}?v={stamp}"')
    return html


def safe_home_path(home, stored):
    """A file this server is allowed to hand out: one inside `home`.

    The path comes from the database rather than the request, so this is
    belt and braces - but `cmd_file` can write a PDF anywhere, and a row
    edited by hand should not be able to turn into an arbitrary file
    read."""
    if not stored:
        return None
    try:
        target = Path(stored).resolve()
        target.relative_to(Path(home).resolve())
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


def safe_label_path(home, stored):
    """The archived label for one sale. See `safe_home_path`."""
    return safe_home_path(home, stored)


def photo_dir(home):
    """Where photograph bytes live - `home/photos/`, beside `labels/`."""
    return Path(home) / "photos"


# ---------------------------------------------------------- serialisation

# What a client is allowed to write on a listing. `paid` and `bin` are
# not here because neither is a plain column write - one is a money parse
# and the other resolves a code or a name against `bins`.
#
# One tuple, read by the single-item route and by the bulk one. Two lists
# would mean a field editable one row at a time and not forty, which is
# the kind of difference nobody notices until they hit it.
ITEM_FIELDS = ("title", "price", "category", "condition", "era",
               "notes", "description", "state")


def _item_sets(body):
    """The SET clause for whatever of `ITEM_FIELDS` a body carries."""
    sets, params = [], []
    for key in ITEM_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if key == "price" and value not in (None, ""):
            value = listings_mod.parse_money(value)
            if value is None:
                raise ValueError("price must be a number")
        sets.append(f"{key}=?")
        params.append(value if value != "" else None)
    return sets, params


def _order_row(r):
    """The queue payload. Deliberately no address.

    She is looking at a list on a phone in a kitchen; the buyer's home
    address belongs on the one screen that needs it, not in every
    response that might get cached or screenshotted."""
    buyer = (r["buyer"] or "").strip()
    return {
        "id": r["id"],
        "code": r["code"],
        "item": r["item"],
        "buyer": buyer.split()[0] if buyer else None,
        "price": r["price"],
        "ship_by": r["ship_by"],
        "status": r["status"],
        "printed": bool(r["printed_at"]),
        # Not the same question as `printed`, and the queue needs both: a
        # local-pickup sale has no label file and never will, while a
        # recorded-but-unprinted one has a file waiting. One is a state,
        # the other is a job.
        "has_label": bool(r["label_pdf"]),
        # A print failure is written to sales.notes, and Pending is the
        # screen she looks at after one. Without this the note existed
        # only on the detail screen of an order she has no reason to
        # suspect.
        "notes": r["notes"],
    }


def _order_detail(r):
    d = _order_row(r)
    d.update({
        "buyer": r["buyer"],
        "order_id": r["order_id"],
        "listing_id": r["listing_id"],
        "received_at": r["received_at"],
        "tracking": r["tracking"],
        "ship_to": r["ship_to"],
        "weight": r["weight"],
        "service": r["service"],
        "notes": r["notes"],
        "printed_at": r["printed_at"],
        "print_count": r["print_count"],
        "has_label": bool(r["label_pdf"]),
    })
    # Postage, and whether anybody actually knows it. The two travel
    # together on purpose: a number without its provenance is an estimate
    # that will be read as a fact by the next screen to show it.
    postage = _column(r, "postage")
    d["postage"] = postage
    d["postage_source"] = _column(r, "postage_source")
    d["kept"] = listings_mod.kept(r["price"], postage)
    return d


def _column(row, name):
    """A column that may predate its migration on somebody's database."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


# ------------------------------------------------------------- the server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Do not advertise the Python version to the open internet.
    server_version = "mplabel"
    sys_version = ""

    # (method, compiled path, handler name, needs auth)
    ROUTES = [
        ("GET", r"^/healthz$", "h_health", False),
        ("POST", r"^/api/login$", "h_login", False),
        ("POST", r"^/api/logout$", "h_logout", False),
        ("GET", r"^/api/session$", "h_session", False),
        ("GET", r"^/api/orders$", "h_orders", True),
        ("GET", r"^/api/orders/(?P<sid>\d+)$", "h_order", True),
        ("GET", r"^/api/orders/(?P<sid>\d+)/label$", "h_label", True),
        ("GET", r"^/api/lookup/(?P<code>[0-9A-Za-z]{3,4})$", "h_lookup", True),
        ("GET", r"^/api/pending$", "h_pending", True),
        ("GET", r"^/api/bins$", "h_bins", True),
        ("POST", r"^/api/bins$", "h_make_bin", True),
        ("GET", r"^/api/bins/(?P<needle>[^/]{1,64})$", "h_bin", True),
        ("POST", r"^/api/bins/(?P<needle>[^/]{1,64})/name$", "h_rename_bin",
         True),
        ("GET", r"^/api/inventory$", "h_inventory", True),
        ("GET", r"^/api/inventory/(?P<lid>\d+)$", "h_item", True),
        ("POST", r"^/api/inventory/(?P<lid>\d+)/bin$", "h_move_bin", True),
        ("GET", r"^/api/sold$", "h_sold", True),
        ("GET", r"^/api/stats$", "h_stats", True),
        ("GET", r"^/api/system$", "h_system", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/ship$", "h_ship", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/unship$", "h_unship", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/fields$", "h_fields", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/print$", "h_print", True),
        ("POST", r"^/api/print/pending$", "h_print_pending", True),
        # A label from anywhere else. No order behind it and no row after
        # it - see h_print_label.
        ("POST", r"^/api/print/label$", "h_print_label", True),
        ("POST", r"^/api/label/preview$", "h_label_preview", True),
        # The sourcing half. Cost basis enters the system here, which is
        # why every margin in the analytics is null until it does.
        ("GET", r"^/api/trips$", "h_trips", True),
        ("POST", r"^/api/trips$", "h_make_trip", True),
        ("GET", r"^/api/trips/(?P<tid>\d+)$", "h_trip", True),
        ("POST", r"^/api/trips/(?P<tid>\d+)/fields$", "h_trip_fields", True),
        ("GET", r"^/api/photos$", "h_photos", True),
        ("POST", r"^/api/photos$", "h_add_photo", True),
        ("GET", r"^/api/photos/(?P<pid>\d+)$", "h_photo", True),
        ("POST", r"^/api/photos/(?P<pid>\d+)/attach$", "h_attach_photo",
         True),
        # The aisle: what she pointed the camera at, and what she did
        # about it.
        ("GET", r"^/api/worth$", "h_worth", True),
        ("GET", r"^/api/candidates$", "h_candidates", True),
        ("POST", r"^/api/candidates$", "h_add_candidate", True),
        ("POST", r"^/api/candidates/(?P<cid>\d+)$", "h_update_candidate",
         True),
        ("POST", r"^/api/candidates/(?P<cid>\d+)/decision$", "h_decide",
         True),
        ("POST", r"^/api/trips/(?P<tid>\d+)/receipt$", "h_receipt", True),
        ("GET", r"^/api/trips/(?P<tid>\d+)/receipt-lines$",
         "h_receipt_lines", True),
        ("GET", r"^/api/trips/(?P<tid>\d+)/reconcile$", "h_propose", True),
        ("POST", r"^/api/trips/(?P<tid>\d+)/reconcile$", "h_reconcile",
         True),
        ("POST", r"^/api/devices$", "h_register_device", True),
        ("GET", r"^/api/devices$", "h_devices", True),
        ("POST", r"^/api/inventory$", "h_make_item", True),
        ("POST", r"^/api/inventory/(?P<lid>\d+)/fields$", "h_item_fields",
         True),
        ("POST", r"^/api/inventory/bulk$", "h_bulk_items", True),
        ("GET", r"^/api/inventory/(?P<lid>\d+)/photos$", "h_item_photos",
         True),
        ("POST", r"^/api/import/preview$", "h_import_preview", True),
        ("POST", r"^/api/import/commit$", "h_import_commit", True),
    ]
    _COMPILED = [(m, re.compile(p), h, a) for m, p, h, a in ROUTES]

    # --- plumbing

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    @property
    def cfg(self):
        return self.server.cfg

    def db(self):
        return self.server.db()

    def _send(self, status, body=b"", ctype="application/json",
              extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This is a private tool; nothing here should be framed or sniffed.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, status=200, extra_headers=()):
        self._send(status, json.dumps(obj).encode(), extra_headers=extra_headers)

    def fail(self, status, message):
        self.json({"error": message}, status=status)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw)

    def client_id(self):
        """Who to throttle.

        Behind the tunnel every connection arrives from 127.0.0.1, so the
        peer address alone would lump the whole internet together.
        CF-Connecting-IP is only trustworthy because we bind to loopback
        and cloudflared is the sole thing that can reach us."""
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def authed(self):
        """A signed token, from a cookie or an Authorization header.

        The token was already a stateless bearer credential - a cookie
        was just how a browser carries one. A native app has no cookie
        jar worth the name, and `Set-Cookie` handling outside a browser
        is the sort of thing that works until it silently does not, so
        the header is the first-class form and the cookie stays for the
        PWA. Same token, same signature, same expiry: nothing new to
        revoke and no second credential to leak."""
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            return valid_token(self.cfg, auth[7:].strip())
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(COOKIE_NAME)
        return bool(morsel) and valid_token(self.cfg, morsel.value)

    def _cookie_header(self, value, max_age):
        secure = str(self.cfg.get("web_secure_cookie", "auto")).lower()
        if secure == "auto":
            https = (self.headers.get("X-Forwarded-Proto", "").lower()
                     == "https")
        else:
            https = secure in ("1", "yes", "true", "on")
        parts = [f"{COOKIE_NAME}={value}", "Path=/", "HttpOnly",
                 "SameSite=Lax", f"Max-Age={max_age}"]
        if https:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    # --- dispatch

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        # Routed as a GET; _send drops the body but keeps the headers, so
        # a health checker or a curl -I gets the truth rather than a 501.
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        path = urlparse(self.path).path
        # `/api/v1/...` is the same surface under a name that will not
        # move. The PWA ships with this server and can be changed in the
        # same commit as a route; an app on a phone cannot, and a native
        # client that has to guess whether /api/orders still means what
        # it meant last month has no contract at all. One alias, no
        # duplicated handlers, and the day v2 is needed the unversioned
        # paths are what get to change.
        if path.startswith(API_PREFIX + "/"):
            path = "/api/" + path[len(API_PREFIX) + 1:]
        try:
            for m, pattern, name, needs_auth in self._COMPILED:
                if m != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                if needs_auth and not self.authed():
                    return self.fail(401, "not authenticated")
                # SameSite=Lax stops a cross-site form POST carrying the
                # cookie, and a custom header cannot be set cross-origin
                # without a preflight this server never approves. Together
                # that is enough CSRF protection for one user and no
                # third-party embeds.
                if method != "GET" and needs_auth and \
                        self.headers.get("X-Mplabel") != "1":
                    return self.fail(400, "missing X-Mplabel header")
                return getattr(self, name)(**match.groupdict())
            if method == "GET":
                routed = self.route_shell(path)
                if routed is not None:
                    return routed
                return self.serve_static(path)
            self.fail(404, "no such endpoint")
        except PrintError as exc:
            # 502, not 500: the printer failed, not this server, and she
            # needs the actual reason. The blanket handler below rendered
            # a switched-off printer as the literal "internal error".
            log.error("print failed: %s", exc)
            self.fail(502, str(exc))
        except ValueError as exc:
            self.fail(400, str(exc))
        except BrokenPipeError:
            log.debug("client went away")
        except Exception:
            # Never hand a traceback to the browser.
            log.exception("unhandled error serving %s", path)
            self.fail(500, "internal error")

    # --- handlers

    def h_health(self):
        """Unauthenticated on purpose, and says nothing about the data.

        The build stamp is not data - it is the only way to tell whether
        the Pi is running the code you think it is, which the version
        string cannot do because it never moves."""
        self.json({"ok": True, "build": build_mod.stamp()})

    def h_login(self):
        who = self.client_id()
        if self.server.throttle.locked(who):
            return self.fail(429, "too many attempts, wait a few minutes")
        password = (self.body() or {}).get("password") or ""
        stored = self.cfg.get("web_password_hash") or ""
        if not stored or not verify_password(password, stored):
            self.server.throttle.record_failure(who)
            log.warning("failed login from %s", who)
            return self.fail(401, "wrong password")
        self.server.throttle.clear(who)
        days = int(self.cfg.get("web_session_days", 30))
        token = issue_token(self.cfg, days)
        # The token is in the body as well as the cookie. The browser
        # uses the cookie and ignores this; a native client stores the
        # token and sends it as `Authorization: Bearer`. Handing it over
        # rather than making the app scrape Set-Cookie is the difference
        # between a contract and a trick.
        self.json({"ok": True, "token": token,
                   "expires_in": days * 86400},
                  extra_headers=[self._cookie_header(token, days * 86400)])

    def h_logout(self):
        self.json({"ok": True}, extra_headers=[self._cookie_header("", 0)])

    def h_session(self):
        self.json({"authenticated": self.authed(),
                   "api_version": API_VERSION,
                   "api_prefix": API_PREFIX})

    def h_bins(self):
        """Every bin, with how much is in it.

        A bin exists because someone named it, so a newly made or newly
        emptied one is still on this list - the app's picker is this list
        and a "new bin" field, not a free-text box."""
        return self.json({"bins": listings_mod.bins_in_use(self.db())})

    def h_make_bin(self):
        """Name a place. The code is minted here, not typed.

        Returns the code so the client can print a shelf tag for it
        without a second request."""
        body = self.body() or {}
        row = listings_mod.create_bin(self.db(), body.get("name"),
                                      notes=body.get("notes"))
        return self.json({"ok": True, "bin": row})

    def h_rename_bin(self, needle):
        """Change what a bin is called. The code, and the tag on the
        shelf carrying it, do not move."""
        import urllib.parse

        found = listings_mod.find_bin(self.db(),
                                      urllib.parse.unquote(needle))
        if not found:
            return self.fail(404, "no such bin")
        body = self.body() or {}
        row = listings_mod.rename_bin(self.db(), found["code"],
                                      body.get("name"))
        return self.json({"ok": True, "bin": row})

    def h_bin(self, needle):
        """What is in one bin, addressed by code or by name."""
        import urllib.parse

        try:
            found = listings_mod.bin_contents(
                self.db(), urllib.parse.unquote(needle))
        except ValueError:
            return self.fail(404, "no such bin")
        return self.json(found)

    def h_inventory(self):
        """The shelf, searchable.

        One query across title, bin name, bin code and category, because
        that is how the thing is actually looked for - "the blue one",
        "ATTIC", "glass" - and separate filters would make her choose
        which kind of remembering she is doing before she has
        remembered."""
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q") or [""])[0].strip()
        state = (qs.get("state") or [""])[0].strip()
        limit = min(int((qs.get("limit") or ["200"])[0] or 200), 500)

        # `paid` and `days_listed` are here for the desk's table, which
        # has Paid, Margin and Listed columns. `days_listed` is the
        # expression `v_listing_perf` uses, copied rather than reworded:
        # a table and the analytics beside it disagreeing about how old
        # something is would be a bug nobody could see.
        sql = ("SELECT l.id, l.listing_id, l.title, l.price, l.paid, "
               "l.state, l.category, l.era, l.inventory_code, l.bin_code, "
               "b.name AS bin, l.listed_at, l.sold_at, "
               "CASE WHEN l.sold_at IS NULL AND l.listed_at IS NOT NULL "
               "     THEN CAST(julianday('now') - julianday(l.listed_at) "
               "               AS INTEGER) END AS days_listed "
               "FROM listings l LEFT JOIN bins b ON b.code = l.bin_code "
               "WHERE 1=1")
        args = []
        if state:
            sql += " AND l.state=?"
            args.append(state)
        if q:
            sql += (" AND (l.title LIKE ? OR b.name LIKE ? OR l.bin_code = ? "
                    "OR l.category LIKE ? OR l.inventory_code LIKE ?)")
            args += [f"%{q}%", f"%{q}%", q.strip().upper(),
                     f"%{q}%", f"%{q}%"]
        sql += " ORDER BY COALESCE(l.sold_at, l.listed_at, l.title) DESC LIMIT ?"
        args.append(limit)

        rows = [dict(r) for r in self.db().execute(sql, args).fetchall()]

        # How many are in each state, over the whole table - deliberately
        # ignoring `q`, `state` and `limit`.
        #
        # It rides on this response rather than being its own endpoint
        # because the shelf is the screen that needs it and it already
        # makes this request; a fourth call from a phone on house Wi-Fi
        # to answer one integer is a worse trade.
        #
        # Unfiltered because the number it exists for is "what has
        # arrived that I have not listed", which is a fact about the
        # shelf and not about the search box. Scoped to the query it
        # would fall to nothing the moment she typed, which is exactly
        # when a queue count is least believable.
        states = {r["state"] or "unknown": r["n"] for r in self.db().execute(
            "SELECT state, COUNT(*) AS n FROM listings GROUP BY state")}
        return self.json({"items": rows, "count": len(rows),
                          "states": states})

    def h_item(self, lid):
        """One thing, plus what else is in its bin.

        `bin_mates` is the number the shelf view needs and the client
        should not have to derive with a second request."""
        item = self._item_row(int(lid))
        if item is None:
            return self.fail(404, "no such item")
        mates = 0
        if item.get("bin_code"):
            mates = max(0, len(listings_mod.bin_contents(
                self.db(), item["bin_code"])["items"]) - 1)
        item["bin_mates"] = mates
        return self.json({"item": item})

    def h_move_bin(self, lid):
        """Put one thing in a bin, or take it out of one.

        The body takes a code or a name, because the phone has scanned
        one and a person has typed the other. An empty value clears it,
        which is what "not set" means on the shelf screen - there is no
        separate delete."""
        body = self.body() or {}
        code = listings_mod.set_bin(self.db(), int(lid), body.get("bin"))
        return self.json({"ok": True, "id": int(lid), "bin_code": code})

    # --- the aisle

    def h_worth(self):
        """What things like this have actually sold for.

        Answered from her own history and nothing else. The model's guess
        travels separately and is labelled as a guess - it has no market
        data, so putting the two in one number would launder one into the
        other."""
        qs = parse_qs(urlparse(self.path).query)
        self.json(listings_mod.worth(
            self.db(),
            category=(qs.get("category") or [None])[0],
            title=(qs.get("title") or [None])[0]))

    def h_candidates(self):
        qs = parse_qs(urlparse(self.path).query)
        trip = (qs.get("trip") or [None])[0]
        decision = (qs.get("decision") or [None])[0]
        self.json({"candidates": shopping_mod.candidates(
            self.db(), trip_id=int(trip) if trip else None,
            decision=decision or None)})

    def h_add_candidate(self):
        """Something she photographed. No decision yet, and no listing -
        most of these never become one."""
        body = self.body() or {}
        self.json({"ok": True, "candidate": shopping_mod.add_candidate(
            self.db(), trip_id=body.get("trip"), photo_id=body.get("photo"),
            title=body.get("title"), era=body.get("era"),
            condition=body.get("condition"), category=body.get("category"),
            asking=body.get("asking"))})

    def h_update_candidate(self, cid):
        body = self.body() or {}
        self.json({"ok": True,
                   "candidate": shopping_mod.update(self.db(), int(cid),
                                                    **body)})

    def h_decide(self, cid):
        """Cart it or put it back. A passed one is kept - the same object
        turns up again next month."""
        body = self.body() or {}
        self.json({"ok": True,
                   "candidate": shopping_mod.decide(self.db(), int(cid),
                                                    body.get("decision"))})

    def h_receipt(self, tid):
        """The receipt, as text the phone read off it.

        The OCR happens on the phone - Vision does it on-device for free,
        and shipping the picture here to read it would put her receipts
        on the wire for no gain. What arrives is the reading; the
        photograph stays where it was taken."""
        body = self.body() or {}
        lines = shopping_mod.store_receipt(self.db(), int(tid),
                                           body.get("text"))
        self.json({"ok": True, "lines": lines,
                   "trip": listings_mod.trip_summary(self.db(), int(tid))})

    def h_receipt_lines(self, tid):
        self.json({"lines": shopping_mod.receipt(self.db(), int(tid)),
                   "trip": listings_mod.trip_summary(self.db(), int(tid))})

    def h_propose(self, tid):
        """What it thinks the receipt says about the cart. Writes nothing."""
        self.json(shopping_mod.propose(self.db(), int(tid)))

    def h_reconcile(self, tid):
        """Turn what she confirmed into inventory. Only what she confirmed."""
        body = self.body() or {}
        created = shopping_mod.apply(self.db(), int(tid),
                                     body.get("assignments"))
        self.json({"ok": True, "created": created,
                   "trip": listings_mod.trip_summary(self.db(), int(tid))})

    # --- push

    def h_register_device(self):
        """A phone asking to be told.

        Authenticated like everything else: a token registered by anyone
        who could reach this port would be a stranger receiving her
        buyers' names in a notification."""
        from . import notify as notify_mod

        body = self.body() or {}
        conn = self.db()
        conn.executescript(notify_mod.SCHEMA)
        token = notify_mod.register(
            conn, body.get("token"),
            environment=body.get("environment") or "production",
            label=body.get("label"))
        self.json({"ok": True, "registered": token[:8] + "..."})

    def h_devices(self):
        """What is registered, without handing the tokens back out."""
        from . import notify as notify_mod

        conn = self.db()
        conn.executescript(notify_mod.SCHEMA)
        rows = []
        for device in notify_mod.devices(conn):
            rows.append({"environment": device["environment"],
                         "registered_at": device["registered_at"],
                         "label": device["label"],
                         "token_prefix": device["token"][:8]})
        self.json({"devices": rows})

    # --- the sourcing half
    #
    # What a thing sold for has always been here; what it *cost* arrives
    # through these. Until it does, `v_listing_perf.margin` is null on
    # every row and the profit screen can only honestly say "gross".

    def h_trips(self):
        self.json({"trips": listings_mod.trip_summary(self.db())})

    def h_make_trip(self):
        body = self.body() or {}
        trip = listings_mod.create_trip(
            self.db(), body.get("store"),
            occurred_at=body.get("occurred_at"),
            receipt_total=body.get("receipt_total"),
            notes=body.get("notes"))
        self.json({"ok": True, "trip": trip})

    def h_trip(self, tid):
        """One run: the money, what came home, and the receipt shots.

        The receipt's own lines are not in here and there is no table for
        them. A thrift receipt itemises by department - "HOUSEWARES
        $4.99" - so a line is not an object and parsing it into rows
        would invent a precision the paper does not have. The photograph
        is the record; the attribution is the person's."""
        trip = listings_mod.trip_summary(self.db(), int(tid))
        if trip is None:
            return self.fail(404, "no such trip")
        items = self.db().execute(
            "SELECT id, title, price, paid, state, inventory_code, bin_code "
            "FROM listings WHERE trip_id=? ORDER BY id", (int(tid),))
        photos = self.db().execute(
            "SELECT id, taken_at, listing_id FROM photos WHERE trip_id=? "
            "ORDER BY id", (int(tid),))
        self.json({"trip": trip,
                   "items": [dict(r) for r in items],
                   "photos": [dict(r) for r in photos]})

    def h_trip_fields(self, tid):
        """Correct a trip. Allow-listed, the same way `h_fields` is."""
        if listings_mod.trip_summary(self.db(), int(tid)) is None:
            return self.fail(404, "no such trip")
        body = self.body() or {}
        sets, params = [], []
        for key in ("store", "occurred_at", "receipt_total", "notes"):
            if key not in body:
                continue
            value = body[key]
            if key == "receipt_total" and value not in (None, ""):
                value = listings_mod.parse_money(value)
                if value is None:
                    raise ValueError("receipt_total must be a number")
            sets.append(f"{key}=?")
            params.append(value if value != "" else None)
        if not sets:
            raise ValueError("nothing to change")
        params.append(int(tid))
        self.db().execute(
            f"UPDATE trips SET {', '.join(sets)} WHERE id=?", params)
        self.db().commit()
        self.json({"ok": True,
                   "trip": listings_mod.trip_summary(self.db(), int(tid))})

    def h_photos(self):
        """The triage pile: captures that are not about anything yet."""
        self.json({"photos": listings_mod.untriaged(self.db())})

    def h_add_photo(self):
        """One photograph, as raw bytes with a real Content-Type.

        Not multipart. There is exactly one file and no other fields - the
        trip or listing it belongs to is in the query string - so a
        multipart parser here would be a dependency and a second thing to
        get wrong for no gain.

        The stored name is the digest, which is what makes an upload
        idempotent: she is in a shop on one bar of signal, the client
        retries, and the same bytes must not become a second row in the
        triage pile."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        ext = PHOTO_TYPES.get(ctype.lower())
        if ext is None:
            raise ValueError(
                "a photo must be " + ", ".join(sorted(set(PHOTO_TYPES)))
                + f" - got {ctype or 'nothing'}")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("no photo in the body")
        if length > MAX_PHOTO:
            raise ValueError("photo too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("the upload was cut short")

        digest = hashlib.sha256(raw).hexdigest()
        directory = photo_dir(self.cfg.get("home"))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + ext)
        # Written before the row, and only if it is not already there.
        # A row pointing at a file that does not exist is the failure
        # `verify` exists to catch elsewhere; do not create one here.
        if not path.exists():
            path.write_bytes(raw)

        qs = parse_qs(urlparse(self.path).query)
        trip = (qs.get("trip") or [None])[0]
        listing = (qs.get("listing") or [None])[0]
        photo = listings_mod.add_photo(
            self.db(), path, sha256=digest,
            trip_id=int(trip) if trip else None,
            listing_id=int(listing) if listing else None)
        self.json({"ok": True, "photo": photo})

    def h_photo(self, pid):
        row = self.db().execute("SELECT path FROM photos WHERE id=?",
                                (int(pid),)).fetchone()
        if row is None:
            return self.fail(404, "no such photo")
        path = safe_home_path(self.cfg.get("home"), row["path"])
        if path is None:
            return self.fail(404, "the file is gone")
        ctype = next((k for k, v in PHOTO_TYPES.items()
                      if v == path.suffix.lower()), "application/octet-stream")
        self._send(200, path.read_bytes(), ctype=ctype)

    def h_attach_photo(self, pid):
        """Say what a capture turned out to be about."""
        if self.db().execute("SELECT id FROM photos WHERE id=?",
                             (int(pid),)).fetchone() is None:
            return self.fail(404, "no such photo")
        body = self.body() or {}
        photo = listings_mod.attach_photo(
            self.db(), int(pid),
            listing_id=body.get("listing"), trip_id=body.get("trip"))
        self.json({"ok": True, "photo": photo})

    def h_make_item(self):
        """Add something by hand.

        Two things arrive this way and neither has an email behind it: a
        local pickup sale, which Facebook never sends a label for, and a
        thing off a shelf that is being listed for the first time."""
        body = self.body() or {}
        item = listings_mod.create_item(
            self.db(), body.get("title"),
            price=body.get("price"), paid=body.get("paid"),
            category=body.get("category"), condition=body.get("condition"),
            era=body.get("era"), notes=body.get("notes"),
            trip_id=body.get("trip"))
        if item is None:
            raise ValueError("the item could not be created")
        if body.get("bin"):
            listings_mod.set_bin(self.db(), item["id"], body["bin"])
        for photo_id in body.get("photos") or []:
            listings_mod.attach_photo(self.db(), int(photo_id),
                                      listing_id=item["id"])
        self.json({"ok": True, "item": self._item_row(item["id"])})

    def h_item_fields(self, lid):
        """Correct a thing on the shelf, cost included.

        `paid` is here rather than on a route of its own because triage
        is the same operation as a correction: someone is answering "what
        did this cost" from the receipt in front of them, and answering
        it twice must be allowed to overwrite."""
        row = self._item_row(int(lid))
        if row is None:
            return self.fail(404, "no such item")
        body = self.body() or {}
        if "paid" in body:
            listings_mod.set_cost(self.db(), int(lid), body["paid"])
        sets, params = _item_sets(body)
        # Publishing a draft dates it, here rather than in the client.
        #
        # The date a thing went up is a fact about the transition, not
        # something a caller supplies - and `listed_at` is what
        # `v_aging` and `days_listed` are computed from, so a client that
        # simply forgot to send it would leave the row out of the aging
        # report with nothing to show it had been missed. Only when it is
        # unset: republishing must not restart the clock.
        if (body.get("state") == "active"
                and (row or {}).get("state") == "draft"
                and not (row or {}).get("listed_at")):
            sets.append("listed_at=?")
            params.append(datetime.now().date().isoformat())
        if sets:
            params.append(int(lid))
            self.db().execute(
                f"UPDATE listings SET {', '.join(sets)} WHERE id=?", params)
            self.db().commit()
        if "paid" not in body and not sets:
            raise ValueError("nothing to change")
        self.json({"ok": True, "item": self._item_row(int(lid))})

    def _csv_body(self):
        body = self.body() or {}
        text = body.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("no spreadsheet in the request")
        mapping = body.get("mapping")
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError("the column mapping must be an object")
        return text, mapping, body

    def h_import_preview(self):
        """What committing this file would do, having done none of it.

        Stateless: the client holds the file and sends it again to
        commit. The alternative is parking a parsed upload between two
        requests, which needs a session store this server does not have
        and would not want for one screen.

        The CSV travels in the JSON body. `MAX_BODY` is 2MB and a
        spreadsheet of old sales is kilobytes; `/api/photos`' raw-bytes
        bypass exists only because a twelve-megabyte image cannot fit
        through that limit, and nothing else should copy it."""
        text, mapping, _ = self._csv_body()
        self.json(listings_mod.plan_import(self.db(), text, mapping))

    def h_import_commit(self):
        """Write it. Returns counts by outcome, not a total.

        "Imported 5 sales" is the sentence that hides the thing worth
        knowing: `upsert_listing` fills blanks and never overwrites, so
        re-importing a corrected spreadsheet reports five rows and
        changes nothing."""
        text, mapping, body = self._csv_body()
        state = body.get("state") or None
        if state and state not in ("active", "sold", "expired", "removed"):
            raise ValueError(f"{state!r} is not a state a sale can be in")
        result = listings_mod.commit_import(self.db(), text, mapping, state)
        listings_mod.refresh(self.db())
        self.json(result)

    def h_item_photos(self, lid):
        """The photographs of one thing.

        `GET /api/photos` answers the opposite question - captures about
        nothing yet - so the writer screen had no way to ask which
        pictures belong to the draft it is showing."""
        if self._item_row(int(lid)) is None:
            return self.fail(404, "no such item")
        self.json({"photos": listings_mod.photos_for(self.db(), int(lid))})

    def h_bulk_items(self):
        """The same edit on many things at once.

        The desk's inventory table is where forty rows get a bin, an era
        or a state in one go, and the confirm dialog in front of this
        says there is no undo - so a half-applied edit is the one outcome
        that must be impossible. One transaction, and a row the
        allow-list refuses rolls back the ones before it.

        A client-side loop over `/api/inventory/{id}/fields` was the
        alternative and is worse in both directions: forty round trips
        through a tunnel, and nothing able to say which of them landed
        when the eleventh fails.
        """
        body = self.body() or {}
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("no items selected")
        # The same cap `h_inventory` puts on a page. Past this it is not a
        # selection, it is a migration, and it belongs in the CLI.
        if len(ids) > 500:
            raise ValueError("too many items in one edit")
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            raise ValueError("item ids must be numbers")

        changes = body.get("set") or {}
        if not isinstance(changes, dict) or not changes:
            raise ValueError("nothing to change")
        unknown = set(changes) - set(ITEM_FIELDS) - {"bin", "paid"}
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")

        conn = self.db()
        # Everything that can be refused is refused before anything is
        # written. `set_bin` and `set_cost` each commit on their own, so
        # calling them in a loop would leave the first ten edits standing
        # when the eleventh is rejected - a rollback afterwards would have
        # nothing left to undo. Validate, then one statement.
        sets, params = _item_sets(changes)
        if "bin" in changes:
            needle = changes["bin"]
            if needle in (None, ""):
                code = None
            else:
                found = listings_mod.find_bin(conn, needle)
                if not found:
                    raise ValueError(
                        f"no bin {needle!r}. Make it first - a thing cannot "
                        f"be somewhere that has no name")
                code = found["code"]
            sets.append("bin_code=?")
            params.append(code)
        if "paid" in changes:
            paid = changes["paid"]
            value = None if paid in (None, "") else \
                listings_mod.parse_money(paid)
            if paid not in (None, "") and value is None:
                raise ValueError("paid must be a number")
            sets.append("paid=?")
            params.append(value)
        if not sets:
            raise ValueError("nothing to change")

        holes = ",".join("?" * len(ids))
        found = conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE id IN ({holes})",
            ids).fetchone()[0]
        if found != len(set(ids)):
            raise ValueError("some of those items no longer exist")

        conn.execute(
            f"UPDATE listings SET {', '.join(sets)} WHERE id IN ({holes})",
            params + ids)
        conn.commit()
        self.json({"ok": True, "changed": len(set(ids))})

    def _item_row(self, lid):
        row = self.db().execute(
            "SELECT l.id, l.listing_id, l.title, l.price, l.paid, l.state, "
            "l.category, l.condition, l.era, l.inventory_code, l.bin_code, "
            "b.name AS bin, l.listed_at, l.sold_at, l.notes, "
            "l.description, l.trip_id "
            "FROM listings l LEFT JOIN bins b ON b.code = l.bin_code "
            "WHERE l.id=?", (int(lid),)).fetchone()
        return dict(row) if row else None

    def h_orders(self):
        # CLOSED_STATUSES, not `!= 'shipped'` - a cancelled order is closed
        # too, and would otherwise sit in her queue forever asking to be
        # posted.
        from . import cli as cli_mod

        marks = ",".join("?" * len(cli_mod.CLOSED_STATUSES))
        rows = self.db().execute(
            f"SELECT * FROM sales WHERE status NOT IN ({marks}) "
            f"ORDER BY ship_by IS NULL, ship_by",
            cli_mod.CLOSED_STATUSES).fetchall()
        self.json({"orders": [_order_row(r) for r in rows]})

    def h_order(self, sid):
        row = self.db().execute("SELECT * FROM sales WHERE id=?",
                                (int(sid),)).fetchone()
        if row is None:
            return self.fail(404, "no such order")
        detail = _order_detail(row)
        # Offer an estimate only where there is nothing measured, and
        # only where other parcels have given it something to reason
        # from. `estimate_postage` returns (None, None) rather than a
        # number when it has no basis, and that is the common case.
        if detail.get("postage") is None:
            guess, source = listings_mod.estimate_postage(
                self.db(), row["weight"])
            detail["postage"] = guess
            detail["postage_source"] = source
            detail["kept"] = listings_mod.kept(row["price"], guess)
        self.json(detail)

    def h_label(self, sid):
        row = self.db().execute("SELECT label_pdf FROM sales WHERE id=?",
                                (int(sid),)).fetchone()
        if row is None:
            return self.fail(404, "no such order")
        path = safe_label_path(self.cfg.get("home"), row["label_pdf"])
        if path is None:
            return self.fail(404, "no label on file")
        self._send(200, path.read_bytes(), ctype="application/pdf")

    def h_lookup(self, code):
        """What a scanned code names - a parcel, or a thing on a shelf.

        Two code spaces meet here and they are not the same length by
        accident: a parcel code is 3 characters and released for reuse
        once the parcel ships, an inventory code is 4 and never reused.
        Sales are searched first because a code that is currently on a
        box waiting to go out is the more urgent of the two readings.

        Case-insensitive: this is read off thermal paper by a camera,
        and the alphabet has no lowercase in it anyway."""
        # Imported here, not at module scope, like every other use of
        # cli in this file: cli imports web for `serve`.
        from . import cli as cli_mod

        code = (code or "").upper()
        sale = cli_mod.find_sale(self.db(), code)
        if sale is not None and (sale["code"] or "").upper() == code:
            return self.json({"kind": "sale", "id": sale["id"],
                              "detail": _order_detail(sale)})

        # `id` is not decoration: it is the row id every other inventory
        # route is keyed on, so without it a scanned code identifies an
        # item the client then cannot open. Leaving it out shipped as
        # "Key id not found in key decoding container" on a phone, which
        # names the field and nothing else.
        #
        # The bin comes along for the same reason it does on /inventory -
        # scanning a thing and being told where it lives is most of the
        # point - and the name is joined here so the client is never
        # holding a code it has to resolve with a second request.
        row = self.db().execute(
            "SELECT l.id, l.listing_id, l.title, l.price, l.state, "
            "l.category, l.inventory_code, l.bin_code, b.name AS bin "
            "FROM listings l LEFT JOIN bins b ON b.code = l.bin_code "
            "WHERE UPPER(l.inventory_code)=?",
            (code,)).fetchone()
        if row is not None:
            return self.json({"kind": "listing", "listing": dict(row)})
        return self.fail(404, f"nothing here is called {code}")

    def h_pending(self):
        """Recorded but never printed - the same query cmd_pending uses,
        minus the date window, because on a phone she wants to see the
        backlog before choosing how much of it to print."""
        from . import cli as cli_mod

        marks = ",".join("?" * len(cli_mod.CLOSED_STATUSES))
        rows = self.db().execute(
            f"SELECT * FROM sales WHERE printed_at IS NULL "
            f"AND status NOT IN ({marks}) AND label_pdf IS NOT NULL "
            f"ORDER BY received_at", cli_mod.CLOSED_STATUSES).fetchall()
        self.json({"pending": [_order_row(r) for r in rows]})

    def h_sold(self):
        """What has sold, newest first, with how long it took.

        A direct query rather than `v_listing_perf`, deliberately. That
        view has no row id - it is keyed on `listing_id`, which parses as
        NULL on plenty of real mail - so a row read from it cannot be
        opened. Widening the view would work, but `sheets.TABS` selects
        from these views by column name and they are shared with the
        spreadsheet; a query here is cheaper than a shared thing changed
        for one screen.

        `days_to_sell` is the same expression the view uses. If those two
        ever disagree the view is the one to believe - it is what the
        spreadsheet has been reporting for months.
        """
        limit = 200
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = min(int((qs.get("limit") or ["200"])[0] or 200), 500)
        except ValueError:
            pass
        rows = self.db().execute(
            "SELECT l.id, l.listing_id, l.title, l.price, l.state, "
            "l.category, l.inventory_code, l.bin_code, b.name AS bin, "
            "l.listed_at, l.sold_at, "
            "CASE WHEN l.sold_at IS NOT NULL AND l.listed_at IS NOT NULL "
            "     THEN CAST(julianday(l.sold_at) - julianday(l.listed_at) "
            "               AS INTEGER) END AS days_to_sell "
            "FROM listings l LEFT JOIN bins b ON b.code = l.bin_code "
            "WHERE l.state = 'sold' "
            "ORDER BY l.sold_at IS NULL, l.sold_at DESC LIMIT ?",
            (limit,)).fetchall()
        return self.json({"items": [dict(r) for r in rows],
                          "count": len(rows)})

    def h_stats(self):
        conn = self.db()
        listings_mod.refresh(conn)

        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]

        # How much of this is actually costed.
        #
        # `v_monthly` has carried `net` and `costed` since the views were
        # written and this endpoint never selected either, so the profit
        # screen said "there is no cost basis yet" for as long as that was
        # true and then went on saying it. A screen that tells her the
        # opposite of the truth is worse than one that says nothing, and
        # the fraction is what decides which sentence is honest: `net`
        # over two costed listings out of ninety is not a month's profit.
        coverage = conn.execute(
            "SELECT COUNT(*) AS sold, "
            "       SUM(paid IS NOT NULL) AS costed, "
            "       ROUND(SUM(margin), 2) AS margin "
            "FROM v_listing_perf WHERE state = 'sold'").fetchone()
        self.json({
            "price_bands": rows("SELECT * FROM v_price_band ORDER BY avg_price"),
            "monthly": rows("SELECT * FROM v_monthly LIMIT 12"),
            "aging": rows("SELECT * FROM v_aging LIMIT 10"),
            "cost": {"sold": coverage["sold"] or 0,
                     "costed": coverage["costed"] or 0,
                     "margin": coverage["margin"]},
        })

    def h_system(self):
        """What Settings shows. Configuration only - no secrets.

        `web_password_hash`, `imap_password` and `sheets_key` all live in
        the same config dict, so this allow-lists rather than filtering:
        a key added later is invisible here until someone chooses to show
        it.

        With a remote printd the printer settings are read from *it*, not
        from this host's config. They describe a roll of stock in a room,
        and reporting a local copy would show 0.12 on her phone during the
        exact week she is tuning it to 0.15 on the Pi - with nothing to
        say the number was stale."""
        cfg = self.cfg
        row = self.db().execute(
            "SELECT MAX(printed_at) AS last FROM sales").fetchone()
        out = {
            "backend": cfg.get("printer_backend"),
            "poll_seconds": cfg.get("poll_seconds"),
            "last_printed_at": row["last"] if row else None,
            "printer_source": "local",
        }
        keys = ("device", "darkness", "gap_inches", "media_tracking",
                "head_dots", "dpi")

        if cfg.get("printer_backend") in printers_mod.REMOTE_BACKENDS:
            out["printer_source"] = cfg.get("printd_url")
            try:
                health = printers_mod.printd_health(cfg)
            except printers_mod.PrinterUnavailable as exc:
                # Say so rather than silently falling back to local values,
                # which would look identical to a healthy answer.
                out["printer_reachable"] = False
                out["printer_error"] = str(exc)
                return self.json(out)
            out["printer_reachable"] = True
            out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            for k in keys:
                out[k] = health.get(k)
            out["device_present"] = health.get("device_present")
            out["printing"] = health.get("printing")
            out["printd_build"] = health.get("build")
            return self.json(out)

        out.update({
            "device": cfg.get("printer_device"),
            "darkness": cfg.get("printer_darkness"),
            "gap_inches": cfg.get("gap_inches"),
            "media_tracking": cfg.get("media_tracking"),
            "head_dots": cfg.get("printer_head_dots"),
            "dpi": cfg.get("printer_dpi"),
        })
        self.json(out)

    def _sale(self, sid):
        row = self.db().execute("SELECT * FROM sales WHERE id=?",
                                (int(sid),)).fetchone()
        if row is None:
            self.fail(404, "no such order")
            return None
        return row

    def h_ship(self, sid):
        row = self._sale(sid)
        if row is None:
            return
        # Closing a sale that has a label on file but no recorded print is
        # a contradiction worth keeping rather than erasing: either the
        # label printed and was recorded as failed, or she posted a parcel
        # with no label. Note it so `pending` and the sheet show why.
        #
        # Deliberately NOT stamping printed_at. That would invent a print
        # that never happened - and a local-pickup sale has no label at
        # all, so shipping it unprinted is simply correct.
        if row["label_pdf"] and not row["printed_at"]:
            self.db().execute(
                "UPDATE sales SET status='shipped', "
                "notes=COALESCE(notes || ' | ', '') || ? WHERE id=?",
                ("shipped from the phone with no recorded print",
                 row["id"]))
        else:
            self.db().execute(
                "UPDATE sales SET status='shipped' WHERE id=?", (row["id"],))
        self.db().commit()
        self.json({"ok": True, "code": row["code"]})

    def h_unship(self, sid):
        """Undo, straight after a mis-tap.

        The previous status is derived rather than stored: a sale that has
        a printed_at was 'printed', otherwise 'to_ship'. That is the whole
        state machine, so a column to remember it would only be another
        thing to keep true."""
        row = self._sale(sid)
        if row is None:
            return
        back = "printed" if row["printed_at"] else "to_ship"
        self.db().execute("UPDATE sales SET status=? WHERE id=?",
                          (back, row["id"]))
        self.db().commit()
        self.json({"ok": True, "status": back})

    def h_fields(self, sid):
        """Correct what the email parser got wrong.

        Allow-listed columns, interpolated only from that fixed set - the
        request never names a column."""
        row = self._sale(sid)
        if row is None:
            return
        body = self.body() or {}
        allowed = {"item", "buyer", "price", "notes", "ship_by", "postage"}
        sets, params = [], []
        for key in allowed:
            if key in body:
                value = body[key]
                if key in ("price", "postage") and value not in (None, ""):
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        raise ValueError(f"{key} must be a number")
                sets.append(f"{key}=?")
                params.append(value if value != "" else None)
        if "postage" in body:
            # A person typed it, so it is measured. Clearing it clears
            # the provenance too - an orphaned 'confirmed' on a null
            # would make the next estimate look like it had been checked.
            sets.append("postage_source=?")
            params.append("confirmed" if body["postage"] not in (None, "")
                          else None)
        if not sets:
            raise ValueError("nothing to change")
        params.append(row["id"])
        self.db().execute(
            f"UPDATE sales SET {', '.join(sets)} WHERE id=?", params)
        self.db().commit()
        self.json(_order_detail(self._sale(sid)))

    def _print_one(self, row, force=False):
        """Print one archived label, exactly the way the CLI does.

        Two things this must not skip. `cli.print_label` takes the flock,
        so printing queues behind the poll loop instead of interleaving
        bytes with it. And `label_belongs_to` re-reads the recipient off
        the PDF and checks it against the address recorded from that same
        page - archived labels have pointed at the wrong buyer before, and
        printing one posts a parcel to a stranger. Reprinting from a phone
        is the *easy* path, so it is the one that most needs the backstop;
        going straight to print_label from here would quietly route around
        it."""
        from . import cli as cli_mod

        path = safe_label_path(self.cfg.get("home"), row["label_pdf"])
        if path is None:
            raise ValueError("no label file for that order")
        ok, detail = cli_mod.label_belongs_to(row)
        if not ok and not force:
            raise ValueError(
                f"refusing to print: {detail}. This sale is "
                f"{row['item']!r} for {row['buyer']!r}.")
        conn = self.db()
        code = cli_mod.ensure_code(conn, row["message_id"])
        try:
            cli_mod.print_label(self.cfg, str(path), code)
        except printers_mod.PrinterUnavailable as exc:
            # Note it on the sale so Pending can show why, then re-raise
            # as a PrintError so she gets the reason and not the word
            # "internal".
            conn.execute("UPDATE sales SET notes=? WHERE id=?",
                         (f"print failed: {exc}", row["id"]))
            conn.commit()
            raise PrintError(str(exc))
        cli_mod.mark_printed(conn, row["message_id"])
        return code

    def h_print(self, sid):
        row = self._sale(sid)
        if row is None:
            return
        force = bool((self.body() or {}).get("force"))
        self.json({"ok": True, "code": self._print_one(row, force=force)})

    def h_print_pending(self):
        """Batch-print the backlog, or say what would print.

        Failures are per-row: one bad label must not abandon the rest of
        the batch, which is the whole reason this screen exists."""
        body = self.body() or {}
        ids = body.get("ids") or []
        if not isinstance(ids, list):
            raise ValueError("ids must be a list")
        rows = [r for r in (
            self.db().execute("SELECT * FROM sales WHERE id=?",
                              (int(i),)).fetchone() for i in ids)
            if r is not None]
        # Idempotent unless forced. A batch of nine can outlive
        # Cloudflare's 100s edge timeout, and she then sees an error for
        # labels that did print - with the same nine still selected and
        # every reason to press the button again.
        if not body.get("force"):
            rows = [r for r in rows if not r["printed_at"]]
        if body.get("dry_run"):
            return self.json({"dry_run": True,
                              "would_print": [_order_row(r) for r in rows]})
        printed, failed = [], []
        for row in rows:
            try:
                printed.append(self._print_one(row))
            except Exception as exc:
                log.error("print failed for sale %s: %s", row["id"], exc)
                failed.append({"id": row["id"], "error": str(exc)})
        self.json({"printed": printed, "failed": failed})

    def _label_upload(self):
        """The PDF and the crop options off one request. Shared by the
        print route and the preview, so a preview cannot be measuring a
        different page or a different block than the print would."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype.lower() not in ("application/pdf", "application/x-pdf"):
            raise ValueError(
                f"a label must be application/pdf - got {ctype or 'nothing'}")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("no PDF in the body")
        if length > MAX_LABEL:
            raise ValueError(
                f"that PDF is {length // 1024}kB, over the "
                f"{MAX_LABEL // 1024 // 1024}MB limit for a label")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("the upload was cut short")
        if not raw.startswith(b"%PDF"):
            raise ValueError("that file is not a PDF")

        qs = parse_qs(urlparse(self.path).query)

        def one(name):
            return (qs.get(name) or [None])[0]

        rotate = one("rotate")
        if rotate is not None:
            rotate = int(rotate)
            if rotate not in (0, 90, 180, 270):
                raise ValueError("rotate must be 0, 90, 180 or 270")
        region = one("region")
        return raw, {
            "page_index": int(one("page") or 1) - 1,
            "force_rotation": rotate,
            "region": int(region) if region else None,
        }, one

    def h_label_preview(self):
        """The page with the crop drawn on it, as a PNG.

        The outline comes from `label.crop_box` - the same call the print
        path makes - so it cannot show a rectangle the printer will not
        use. That is the rule the marker band already follows: a crop and
        the drawing of it come from one function, because a box computed
        twice is a picture that agrees with nothing.

        A picture rather than coordinates for a browser to plot, for the
        same reason. Handing out numbers invites a second implementation
        of where the crop is, and the one that is wrong would be the one
        she is looking at."""
        raw, opts, _one = self._label_upload()
        with tempfile.TemporaryDirectory(prefix="mplabel_prev_") as tmpdir:
            src = Path(tmpdir) / "in.pdf"
            src.write_bytes(raw)
            png, _info = label.preview_png(src, **opts)
        self._send(200, png, ctype="image/png",
                   extra_headers=[("Cache-Control", "no-store")])

    def h_print_label(self):
        """Print a 4x6 label this system has never seen before.

        Everything else on this server prints a label it already has, off
        a `sales` row, checked against the address recorded when that sale
        was filed. This prints a PDF somebody just handed it - eBay,
        PirateShip, a carrier's own site, a parcel that is not a sale at
        all - and that difference is the whole design of it:

        Nothing is recorded here. No sales row, no listing, no file kept
        in `labels/`. There is no order for it to belong to, and inventing
        one would put a parcel that is not a sale into revenue, into
        sell-through and into the Sheet. What *is* recorded is printd's
        journal, which is the only durable record this system has anyway -
        the G4 is write-only, so a print is at-least-once and the paper is
        the source of truth. With `printer_backend` pointing straight at a
        device there is no journal at all, and the answer says so rather
        than implying a record that does not exist.

        `label_belongs_to` has nothing to check against for the same
        reason - there is no recorded recipient to compare the PDF with.
        The backstop here is that a person chose this file a second ago,
        which is a different guarantee and a weaker one. Do not paper over
        that by inventing a row to check against.

        Raw bytes with a real Content-Type, like `POST /api/photos` and
        for the same reason: one file, no other fields, everything else in
        the query string, and so no multipart parser to add and get wrong.
        """
        from . import cli as cli_mod

        raw, opts, one = self._label_upload()
        dry = str(one("dry_run") or "").lower() in ("1", "yes", "true")
        force = str(one("force") or "").lower() in ("1", "yes", "true")

        # Derived from the bytes, so the same PDF sent twice is the same
        # job. She is on a phone behind a tunnel; a request that times out
        # after the label came out is exactly the case printd's journal
        # exists to answer, and a random id would turn her retry into a
        # second label instead of a 409. Asking for it again on purpose is
        # `force`, which is a different intent and gets a different id.
        digest = hashlib.sha256(raw).hexdigest()
        job = f"adhoc-{digest[:16]}"
        if force:
            job += "-" + secrets.token_hex(4)

        with tempfile.TemporaryDirectory(prefix="mplabel_adhoc_") as tmpdir:
            src = Path(tmpdir) / "in.pdf"
            src.write_bytes(raw)
            out = Path(tmpdir) / "label_4x6.pdf"
            # Any refusal in here is a ValueError carrying what it
            # measured, which the dispatcher turns into a 400 with that
            # sentence in it. That sentence is the whole feature when a
            # crop cannot be resolved: "this may not be a shipping label"
            # is actionable and "bad request" is not.
            info = label.to_4x6(src, out, **opts)
            info["job"] = job
            info["sha256"] = digest
            info["bytes"] = len(raw)
            backend = self.cfg.get("printer_backend")
            info["recorded"] = ("printd journal"
                                if backend in printers_mod.REMOTE_BACKENDS
                                else "nowhere - this backend writes straight "
                                     "to the device and keeps no journal")
            if dry:
                # Converts and measures, opens no device and journals
                # nothing. This is the cheap way to find out whether a new
                # seller's PDF crops correctly, and it costs no stock -
                # which on a printer that cannot report a failure is the
                # difference between one label and several.
                info["dry_run"] = True
                info["printed"] = False
                return self.json({"ok": True, "label": info})

            try:
                cli_mod.print_label(self.cfg, str(out), job=job)
            except printers_mod.PrinterUnavailable as exc:
                raise PrintError(str(exc))
            info["printed"] = True
            log.info("printed ad-hoc label %s (%d bytes, %s)",
                     job, len(raw), info["size_in"])
            return self.json({"ok": True, "label": info})

    def route_shell(self, path):
        """Send a browser to the front end built for it, once.

        Returns None when this request is not about an entry point, so
        every asset, every API route and every deep link falls straight
        through. Only `/` and `/desk` are decided here.

        Three rules, in order, and the order is the whole design:

          1. `?ui=phone` / `?ui=desk` is a person saying which. It is
             remembered in a cookie and it wins from then on.
          2. The cookie, if there is one.
          3. The request's own account of itself.

        Without 1 and 2 the link each shell carries to the other is a
        button that bounces you back where you came from - click "the
        phone app" on a laptop, get redirected to the desk, forever. The
        two links carry `?ui=`, which is what makes them work at all.
        """
        if path not in ("/", "/desk", "/desk/"):
            return None
        from . import cli as cli_mod
        if not cli_mod.truthy(self.cfg.get("web_auto_route", "yes")):
            return None
        # Only a navigation gets routed. Every browser asks for
        # `text/html` when following a link or typing an address, and
        # nothing else does - so curl, a health check and anything
        # scripted keep getting the same answer at `/` they always got,
        # rather than a 302 they have to be taught to follow.
        if "text/html" not in (self.headers.get("Accept") or ""):
            return None

        here = "desk" if path.startswith("/desk") else "phone"
        asked = (parse_qs(urlparse(self.path).query).get("ui") or [None])[0]

        if asked in UI_CHOICES:
            # Remember it, then put her where she asked to be. Redirect
            # even when she is already there, so the `?ui=` comes off the
            # address bar and a later refresh is not a second vote.
            target = "/desk" if asked == "desk" else "/"
            return self._send(302, b"", ctype="text/plain", extra_headers=[
                ("Location", target),
                ("Set-Cookie", self._ui_cookie(asked)),
                ("Cache-Control", "no-store")])

        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(UI_COOKIE)
        want = morsel.value if morsel and morsel.value in UI_CHOICES else None
        if want is None:
            want = "desk" if prefers_desk(
                self.headers.get("User-Agent"),
                self.headers.get("Sec-CH-UA-Mobile")) else "phone"
        if want == here:
            return None

        # Vary, because this is one URL answering two ways off a header
        # and a cookie, and the intended deployment has Cloudflare in
        # front of it. `no-store` should already stop anything caching a
        # redirect; saying which inputs it turns on costs nothing and the
        # failure it prevents is her phone being handed the laptop's
        # answer out of a cache she cannot see.
        return self._send(302, b"", ctype="text/plain", extra_headers=[
            ("Location", "/desk" if want == "desk" else "/"),
            ("Cache-Control", "no-store"),
            ("Vary", "User-Agent, Sec-CH-UA-Mobile, Cookie")])

    def _ui_cookie(self, choice):
        """A year, and not HttpOnly.

        This is a display preference, not a credential: nothing can be
        done with it, and letting the page read it is what would make a
        client-side switch possible later. `Secure` follows the session
        cookie's rule rather than inventing a second one."""
        bits = [f"{UI_COOKIE}={choice}", "Path=/", "SameSite=Lax",
                "Max-Age=31536000"]
        secure = str(self.cfg.get("web_secure_cookie", "auto")).lower()
        if secure in ("1", "yes", "true", "on") or (
                secure == "auto"
                and self.headers.get("X-Forwarded-Proto") == "https"):
            bits.append("Secure")
        return "; ".join(bits)

    def serve_static(self, path):
        target = safe_static_path(path)
        if target is None:
            return self.fail(400, "bad path")
        if not target.is_file():
            return self.fail(404, "not found")

        if target.name in SHELLS:
            # Stamp the asset URLs. Cache-Control alone is not enough:
            # the intended route in is a Cloudflare tunnel, and the edge
            # caches .js and .css by extension. A stale app.js against a
            # newer API is a confusing failure that looks like a bug in
            # the app, so the URL changes whenever the file does.
            return self._send(200, shell_html(target).encode(),
                              ctype="text/html; charset=utf-8",
                              extra_headers=[("Cache-Control", "no-store")])

        # An updated app must actually reach her phone. Deploying here is
        # `systemctl restart`, with no filename hashing to bust a cache,
        # so a stale app.js would quietly survive the upgrade and she
        # would be running last week's code against this week's API.
        # no-cache means revalidate every time, not "do not store" - the
        # ETag turns that into a 304 for everything that has not moved.
        stat = target.stat()
        etag = '"{:x}-{:x}"'.format(int(stat.st_mtime), stat.st_size)
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", ctype="text/plain",
                              extra_headers=[("ETag", etag)])
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # Icons are content-addressed by nothing, but they change about
        # never; the shell is what has to stay fresh.
        cache = ("public, max-age=604800" if target.suffix in (".png", ".svg")
                 else "no-cache")
        self._send(200, target.read_bytes(), ctype=ctype,
                   extra_headers=[("ETag", etag), ("Cache-Control", cache)])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg):
        super().__init__(addr, Handler)
        self.cfg = cfg
        self.throttle = Throttle()
        # sqlite3 connections are not shareable between threads, so each
        # worker keeps its own. connect_db is idempotent and cheap, and
        # going through it is what gets WAL and the busy timeout.
        self._local = threading.local()

    def db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            from . import cli as cli_mod
            conn = cli_mod.connect_db(self.cfg["home"])
            self._local.conn = conn
        return conn


def serve(cfg, bind=None, port=None):
    bind = bind or cfg.get("web_bind", "127.0.0.1")
    port = int(port or cfg.get("web_port", 8080))
    if not cfg.get("web_password_hash"):
        # EX_CONFIG, not 1, and the unit carries RestartPreventExitStatus
        # for it. printd learned this the expensive way: `Restart=always`
        # against a permanent refusal flapped every ten seconds and buried
        # the one line saying what was wrong. The next start reads the same
        # file and reaches the same conclusion, so staying dead where it
        # can be seen is the only useful behaviour.
        print("web_password_hash is not set, and this refuses to serve the "
              "database unauthenticated.\nRun `mplabel passwd`, put the line "
              "it prints into /etc/mplabel.conf, and start it again.",
              file=sys.stderr)
        raise SystemExit(printd_mod.EX_CONFIG)
    httpd = Server((bind, port), cfg)
    log.info("serving on http://%s:%d", bind, port)
    if bind not in ("127.0.0.1", "localhost", "::1"):
        log.warning("bound to %s - reachable from the network. The intended "
                    "route in is a Cloudflare tunnel to loopback.", bind)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
