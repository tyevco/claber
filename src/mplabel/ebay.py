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
# inventory items and offers; `sell.account.readonly` is how `check`
# confirms the business policies and the inventory location exist without
# taking permission to change them; `sell.fulfillment.readonly` is orders.
#
# Note these are always the api.ebay.com URIs even in sandbox - the scope
# strings are identifiers, not endpoints, and rewriting them to the
# sandbox host is a rejection that looks like a permissions problem.
SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
)

# Refresh a little early. A token that expires between the check and the
# call is a 401 on a request that had every reason to work.
EXPIRY_SLACK = 300

TIMEOUT = 30


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

    Worth its own function because the interesting part is usually
    `parameters`, which names the field that was wrong, and it is nested
    two levels down where nobody looks.
    """
    errors = (payload or {}).get("errors") or []
    parts = []
    for err in errors:
        text = err.get("message") or err.get("longMessage") or "?"
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

    Returns a list of (label, value, problem_or_None) so the caller does
    the printing and this stays testable.
    """
    rows = []

    def add(label, value, problem=None):
        rows.append((label, value, problem))

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
        # inside eBay's own UI, where it looks like eBay's problem.
        add(label, value or "(unset)",
            None if value else "needed to publish, not to draft")
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
