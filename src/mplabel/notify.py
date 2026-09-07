"""
notify.py - push notifications, and the three things that earn one.

The design is blunt about the scope and it is worth keeping: **a parcel
is due, the printer failed, or money has no home.** Nothing else. A
notification that is not one of those trains her to swipe them all away,
and the one that matters is then the one she swipes away fastest.

Two dependencies that are not Python packages
---------------------------------------------
APNs requires HTTP/2 and an ES256-signed JWT, and the stdlib has neither.
The obvious answer is `httpx[http2]` plus `cryptography`, which is a
compiler toolchain and about 40MB on a Pi that deliberately runs a short
dependency list - the same rule that keeps `savedpage.py` on stdlib
HTMLParser and the label overlay on hand-written PDF bytes.

So this shells out to two programs that are already on the machine:

    curl --http2   sends the request
    openssl        signs the JWT

Both are present on Raspberry Pi OS and both are already relied on
elsewhere in this project's own instructions. The cost is that the
failure modes are a subprocess's rather than a library's, which is why
every call here checks the exit status and keeps stderr.

`openssl dgst -sha256 -sign` emits a DER signature and JOSE wants the raw
r||s pair, so `_der_to_jose` unpacks it. That is 20 lines of parsing
rather than a dependency, and it is unit-tested against a signature
openssl actually produced.

What is deliberately not here
-----------------------------
No retry queue and no delivery guarantee. APNs is best effort by design;
a notification that arrives late is worse than useless and one that
arrives twice is noise. Everything this notifies about is also visible
in the app, which is the actual source of truth - the push is a tap on
the shoulder, not a channel.
"""

import base64
import json
import logging
import os
import subprocess
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("mplabel.notify")

# Apple's, and the sandbox one for a build signed with a development
# profile. A token registered against one is meaningless to the other -
# which presents as a 400 BadDeviceToken and reads like a bad token.
APNS_HOST = "api.push.apple.com"
APNS_SANDBOX_HOST = "api.sandbox.push.apple.com"

# The JWT is good for an hour; Apple refuses one older than that and
# rate-limits regeneration, so it is cached until it is nearly stale.
TOKEN_TTL = 3000

SCHEMA = """
-- A phone that has asked to be told. One row per install, keyed by the
-- APNs token itself: re-registering the same device is an update rather
-- than a second row, and a token that Apple later rejects is deleted
-- rather than marked, because a dead token has nothing to say.
CREATE TABLE IF NOT EXISTS devices (
    token        TEXT PRIMARY KEY,
    environment  TEXT,              -- production | sandbox
    registered_at TEXT,
    last_sent_at TEXT,
    label        TEXT               -- "her phone", for a person reading
);

-- What has already been said, so nothing is said twice.
--
-- Keyed by the *thing* and the *kind*, not by a timestamp: "7QK is due"
-- is one notification however many times the poller notices it, and the
-- alternative - a cooldown in minutes - would go off again the moment
-- the process restarted.
CREATE TABLE IF NOT EXISTS notices (
    kind      TEXT NOT NULL,
    subject   TEXT NOT NULL,
    sent_at   TEXT,
    PRIMARY KEY (kind, subject)
);
"""


class NotifyError(RuntimeError):
    """Something about the configuration or the tools, not the phone."""


# ------------------------------------------------------------- the JWT

def _der_to_jose(der):
    """An ECDSA signature, DER in and JOSE out.

    openssl emits `SEQUENCE { INTEGER r, INTEGER s }`; a JWT wants the
    two numbers concatenated as fixed-width big-endian, 32 bytes each for
    P-256. The leading zero DER adds to keep an integer positive has to
    come off, and a short integer has to be padded back up - getting
    either wrong produces a signature Apple rejects with no explanation
    beyond 403 InvalidProviderToken.
    """
    if not der or der[0] != 0x30:
        raise NotifyError("openssl did not return a DER signature")

    def read_int(buf, at):
        if buf[at] != 0x02:
            raise NotifyError("malformed DER signature")
        length = buf[at + 1]
        value = buf[at + 2:at + 2 + length]
        return value.lstrip(b"\x00").rjust(32, b"\x00"), at + 2 + length

    # Skip the SEQUENCE header; its length byte may be long-form.
    at = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    r, at = read_int(der, at)
    s, _ = read_int(der, at)
    return r + s


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign(key_path, message):
    """ES256 over the JWT's signing input, via openssl."""
    try:
        done = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
            input=message, capture_output=True, check=False)
    except FileNotFoundError:
        raise NotifyError("openssl is not installed, and the JWT cannot be "
                          "signed without it")
    if done.returncode != 0:
        raise NotifyError("openssl could not sign with "
                          f"{key_path}: {done.stderr.decode().strip()}")
    return _der_to_jose(done.stdout)


_cached = {"token": None, "at": 0}


def provider_token(cfg, now=None):
    """The JWT APNs wants in `authorization`, cached for its hour."""
    now = now or time.time()
    if _cached["token"] and now - _cached["at"] < TOKEN_TTL:
        return _cached["token"]

    key_path = cfg.get("apns_key_path") or ""
    key_id = cfg.get("apns_key_id") or ""
    team_id = cfg.get("apns_team_id") or ""
    missing = [name for name, value in
               (("apns_key_path", key_path), ("apns_key_id", key_id),
                ("apns_team_id", team_id)) if not value]
    if missing:
        raise NotifyError("not configured for push: " + ", ".join(missing)
                          + " must be set")
    if not os.path.exists(key_path):
        raise NotifyError(f"the APNs key is not at {key_path}")

    header = _b64(json.dumps({"alg": "ES256", "kid": key_id}).encode())
    payload = _b64(json.dumps({"iss": team_id, "iat": int(now)}).encode())
    signing_input = f"{header}.{payload}".encode()
    token = f"{header}.{payload}.{_b64(_sign(key_path, signing_input))}"
    _cached.update(token=token, at=now)
    return token


# ------------------------------------------------------------- sending

def _host(cfg):
    return (APNS_SANDBOX_HOST
            if str(cfg.get("apns_environment", "production")).lower()
               == "sandbox"
            else APNS_HOST)


def send_one(cfg, device_token, title, body, thread=None, sender=None):
    """One notification to one phone. Returns (ok, detail).

    `sender` is the seam the tests use - nothing here should ever reach
    Apple from a test suite, and a fake that records what it was asked to
    send is worth more than one that pretends to succeed.
    """
    topic = cfg.get("apns_topic") or ""
    if not topic:
        raise NotifyError("apns_topic must be the app's bundle id")

    payload = {"aps": {"alert": {"title": title, "body": body},
                       "sound": "default",
                       "interruption-level": "active"}}
    if thread:
        payload["aps"]["thread-id"] = thread

    if sender is not None:
        return sender(device_token, payload)

    jwt = provider_token(cfg)
    url = f"https://{_host(cfg)}/3/device/{device_token}"
    try:
        done = subprocess.run(
            ["curl", "--http2", "--silent", "--show-error",
             "--write-out", "\n%{http_code}",
             "--header", f"authorization: bearer {jwt}",
             "--header", f"apns-topic: {topic}",
             "--header", "apns-push-type: alert",
             "--data", json.dumps(payload), url],
            capture_output=True, check=False, timeout=20)
    except FileNotFoundError:
        raise NotifyError("curl is not installed, and APNs needs HTTP/2")
    except subprocess.TimeoutExpired:
        return False, "APNs did not answer in 20s"

    out = done.stdout.decode().strip().rsplit("\n", 1)
    status = out[-1] if out else ""
    detail = out[0] if len(out) > 1 else done.stderr.decode().strip()
    return status == "200", detail or status


def devices(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM devices ORDER BY registered_at")]


def register(conn, token, environment="production", label=None):
    """One row per device, keyed by the token itself."""
    token = (token or "").strip()
    if not token or len(token) < 32:
        raise ValueError("that is not an APNs device token")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO devices (token, environment, registered_at, label) "
        "VALUES (?,?,?,?) ON CONFLICT(token) DO UPDATE SET "
        "environment=excluded.environment, registered_at=excluded.registered_at",
        (token, environment, now, label))
    conn.commit()
    return token


def forget(conn, token):
    """A token Apple has rejected. Deleted, not flagged: a dead token has
    nothing left to say, and keeping it means deciding every time whether
    to try it again."""
    conn.execute("DELETE FROM devices WHERE token=?", (token,))
    conn.commit()


def already_said(conn, kind, subject):
    return conn.execute(
        "SELECT 1 FROM notices WHERE kind=? AND subject=?",
        (kind, str(subject))).fetchone() is not None


def remember(conn, kind, subject):
    conn.execute(
        "INSERT OR REPLACE INTO notices (kind, subject, sent_at) "
        "VALUES (?,?,?)",
        (kind, str(subject),
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()


# --------------------------------------------------------- the triggers

def due_parcels(conn, today=None):
    """Parcels whose ship-by is today or gone.

    The deadline is a real Facebook commitment and missing it hurts her
    seller rating, which is why this is one of the three."""
    from . import cli as cli_mod

    today = today or date.today()
    marks = ",".join("?" * len(cli_mod.CLOSED_STATUSES))
    rows = conn.execute(
        f"SELECT id, code, item, ship_by FROM sales "
        f"WHERE status NOT IN ({marks}) AND ship_by IS NOT NULL "
        f"AND ship_by <= ? ORDER BY ship_by",
        list(cli_mod.CLOSED_STATUSES) + [today.isoformat()]).fetchall()
    return [dict(r) for r in rows]


def unattributed(conn, older_than_days=2):
    """Trips with money that has not been attached to anything.

    Given a couple of days first: telling her about it on the drive home
    is nagging, not helping - the receipt is in her bag and triage is a
    kitchen-table job."""
    cutoff = (date.today() - timedelta(days=older_than_days)).isoformat()
    rows = conn.execute(
        "SELECT t.id, t.store, t.receipt_total, "
        "  (SELECT COALESCE(SUM(paid), 0) FROM listings "
        "     WHERE trip_id = t.id) AS assigned "
        "FROM trips t WHERE t.receipt_total IS NOT NULL "
        "AND COALESCE(t.occurred_at, '') <= ? ", (cutoff,)).fetchall()
    out = []
    for row in rows:
        left = round((row["receipt_total"] or 0) - (row["assigned"] or 0), 2)
        if left > 0.005:
            out.append({"id": row["id"], "store": row["store"],
                        "unassigned": left})
    return out


def failed_prints(conn, today=None):
    """Labels recorded but never printed, today.

    The printer is write-only, so this is not "a print failed" - nothing
    can know that. It is "this was recorded and no print was ever
    recorded for it", which is the same set `mplabel pending` shows and
    the reason that command exists."""
    # `date('now')` in SQLite is **UTC**, and every date this system
    # handles - a ship-by off a Facebook email, an evening at the kitchen
    # table - is local. Between 7pm and midnight Eastern the two differ,
    # so a label recorded tonight would not be "today" and a parcel due
    # tomorrow would be reported as due now. The comparison is made
    # against a local date computed here for that reason, and the same
    # goes for `due_parcels`.
    today = (today or date.today()).isoformat()
    rows = conn.execute(
        "SELECT id, code, item FROM sales "
        "WHERE printed_at IS NULL AND label_pdf IS NOT NULL "
        "AND status = 'to_ship' AND date(received_at) = ?", (today,)
    ).fetchall()
    return [dict(r) for r in rows]


def run(cfg, conn, sender=None, today=None, remember_sent=True):
    """Say the three things, once each. Returns what was sent.

    `remember_sent=False` is what makes `--dry-run` actually dry. It is a
    flag rather than a rollback because `remember` commits - a caller
    that ran the whole decision and then called `rollback()` would have
    written the notices anyway and reported that it had not, which is the
    dry run lying about the one thing it exists to be honest about."""
    conn.executescript(SCHEMA)
    conn.commit()

    targets = devices(conn)
    sent, skipped = [], []

    def say(kind, subject, title, body):
        if already_said(conn, kind, subject):
            return
        if not targets:
            skipped.append((kind, subject))
            return
        delivered = False
        for device in targets:
            ok, detail = send_one(cfg, device["token"], title, body,
                                  thread=kind, sender=sender)
            if ok:
                delivered = True
            else:
                log.warning("push to %s failed: %s",
                            device["token"][:8], detail)
                # 410 is Apple saying the app is gone from that phone.
                if "410" in str(detail) or "Unregistered" in str(detail):
                    forget(conn, device["token"])
        if delivered:
            if remember_sent:
                remember(conn, kind, subject)
            sent.append({"kind": kind, "subject": subject, "title": title,
                         "body": body})

    for parcel in due_parcels(conn, today=today):
        code = parcel["code"] or f"#{parcel['id']}"
        say("due", code, f"{code} goes out today",
            (parcel["item"] or "A parcel") + " is at its ship-by date.")

    unprinted = failed_prints(conn, today=today)
    if unprinted:
        codes = ", ".join(p["code"] or f"#{p['id']}" for p in unprinted[:3])
        say("unprinted", codes,
            f"{len(unprinted)} label"
            + ("" if len(unprinted) == 1 else "s") + " never printed",
            f"{codes} recorded but no print. The printer cannot confirm "
            "one, so the paper is the only proof.")

    for trip in unattributed(conn):
        say("money", trip["id"],
            f"${trip['unassigned']:.2f} has no home",
            f"{trip['store']} still has cost that is not attached to "
            "anything. Triage takes a minute.")

    return {"sent": sent, "skipped": skipped, "devices": len(targets)}
