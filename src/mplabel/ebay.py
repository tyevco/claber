"""
ebay.py - the other selling channel, and the first one with an API.

`listings.py` opens by explaining that Marketplace has no API for an
individual seller, so a listing catalogue has to be assembled out of a
mailbox. eBay is the opposite situation, and that is the whole reason
this module exists: what is listed, what sold and for how much can be
*asked for* rather than inferred from a subject line.

Why urllib and not curl
-----------------------
`notify.py` shells out to curl and openssl, and that is not the house
style - it is a concession to APNs specifically, which needs HTTP/2 and
an ES256-signed JWT that the stdlib cannot produce. eBay needs neither.
It is plain JSON over HTTPS with a bearer token, which `urllib` has done
since forever, so this follows the actual rule in CLAUDE.md: any HTTP
client should be urllib rather than requests.

The seam
--------
Every byte in and out goes through `_transport`, one function taking
(method, url, headers, body) and returning (status, headers, body). The
tests replace it wholesale. There is no other network call in this
module, deliberately - the moment there is a second way out, half the
code stops being testable without a network.

Two environments, one code path
-------------------------------
`ebay_environment` picks the host *and* which credentials are read, so a
sandbox key can never be sent to production by having edited one line of
config and forgotten another. Sandbox is where this gets built; note its
category and aspect data is stale and some Sell endpoints behave
differently, so it proves the plumbing and not the listing.

The refresh token is an eighteen-month fuse
-------------------------------------------
eBay's access token lasts two hours and refreshes silently. The *refresh*
token lasts about eighteen months and then everything stops with
`invalid_grant`. eBay reports its lifetime **only in the reply to the
initial authorization-code exchange** - a refresh does not repeat it - so
the expiry is recorded at that moment or it cannot be recovered
afterwards. `mplabel ebay check` counts the days down, because eighteen
months from now nobody will remember this paragraph.
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("mplabel.ebay")

# sysexits.h EX_CONFIG, the same refusal printd and notify make. A
# missing credential is permanent: retrying it for ever buries the one
# line that says what is wrong.
EX_CONFIG = 78

# Hosts, per environment. The `auth` host is where a *person* consents;
# the `api` host is where this program talks. They are different machines
# and mixing them up produces a 404 that reads like a wrong path.
HOSTS = {
    "sandbox": {
        "api": "https://api.sandbox.ebay.com",
        "auth": "https://auth.sandbox.ebay.com",
    },
    "production": {
        "api": "https://api.ebay.com",
        "auth": "https://auth.ebay.com",
    },
}

# What we ask consent for, and nothing beyond it. `sell.inventory` covers
# inventory items, offers and the location; `sell.account` is the
# business policies, and it is a *write* scope because `ebay setup`
# creates them; `sell.fulfillment.readonly` is orders, read-only because
# nothing here changes an order on eBay's side.
#
# Note these are always the api.ebay.com URIs even in sandbox - the scope
# strings are identifiers, not endpoints, and rewriting them to the
# sandbox host is a rejection that looks like a permissions problem.
ACCOUNT_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.account"

SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    # Write, because `ebay setup` creates the business policies. The
    # readonly one was enough while `check` only read them back, and a
    # token minted before this line keeps the old set - `refresh_access`
    # replays the *stored* scopes, deliberately, so widening this does
    # not silently widen a token she already granted. `setup` says so
    # rather than letting eBay answer 403.
    ACCOUNT_SCOPE,
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
)

# Refresh a little early. A token that expires between the check and the
# call is a 401 on a request that had every reason to work.
EXPIRY_SLACK = 300

TIMEOUT = 30


# Where a listing is posted on eBay. Declared here rather than in
# `listings.SCHEMA` for the same reason `shopping.py` owns its own: the
# module that writes a table owns it. `connect_db` runs this after
# listings', because the reference below needs that table to exist.
#
# A real relation, unlike `sales -> listings`, and it earns one: eBay
# *mints* the sku and the offer id, so there is an actual key to point
# at rather than a title to match on. It also has to be a new table
# rather than columns on `listings` - a column added by `ALTER TABLE`
# gets no foreign key at all on a database that already migrated, which
# is the `bin_code` incident, while `CREATE TABLE IF NOT EXISTS` gives a
# fresh Pi and hers the identical constrained table.
#
# ON DELETE CASCADE, not SET NULL: unlike a bin, this row has no meaning
# without the listing it describes.
#
# One row per listing, keyed on it, because the question being asked is
# "where is this listed" - the same question `listings.bin_code`
# answers about a shelf. A relist overwrites. If "what has this been
# listed as before?" turns out to be real, that is a new table beside
# this one rather than a different shape of it.
#
# `ebay_item` is eBay's *listingId* and is null until she publishes,
# which this system deliberately never does. It is not called
# `listing_id`: that name already means two different things in this
# database - a Facebook id on `sales` and a possibly-synthetic key on
# `listings` - and a third meaning is how `build_job`'s "buffers" went
# wrong.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ebay_offers (
    listing_id INTEGER PRIMARY KEY
               REFERENCES listings(id) ON DELETE CASCADE,
    sku        TEXT UNIQUE,
    offer_id   TEXT,
    ebay_item  TEXT,
    state      TEXT,          -- draft | published | ended
    pushed_at  TEXT,
    -- Of what was last sent, so a push that would change nothing makes
    -- no call at all.
    digest     TEXT
);
"""


class EbayError(RuntimeError):
    """Something about the configuration, the tokens, or eBay's answer."""


class EbayConfigError(EbayError):
    """Specifically a configuration problem - the caller exits 78."""


# ------------------------------------------------------------ the seam

def _transport(method, url, headers, body=None, timeout=TIMEOUT):
    """The only way out of this module. Tests replace it.

    Returns (status, headers, body_bytes). An HTTP error is a normal
    return rather than an exception: eBay puts the reason for a refusal
    in the body, and `urlopen` raising on a 400 would throw that away.
    """
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # The body is the interesting half. Read it before the handle
        # closes or the error says nothing but its own status code.
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise EbayError(f"cannot reach eBay: {exc.reason}") from exc


# ------------------------------------------------------------- config

def environment(cfg):
    env = (cfg.get("ebay_environment") or "sandbox").strip().lower()
    if env not in HOSTS:
        raise EbayConfigError(
            f"ebay_environment is {env!r}; expected sandbox or production")
    return env


def api_host(cfg):
    return HOSTS[environment(cfg)]["api"]


def auth_host(cfg):
    return HOSTS[environment(cfg)]["auth"]


def credentials(cfg):
    """App id, cert id and RuName, or a refusal naming what is missing.

    Named rather than numbered in the error because eBay calls them three
    different things depending which page you are on - App ID is also
    "Client ID", Cert ID is also "Client Secret", and the RuName is not a
    URL however much it looks like it should be.
    """
    missing = [key for key in
               ("ebay_app_id", "ebay_cert_id", "ebay_ru_name")
               if not (cfg.get(key) or "").strip()]
    if missing:
        raise EbayConfigError(
            "not configured for eBay - missing " + ", ".join(missing) +
            ". See docs/ebay.md; the values come from the developer "
            "portal's Application Keys page and are per-environment.")
    return (cfg["ebay_app_id"].strip(),
            cfg["ebay_cert_id"].strip(),
            cfg["ebay_ru_name"].strip())


def _basic(app_id, cert_id):
    raw = f"{app_id}:{cert_id}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# -------------------------------------------------------- token store

def token_path(cfg):
    """Where the tokens live.

    Not the config file. `/etc/mplabel.conf` is hand-edited and read by
    several commands; these rotate every two hours and are a credential,
    so they get their own file with its own mode - the same treatment
    `sheets_key` and the APNs `.p8` already get.
    """
    return Path(cfg["home"]) / "ebay" / "tokens.json"


def load_tokens(cfg):
    path = token_path(cfg)
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise EbayError(f"cannot read {path}: {exc}") from exc


def save_tokens(cfg, tokens):
    """Write the token file 0600, creating its directory.

    Opened with the mode rather than chmod'ed afterwards: between the
    write and the chmod the refresh token is world-readable, and on a
    machine that also serves a web app that window is not academic.
    """
    path = token_path(cfg)
    # 0700, and belt-and-braces with `install_pi.sh` creating it: the
    # installer is not run by the documented pull-and-pip-install route,
    # which is how `photos/` ended up owned by whoever uploaded first.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(tokens, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def _now():
    return datetime.now(timezone.utc)


def _iso(when):
    return when.replace(microsecond=0).isoformat()


# --------------------------------------------------------------- oauth

def consent_url(cfg, scopes=SCOPES, state=None):
    """The URL a person opens to grant this application access.

    The Pi is headless and eBay's consent screen is a web page with a
    login on it, so this is deliberately a string to carry elsewhere
    rather than something to automate. `redirect_uri` is the RuName, not
    a URL - eBay resolves it to the redirect configured against the
    keyset, and sending the URL itself is rejected as a mismatch.
    """
    app_id, _, ru_name = credentials(cfg)
    query = {
        "client_id": app_id,
        "redirect_uri": ru_name,
        "response_type": "code",
        "scope": " ".join(scopes),
    }
    if state:
        query["state"] = state
    return f"{auth_host(cfg)}/oauth2/authorize?" + urllib.parse.urlencode(query)


def _token_request(cfg, form):
    app_id, cert_id, _ = credentials(cfg)
    body = urllib.parse.urlencode(form).encode()
    status, _, raw = _transport(
        "POST", f"{api_host(cfg)}/identity/v1/oauth2/token",
        {"Authorization": _basic(app_id, cert_id),
         "Content-Type": "application/x-www-form-urlencoded"},
        body)
    try:
        answer = json.loads(raw or b"{}")
    except ValueError:
        raise EbayError(
            f"eBay returned {status} and something that is not JSON: "
            f"{(raw or b'')[:200]!r}")
    if status != 200:
        # eBay's OAuth errors are two fields and both matter:
        # `error` is machine-readable, `error_description` is the one
        # that says which of the two ids is wrong.
        raise EbayError(
            f"eBay refused the token request ({status}): "
            f"{answer.get('error', 'unknown')} - "
            f"{answer.get('error_description', 'no reason given')}")
    return answer


def exchange_code(cfg, code):
    """Turn the pasted authorization code into a stored refresh token.

    This is the *only* moment eBay says how long the refresh token lives
    (`refresh_token_expires_in`); a later refresh does not repeat it. So
    the absolute expiry is computed and written here or it is lost.
    """
    _, _, ru_name = credentials(cfg)
    answer = _token_request(cfg, {
        "grant_type": "authorization_code",
        "code": code.strip(),
        "redirect_uri": ru_name,
    })
    now = _now()
    tokens = {
        "environment": environment(cfg),
        "access_token": answer["access_token"],
        "access_expires_at": _iso(
            now + timedelta(seconds=int(answer.get("expires_in", 7200)))),
        "refresh_token": answer["refresh_token"],
        "obtained_at": _iso(now),
        "scopes": list(SCOPES),
    }
    lifetime = answer.get("refresh_token_expires_in")
    if lifetime:
        tokens["refresh_expires_at"] = _iso(
            now + timedelta(seconds=int(lifetime)))
    save_tokens(cfg, tokens)
    return tokens


def _assert_environment(cfg, tokens):
    """Refuse to use a token minted against the other eBay.

    The two keysets are different strings that look alike, so pointing a
    sandbox install at production is one edited config line - and the
    answer is a 401 that reads as the credentials being wrong rather
    than as being aimed at the wrong host.

    This has to guard *use*, not just refresh: an access token is good
    for two hours, so checking only on the refresh path leaves a window
    that long in which sandbox credentials are sent to the live account.
    """
    stored = tokens.get("environment")
    if stored and stored != environment(cfg):
        raise EbayConfigError(
            f"the stored token is for {stored} and ebay_environment "
            f"is {environment(cfg)} - run `mplabel ebay auth` again")


def refresh_access(cfg, tokens=None):
    """Mint a fresh access token from the stored refresh token.

    The `scope` parameter is required here and is easy to miss: without
    it eBay issues a token with no scopes at all, which then fails every
    call with a 403 that reads as a permissions problem with the account
    rather than with the token.
    """
    tokens = dict(tokens or load_tokens(cfg))
    if not tokens.get("refresh_token"):
        raise EbayConfigError(
            "no eBay refresh token stored - run `mplabel ebay auth`")
    _assert_environment(cfg, tokens)
    answer = _token_request(cfg, {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "scope": " ".join(tokens.get("scopes") or SCOPES),
    })
    tokens["access_token"] = answer["access_token"]
    tokens["access_expires_at"] = _iso(
        _now() + timedelta(seconds=int(answer.get("expires_in", 7200))))
    save_tokens(cfg, tokens)
    return tokens


def access_token(cfg):
    """A usable access token, refreshed if this one is about to die."""
    tokens = load_tokens(cfg)
    if not tokens:
        raise EbayConfigError(
            "no eBay tokens stored - run `mplabel ebay auth`")
    _assert_environment(cfg, tokens)
    expires = tokens.get("access_expires_at")
    if tokens.get("access_token") and expires:
        try:
            left = (datetime.fromisoformat(expires) - _now()).total_seconds()
        except ValueError:
            left = -1
        if left > EXPIRY_SLACK:
            return tokens["access_token"]
    return refresh_access(cfg, tokens)["access_token"]


def refresh_days_left(tokens):
    """Days until the refresh token dies, or None if it never said."""
    expires = tokens.get("refresh_expires_at")
    if not expires:
        return None
    try:
        return (datetime.fromisoformat(expires) - _now()).days
    except ValueError:
        return None


# ----------------------------------------------------------- the calls

def call(cfg, method, path, body=None, marketplace=None, headers=None):
    """One authenticated JSON call. Returns (status, decoded body).

    A non-2xx is returned rather than raised, for the same reason
    `_transport` returns it: eBay's refusals carry an `errors` array
    naming the field, and the caller usually wants to say which one.
    """
    token = access_token(cfg)
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Language": "en-US",
        "X-EBAY-C-MARKETPLACE-ID":
            marketplace or cfg.get("ebay_marketplace") or "EBAY_US",
    }
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})

    url = path if path.startswith("http") else api_host(cfg) + path
    status, _, answer = _transport(method, url, request_headers, raw)
    if not answer:
        return status, {}
    try:
        return status, json.loads(answer)
    except ValueError:
        raise EbayError(
            f"eBay returned {status} and something that is not JSON: "
            f"{answer[:200]!r}")


def describe_errors(payload):
    """eBay's `errors` array as one readable line.

    Worth its own function because the interesting part is scattered:
    `parameters` names the field that was wrong and is nested two levels
    down, and `longMessage` is often the only one of the two texts that
    says anything.

    **Both texts, when they differ.** This used to be
    `message or longMessage`, which reads as a sensible preference and
    threw away the useful half: a real sandbox refusal came back as
    `20403: Invalid .` - eBay's template with an empty field name - while
    `longMessage` carried the actual reason. A diagnostic that discards
    the diagnosis is worse than none, because it looks like the whole
    answer.
    """
    errors = (payload or {}).get("errors") or []
    parts = []
    for err in errors:
        short = (err.get("message") or "").strip()
        long = (err.get("longMessage") or "").strip()
        # A template with nothing filled in - "Invalid ." - is not a
        # message, so do not let it stand in for one.
        if short.rstrip(" .") in ("", "Invalid"):
            short = ""
        text = " - ".join(dict.fromkeys(t for t in (short, long) if t)) or "?"
        params = ", ".join(
            f"{p.get('name')}={p.get('value')}"
            for p in err.get("parameters") or [] if p.get("name"))
        parts.append(f"{err.get('errorId', '?')}: {text}"
                     + (f" ({params})" if params else ""))
    return "; ".join(parts) or "no reason given"


# ------------------------------------------------------------- check

def check(cfg):
    """Everything that can be known without changing anything.

    Modelled on `notify --check`, which exists because a refusal cannot
    tell you whose fault it is. The same is true here and worse: eBay
    answers a call made with an unscoped token, a sandbox token sent to
    production, and a genuinely unauthorised account with three
    variations on the same 401.

    Returns a list of (label, value, problem_or_None, blocking) so the
    caller does the printing and this stays testable.

    `blocking` is the difference between "this install is broken" and
    "you have not got to that part yet", and it decides the exit code.
    Exit 78 means a *permanent* misconfiguration - the unit carries
    `RestartPreventExitStatus=78` on the strength of that - so it must
    not fire for the publish-time policies, which nothing here needs
    because nothing here publishes. Reporting a healthy sandbox install
    as a failure is how an exit code stops being believed.
    """
    rows = []

    def add(label, value, problem=None, blocking=True):
        rows.append((label, value, problem, blocking))

    try:
        env = environment(cfg)
    except EbayConfigError as exc:
        add("environment", "?", str(exc))
        return rows
    add("environment", env)
    add("api host", api_host(cfg))

    try:
        app_id, _, ru_name = credentials(cfg)
    except EbayConfigError as exc:
        add("credentials", "(unset)", str(exc))
        return rows
    add("app id", app_id)
    add("RuName", ru_name)
    add("marketplace", cfg.get("ebay_marketplace") or "EBAY_US")

    path = token_path(cfg)
    if not path.exists():
        add("tokens", f"{path} - NOT FOUND",
            "run `mplabel ebay auth`")
        return rows
    mode = oct(os.stat(path).st_mode & 0o777)
    add("tokens", f"{path} ({mode})",
        None if mode == "0o600" else "should be 0600 - it is a credential")

    tokens = load_tokens(cfg)
    stored_env = tokens.get("environment")
    add("token environment", stored_env or "(unrecorded)",
        None if not stored_env or stored_env == env
        else f"stored for {stored_env}, configured for {env}")
    add("obtained", tokens.get("obtained_at") or "(unrecorded)")

    days = refresh_days_left(tokens)
    if days is None:
        add("refresh token", "expiry unrecorded",
            "eBay only says the lifetime at `ebay auth` time; "
            "re-run it to record one")
    else:
        # Thirty days is enough notice to do it calmly. Below zero it has
        # already happened and every call is failing with invalid_grant.
        add("refresh token", f"{days} days left",
            None if days > 30 else
            "re-run `mplabel ebay auth` - this is about to stop working")

    for key, label in (("ebay_merchant_location", "location"),
                       ("ebay_fulfillment_policy", "fulfillment policy"),
                       ("ebay_payment_policy", "payment policy"),
                       ("ebay_return_policy", "return policy")):
        value = (cfg.get(key) or "").strip()
        # Not required to create a draft - required to publish one. Said
        # plainly here because the failure otherwise arrives weeks later
        # inside eBay's own UI, where it looks like eBay's problem - but
        # *not* blocking, or a sandbox install that authenticates and
        # pulls orders perfectly well reports itself broken.
        add(label, value or "(unset)",
            None if value else "needed to publish, not to draft",
            blocking=False)
    return rows


# ------------------------------------------------------------- orders

ORDERS_PATH = "/sell/fulfillment/v1/order"

# eBay's own maximum for this resource. Asking for more is a 400.
PAGE_LIMIT = 50

# How far back `pull` looks when nobody says. Deliberately shorter than
# `lookback_days` for mail: a pulled order is deduplicated against the
# database by order id, so a wide window costs API calls rather than
# correctness - but there is no Gmail-threading problem here to need one.
DEFAULT_SINCE_DAYS = 14


def _money(node):
    """A `{"value": "42.00", "currency": "USD"}` as a float.

    eBay sends money as a decimal *string*, which is the right choice on
    their side and a trap on ours: `float` is correct here because
    `sales.price` is a REAL and every other price in this system already
    is, but the string must not be handed straight to SQLite - a column
    typed REAL will take "42.00" and store text, and the averages then
    silently come out wrong.
    """
    if not node:
        return None
    value = node.get("value")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _local_date(stamp):
    """An eBay UTC timestamp as a **local** `YYYY-MM-DD`.

    This is load bearing and the trap is recorded in CLAUDE.md: every
    date in this system is local, and `notify.due_parcels` compares
    `ship_by` as a *string* against `date.today().isoformat()`. eBay
    sends RFC 3339 UTC, so storing it raw fails twice over -
    `'2026-09-15T06:59:59.000Z' <= '2026-09-15'` is false because the
    `T` sorts after nothing, so a parcel due today is never reported
    until the day after it was due; and `06:59:59Z` is the previous
    evening in Eastern anyway.
    """
    when = _parse_stamp(stamp)
    return when.astimezone().date().isoformat() if when else None


def _local_stamp(stamp):
    """An eBay UTC timestamp as a local ISO string with its offset.

    Matches the shape the mail path writes - `mailparse` stores
    `received.isoformat()` carrying the sender's offset - because
    `cmd_pending` slices the first ten characters off this column and
    `notify.failed_prints` calls `date()` on it. A bare `Z` would put
    those two on the wrong side of local midnight all evening.
    """
    when = _parse_stamp(stamp)
    return when.astimezone().isoformat(timespec="seconds") if when else None


def _parse_stamp(stamp):
    if not stamp:
        return None
    text = stamp.strip()
    # RFC 3339 says `Z`; `fromisoformat` only learned it in 3.11 and the
    # Pi is not guaranteed to be there.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A stamp with no offset is meaningless to `astimezone`, which would
    # read it as local and shift it again. eBay always sends one; a
    # fixture might not.
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


def _ship_to(order):
    """The buyer's address as one line, the way the label path stores it.

    Note this is the address eBay *says*. `sales.ship_to` is what
    `label_belongs_to` compares against the recipient read back off the
    printed PDF, and for a Facebook sale both sides come from the same
    page - so they are string-equal by construction. They will not be
    here, which is why the attach path has to cross-check the two loosely
    once and then store the PDF's version. Do not wire this column
    straight into a print-time comparison.
    """
    for instruction in order.get("fulfillmentStartInstructions") or []:
        ship_to = (instruction.get("shippingStep") or {}).get("shipTo") or {}
        address = ship_to.get("contactAddress") or {}
        parts = [
            ship_to.get("fullName"),
            address.get("addressLine1"),
            address.get("addressLine2"),
            " ".join(p for p in (address.get("city"),
                                 address.get("stateOrProvince"),
                                 address.get("postalCode")) if p),
        ]
        line = ", ".join(p for p in parts if p)
        if line:
            return line
    return None


def order_to_sale(order):
    """One eBay order as a `sales` row.

    Every key here is a real column on `sales` except `sku`, which is
    the line item's SKU - the handle back to `listings.inventory_code`
    through `ebay_offers`. `cli.upsert` filters to its own whitelist, so
    the extra key is dropped rather than erroring; it is here because the
    dry run should show it and because resolving through it is what stops
    `link_sales` minting a phantom listing beside the real one.

    Three decisions in this mapping matter more than the rest.

    **`message_id` is synthetic.** `ebay:<orderId>` rather than NULL,
    because six call sites on the print path key on that column and all
    six fail *silently* on a falsy one - `ensure_code` mints no parcel
    code, `mark_printed` matches no row so a printed parcel never leaves
    Pending, and `notify.failed_prints` then reports it every day. The
    scheme prefix is the same move `title_key`'s `saved:` and
    `import_dyi`'s `dyi:` already make, and it cannot collide with a real
    Message-ID, which is always `<...@...>`.

    **`price` is the line items, not the order total.**
    `pricingSummary.total` carries delivery and sales tax. Putting it
    here would put tax into `v_monthly.gross` and into the median
    `listings.worth` prices the next object off - the
    `amount_with_offset` trap in a new dress, and the same silent
    corruption of every average.

    **The dates are converted to local.** See `_local_date`.
    """
    line_items = order.get("lineItems") or []
    costs = [_money(item.get("lineItemCost")) for item in line_items]
    costs = [c for c in costs if c is not None]

    ship_by = None
    for item in line_items:
        due = (item.get("lineItemFulfillmentInstructions")
               or {}).get("shipByDate")
        if due:
            # The earliest deadline across the order: one parcel, and the
            # tightest date is the one that matters.
            local = _local_date(due)
            if local and (ship_by is None or local < ship_by):
                ship_by = local

    buyer = order.get("buyer") or {}
    ship_to_name = None
    for instruction in order.get("fulfillmentStartInstructions") or []:
        ship_to = (instruction.get("shippingStep") or {}).get("shipTo") or {}
        ship_to_name = ship_to.get("fullName")
        if ship_to_name:
            break

    service = None
    for instruction in order.get("fulfillmentStartInstructions") or []:
        step = instruction.get("shippingStep") or {}
        service = " ".join(p for p in (step.get("shippingCarrierCode"),
                                       step.get("shippingServiceCode")) if p)
        if service:
            break

    return {
        "order_id": order.get("orderId"),
        # Not NULL. See the docstring - this one is not cosmetic.
        "message_id": f"ebay:{order.get('orderId')}",
        "channel": "ebay",
        "received_at": _local_stamp(order.get("creationDate")),
        # The name on the parcel, falling back to the eBay handle. eBay
        # masks the username in some contexts (`b***r`), so the shipping
        # name is both more useful and more reliable.
        "buyer": ship_to_name or buyer.get("username"),
        "item": line_items[0].get("title") if line_items else None,
        "price": round(sum(costs), 2) if costs else None,
        "ship_by": ship_by,
        "ship_to": _ship_to(order),
        "service": service or None,
        "status": "to_ship",
        # Not a `sales` column; `upsert` drops it.
        "sku": line_items[0].get("sku") if line_items else None,
    }


def get_orders(cfg, since=None, limit=None):
    """Every order created since `since`, newest page first.

    `since` is a date or datetime; the default window is
    `DEFAULT_SINCE_DAYS`. Returns a list rather than a generator so the
    caller can count before printing anything - a dry run that says
    "would insert 3" has to know that before the first line.
    """
    if since is None:
        since = _now() - timedelta(days=DEFAULT_SINCE_DAYS)
    if isinstance(since, str):
        since = _parse_stamp(since) or (
            _now() - timedelta(days=DEFAULT_SINCE_DAYS))
    if not isinstance(since, datetime):
        since = datetime(since.year, since.month, since.day,
                         tzinfo=timezone.utc)

    # eBay wants an RFC 3339 range with an open right end. The brackets
    # and the `..` are part of the syntax, not decoration.
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = {"filter": f"creationdate:[{stamp}..]",
             "limit": str(min(limit or PAGE_LIMIT, PAGE_LIMIT))}

    orders = []
    path = ORDERS_PATH + "?" + urllib.parse.urlencode(query)
    while path:
        status, payload = call(cfg, "GET", path)
        if status != 200:
            raise EbayError(
                f"eBay refused the order list ({status}): "
                + describe_errors(payload))
        orders.extend(payload.get("orders") or [])
        if limit and len(orders) >= limit:
            return orders[:limit]
        # `next` is a full href when there is another page and absent
        # when there is not. Following it rather than incrementing an
        # offset ourselves keeps the filter intact.
        path = payload.get("next")
    return orders


INVENTORY_ITEM_PATH = "/sell/inventory/v1/inventory_item"


def existing_skus(cfg, limit=200):
    """Every SKU already on the account, read-only.

    Worth one call before the first push ever happens. eBay's SKU
    uniqueness is per-account and **permanent** - reusing one does not
    error, it silently re-points the old inventory item at a new object,
    so a listing already live would start describing something else.
    Deriving ours from `listings.inventory_code` only avoids that if
    nothing is already using the same shape, and this is how to know
    rather than assume.
    """
    skus, offset = [], 0
    while True:
        page = min(limit - len(skus), 100)
        if page <= 0:
            break
        status, payload = call(
            cfg, "GET",
            f"{INVENTORY_ITEM_PATH}?limit={page}&offset={offset}")
        if status != 200:
            raise EbayError(
                f"eBay refused the inventory list ({status}): "
                + describe_errors(payload))
        items = payload.get("inventoryItems") or []
        skus.extend(item.get("sku") for item in items if item.get("sku"))
        if not payload.get("next") or not items:
            break
        offset += len(items)
    return skus


PHOTO_PATH = "/ebay/photo/"


def photo_url(cfg, digest):
    """The public URL eBay fetches one photograph at.

    `ebay_photo_base` is the outside of the tunnel - the same host the
    phone reaches, without a path. It has to be **https**: eBay refuses
    a plain-http `imageUrls` outright, and the refusal names the field
    rather than the scheme.

    Deliberately a separate key from `ebay_notification_endpoint` even
    though both are the same host today. That one is a whole URL eBay
    stores and hashes into the deletion challenge, so a trailing slash
    changes its meaning; this one is a base that gets a path appended.
    Making one serve both jobs is how a shared string acquires two
    meanings.
    """
    base = (cfg.get("ebay_photo_base") or "").strip().rstrip("/")
    if not base:
        raise EbayConfigError(
            "ebay_photo_base is not set, so there is no public URL to "
            "give eBay for a photograph. See docs/ebay.md.")
    if not base.startswith("https://"):
        raise EbayConfigError(
            f"ebay_photo_base is {base!r}; eBay refuses a non-https "
            f"imageUrls, and the refusal names the field rather than "
            f"the scheme.")
    return f"{base}{PHOTO_PATH}{digest}"


# ------------------------------------------------------ the prerequisites

# eBay validates these when the **offer is created**, not when it is
# published - which reads backwards and is the trap `docs/ebay.md`
# records. So they gate the very first push, even though nothing here
# publishes on the production account.
POLICY_KINDS = {
    "fulfillment": "/sell/account/v1/fulfillment_policy",
    "payment": "/sell/account/v1/payment_policy",
    "return": "/sell/account/v1/return_policy",
}

# What `setup` creates when the account has none. Deliberately plain:
# these describe how she already ships, and anything cleverer is a
# decision eBay's own UI is better at presenting.
DEFAULT_POLICIES = {
    # No shipping service here on purpose - see `fulfillment_body`. The
    # code eBay will accept is per-marketplace and moves, so it is asked
    # for rather than written down.
    "fulfillment": {
        "name": "mplabel ground",
        "marketplaceId": "EBAY_US",
        "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
        "handlingTime": {"unit": "DAY", "value": 3},
    },
    "payment": {
        "name": "mplabel managed",
        "marketplaceId": "EBAY_US",
        "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
    },
    "return": {
        "name": "mplabel 30 day",
        "marketplaceId": "EBAY_US",
        "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
        "returnsAccepted": True,
        "returnPeriod": {"unit": "DAY", "value": 30},
        "returnShippingCostPayer": "BUYER",
    },
}

LOCATION_PATH = "/sell/inventory/v1/location"


def has_scope(tokens, scope):
    """Whether the *stored* token carries a scope.

    Asked rather than assumed because `refresh_access` replays what was
    granted, not what `SCOPES` currently says - so widening the constant
    does not widen a token, and the difference has to be visible.
    """
    return scope in (tokens.get("scopes") or [])


def existing_policies(cfg, kind):
    """Every policy of one kind already on the account."""
    path = POLICY_KINDS[kind]
    marketplace = cfg.get("ebay_marketplace") or "EBAY_US"
    status, payload = call(
        cfg, "GET", f"{path}?marketplace_id={marketplace}")
    if status != 200:
        raise EbayError(f"eBay refused the {kind} policy list ({status}): "
                        + describe_errors(payload))
    # eBay names the array after the kind: fulfillmentPolicies, etc.
    for key, value in payload.items():
        if isinstance(value, list):
            return value
    return []


def fulfillment_body(cfg, service):
    """The fulfillment policy, around a service eBay said it accepts.

    `service` comes from `choose_shipping_service`, which picks from
    what the marketplace actually offers. Passing a code we merely like
    is how the first real attempt at this failed: "Please select a valid
    shipping service", on `USPSGroundAdvantage`, which is a real service
    and simply not one that account would take.
    """
    if not service or not service.get("code"):
        raise EbayError(
            "no usable domestic shipping service to build a policy "
            "around - eBay offered none this marketplace accepts.")
    body = dict(DEFAULT_POLICIES["fulfillment"],
                marketplaceId=cfg.get("ebay_marketplace") or "EBAY_US")
    body["shippingOptions"] = [{
        "optionType": "DOMESTIC",
        "costType": "FLAT_RATE",
        "shippingServices": [{
            "sortOrder": 1,
            "shippingCarrierCode": service.get("carrier") or "USPS",
            "shippingServiceCode": service["code"],
            "freeShipping": True,
            "buyerResponsibleForShipping": False,
        }],
    }]
    return body


def ensure_policies(cfg, dry_run=False, service=None, out=None):
    """The three business policies, created only where none exists.

    Returns {kind: (policy_id, what_happened)}. Never edits one that is
    already there: a policy is how she actually ships and returns, and
    a tool that rewrites it because its own defaults differ is a tool
    that changes her terms without being asked.

    `out` is the caller's dict, filled in as each policy is settled, so
    that a refusal partway through does not take the ids of the ones
    already created with it. That happened on a real account: the
    address check ran *after* this, all three policies were created,
    `ensure_location` raised, and the command said nothing about the
    three real policies now sitting on her account. Returning a value
    is no use to a caller that never receives it.
    """
    out = {} if out is None else out
    for kind, path in POLICY_KINDS.items():
        found = existing_policies(cfg, kind)
        if found:
            first = found[0]
            out[kind] = (first.get(f"{kind}PolicyId") or first.get("policyId"),
                         f"already there ({first.get('name')})")
            continue
        if dry_run:
            out[kind] = (None, f"would create {DEFAULT_POLICIES[kind]['name']!r}")
            continue
        if kind == "fulfillment":
            body = fulfillment_body(cfg, service)
        else:
            body = dict(DEFAULT_POLICIES[kind],
                        marketplaceId=cfg.get("ebay_marketplace") or "EBAY_US")
        status, payload = call(cfg, "POST", path, body)
        if status not in (200, 201):
            raise EbayError(f"eBay refused to create the {kind} policy "
                            f"({status}): " + describe_errors(payload))
        out[kind] = (payload.get(f"{kind}PolicyId") or payload.get("policyId"),
                     "created")
    return out


def location_address(cfg):
    """Where parcels are posted from. Hers, so it cannot be invented.

    A warehouse location needs **postcode and country**, or **city,
    state and country** - and sending only the country is `25802: Input
    error`, which says nothing about which field. That is the whole
    reason this is its own function with its own refusal.

    It is not cosmetic either: eBay shows buyers a delivery estimate
    computed from it, so a placeholder would be a wrong promise on every
    listing rather than a tidy default.
    """
    country = (cfg.get("ebay_location_country") or "US").strip().upper()
    postcode = (cfg.get("ebay_location_postcode") or "").strip()
    city = (cfg.get("ebay_location_city") or "").strip()
    state = (cfg.get("ebay_location_state") or "").strip()

    if postcode:
        return {"postalCode": postcode, "country": country}
    if city and state:
        return {"city": city, "stateOrProvince": state, "country": country}
    raise EbayConfigError(
        "eBay needs to know where parcels are posted from, and it is not "
        "a thing this\ncan guess: buyers are shown a delivery estimate "
        "computed from it.\n\nSet `ebay_location_postcode`, or "
        "`ebay_location_city` and `ebay_location_state`,\nin "
        "/etc/mplabel.conf.")


def ensure_location(cfg, key=None, dry_run=False):
    """The inventory location an offer has to name.

    `merchantLocationKey` is ours to choose and permanent per account.
    Creating one that exists answers 409, which is a success here - the
    location being there is the whole requirement.
    """
    key = key or (cfg.get("ebay_merchant_location") or "home").strip()
    status, _payload = call(cfg, "GET", f"{LOCATION_PATH}/{key}")
    if status == 200:
        return key, "already there"
    # Asked for before the dry run answers, so `--dry-run` refuses on a
    # missing address rather than reporting a creation that would fail.
    address = location_address(cfg)
    if dry_run:
        return key, f"would create at {address}"
    body = {
        "location": {"address": address},
        "name": key,
        # WAREHOUSE, not STORE: nothing here is a shopfront, and a store
        # location needs a full street address and opening hours.
        "locationTypes": ["WAREHOUSE"],
        "merchantLocationStatus": "ENABLED",
    }
    status, payload = call(cfg, "POST", f"{LOCATION_PATH}/{key}", body)
    if status in (200, 201, 204):
        return key, "created"
    if status == 409:
        return key, "already there"
    raise EbayError(f"eBay refused to create the location ({status}): "
                    + describe_errors(payload))


# ------------------------------------------------------------- taxonomy

# The US tree. `getDefaultCategoryTreeId` answers this per marketplace;
# it is 0 for EBAY_US and has been for years, so this asks only when the
# marketplace is something else.
DEFAULT_TREES = {"EBAY_US": "0"}


def category_tree_id(cfg):
    marketplace = cfg.get("ebay_marketplace") or "EBAY_US"
    known = DEFAULT_TREES.get(marketplace)
    if known:
        return known
    status, payload = call(
        cfg, "GET", "/commerce/taxonomy/v1/get_default_category_tree_id"
                    f"?marketplace_id={marketplace}")
    if status != 200:
        raise EbayError(f"eBay refused the category tree id ({status}): "
                        + describe_errors(payload))
    return payload.get("categoryTreeId")


def suggest_categories(cfg, title, limit=3):
    """eBay's guesses at where a title belongs, best first.

    A guess, and named one. A wrong category is a listing nobody
    searching for the thing will ever see, which is a silent failure of
    exactly the kind this project keeps finding - so `push` prints these
    and refuses to publish until one is confirmed.
    """
    tree = category_tree_id(cfg)
    status, payload = call(
        cfg, "GET",
        f"/commerce/taxonomy/v1/category_tree/{tree}/get_category_suggestions"
        f"?q={urllib.parse.quote(title or '')}")
    if status != 200:
        raise EbayError(f"eBay refused the category suggestion ({status}): "
                        + describe_errors(payload))
    out = []
    for row in (payload.get("categorySuggestions") or [])[:limit]:
        category = row.get("category") or {}
        ancestors = [a.get("categoryName")
                     for a in reversed(row.get("categoryTreeNodeAncestors")
                                       or [])]
        out.append({
            "id": category.get("categoryId"),
            "name": category.get("categoryName"),
            "path": " > ".join([a for a in ancestors if a]
                               + [category.get("categoryName") or ""]),
        })
    return out


def required_aspects(cfg, category_id):
    """The item specifics eBay will refuse a publish without.

    Asked before publishing rather than discovered from the refusal,
    because the refusal names them one at a time and each round trip is
    another failed publish.
    """
    tree = category_tree_id(cfg)
    status, payload = call(
        cfg, "GET",
        f"/commerce/taxonomy/v1/category_tree/{tree}"
        f"/get_item_aspects_for_category?category_id={category_id}")
    if status != 200:
        raise EbayError(f"eBay refused the aspects for {category_id} "
                        f"({status}): " + describe_errors(payload))
    out = []
    for aspect in payload.get("aspects") or []:
        constraint = aspect.get("aspectConstraint") or {}
        if constraint.get("aspectRequired"):
            out.append(aspect.get("localizedAspectName"))
    return [a for a in out if a]


# ----------------------------------------------------------- the push

INVENTORY_PATH = "/sell/inventory/v1/inventory_item"
OFFER_PATH = "/sell/inventory/v1/offer"

# A SKU derived from `listings.inventory_code` rather than equal to it.
# Two payoffs: it reads as ours in Seller Hub, and the local code stays
# the source - if eBay ever refuses or forces a change to a SKU, the
# label already stuck to the box in the loft is still correct.
SKU_PREFIX = "MP-"


def sku_for(inventory_code):
    if not inventory_code:
        raise EbayError(
            "this listing has no inventory_code, so it has no SKU. "
            "`ensure_inventory_codes` mints them lazily - run a command "
            "that calls it first.")
    return f"{SKU_PREFIX}{inventory_code.strip().upper()}"


def photo_reachable(cfg, url, timeout=10):
    """Can anything on the internet fetch this? Asked before publishing.

    eBay fetches `imageUrls` itself, from its own servers, and its
    refusal for an image it could not get names the *field* rather than
    the reason. On this deployment the likeliest reason by far is that
    the tunnel is down - the Pi behind a home router is exactly the
    machine that disappears - so the question is worth asking locally
    where the answer can say so.

    A local 200 does not prove eBay can reach it; a local failure does
    prove eBay cannot. The check is one-directional and says which.
    """
    try:
        status, _headers, _body = _transport("GET", url, {}, timeout=timeout)
    except EbayError as exc:
        return False, str(exc)
    if status == 200:
        return True, "reachable from here"
    if status == 404:
        return False, ("404 - the digest is not attached to a listing with "
                       "an ebay_offers row yet, so the allowlist refuses it")
    return False, f"answered {status}"


# eBay's hard limit. Her titles routinely run past a hundred characters
# - "Antique 1900-1915 American Edwardian / Late Victorian..." - so this
# fires often rather than never, and `push` says when it has.
TITLE_LIMIT = 80


def ebay_title(title):
    """The title eBay will accept, cut at a word where it can be.

    Truncating mid-word reads as a corrupted listing rather than a long
    one, and a silent cut is worse than either: the desk shows her full
    title and eBay would show 80 characters of it with no indication
    anywhere that they differ. `push` prints the cut version.
    """
    title = (title or "").strip()
    if len(title) <= TITLE_LIMIT:
        return title
    cut = title[:TITLE_LIMIT]
    space = cut.rfind(" ")
    # Only back off to a word boundary if that does not throw away a
    # quarter of the room; an unbroken 80-character string is rare and
    # cutting it hard is better than returning almost nothing.
    if space > TITLE_LIMIT * 0.75:
        cut = cut[:space]
    return cut.rstrip(" ,;-/")


def inventory_item_body(listing, image_urls, aspects=None):
    """One listing as eBay's inventory item.

    `condition` is eBay's enumeration, not hers: her `condition` column
    is free text a person wrote ("chipped", "good"), and guessing an
    enum from it would publish a claim about an object a buyer is going
    to unwrap. USED_GOOD is the floor, and anything more specific has to
    be said deliberately.
    """
    product = {
        "title": ebay_title(listing.get("title")),
        "description": (listing.get("description")
                        or listing.get("title") or ""),
    }
    if image_urls:
        product["imageUrls"] = list(image_urls)
    if aspects:
        # eBay wants every value as a list, even the single ones.
        product["aspects"] = {k: (v if isinstance(v, list) else [v])
                              for k, v in aspects.items()}
    return {
        "product": product,
        "condition": "USED_GOOD",
        "availability": {
            "shipToLocationAvailability": {"quantity": 1},
        },
    }


def offer_body(cfg, sku, listing, category_id):
    price = listing.get("price")
    if price is None:
        raise EbayError(
            "this listing has no price, and an offer must carry one. "
            "Set it before pushing.")
    return {
        "sku": sku,
        "marketplaceId": cfg.get("ebay_marketplace") or "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": str(category_id),
        "listingDescription": (listing.get("description")
                               or listing.get("title") or ""),
        "pricingSummary": {"price": {"value": f"{float(price):.2f}",
                                     "currency": "USD"}},
        "listingPolicies": {
            "fulfillmentPolicyId": cfg.get("ebay_fulfillment_policy"),
            "paymentPolicyId": cfg.get("ebay_payment_policy"),
            "returnPolicyId": cfg.get("ebay_return_policy"),
        },
        "merchantLocationKey": cfg.get("ebay_merchant_location") or "home",
    }


def put_inventory_item(cfg, sku, body):
    """Create or replace. eBay answers 204 with no body on success."""
    status, payload = call(cfg, "PUT", f"{INVENTORY_PATH}/{sku}", body,
                           headers={"Content-Type": "application/json"})
    if status not in (200, 201, 204):
        raise EbayError(f"eBay refused the inventory item ({status}): "
                        + describe_errors(payload))
    return sku


def find_offer(cfg, sku):
    """The offer already on this SKU, or None.

    Asked rather than remembered, because `ebay_offers.offer_id` can be
    stale in exactly one direction that matters: a row written here and
    an offer deleted in eBay's UI. Creating a second offer for a SKU
    that has one is a 400 naming neither.
    """
    marketplace = cfg.get("ebay_marketplace") or "EBAY_US"
    status, payload = call(
        cfg, "GET",
        f"{OFFER_PATH}?sku={urllib.parse.quote(sku)}"
        f"&marketplace_id={marketplace}")
    if status == 404:
        return None
    if status != 200:
        raise EbayError(f"eBay refused the offer lookup ({status}): "
                        + describe_errors(payload))
    offers = payload.get("offers") or []
    return offers[0] if offers else None


def create_or_update_offer(cfg, sku, body):
    """Returns (offer_id, created_or_updated)."""
    existing = find_offer(cfg, sku)
    if existing and existing.get("offerId"):
        offer_id = existing["offerId"]
        status, payload = call(cfg, "PUT", f"{OFFER_PATH}/{offer_id}", body)
        if status not in (200, 204):
            raise EbayError(f"eBay refused the offer update ({status}): "
                            + describe_errors(payload))
        return offer_id, "updated"
    status, payload = call(cfg, "POST", OFFER_PATH, body)
    if status not in (200, 201):
        raise EbayError(f"eBay refused the offer ({status}): "
                        + describe_errors(payload))
    return payload.get("offerId"), "created"


def publish_offer(cfg, offer_id):
    """Make the offer a live listing. Returns eBay's listingId.

    Refused on production by the caller, not here: this function is the
    mechanism and `cmd_ebay_push` is the policy, so a future caller
    cannot get the mechanism without meeting the policy.
    """
    status, payload = call(cfg, "POST", f"{OFFER_PATH}/{offer_id}/publish")
    if status not in (200, 201):
        raise EbayError(f"eBay refused to publish ({status}): "
                        + describe_errors(payload))
    return payload.get("listingId")


# ------------------------------------------------ the programme opt-in

PROGRAM_PATH = "/sell/account/v1/program"

# Business policies are a seller *programme*, and an account that has not
# joined it cannot have policies at all. eBay's refusal for that is not
# the sentence you would expect: a real sandbox account answered the
# policy list with `20403: Invalid .` - its own template with an empty
# field name - which reads as a malformed request rather than an account
# that has not opted in.
POLICY_PROGRAM = "SELLING_POLICY_MANAGEMENT"


def opted_in_programs(cfg):
    """Which seller programmes this account has joined."""
    status, payload = call(
        cfg, "GET", f"{PROGRAM_PATH}/get_opted_in_programs")
    if status != 200:
        raise EbayError(f"eBay refused the programme list ({status}): "
                        + describe_errors(payload))
    return [row.get("programType") for row in payload.get("programs") or []
            if row.get("programType")]


def opt_in_to_program(cfg, program=POLICY_PROGRAM):
    """Join a seller programme. Account-level, and not instant.

    eBay says this can take **up to 24 hours** to process, so a
    successful call is a request rather than a state change - `setup`
    says so rather than going straight on to create policies that would
    be refused for the next day.

    Never called without an explicit flag. Opting an account into
    business policies changes how every listing on it is managed, which
    is not a thing to do as a side effect of a command someone ran to
    find out what was wrong.
    """
    status, payload = call(cfg, "POST", f"{PROGRAM_PATH}/opt_in",
                           {"programType": program})
    if status not in (200, 201, 204):
        raise EbayError(f"eBay refused the opt-in ({status}): "
                        + describe_errors(payload))
    return program


# ------------------------------------------------- what eBay will ship

SHIPPING_SERVICES_PATH = "/sell/metadata/v1/shipping/marketplace/{}/get_shipping_services"

# What we would *like*, best first. Not what we send: the first of these
# that the marketplace actually offers is what gets used, because eBay's
# service vocabulary is per-marketplace and it moves - `USPSGroundAdvantage`
# replaced First Class Package in 2023, and a sandbox account refused it
# outright with "Please select a valid shipping service". Hardcoding one
# is the same class of guess as hardcoding a category.
PREFERRED_SHIPPING = (
    # `USPSParcel` is eBay's code for **USPS Ground Advantage**, and that
    # is the single most useful thing the real service list said. USPS
    # renamed the service in 2023; eBay updated the *description* and
    # kept the legacy code. So `USPSGroundAdvantage` - the name of the
    # thing - matches nothing at all, which is what the first attempt
    # sent and what "Please select a valid shipping service" meant.
    #
    # It is first because it is what a small parcel actually goes by.
    # `USPSPriority` was winning before this list was checked against a
    # real account, and Priority Mail is a more expensive service nobody
    # asked for.
    "USPSParcel",
    "USPSPriority",
    # Generic, and the reason the list has a tail: these have been valid
    # across marketplaces and eBay redesigns for years, so one of them
    # is a better answer than a refusal.
    "ShippingMethodStandard",
    "Other",
)

# Looked for in the *description* when no preferred code matched, because
# the code and the name of the service can disagree - see above. eBay
# renames a service by editing the description and leaving the code, so
# the description is the half that tracks what the service is called.
PREFERRED_DESCRIPTIONS = ("ground advantage", "standard shipping")


def _service_rows(payload):
    """Every shipping service in the reply, however it is shaped.

    Walked rather than indexed, for the reason `savedpage` and the DYI
    importer are: this response has been read from documentation that
    would not load, so the exact key names are a guess and the shape is
    the only thing worth trusting. A parser that follows a fixed path
    into an undocumented payload fails silently the day it changes.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            code = next((node.get(k) for k in
                         ("shippingServiceId", "shippingServiceCode",
                          "serviceCode", "shippingService")
                         if isinstance(node.get(k), str)), None)
            if code:
                # `validForSellingFlow` false means eBay lists it but
                # will not accept it on a policy - which is exactly the
                # refusal we are trying to avoid. Absent means unknown,
                # and unknown is treated as usable rather than dropped.
                usable = True
                for key in ("validForSellingFlow", "availableForSellingFlow"):
                    if key in node:
                        usable = bool(node[key])
                        break
                found.append({
                    "code": code,
                    "carrier": node.get("shippingCarrierCode")
                    or node.get("shippingCarrier"),
                    "description": node.get("description"),
                    "international": bool(node.get("internationalService")),
                    "usable": usable,
                })
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def shipping_services(cfg):
    """Which shipping services this marketplace will actually accept."""
    marketplace = cfg.get("ebay_marketplace") or "EBAY_US"
    status, payload = call(
        cfg, "GET", SHIPPING_SERVICES_PATH.format(marketplace))
    if status != 200:
        raise EbayError(f"eBay refused the shipping service list ({status}): "
                        + describe_errors(payload))
    return _service_rows(payload)


def choose_shipping_service(services, preferred=PREFERRED_SHIPPING):
    """The best domestic service eBay will take, or None.

    Preference order over what is *offered*, never a fixed answer. eBay
    renames services and the sandbox lags production, so the question
    "what would I like" and the question "what will you accept" have to
    stay separate or the second one is never asked.
    """
    offered = {s["code"]: s for s in services
               if s.get("usable") and not s.get("international")}
    for code in preferred:
        if code in offered:
            return offered[code]
    # Then by what the service is *called*. A code that has been renamed
    # keeps its old spelling, so the description is the half that tracks
    # reality - and this is what would have found Ground Advantage
    # without anyone having to learn it is filed under `USPSParcel`.
    for want in PREFERRED_DESCRIPTIONS:
        for row in offered.values():
            if want in (row.get("description") or "").lower():
                return row
    # Nothing preferred; take the first domestic one eBay will accept
    # rather than refusing outright. Said out loud by the caller.
    return next(iter(offered.values()), None)
