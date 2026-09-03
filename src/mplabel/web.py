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
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse, unquote

from . import listings as listings_mod
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

# Bodies are small until photos arrive in phase 4; anything larger than
# this is a mistake or an attack, and reading it into memory on a Pi is
# how you get the OOM killer to stop the label printer.
MAX_BODY = 2 * 1024 * 1024

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


# ----------------------------------------------------------- path safety

def safe_static_path(rel):
    """Resolve `rel` under STATIC, or None if it escapes.

    Serving files by name off a user-supplied path is the classic
    traversal hole, and it reads as fine right up until someone asks for
    ../../../etc/passwd. Resolve first, then check containment - string
    prefix checks miss symlinks."""
    rel = unquote(rel or "").lstrip("/")
    if not rel:
        rel = "index.html"
    try:
        target = (STATIC / rel).resolve()
        target.relative_to(STATIC.resolve())
    except (ValueError, OSError):
        return None
    return target


def asset_stamp():
    """A short hex stamp that moves whenever a served asset does."""
    newest = 0
    for name in ("app.js", "app.css"):
        try:
            newest = max(newest, int((STATIC / name).stat().st_mtime))
        except OSError:
            pass
    return format(newest, "x")


def shell_html(path):
    """index.html with its asset URLs version-stamped."""
    stamp = asset_stamp()
    html = path.read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):
        html = html.replace(f'"/{name}"', f'"/{name}?v={stamp}"')
    return html


def safe_label_path(home, stored):
    """The archived label for one sale.

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


# ---------------------------------------------------------- serialisation

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
    return d


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
        ("GET", r"^/api/pending$", "h_pending", True),
        ("GET", r"^/api/stats$", "h_stats", True),
        ("GET", r"^/api/system$", "h_system", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/ship$", "h_ship", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/unship$", "h_unship", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/fields$", "h_fields", True),
        ("POST", r"^/api/orders/(?P<sid>\d+)/print$", "h_print", True),
        ("POST", r"^/api/print/pending$", "h_print_pending", True),
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
        self.json({"ok": True},
                  extra_headers=[self._cookie_header(token, days * 86400)])

    def h_logout(self):
        self.json({"ok": True}, extra_headers=[self._cookie_header("", 0)])

    def h_session(self):
        self.json({"authenticated": self.authed()})

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
        self.json(_order_detail(row))

    def h_label(self, sid):
        row = self.db().execute("SELECT label_pdf FROM sales WHERE id=?",
                                (int(sid),)).fetchone()
        if row is None:
            return self.fail(404, "no such order")
        path = safe_label_path(self.cfg.get("home"), row["label_pdf"])
        if path is None:
            return self.fail(404, "no label on file")
        self._send(200, path.read_bytes(), ctype="application/pdf")

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

    def h_stats(self):
        conn = self.db()
        listings_mod.refresh(conn)

        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]

        self.json({
            "price_bands": rows("SELECT * FROM v_price_band ORDER BY avg_price"),
            "monthly": rows("SELECT * FROM v_monthly LIMIT 12"),
            "aging": rows("SELECT * FROM v_aging LIMIT 10"),
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
        allowed = {"item", "buyer", "price", "notes", "ship_by"}
        sets, params = [], []
        for key in allowed:
            if key in body:
                value = body[key]
                if key == "price" and value not in (None, ""):
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        raise ValueError("price must be a number")
                sets.append(f"{key}=?")
                params.append(value if value != "" else None)
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

    def serve_static(self, path):
        target = safe_static_path(path)
        if target is None:
            return self.fail(400, "bad path")
        if not target.is_file():
            return self.fail(404, "not found")

        if target.name == "index.html":
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
        raise SystemExit(
            "web_password_hash is not set, and this refuses to serve the "
            "database unauthenticated.\nRun `mplabel passwd`, put the line "
            "it prints into /etc/mplabel.conf, and start it again.")
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
