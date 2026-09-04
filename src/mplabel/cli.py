#!/usr/bin/env python3
"""
mplabel.py - watch a mailbox for Facebook Marketplace shipping-label emails,
record each sale, convert the label to 4x6, and print it.

    ./mplabel.py check                 one pass, no printing
    ./mplabel.py run                   one pass, prints
    ./mplabel.py run --loop            keep polling (what systemd runs)
    ./mplabel.py file label.pdf        convert one PDF by hand
    ./mplabel.py reprint 2379911152536775
    ./mplabel.py list                  what still needs shipping
    ./mplabel.py test-print            print the most recent label again

Config comes from /etc/mplabel.conf or ~/.config/mplabel.conf, or the
environment. See mplabel.conf.example.
"""

import argparse
import configparser
import csv
import email
import hashlib
import imaplib
import io
import logging
import os
import random
import socket
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    # Windows has no fcntl. The Pi is the deployment target and the print
    # lock matters there, but importing this module has to work anywhere or
    # the tests cannot run off-target - which is where they are written.
    fcntl = None

from . import backfill as backfill_mod
from . import label
from . import listings as listings_mod
from . import mailparse
from . import printers
from . import savedpage as savedpage_mod
from . import sheets as sheets_mod
from . import inventory as inventory_mod
from . import supvan as supvan_mod

log = logging.getLogger("mplabel")

DEFAULTS = {
    "imap_host": "imap.gmail.com",
    "imap_port": "993",
    "imap_user": "",
    "imap_password": "",
    "imap_folder": "INBOX",
    "processed_label": "Shipped-Labels",
    "home": str(Path.home() / "marketplace"),
    # TSPL, confirmed by printing a real label. The G4's IEEE-1284 id
    # claims COMMAND SET:ESC/POS, which is boilerplate - the same string
    # calls this thermal printer an "Impact Printer". Do not switch this
    # back on the strength of the id alone; print something first.
    "printer_backend": "tspl",
    "printer_queue": "",
    "printer_device": "/dev/usb/lp0",
    "printer_dpi": "203",
    "printer_darkness": "8",
    "printer_speed": "4",
    "media_tracking": "gap",
    "gap_inches": "0.12",
    "escpos_band_rows": "128",
    # The 48mm inventory label maker (SUPVAN/KATA T50M Pro). It is a HID
    # device, not a printer: no /dev/usb/lpN, writes go to a hidraw node.
    # hidraw0 is only the usual number - another HID device plugged in
    # first takes it and this one becomes hidraw1. Nothing here prints to
    # it; `mplabel supvan-probe` polls its status and moves no paper.
    "supvan_device": "/dev/hidraw0",
    # Dots across the print head. render_bitmap refuses to render wider
    # than this; the overflow is clipped by the hardware and ejects a
    # second, near-blank label. 812 is the G4 at 203dpi.
    "printer_head_dots": "812",
    # A 3-character code printed small in the top right, so a stack of parcels
    # can be told apart at a glance. Stored on the sale and mirrored to the
    # sheet; the archived PDF is left unstamped.
    "label_code": "yes",
    "label_code_size": "8",
    "settle_seconds": "2.0",
    "poll_seconds": "120",
    # How far back each poll looks. Read state is not a filter - Gmail
    # marks a whole conversation read when you open it - so the window
    # plus the message_id check is what stops repeats.
    "lookback_days": "7",
    "auto_print": "yes",
    # Google Sheets. Leave sheets_key blank to disable the sync entirely.
    "sheets_key": "",
    "sheets_id": "",
    "sheets_name": "Marketplace",
    "sheets_after_poll": "yes",
    # The phone app. Bind to loopback by default: the intended route in is
    # a Cloudflare tunnel, and binding 0.0.0.0 would also answer anything
    # else that can reach the Pi. Set web_bind = 0.0.0.0 for LAN-only use.
    "web_bind": "127.0.0.1",
    "web_port": "8080",
    # scrypt hash of the one shared password; `mplabel passwd` prints it.
    # Empty means the app refuses to start rather than run unauthenticated.
    "web_password_hash": "",
    "web_session_days": "30",
    # yes | no | auto. auto sets the cookie's Secure flag when the request
    # arrived over HTTPS, which behind cloudflared means trusting
    # X-Forwarded-Proto. Forcing yes on a plain-HTTP LAN test makes the
    # browser drop the cookie and the login fails with no visible reason.
    "web_secure_cookie": "auto",
    # The print service. `printer_backend = pi-http` sends jobs here
    # instead of straight to a device; every other printer_* key stays
    # on the machine with the printer, which is the machine being tuned.
    # Rolling back the split is one line: set printer_backend to tspl.
    "printd_bind": "127.0.0.1",
    "printd_port": "9101",
    "printd_url": "",
    # Shared secret. Both sides need the same value; printd refuses to
    # start without one rather than accept unsigned jobs.
    "printd_secret": "",
    "printd_timeout": "45",
    "printd_state_dir": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    id           INTEGER PRIMARY KEY,
    order_id     TEXT,
    listing_id   TEXT,
    message_id   TEXT UNIQUE,
    received_at  TEXT,
    buyer        TEXT,
    item         TEXT,
    price        REAL,
    ship_by      TEXT,
    tracking     TEXT,
    ship_to      TEXT,
    weight       TEXT,
    service      TEXT,
    raw_pdf      TEXT,
    label_pdf    TEXT,
    printed_at   TEXT,
    print_count  INTEGER DEFAULT 0,
    code         TEXT,
    status       TEXT DEFAULT 'to_ship',
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_status   ON sales(status);
CREATE INDEX IF NOT EXISTS idx_tracking ON sales(tracking);
-- NOT unique on listing_id. One listing can sell more than once: a buyer
-- cancels, someone else buys the same item, and Facebook sends a second
-- label email with the same listing_id and a new order_id. The unit of a
-- sale is the order.
CREATE INDEX IF NOT EXISTS idx_listing ON sales(listing_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_order
    ON sales(order_id) WHERE order_id IS NOT NULL;
"""

# (table, column, declaration) for every column added after the first
# install. CREATE TABLE IF NOT EXISTS will not touch a database that
# already holds real sales, so a new column has to be named here as well
# as in the SCHEMA it belongs to - otherwise it exists on fresh installs
# and nowhere else, and the difference only shows up on her Pi.
MIGRATIONS = [
    ("sales", "code", "TEXT"),
    ("listings", "inventory_code", "TEXT"),
]


# ----------------------------------------------------------------- config

def load_config(path=None):
    cfg = dict(DEFAULTS)
    candidates = [Path(path)] if path else [
        Path("/etc/mplabel.conf"),
        Path.home() / ".config" / "mplabel.conf",
    ]
    for cand in candidates:
        if cand and cand.exists():
            parser = configparser.ConfigParser()
            parser.read(cand)
            if parser.has_section("mplabel"):
                cfg.update({k: v for k, v in parser["mplabel"].items()})
            log.debug("config from %s", cand)
            break

    # Environment wins, so secrets can stay out of the file entirely.
    for key in cfg:
        env = os.environ.get("MPLABEL_" + key.upper())
        if env:
            cfg[key] = env
    return cfg


def truthy(v):
    return str(v).strip().lower() in ("1", "yes", "true", "on")


# --------------------------------------------------------------------- db

def connect_db(home):
    home = Path(home)
    (home / "labels").mkdir(parents=True, exist_ok=True)
    db = home / "sales.db"
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        # Two processes share this file now - the poll loop and the web
        # app - and the default rollback journal turns that into
        # "database is locked" the first time they overlap, which would
        # surface as a failed print in the middle of a nine-item batch.
        #
        # journal_mode is a property of the file and persists; busy_timeout
        # is per-connection, so every opener has to ask for it. Under WAL,
        # synchronous=NORMAL is still crash-safe and is much kinder to an
        # SD card than FULL.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        # listings owns its own tables, but the poll loop writes mail_events
        # as it goes, so they have to exist from the start. Both scripts are
        # CREATE TABLE IF NOT EXISTS.
        conn.executescript(listings_mod.SCHEMA)
        # ...which is exactly why a new column needs saying separately: the
        # database already holds real sales and CREATE TABLE IF NOT EXISTS
        # will not touch them. See MIGRATIONS.
        wanted = {}
        for table, column, decl in MIGRATIONS:
            wanted.setdefault(table, []).append((column, decl))
        for table, columns in wanted.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns:
                if column not in have:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    log.info("added %s.%s to the existing database",
                             table, column)

        # idx_listing used to be UNIQUE, which made a second sale of the
        # same listing impossible - and a cancel-and-rebuy is exactly that.
        # CREATE INDEX IF NOT EXISTS will not replace it, so it has to go
        # explicitly.
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_listing' AND sql LIKE '%UNIQUE%'").fetchone()
        if legacy:
            conn.execute("DROP INDEX idx_listing")
            conn.execute("CREATE INDEX idx_listing ON sales(listing_id)")
            log.info("replaced the unique index on sales.listing_id - one "
                     "listing can sell more than once")
        conn.commit()
    except sqlite3.OperationalError as exc:
        # sqlite says only "unable to open database file" whether the
        # directory is missing, unwritable, or the file itself is owned by
        # someone else. Say which path, so it is fixable in one step.
        raise SystemExit(
            f"cannot open {db}: {exc}\n"
            f"Check that {home} exists and is writable by the user running "
            f"this ({os.getenv('USER') or 'you'}). Running mplabel under "
            f"sudo even once leaves root-owned files there that the service "
            f"user can no longer write.")
    return conn


def upsert(conn, rec):
    fields = [k for k in rec if k in {
        "order_id", "listing_id", "message_id", "received_at", "buyer",
        "item", "price", "ship_by", "tracking", "ship_to", "weight",
        "service", "raw_pdf", "label_pdf", "status", "notes"}]
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    conn.execute(f"INSERT OR IGNORE INTO sales ({cols}) VALUES ({marks})",
                 [rec[f] for f in fields])
    conn.commit()


def candidate_ids(imap, cfg, host):
    """Which messages to consider this poll.

    Not UNSEEN. Gmail groups messages into a conversation, and opening a
    conversation marks *every* message in it read - she sold nine things
    at once, Gmail threaded them, and one glance at the thread hid the
    other eight labels forever. Read state cannot gate printing.

    So: everything from Facebook within a recent window, deduplicated
    against what is already in the database. Deliberately not filtered on
    the processed Gmail label either - Gmail's search is thread-aware in
    places, and labelling one message must not be able to hide its eight
    siblings."""
    days = int(cfg.get("lookback_days") or 7)
    doms = " OR ".join(mailparse.SENDER_DOMAINS)
    queries = []
    if "gmail" in host:
        queries.append(f'(X-GM-RAW "from:({doms}) newer_than:{days}d")')
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    queries.append(f'(OR (FROM "facebookmail.com") '
                   f'(FROM "marketplace.facebook.com") SINCE {since})')
    queries.append('(UNSEEN FROM "facebook")')

    for q in queries:
        try:
            typ, data = imap.search(None, q)
        except imaplib.IMAP4.error:
            continue
        if typ == "OK":
            return data[0].split() if data and data[0] else []
    return []


def peek_headers(imap, num):
    """Fetch just the headers needed to triage a message.

    BODY.PEEK is the point: a plain FETCH sets \\Seen, and read state is
    no longer a filter, so touching it would be both pointless and rude.
    From and Subject come along because deciding whether this is a label
    email has to happen before the body is worth downloading."""
    try:
        typ, data = imap.fetch(
            num, "(BODY.PEEK[HEADER.FIELDS "
                 "(MESSAGE-ID SUBJECT FROM REPLY-TO)])")
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data or not data[0]:
        return None
    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
    return email.message_from_bytes(raw) if raw else None


def already_recorded(conn, message_id, is_label):
    """True if this message has already been turned into the data we want.

    Which table counts depends on what the message is, and conflating the
    two cost fifteen unprinted labels. `backfill` records *every*
    classified Facebook message in mail_events, `shipping_label` included
    - but that only means the subject was catalogued, not that a label
    was ever printed. A label email is handled only once it is in
    `sales`; everything else is handled once it is in `mail_events`."""
    if not message_id:
        return False
    table = "sales" if is_label else "mail_events"
    return conn.execute(f"SELECT 1 FROM {table} WHERE message_id=?",
                        (message_id,)).fetchone() is not None


def already_seen(conn, message_id, order_id):
    """Has this *order* already been recorded?

    Keyed on the order, never the listing. It used to check listing_id
    too, which quietly broke the cancel-and-rebuy case: a buyer cancels,
    someone else buys the same item, Facebook sends a second label email
    with the same listing_id and a new order_id - and it was discarded, so
    the second buyer's label never printed while the record still named
    the first buyer. A wasted label is cheap; a parcel posted to the wrong
    person is not.

    order_id still catches the case this was really guarding against, a
    resend of the same order's label email."""
    if message_id and conn.execute(
            "SELECT 1 FROM sales WHERE message_id=?", (message_id,)).fetchone():
        return True
    if order_id and conn.execute(
            "SELECT 1 FROM sales WHERE order_id=?", (order_id,)).fetchone():
        return True
    return False


# ---------------------------------------------------------------- printing

# Statuses that mean the sale is done with, one way or the other. Such a
# row is not outstanding, is never printed again, and its parcel code goes
# back in the pool.
CLOSED_STATUSES = ("shipped", "cancelled")

# Digits and capitals, minus I L O U - the characters that get misread as
# 1, 1, 0 and V on a thermal label read across a room. 32 symbols over 3
# places is 32768 codes, so collisions among open parcels never bite.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 3


def allocate_code(conn):
    """A code that no unshipped parcel is already using.

    Scoped to unshipped deliberately: the code exists to tell apart the
    boxes waiting to go out, so once a parcel ships its code is free
    again. Random rather than sequential, so a re-run cannot silently
    hand out a code that is still on a box in the hall."""
    taken = {(r[0] or "").upper() for r in conn.execute(
        "SELECT code FROM sales WHERE code IS NOT NULL "
        f"AND status NOT IN ({','.join('?' * len(CLOSED_STATUSES))})",
        CLOSED_STATUSES)}
    for _ in range(200):
        code = "".join(random.choice(CODE_ALPHABET)
                       for _ in range(CODE_LENGTH))
        if code not in taken:
            return code
    # Only reachable if a startling share of the space is in use. Walk it
    # rather than keep rolling.
    for n in range(len(CODE_ALPHABET) ** CODE_LENGTH):
        code, rest = "", n
        for _ in range(CODE_LENGTH):
            code = CODE_ALPHABET[rest % len(CODE_ALPHABET)] + code
            rest //= len(CODE_ALPHABET)
        if code not in taken:
            return code
    log.warning("every code is in use by an unshipped parcel")
    return "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def ensure_code(conn, message_id):
    """The row's code, allocating one if it has none.

    Idempotent, so a reprint puts the same digits on the paper as the
    first print did - and as the sheet says."""
    if not message_id:
        return None
    row = conn.execute("SELECT code FROM sales WHERE message_id=?",
                       (message_id,)).fetchone()
    if row is None:
        return None
    if row["code"]:
        return row["code"]
    code = allocate_code(conn)
    conn.execute("UPDATE sales SET code=? WHERE message_id=?",
                 (code, message_id))
    conn.commit()
    return code


def _norm_addr(text):
    return " ".join((text or "").split()).upper()


def label_belongs_to(row):
    """Does the archived PDF actually belong to this sale?

    Returns (ok, detail). Labels were once named after the listing, so two
    orders for one listing wrote to the same file; and where no ids parsed,
    the fallback name was a timestamp to the second, which collides inside
    a batch. Both leave a row pointing at somebody else's label, and
    printing that posts a parcel to the wrong person.

    `ship_to` was read off the label when the sale was recorded, so the
    page and the record must still agree. Where there is nothing to compare
    - no stored address, or the page will not parse - say so rather than
    guessing, and let the caller decide."""
    if not row["label_pdf"]:
        return False, "no label file recorded"
    path = Path(row["label_pdf"])
    if not path.exists():
        return False, f"label file is missing: {path}"
    stored = row["ship_to"]
    if not stored:
        return True, "no recorded address to check against"
    try:
        found = label.extract_label_fields(path).get("ship_to")
    except Exception as exc:
        return True, f"could not read the label ({exc})"
    if not found:
        return True, "no address found on the label"
    if _norm_addr(found) != _norm_addr(stored):
        return False, (f"label is addressed to {found!r} but this sale "
                       f"recorded {stored!r}")
    return True, ""


# print_lock moved to printers.py so a print daemon can take it without
# importing label (and with it pdfplumber and pypdf). Re-exported here
# because that is where every caller in this module expects it.
print_lock = printers.print_lock


def print_label(cfg, pdf_path, code=None):
    backend = cfg["printer_backend"]
    kwargs = printers.backend_kwargs(cfg, backend, code=code)

    # Stamp a throwaway copy rather than the archive, so a reprint cannot
    # double-stamp and labels/<ref>_4x6.pdf stays as Facebook sent it.
    #
    # The name has to be unique per *job*, not per code and PID. web.Server
    # is a ThreadingHTTPServer, so two handler threads share a PID - and a
    # double-tap on a laggy phone is two prints of the same sale, hence the
    # same code. The old name collided, and one thread's `finally` unlinked
    # the file the other was still reading.
    tmp = None
    if code and truthy(cfg.get("label_code", "yes")):
        fh = tempfile.NamedTemporaryFile(
            prefix=f"mplabel_{code}_", suffix=".pdf", delete=False)
        fh.close()
        tmp = Path(fh.name)
        label.stamp_code(pdf_path, tmp,
                         code, size=float(cfg.get("label_code_size", 8)))
        pdf_path = tmp

    log.info("printing %s via %s%s", Path(pdf_path).name, backend,
             f" [{code}]" if code else "")
    # Only lock when this process is the one touching the printer. With a
    # remote backend printd owns the device and takes the lock itself -
    # and since Phase 3 runs printd on the same Pi over loopback, holding
    # it here would deadlock against printd trying to acquire it.
    try:
        if backend in printers.REMOTE_BACKENDS:
            printers.send(pdf_path, backend, **kwargs)
        else:
            with printers.print_lock(cfg):
                printers.send(pdf_path, backend, **kwargs)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def mark_printed(conn, message_id):
    conn.execute("UPDATE sales SET printed_at=?, print_count=print_count+1, "
                 "status=CASE WHEN status='to_ship' THEN 'printed' "
                 "ELSE status END WHERE message_id=?",
                 (datetime.now().isoformat(timespec="seconds"), message_id))
    conn.commit()


# --------------------------------------------------------------- processing

def process_message(cfg, conn, msg, do_print):
    parsed = mailparse.parse(msg)
    if already_seen(conn, parsed.get("message_id"), parsed.get("order_id")):
        log.debug("skipping, already recorded")
        return None

    fname, blob = mailparse.attachment(msg, ".pdf")
    if not blob:
        log.warning("no PDF attached to %s", parsed.get("subject"))
        return None

    # The name has to be unique per *email*, not per listing. It used to be
    # the listing id, so a cancel-and-rebuy wrote the second buyer's label
    # over the first one's file and both rows then pointed at it; and where
    # no ids were parsed the fallback was a timestamp to the second, which
    # collides trivially when a batch of labels is processed in a loop.
    # Either way a reprint posts a parcel to the wrong person.
    # Facebook names the attachment label_<id>.pdf, and on real mail that
    # is the only id we get - the body's order_id/listing_id links parsed
    # as NULL on all 18 real labels. Prefer it over a timestamp, which is
    # both unsearchable and, at second resolution, collides inside a batch.
    from_name = Path(fname or "").stem.replace("label_", "").strip()
    ref = (parsed.get("order_id")
           or parsed.get("listing_id")
           or (from_name if from_name.isalnum() else None)
           or datetime.now().strftime("%Y%m%d%H%M%S"))
    unique = hashlib.sha1(
        (parsed.get("message_id") or uuid.uuid4().hex).encode("utf-8")
    ).hexdigest()[:8]
    labels = Path(cfg["home"]) / "labels"
    raw_pdf = labels / f"{ref}_{unique}_source.pdf"
    out_pdf = labels / f"{ref}_{unique}_4x6.pdf"
    raw_pdf.write_bytes(blob)

    info = label.to_4x6(raw_pdf, out_pdf)
    log.info("cropped %s -> %.2f x %.2f in (rot %d)", ref,
             *info["size_in"], info["rotation"])

    rec = dict(parsed)
    rec["raw_pdf"] = str(raw_pdf)
    rec["label_pdf"] = str(out_pdf)
    # Only the label carries these; do not overwrite anything from the email.
    for k, v in label.extract_label_fields(out_pdf).items():
        rec.setdefault(k, v)

    upsert(conn, rec)

    if do_print:
        try:
            print_label(cfg, out_pdf, ensure_code(conn, rec.get("message_id")))
            mark_printed(conn, rec.get("message_id"))
        except Exception as exc:
            log.error("print failed for %s: %s", ref, exc)
            conn.execute("UPDATE sales SET notes=? WHERE message_id=?",
                         (f"print failed: {exc}", rec.get("message_id")))
            conn.commit()
    return rec


def record_event(conn, msg):
    """Note a non-label Facebook email in mail_events.

    A sale generates "New Marketplace order for <item>" first, and a local
    pickup sale generates *only* that - no label email ever arrives. Before
    this, those sales were invisible: the poller kept label mail and
    discarded everything else, so the database only ever knew about items
    that were shipped.

    Returns 1 if a sale-side event was stored, else 0."""
    # The IMAP search only matches the From header as text, so check the
    # sender domain before believing a subject line. Otherwise anyone who
    # puts "Facebook" in a display name can post a sale into her figures.
    if not mailparse.is_from_facebook(msg):
        return 0
    subject = mailparse._decode(msg.get("Subject"))
    kind = listings_mod.classify(subject)
    if not kind or kind == "shipping_label":
        return 0
    try:
        occurred = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        occurred = None
    parsed = mailparse.parse(msg)
    # Her own purchases carry the seller's listing id; drop it.
    buyer_side = kind in listings_mod.BUYER_KINDS
    listings_mod.record_event(
        conn, mailparse._decode(msg.get("Message-ID")), occurred, kind,
        subject,
        listing_id=None if buyer_side else parsed.get("listing_id"),
        amount=parsed.get("price"), counterparty=parsed.get("buyer"))
    conn.commit()
    return 0 if buyer_side else 1


def poll_once(cfg, conn, do_print):
    host, port = cfg["imap_host"], int(cfg["imap_port"])
    user, pw = cfg["imap_user"], cfg["imap_password"]
    if not user or not pw:
        raise SystemExit("imap_user / imap_password not configured")

    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(user, pw)
        imap.select(cfg["imap_folder"])
        ids = candidate_ids(imap, cfg, host)
        log.info("%d candidate(s) in the last %s day(s)",
                 len(ids), cfg.get("lookback_days") or 7)

        handled = noted = skipped = 0
        for num in ids:
            # Triage on headers before pulling the body: most candidates
            # are mail we have already handled, and BODY.PEEK leaves the
            # message's read state alone.
            hdr = peek_headers(imap, num)
            if hdr is not None:
                mid = mailparse._decode(hdr.get("Message-ID")) or None
                if already_recorded(conn, mid,
                                    mailparse.is_label_email(hdr)):
                    skipped += 1
                    continue
            typ, raw = imap.fetch(num, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            if not mailparse.is_label_email(msg):
                # Not a label, but it may still be a sale - and a local
                # pickup sale produces nothing else. Note it, then put the
                # mail back unread exactly as before: recording an event
                # does not consume the message.
                try:
                    noted += record_event(conn, msg)
                except Exception:
                    log.exception("could not record event for %s",
                                  num.decode())
                imap.store(num, "-FLAGS", "\\Seen")
                continue
            try:
                rec = process_message(cfg, conn, msg, do_print)
            except Exception:
                log.exception("failed on message %s", num.decode())
                imap.store(num, "-FLAGS", "\\Seen")
                continue
            if rec:
                handled += 1
                print(f"  {rec.get('item','?')}  ->  {rec.get('buyer','?')}  "
                      f"${rec.get('price','?')}  "
                      f"[{rec.get('tracking','no tracking')}]")
                tag = cfg["processed_label"]
                if tag and "gmail" in host:
                    try:
                        imap.store(num, "+X-GM-LABELS", tag)
                    except Exception:
                        log.debug("could not apply Gmail label %s", tag)
        if skipped:
            log.debug("%d candidate(s) already recorded", skipped)
        if noted:
            log.info("%d sale/listing event(s) noted from non-label mail",
                     noted)
            listings_mod.refresh(conn)
        if (handled or noted) and truthy(cfg.get("sheets_after_poll")) \
                and cfg.get("sheets_key"):
            try:
                sync_sheets(cfg, conn)
            except Exception:
                # A Sheets outage must never stop labels printing.
                log.exception("sheet sync failed; labels are unaffected")
        return handled
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()


def loop(cfg, conn, do_print):
    interval = int(cfg["poll_seconds"])
    backoff = interval
    log.info("polling %s every %ds", cfg["imap_host"], interval)
    while True:
        try:
            poll_once(cfg, conn, do_print)
            backoff = interval
        except (imaplib.IMAP4.abort, socket.error, OSError) as exc:
            # Wifi drops and IMAP timeouts are normal on a Pi; back off and retry.
            log.warning("connection problem (%s), retrying in %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 1800)
            continue
        except Exception:
            log.exception("unexpected error in poll")
        time.sleep(interval)


# ---------------------------------------------------------------- commands

def cmd_file(cfg, args):
    src = Path(args.pdf)
    out = Path(args.output) if args.output else src.with_name(src.stem + "_4x6.pdf")
    info = label.to_4x6(src, out, force_rotation=args.rotate)
    if getattr(args, "code", None):
        # Handy for checking placement on a real label without printing.
        label.stamp_code(out, out, args.code,
                         size=float(cfg.get("label_code_size", 8)))
    print(f"{out}  {info['size_in'][0]} x {info['size_in'][1]} in  "
          f"(rotated {info['rotation']})")
    for k, v in label.extract_label_fields(out).items():
        print(f"  {k}: {v}")
    if args.print_it:
        print_label(cfg, out)


def cmd_supvan_probe(cfg, args):
    """Ask the 48mm inventory label maker how it is.

    A status poll is read-only: it moves no paper, and it is the one round
    trip that proves the HID transport end to end - the report size, the
    leading report-id byte, and the udev permissions - before anything
    harder is attempted. Nothing in mplabel prints to this device; see
    supvan.print_bitmap for what is still unknown."""
    device = args.device or cfg.get("supvan_device",
                                    supvan_mod.DEFAULT_DEVICE)
    print(f"device: {device}")
    try:
        status = supvan_mod.poll_status(device)
    except supvan_mod.SupvanError as exc:
        # Absent or root-only is the ordinary case on a machine that is not
        # the Pi, so say what is wrong in one line rather than a traceback.
        raise SystemExit(str(exc))
    print(supvan_mod.format_status(status))

    if not getattr(args, "deep", False):
        return

    # Everything below still moves no paper - see SAFE_PROBE_OPCODES, which
    # is a safety boundary rather than a convenience. The replies to the
    # revision and media commands have no known format, so they are shown
    # raw: the point is to learn whether they share the status reply's
    # leading byte, and whether they answer at all.
    print("\nsafe read-only commands (no paper moves):")
    for name, opcode, reply, error in supvan_mod.probe_deep(device):
        head = f"  0x{opcode:02x} {name:<24}"
        if error:
            print(f"{head} no reply: {error}")
            continue
        shown = reply[:16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in shown)
        print(f"{head} {shown.hex(' ')}  |{text}|")

        # Every reply is length-prefixed, so say how much of it is real.
        # The rest is padding to fill the 64-byte report and means nothing.
        try:
            payload = supvan_mod.reply_payload(reply)
        except supvan_mod.SupvanError as exc:
            print(f"{' ' * len(head)} unframed: {exc}")
            continue
        note = f"{len(payload)} byte payload"
        if opcode == supvan_mod.OP_READ_REVISION:
            try:
                note += f", revision {supvan_mod.decode_revision(reply)}"
            except supvan_mod.SupvanError:
                pass
        elif opcode in (supvan_mod.OP_INQUIRY_STATUS,
                        supvan_mod.OP_CHECK_DEVICE):
            flags = supvan_mod.decode_status(reply)
            lit = [n for n, _o, _m in supvan_mod.STATUS_FLAGS if flags[n]]
            note += f", flags {', '.join(lit) or 'none'}"
        print(f"{' ' * len(head)} {note}")


def _parse_size(text):
    """`WxH` in millimetres, or with an `in` suffix, in inches.

    Returned as millimetres in reading orientation. Inches are allowed
    because label stock is sold in them - 4x1in is a shelf label - and
    converting by hand is how a 4in label becomes a 4mm one."""
    raw = text.strip().lower()
    scale = 1.0
    if raw.endswith("in"):
        raw, scale = raw[:-2], 25.4
    try:
        w, h = (float(v) * scale for v in raw.split("x"))
    except ValueError:
        raise ValueError(f"--size wants WxH, not {text!r}")
    if w <= 0 or h <= 0:
        raise ValueError(f"--size {text!r} is not a label")
    return w, h


def cmd_inventory_label(cfg, args):
    """Draw one inventory label, and show what the printer would burn.

    Above `connect_db` with the other printer commands, and for the same
    reason: this needs no database, and a label preview that stops
    working because the data directory is unwritable is a preview you
    cannot use when you most need one.

    `--preview` is the point of it. It renders the label, assembles the
    real job, then takes that job *back apart* - decompressing it,
    checking every buffer checksum and reading the geometry out of the
    headers - and draws what comes out. So the picture is of the payload
    on the wire, not of what we meant to send, which is the only version
    worth looking at while nothing built here has printed yet."""
    price = None
    if args.price is not None:
        try:
            price = float(args.price)
        except ValueError:
            price = args.price

    if args.qr and args.marker:
        raise SystemExit("--qr and --marker both want the same square; "
                         "pick one")
    try:
        label_mm = _parse_size(args.size)
    except ValueError as exc:
        raise SystemExit(str(exc))
    try:
        raster, stride, rows = inventory_mod.render_label(
            args.code, title=args.title, price=price, with_qr=args.qr,
            with_marker=args.marker, label_mm=label_mm, ecl=args.ecl)
    except ValueError as exc:
        raise SystemExit(str(exc))
    job = supvan_mod.build_job(raster, stride, rows,
                               density=args.density)

    carrier = ("  (+ QR carrying the same code)" if args.qr else
               "  (+ shelf marker carrying the same code)" if args.marker
               else "")
    print(f"code   : {args.code}{carrier}")
    sideways = inventory_mod.reads_sideways(label_mm)
    print(f"label  : {label_mm[0]:g} x {label_mm[1]:g}mm"
          + (" - printed sideways, long axis down the feed"
             if sideways else ""))
    print(f"raster : {stride * 8} x {rows} dots"
          + (f", of which {inventory_mod.media_box(label_mm)[2] - inventory_mod.media_box(label_mm)[0] + 1}"
             f" across is media" if sideways else ""))
    ink = sum(bin(b).count("1") for b in raster)
    print(f"         {100 * ink / (len(raster) * 8):.2f}% ink")
    print(f"job    : {job['buffers']} x {supvan_mod.PRINT_BUF_SIZE} = "
          f"{job['raw_len']} bytes, {len(job['compressed'])} compressed, "
          f"speed {job['speed']} (derived), density {args.density}")

    # Round-trip it whatever we do next: if the job will not come back
    # apart, it is not going to the printer either.
    try:
        back, back_stride, cols = supvan_mod.decode_job(job["compressed"])
    except supvan_mod.SupvanError as exc:
        raise SystemExit(f"the job does not decode: {exc}")
    print(f"decoded: {cols} printhead lines, {back_stride} bytes each, "
          f"every checksum valid")

    if args.preview:
        img = inventory_mod.to_image(back, back_stride, cols,
                                     scale=args.scale)
        # Crop to the media and turn it the way you hold it. The raster
        # is 384 dots because the *bar* is; a 1in label covers 203 of
        # them and the rest is head hanging off the edge, which in a
        # preview reads as a badly laid out label and is nothing of the
        # sort.
        mx0, _my0, mx1, _my1 = inventory_mod.media_box(label_mm)
        img = img.crop((mx0 * args.scale, 0,
                        (mx1 + 1) * args.scale, img.height))
        if sideways:
            img = img.rotate(-90, expand=True)
        img.save(args.preview)
        print(f"\nwrote {args.preview} - the payload, decoded back"
              + (", turned the way you hold it" if sideways else ""))

    if not args.print:
        print("\nnothing sent, no paper moved. Add --print to try it.")
        return

    device = args.device or cfg.get("supvan_device",
                                    supvan_mod.DEFAULT_DEVICE)
    print(f"\nsending to {device}:")

    def step(label, status, lit):
        print(f"  . {label}" + (f": {', '.join(lit) or 'no flags'}"
                                if status else ""))
    try:
        final = supvan_mod.experimental_print(
            job, path=device, speed=job["speed"], on_step=step)
    except supvan_mod.SupvanError as exc:
        raise SystemExit(f"\nstopped: {exc}")
    print(f"\npages printed now reads {final['pages_printed']}.")


def cmd_supvan_test_print(cfg, args):
    """Try to print one test pattern on the label maker. An experiment.

    Everything the document settles is fixed; everything it does not is a
    flag, so a failed attempt is one command away from the next guess
    rather than an edit. The pattern is deliberately asymmetric - a bar,
    a square and an edge rule - because the useful information is in
    *how* it comes out wrong."""
    device = args.device or cfg.get("supvan_device",
                                    supvan_mod.DEFAULT_DEVICE)
    if args.abort:
        # Worth having as its own path: after a stalled attempt the device
        # sits in printing state, and the next run would stack on top of a
        # half-started job rather than starting a clean one.
        try:
            status = supvan_mod.abort_print(device)
        except supvan_mod.SupvanError as exc:
            raise SystemExit(str(exc))
        print(f"device : {device}")
        print("sent stop print (0x14)")
        print(supvan_mod.format_status(status))
        if status["printing"]:
            print("\nStill reporting 'printing'. Power cycle it.")
        return

    if args.reencode:
        # Their image, our encoder. The one experiment that changes a
        # single variable.
        #
        # The replay proved the sequence by sending the vendor's exact
        # bytes. Generating our own changed two things at once: the
        # encoder AND the picture - and their captured print is 99.9%
        # blank where the test pattern is blocky. Re-encoding their image
        # holds the picture still and asks only whether a literals-only
        # stream is acceptable.
        import lzma as _lzma
        image = _lzma.decompress(Path(args.reencode).read_bytes(),
                                 format=_lzma.FORMAT_ALONE)
        compressed = supvan_mod.compress_bitmap(image)
        ink = sum(bin(b).count("1") for b in image)
        print(f"device : {device}")
        print(f"source : {args.reencode}")
        print(f"image  : {len(image)} bytes, {ink} set bits "
              f"({100 * ink / (len(image) * 8):.2f}% ink)")
        print(f"lzma   : {len(compressed)} bytes by our encoder, "
              f"{-(-len(compressed) // 64)} reports")
        print(f"         head {compressed[:13].hex(' ')}")
        if args.dry_run:
            print("\ndry run - nothing sent, no paper moved")
            return
        print("\nrunning the sequence:")

        def reencode_step(labeltext, status, lit):
            print(f"  . {labeltext}" + (f": {', '.join(lit) or 'no flags'}"
                                        if status else ""))
        try:
            final = supvan_mod.experimental_print(
                {"compressed": compressed, "raw_len": len(image)},
                path=device, speed=args.speed, announce="compressed",
                on_step=reencode_step)
        except supvan_mod.SupvanError as exc:
            raise SystemExit(f"\nstopped: {exc}")
        print(f"\npages printed now reads {final['pages_printed']}.")
        return

    if args.replay:
        # Send a pre-made LZMA stream instead of generating one. The point
        # is to separate two failures that look identical from here: a
        # wrong command sequence, and a stream this firmware will not
        # decode. Replaying bytes that are known to have printed on this
        # device settles which one we have.
        compressed = Path(args.replay).read_bytes()
        print(f"device : {device}")
        print(f"replay : {args.replay}, {len(compressed)} bytes")
        print(f"         head {compressed[:13].hex(' ')}")
        if args.dry_run:
            print("\ndry run - nothing sent, no paper moved")
            return
        print("\nrunning the sequence:")

        def replay_step(labeltext, status, lit):
            print(f"  . {labeltext}" + (f": {', '.join(lit) or 'no flags'}"
                                        if status else ""))
        try:
            final = supvan_mod.experimental_print(
                {"compressed": compressed,
                 "raw_len": int.from_bytes(compressed[5:13], "little")},
                path=device, speed=args.speed, announce="compressed",
                on_step=replay_step)
        except supvan_mod.SupvanError as exc:
            raise SystemExit(f"\nstopped: {exc}")
        print(f"\npages printed now reads {final['pages_printed']}.")
        return

    clip = None
    if args.clip:
        try:
            cw, ch = (int(v) for v in args.clip.lower().split("x"))
        except ValueError:
            raise SystemExit(f"--clip wants WxH, not {args.clip!r}")
        clip = (cw, ch)
    if args.style == "ruler":
        # The calibration target, which is drawn rather than plotted and
        # so lives with the other drawing in inventory.py. --clip and
        # --invert are refused rather than ignored: this label exists to
        # be measured, and measuring one that has been quietly altered is
        # worse than not measuring at all.
        if clip or args.invert:
            raise SystemExit(
                "--style ruler is a measurement, so --clip and --invert "
                "are not allowed with it")
        from . import inventory as inventory_mod
        raw, stride, rows = inventory_mod.render_ruler(args.width,
                                                       args.height)
    else:
        raw, stride, rows = supvan_mod.render_test_pattern(
            args.width, args.height, invert=args.invert, style=args.style,
            clip=clip)

    if args.bare_raster:
        # The shape every generated label was refused in, kept so the
        # change that fixed it can be shown to be the change that fixed
        # it. A bare raster carries no buffer header and no checksum, and
        # its length is not a multiple of 4096.
        if args.max_buffer:
            bands = supvan_mod.split_bitmap(raw, stride, args.max_buffer,
                                            dict_size=args.dict_size)
            payload = {"streams": [(c, n) for c, _r, n in bands]}
            total = sum(len(c) for c, _r, _n in bands)
        else:
            compressed = supvan_mod.compress_bitmap(
                raw, args.lzma, dict_size=args.dict_size,
                declare_size=args.declare_size)
            bands = [(compressed, rows, len(raw))]
            payload = {"compressed": compressed, "raw_len": len(raw)}
            total = len(compressed)
        buffers = len(bands)
    else:
        job = supvan_mod.build_job(
            raw, stride, rows, density=args.density,
            margin_top=args.margin, margin_bottom=args.margin,
            dict_size=args.dict_size)
        payload = {"compressed": job["compressed"],
                   "raw_len": job["raw_len"]}
        bands = [(job["compressed"], rows, job["raw_len"])]
        total = len(job["compressed"])
        buffers = job["buffers"]

    # Speed is a function of how well the image compressed, not a
    # constant - see supvan.calc_speed. --speed still overrides, because
    # holding it still is how it gets tested on its own.
    speed = args.speed
    if speed is None:
        speed = (supvan_mod.calc_speed(total // max(buffers, 1))
                 if not args.bare_raster else 60)

    print(f"device : {device}")
    ink = sum(bin(b).count("1") for b in raw)
    blank = sum(1 for y in range(rows) if not any(raw[y * stride:(y + 1) * stride]))
    print(f"pattern: {args.style}, {args.width} dots wide, {stride} bytes/row, "
          f"{rows} rows" + (", inverted" if args.invert else ""))
    # Ink and blank rows stay on show because CLAUDE.md asks for every
    # property that moved to be tabulated before a label is spent, not
    # just the one being varied.
    print(f"         {100 * ink / (len(raw) * 8):.2f}% ink, "
          f"{blank}/{rows} rows blank")
    print(f"raw    : {len(raw)} bytes")
    if args.bare_raster:
        print("shape  : bare raster - no buffer header, no checksum. "
              "This is the shape the device refuses.")
    else:
        print(f"buffers: {buffers} x {supvan_mod.PRINT_BUF_SIZE} = "
              f"{job['raw_len']} bytes, {args.margin}-dot margins, "
              f"density {args.density}")
        print(f"         column-major, line bytes last-first, "
              f"{supvan_mod.MAX_BUF_DATA // stride} lines per buffer")
    single = args.bare_raster and args.max_buffer == 0
    print(f"lzma   : {total} bytes in {len(bands)} stream(s), "
          f"{args.lzma if single else 'device'} container, "
          f"dict {args.dict_size}"
          # --declare-size only reaches the single-stream bare-raster
          # path; everywhere else the size is declared because a stream
          # with no end marker is undecodable without it.
          + (f", size {'declared' if args.declare_size else 'unknown'}"
             if single else ", size declared"))
    for i, (c, band_rows, _n) in enumerate(bands, 1):
        print(f"       {i:>2}: {len(c):>4} bytes, {-(-len(c) // 64):>2} "
              f"reports, head {c[:13].hex(' ')}")
    print(f"announce {args.announce} length, speed {speed}"
          + ("" if args.speed is not None else " (derived)"))

    if args.dry_run:
        print("\ndry run - nothing sent, no paper moved")
        return

    def step(label, status, lit):
        if status is None:
            print(f"  . {label}")
        else:
            print(f"  . {label}: {', '.join(lit) or 'no flags'}")

    print("\nrunning the sequence:")
    try:
        final = supvan_mod.experimental_print(
            payload, path=device, speed=speed, announce=args.announce,
            buffer_len=args.buffer_len, on_step=step)
    except supvan_mod.SupvanError as exc:
        raise SystemExit(f"\nstopped: {exc}")

    if final.get("stalled"):
        print("\nThe device accepted the job and never finished it, so it was "
              "told to stop.\nReseat the media before the next attempt: the "
              "positioning move leaves it out\nof place, which is the seating "
              "error that blocks the following run.")
    print(f"\nfinished. pages printed now reads "
          f"{final['pages_printed']}.")
    print("If nothing came out, the page counter says whether the device "
          "thought it printed.\nIf something came out, what is wrong with "
          "it is the next clue:\n"
          "  mostly black        -> try --invert\n"
          "  sheared diagonally  -> the width is wrong; --width 320/352/384\n"
          "  nothing at all      -> try --lzma alone|xz|raw, or --announce raw")
def cmd_reconcile(cfg, conn, args):
    """Ask printd what it actually printed, and make the database agree.

    This is the way out of the one ambiguity a network boundary adds. A
    timed-out print cannot be retried safely, because "never arrived" and
    "printed, and the acknowledgement was lost" look identical from here -
    and the second one retried is a duplicate label on a parcel. So
    nothing retries; printd keeps a durable journal instead, and this asks
    it.

    Matching is by the parcel code carried in the job id. That is sound
    for exactly the reason the code exists: it is unique among parcels
    that have not shipped."""
    rows = printers.printd_printed(cfg, since=getattr(args, "since", None))
    if not rows:
        print("printd has printed nothing it can still remember")
        return

    fixed = 0
    for row in rows:
        job = row.get("job") or ""
        code = job.rsplit("-", 1)[0] if "-" in job else job
        if not code or code == "selftest":
            continue
        # Only rows that still claim they never printed, and only by a
        # code that is still open - a shipped parcel's code goes back in
        # the pool, so an old job id must not reach a new parcel.
        sale = conn.execute(
            "SELECT id, item, message_id, printed_at FROM sales "
            "WHERE UPPER(code)=? AND printed_at IS NULL "
            f"AND status NOT IN ({','.join('?' * len(CLOSED_STATUSES))})",
            (code.upper(),) + CLOSED_STATUSES).fetchone()
        if not sale:
            continue
        if args.dry_run:
            print(f"  would mark {code} printed: {sale['item']}")
        else:
            mark_printed(conn, sale["message_id"])
            print(f"  marked {code} printed: {sale['item']}")
        fixed += 1

    if not fixed:
        print(f"{len(rows)} job(s) in printd's journal, nothing to correct")
    elif args.dry_run:
        print(f"\n{fixed} row(s) would change. Drop --dry-run to apply.")


def cmd_status(cfg):
    """Ask the printer how it is. Phase 2 of the split plan.

    Answers one question that nothing else can: does this unit talk
    back? If it does, a print can be refused when the paper is out and
    the parcel stays visible in Pending. If it does not, at-least-once
    is the best available and that is worth writing down rather than
    assuming either way."""
    info = printers.ask_status(cfg["printer_device"])
    print(f"device   {info['device']}")
    if info["answered"]:
        print(f"answered yes, to {info.get('query')}")
        print(f"raw      {info['raw']}")
        print(f"state    {', '.join(info['flags'])}")
        print("\nThis printer can be asked. Worth wiring into printd so a\njob is refused when the paper is out - record it as VERIFIED.")
    else:
        print("answered no")
        print(f"note     {info['note']}")
        print("\nNothing to read back, so a failed print cannot be detected\nin software. Record that as a finding: printing stays at-least-once and\nthe paper is the only source of truth.")


def cmd_passwd():
    """Print the `web_password_hash` line for mplabel.conf.

    Prompted rather than taken as an argument, so the password does not
    land in shell history or in the process list on a machine two people
    share."""
    import getpass

    from . import web as web_mod

    first = getpass.getpass("password for the phone app: ")
    if not first:
        raise SystemExit("empty password")
    if first != getpass.getpass("again: "):
        raise SystemExit("they do not match")
    print("\nPut this in the [mplabel] section of /etc/mplabel.conf:\n")
    print(f"web_password_hash = {web_mod.hash_password(first)}\n")
    print("Then restart the app. Changing it logs out every phone, "
          "because the session signing key is derived from this line.")


def sync_sheets(cfg, conn, dry_run=False):
    listings_mod.refresh(conn)
    counts = sheets_mod.sync(conn, cfg.get("sheets_key"),
                             sheet_id=cfg.get("sheets_id") or None,
                             sheet_name=cfg.get("sheets_name") or None,
                             dry_run=dry_run)
    if not dry_run:
        log.info("sheet updated: %s",
                 ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts


def cmd_list(cfg, conn, args):
    rows = conn.execute(
        "SELECT listing_id, item, buyer, price, ship_by, tracking, status, "
        "printed_at, code FROM sales WHERE status NOT IN "
        f"({','.join('?' * len(CLOSED_STATUSES))}) ORDER BY ship_by",
        CLOSED_STATUSES
    ).fetchall()
    if not rows:
        print("nothing outstanding")
        return
    for r in rows:
        printed = "printed" if r["printed_at"] else "NOT PRINTED"
        # The code first: it is what is written on the box in the hall.
        print(f"{r['code'] or '---':<5} {r['ship_by'] or '?':<12} "
              f"${r['price'] or 0:>7.2f}  "
              f"{(r['item'] or '?')[:38]:<40} {r['buyer'] or '?':<18} "
              f"{printed}")


def find_sale(conn, ref):
    """Look a sale up by any of the things you might have to hand.

    The parcel code is included because it is the only one of these that
    is printed on the box: reading it off the label and typing it back is
    the whole point of stamping it there. Case-insensitive, since it is
    read off paper.

    A listing id can now match more than one sale - a cancel and rebuy
    leaves both - so live sales sort first and the newest wins. Handing
    back the cancelled one would print the wrong buyer's label."""
    return conn.execute(
        "SELECT * FROM sales WHERE listing_id=? OR order_id=? OR tracking=? "
        "OR UPPER(code)=? "
        f"ORDER BY status IN ({','.join('?' * len(CLOSED_STATUSES))}), "
        "id DESC LIMIT 1",
        (ref, ref, ref, (ref or "").upper()) + CLOSED_STATUSES).fetchone()


def cmd_reprint(cfg, conn, args):
    row = find_sale(conn, args.ref)
    if not row:
        raise SystemExit(f"no record matching {args.ref}")
    ok, detail = label_belongs_to(row)
    if not ok and not getattr(args, "force", False):
        raise SystemExit(
            f"refusing to print: {detail}\n"
            f"This sale is {row['item']!r} for {row['buyer']!r}. Printing a "
            f"label addressed to someone else posts the parcel to the wrong "
            f"person. Run `mplabel verify` to see how many rows are "
            f"affected, or --force if you are certain.")
    code = ensure_code(conn, row["message_id"])
    print_label(cfg, row["label_pdf"], code)
    mark_printed(conn, row["message_id"])
    print(f"reprinted {row['item']}" + (f"  [{code}]" if code else ""))


def cmd_test_print(cfg, conn, args):
    row = conn.execute("SELECT * FROM sales WHERE label_pdf IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no labels on file yet - run `check` first")
    ok, detail = label_belongs_to(row)
    if not ok and not getattr(args, "force", False):
        raise SystemExit(
            f"refusing to print: {detail}\n"
            f"The newest label on file is {row['item']!r} for "
            f"{row['buyer']!r}. Use --force if you are certain.")
    code = ensure_code(conn, row["message_id"])
    print_label(cfg, row["label_pdf"], code)
    print(f"sent {Path(row['label_pdf']).name} to {cfg['printer_backend']}"
          + (f"  [{code}]" if code else ""))


def cmd_pending(cfg, conn, args):
    """Print labels that were recorded but never printed.

    `run` cannot do this: once a message is in `sales` the poller skips it
    on sight, which is what stops a re-poll reprinting the world. So
    anything recorded by `check` - or by a run that failed at the printer -
    needs its own way out, and this is it.

    Defaults to today only. The window the poller looks back over is days
    wide, and older labels may well have been printed and posted by hand
    already; reprinting those wastes stock and puts a second label on a
    parcel that has gone."""
    # SELECT *, not a column list: label_belongs_to reads ship_to off the
    # row to check the archived label still matches this sale.
    sql = ("SELECT * "
           "FROM sales WHERE printed_at IS NULL AND label_pdf IS NOT NULL "
           f"AND status NOT IN ({','.join('?' * len(CLOSED_STATUSES))})")
    params = list(CLOSED_STATUSES)
    if not args.all:
        since = args.since or datetime.now().strftime("%Y-%m-%d")
        # Compare the date as written in the email rather than converting:
        # received_at carries the sender's offset, and shifting it around
        # timezones would move labels across the day boundary.
        sql += " AND substr(received_at, 1, 10) >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY received_at", params).fetchall()

    if not rows:
        print("nothing pending" if args.all else
              "nothing pending from today - use --since or --all to widen")
        return

    for r in rows:
        print(f"  {r['code'] or '---':<5} {(r['received_at'] or '?')[:10]}  "
              f"${r['price'] or 0:>7.2f}  {(r['item'] or '?')[:44]}")
    if args.dry_run:
        print(f"\n{len(rows)} label(s) would print. Drop --dry-run to send "
              f"them.")
        return

    print()
    sent = 0
    for r in rows:
        # The same guard cmd_reprint uses. This is the *batch* path, so
        # without it one bad archive name posts several parcels to
        # strangers in one go - and it was the only print path that
        # checked nothing but whether the file existed.
        ok, detail = label_belongs_to(r)
        if not ok:
            print(f"  refusing {r['code'] or '---'}: {detail}")
            log.error("refusing to print %s: %s", r["item"], detail)
            continue
        try:
            print_label(cfg, r["label_pdf"], ensure_code(conn, r["message_id"]))
            mark_printed(conn, r["message_id"])
            sent += 1
        except Exception as exc:
            log.error("print failed for %s: %s", r["item"], exc)
            conn.execute("UPDATE sales SET notes=? WHERE message_id=?",
                         (f"print failed: {exc}", r["message_id"]))
            conn.commit()
    print(f"printed {sent} of {len(rows)}")


INVENTORY_CODE_LENGTH = 4


def ensure_inventory_codes(conn):
    """Give every listing a stable code, and never hand one out twice.

    Unlike the parcel code this is *not* recycled. A parcel code is about
    the boxes waiting to go out, so it comes back once one ships; an
    inventory code is stuck to a thing on a shelf and has to stay true for
    as long as that thing exists - including after it sells, or the label
    on the box in the loft starts naming something else.

    Four characters over the same 32-symbol alphabet is about a million
    codes, so 'never reuse' costs nothing."""
    taken = {(r[0] or "").upper() for r in conn.execute(
        "SELECT inventory_code FROM listings WHERE inventory_code IS NOT NULL")}
    rows = conn.execute(
        "SELECT listing_id FROM listings WHERE inventory_code IS NULL "
        "OR inventory_code = ''").fetchall()
    for row in rows:
        while True:
            code = "".join(random.choice(CODE_ALPHABET)
                           for _ in range(INVENTORY_CODE_LENGTH))
            if code not in taken:
                break
        taken.add(code)
        conn.execute("UPDATE listings SET inventory_code=? WHERE listing_id=?",
                     (code, row["listing_id"]))
    conn.commit()
    return len(rows)


def cmd_inventory(cfg, conn, args):
    """Write a CSV of inventory labels for the label maker.

    The T50M Pro is driven by SUPVAN's own editor, which imports a
    spreadsheet and batch-prints - so the join here is a file, not a
    printer backend. Nothing in this project talks to that device.

    Written as utf-8-sig: her titles carry accents and curly quotes
    ("Jean-Francois", the Otagiri pieces), and Excel on Windows reads a
    plain utf-8 CSV as mojibake, which would print onto the labels."""
    listings_mod.refresh(conn)
    fresh = ensure_inventory_codes(conn)

    sql = ("SELECT inventory_code, title, price, state, listing_id "
           "FROM listings WHERE title IS NOT NULL")
    params = []
    if not args.all:
        sql += " AND state = ?"
        params.append(args.state)
    rows = conn.execute(sql + " ORDER BY title", params).fetchall()
    if not rows:
        print(f"no {args.state} listings with a title - use --all to widen")
        return

    out = Path(args.output or "inventory-labels.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        # `barcode` repeats the code so the template can bind a barcode
        # field to its own column; `short_title` is pre-truncated because
        # 48mm does not hold one of her full titles.
        w.writerow(["code", "barcode", "short_title", "title", "price",
                    "state", "listing_id"])
        for r in rows:
            price = "" if r["price"] is None else f"{r['price']:.2f}"
            title = r["title"] or ""
            w.writerow([r["inventory_code"], r["inventory_code"],
                        title[:38], title, price, r["state"],
                        r["listing_id"]])

    print(f"wrote {len(rows)} label(s) to {out}"
          + (f"  ({fresh} new code(s) assigned)" if fresh else ""))
    print("Import it in SUPVAN's editor, bind the fields once as a "
          "template, then batch print.")


def cmd_verify(cfg, conn, args):
    """Check every archived label still matches the sale it belongs to.

    Worth running once after upgrading: labels used to be named after the
    listing, and where no ids parsed, after a timestamp to the second - so
    a second label could overwrite the first and leave the row pointing at
    the wrong person's address."""
    rows = conn.execute(
        "SELECT * FROM sales WHERE label_pdf IS NOT NULL ORDER BY id"
    ).fetchall()
    seen, shared, bad = {}, [], []
    for r in rows:
        seen.setdefault(r["label_pdf"], []).append(r)
    for path, group in seen.items():
        if len(group) > 1:
            shared.append((path, group))
    for r in rows:
        ok, detail = label_belongs_to(r)
        if not ok:
            bad.append((r, detail))

    if shared:
        print(f"{len(shared)} label file(s) claimed by more than one sale:\n")
        for path, group in shared:
            print(f"  {Path(path).name}")
            for r in group:
                print(f"    [{r['code'] or '---'}] {(r['item'] or '?')[:40]:<42}"
                      f" {r['buyer'] or '?'}")
        print()
    if bad:
        print(f"{len(bad)} sale(s) whose label does not match the record:\n")
        for r, detail in bad:
            print(f"  [{r['code'] or '---'}] {(r['item'] or '?')[:40]}")
            print(f"      {detail}")
        print("\nRe-fetch these from the mailbox: mark them cancelled or "
              "delete the rows, then `mplabel check` with a wide enough "
              "--lookback to pick the emails up again.")
    if not shared and not bad:
        print(f"all {len(rows)} archived label(s) match their sale")


def cmd_stats(cfg, conn, args):
    listings_mod.refresh(conn)

    def table(title, sql, fmt):
        rows = conn.execute(sql).fetchall()
        if not rows:
            return
        print(f"\n{title}")
        for r in rows:
            print("  " + fmt(r))

    table("Sell-through by price band",
          "SELECT * FROM v_price_band ORDER BY avg_price",
          lambda r: f"{r['price_band']:<10} {r['sold'] or 0}/{r['listed']} sold"
                    f"  {r['sell_through_pct'] or 0}%"
                    f"  avg {r['avg_days_to_sell'] or '-'} days")
    table("Monthly",
          "SELECT * FROM v_monthly LIMIT 12",
          lambda r: f"{r['month']}  {r['orders']} orders  ${r['gross'] or 0:.2f}"
                    f"  avg ${r['avg_order'] or 0:.2f}")
    table("Sitting longest (active)",
          "SELECT * FROM v_aging LIMIT 10",
          lambda r: f"{(r['title'] or '?')[:34]:<36} ${r['price'] or 0:>7.2f}"
                    f"  {r['days_listed']}d  {r['inquiries']} inquiries")
    table("Fastest sellers",
          "SELECT title, price, days_to_sell FROM v_listing_perf "
          "WHERE days_to_sell IS NOT NULL ORDER BY days_to_sell LIMIT 10",
          lambda r: f"{(r['title'] or '?')[:34]:<36} ${r['price'] or 0:>7.2f}"
                    f"  sold in {r['days_to_sell']}d")
    print()


def cmd_ship(cfg, conn, args):
    _close_sale(conn, args.ref, "shipped")


def cmd_cancel(cfg, conn, args):
    """Mark a sale cancelled - the buyer pulled out.

    Not 'shipped': that would count it as revenue and leave it in the
    sold figures. A cancelled sale stops being outstanding and gives its
    parcel code back, and if the item sells again that is a new order
    alongside this one, not a replacement for it."""
    _close_sale(conn, args.ref, "cancelled")


def _close_sale(conn, ref, status):
    row = find_sale(conn, ref)
    if not row:
        raise SystemExit(f"no record matching {ref}")
    conn.execute("UPDATE sales SET status=? WHERE id=?", (status, row["id"]))
    conn.commit()
    print(f"{status}: {row['item'] or '?'}"
          + (f"  [{row['code']}]" if row["code"] else ""))


def main():
    try:
        _main()
    except printers.PrinterUnavailable as exc:
        # An Exception everywhere else, so the poll loop and the web app
        # can catch it - but at a terminal it should still just print the
        # message and exit non-zero rather than dump a traceback.
        raise SystemExit(str(exc))


def _main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="poll once, do not print")
    p = sub.add_parser("run", help="poll and print")
    p.add_argument("--loop", action="store_true")
    p = sub.add_parser("file", help="convert one PDF")
    p.add_argument("pdf")
    p.add_argument("-o", "--output")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270])
    p.add_argument("--print", dest="print_it", action="store_true")
    p.add_argument("--code", help="stamp this parcel code on the label, to "
                                  "check placement without printing")
    sub.add_parser("list", help="outstanding orders")
    ref_help = ("parcel code from the label, or listing id, order id or "
                "tracking number")
    p = sub.add_parser("reprint", help="print a label again")
    p.add_argument("ref", help=ref_help)
    p.add_argument("--force", action="store_true",
                   help="print even if the label does not match the record")
    sub.add_parser("verify", help="check archived labels against their sales")
    p = sub.add_parser("inventory",
                       help="CSV of inventory labels for the label maker")
    p.add_argument("-o", "--output", help="default inventory-labels.csv")
    p.add_argument("--state", default="active",
                   help="which listings (default: active)")
    p.add_argument("--all", action="store_true",
                   help="every listing, whatever its state")
    p = sub.add_parser("ship", help="mark as shipped")
    p.add_argument("ref", help=ref_help)
    p = sub.add_parser("cancel", help="the buyer pulled out; not a sale")
    p.add_argument("ref", help=ref_help)
    p = sub.add_parser("test-print", help="reprint the newest label")
    p.add_argument("--force", action="store_true",
                   help="print even if the label does not match the sale")
    p = sub.add_parser("probe", help="show printers and USB devices")
    p.add_argument("--remote", action="store_true",
                   help="ask the printd named by printd_url instead of "
                        "globbing this host's /dev")
    p.add_argument("--cups", action="store_true",
                   help="also run `lpinfo -v`. Off by default: CUPS's "
                        "discovery can claim the printer and unbind usblp, "
                        "which is the fault this command exists to find.")
    sub.add_parser("selftest", help="print a tiny text-only TSPL test label")
    p = sub.add_parser("inventory-label",
                       help="draw one inventory label and preview what "
                            "the label maker would burn")
    p.add_argument("--code", required=True,
                   help="the inventory code, and what the QR carries")
    p.add_argument("--title", help="item title, wrapped and ellipsed to fit")
    p.add_argument("--price", help="shown bottom right")
    p.add_argument("--qr", action="store_true",
                   help="add a QR of the same code, so a phone can read "
                        "the box without anyone squinting at four "
                        "characters on thermal paper")
    p.add_argument("--marker", action="store_true",
                   help="add the shelf marker instead of a QR. Same code, "
                        "far bigger modules - a QR version 1 holds 152 "
                        "bits where a code needs 20, and the difference "
                        "is paid for in module size. Read by the phone "
                        "app's scanner, and by nothing else")
    p.add_argument("--ecl", choices=["L", "M", "Q", "H"], default="M",
                   help="QR error correction (default %(default)s). A "
                        "code this short fits version 1 even at H")
    p.add_argument("--size", default="48x30", metavar="WxH",
                   help="label size the way you hold it, in mm (default "
                        "%(default)s). Suffix `in` for inches, so a 4x1in "
                        "shelf label is --size 4x1in. Anything wider than "
                        "the 48mm head is printed with its long axis down "
                        "the feed - the head does not turn")
    p.add_argument("--density", type=int,
                   default=supvan_mod.DEFAULT_DENSITY,
                   help="burn energy 0-15 (default %(default)s)")
    p.add_argument("--preview", metavar="PNG",
                   help="write what the payload decodes back to")
    p.add_argument("--scale", type=int, default=2,
                   help="preview magnification (default %(default)s)")
    p.add_argument("--print", action="store_true",
                   help="actually send it. Nothing built this way has "
                        "printed yet - see open work 1b")
    p.add_argument("--device", help="hidraw node, default supvan_device")

    p = sub.add_parser("supvan-probe",
                       help="status of the 48mm inventory label maker; "
                            "prints nothing and moves no paper")
    p.add_argument("--device", help="hidraw node, default supvan_device "
                                    "from the config (/dev/hidraw0)")
    p.add_argument("--deep", action="store_true",
                   help="also send the other read-only commands and show "
                        "their raw replies; still moves no paper")
    p = sub.add_parser("supvan-test-print",
                       help="EXPERIMENT: try to print a test pattern on the "
                            "label maker. This one does move paper")
    p.add_argument("--device", help="hidraw node, default supvan_device")
    p.add_argument("--dry-run", action="store_true",
                   help="build everything and show it, send nothing")
    p.add_argument("--replay", metavar="FILE",
                   help="send a pre-made LZMA stream from FILE instead of "
                        "generating one. Replaying bytes known to have "
                        "printed separates a wrong sequence from a stream "
                        "the firmware will not decode")
    # Splitting is the default because a whole-label stream does not
    # fit: 419 bytes in 7 reports printed, 695 in 11 was refused with
    # ink ruled out in between. 0 sends one buffer, which is how the
    # old single-stream behaviour is reproduced.
    p.add_argument("--max-buffer", type=int, default=0,
                   help="--bare-raster only, and superseded: split on "
                        "compressed size, chasing a per-buffer byte limit "
                        "that turned out not to exist. The real split is by "
                        "printhead line into 4096-byte buffers and is now "
                        "automatic. 0 disables it")
    p.add_argument("--bare-raster", action="store_true",
                   help="send a bare raster with no print-buffer header or "
                        "checksum - the shape every generated label was "
                        "refused in. Kept so the fix can be shown to be "
                        "the fix")
    p.add_argument("--density", type=int,
                   default=supvan_mod.DEFAULT_DENSITY,
                   help="burn energy 0-15 (default %(default)s, the "
                        "vendor's own default)")
    p.add_argument("--margin", type=int,
                   default=supvan_mod.DEFAULT_MARGIN_DOTS,
                   help="blank dots the firmware feeds at each end of the "
                        "label (default %(default)s). Declared in the "
                        "buffer header; these columns are not sent")
    p.add_argument("--clip", metavar="WxH",
                   help="blank any dot at or beyond W across or H "
                        "down, keeping the image the same size. Their "
                        "print fits 352x171")
    p.add_argument("--style", choices=["blocks", "sparse", "scatter", "ruler"],
                   default="blocks",
                   help="test pattern (default %(default)s). 'sparse' draws "
                        "the same landmarks in outline, 0.66%% ink "
                        "against 7.54%%. 'scatter' is a diagnostic, "
                        "not a picture: working-print ink at "
                        "failing-print size, to say which of the two "
                        "the device objects to. 'ruler' is the "
                        "calibration target - scales on both axes, the "
                        "last dot's number at each far end, and an inset "
                        "comb to measure a clipped edge. Use --width and "
                        "--height to ask about a particular label")
    p.add_argument("--reencode", metavar="FILE",
                   help="decode a captured LZMA stream and re-encode "
                        "the same image with our encoder. Holds the "
                        "picture still and varies only the encoder, "
                        "which --replay and a generated pattern "
                        "cannot separate")
    p.add_argument("--width", type=int,
                   default=supvan_mod.DEFAULT_WIDTH_DOTS,
                   help="dots per row (default %(default)s, 48mm at 203dpi)")
    # 256 rows. A captured USB print sends 12288 bytes = 48 x 256, and the
    # media is what decides it: a short image looks like a job the printer
    # is still waiting to finish.
    p.add_argument("--height", type=int, default=256,
                   help="rows in the test pattern (default %(default)s, "
                        "which matches the captured print)")
    p.add_argument("--invert", action="store_true",
                   help="flip the bit polarity")
    p.add_argument("--lzma", choices=["device", "alone", "xz", "raw"],
                   default="device",
                   help="LZMA container for --max-buffer 0 only; splitting always uses 'device'. 'device' is the "
                        "in-repo literals-only encoder, the only one that "
                        "emits a declared size with no end marker - which is "
                        "the only shape this firmware accepts")
    p.add_argument("--announce", choices=["compressed", "raw"],
                   default="compressed",
                   help="which length 0x5c carries (default %(default)s)")
    # 0x3c, straight off the wire. The captured buffer-full command is
    # `c0 40 00 7b 10 00 08 00 00 3c` - second value 60, where this sent 1.
    p.add_argument("--speed", type=int, default=None,
                   help="second value in 0x10. Derived from the compressed "
                        "size by default (supvan.calc_speed), which is what "
                        "the vendor does - the captured print's 60 is that "
                        "function's answer for a nearly blank label, not a "
                        "constant. Pass a value to hold it still")
    p.add_argument("--buffer-len", choices=["compressed", "raw"],
                   help="which length 0x10 carries; defaults to --announce")
    p.add_argument("--dict-size", type=int,
                   default=supvan_mod.LZMA_DICT_SIZE,
                   help="LZMA dictionary in bytes (default %(default)s). "
                        "Python's preset 9 asks for 64MB, which this device "
                        "cannot allocate")
    # On by default: a captured print from the vendor app declares the
    # size. A store_true flag defaulting to False silently overrode the
    # module default once already, and the run looked like a fair test of
    # the fix when it was not.
    p.add_argument("--no-declare-size", dest="declare_size",
                   action="store_false", default=True,
                   help="write 'unknown' as the uncompressed size instead "
                        "of the real one. The captured print declares it, "
                        "so this is the experiment, not the default")
    p.add_argument("--abort", action="store_true",
                   help="send stop-print and exit. Clears a device left in "
                        "its printing state by an attempt that stalled")
    p = sub.add_parser("scan", help="survey Facebook mail, change nothing")
    p.add_argument("--limit", type=int, default=2000)
    p = sub.add_parser("backfill", help="build listing history from old mail")
    p.add_argument("--limit", type=int)
    p.add_argument("--restart", action="store_true",
                   help="reprocess messages already seen")
    p = sub.add_parser("sheets", help="push everything to Google Sheets")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("import", help="import listings from a file")
    p.add_argument("path")
    p.add_argument("--format", choices=["dyi", "csv", "saved"], default="dyi")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--state", choices=["active", "sold", "expired", "removed"],
                   help="force the state for a capture taken from one tab, "
                        "e.g. --state sold for the Sold tab. Without it the "
                        "state comes from a badge in each card, which the "
                        "Sold tab may not repeat.")
    p = sub.add_parser("pending",
                       help="print labels that were recorded but never "
                            "printed (today only unless widened)")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="from this date instead of today")
    p.add_argument("--all", action="store_true",
                   help="every pending label, however old")
    p.add_argument("--dry-run", action="store_true",
                   help="list them without printing")
    sub.add_parser("stats", help="analytics summary in the terminal")
    p = sub.add_parser("serve", help="run the phone app")
    p.add_argument("--bind", help="default 127.0.0.1; the intended route "
                                  "in is a Cloudflare tunnel")
    p.add_argument("--port", type=int)
    sub.add_parser("passwd", help="hash a password for web_password_hash")
    sub.add_parser("status", help="ask the printer how it is (does it "
                                  "answer at all?)")
    p = sub.add_parser("reconcile",
                       help="ask printd what it printed and correct rows "
                            "that a timeout left looking unprinted")
    p.add_argument("--since", metavar="JOB", help="only jobs after this one")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("printd", help="run the print service")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if args.cmd == "probe":
        if args.remote:
            cfg = load_config(args.config)
            info = printers.printd_health(cfg)
            print(f"printd at {cfg.get('printd_url')}")
            for key in ("backend", "device", "device_present", "dpi",
                        "darkness", "speed", "media_tracking",
                        "gap_inches", "head_dots", "printing"):
                print(f"  {key:15} {info.get(key)}")
            print(f"  {'build':15} {info.get('build')}")
            return
        # Say whose USB bus this is. probe runs before load_config and
        # globs the local /dev, so on the order side of a split it would
        # describe a machine with no printer attached and look like a
        # fault.
        print(f"describing {socket.gethostname()} (local devices). "
              f"Use --remote for the printd host.\n")
        printers.probe(cups=args.cups)
        return

    cfg = load_config(args.config)

    # These touch the printer and the filesystem but never the database.
    # Keep them above connect_db: a missing or unwritable home directory
    # must not stop you testing the printer or converting a PDF by hand.
    if args.cmd == "selftest":
        # Dispatches on the backend: with printer_backend = pi-http this
        # asks printd to print it, rather than reaching past the service
        # to whatever device node exists on *this* host.
        info = printers.selftest(cfg)
        print(f"sent text-only {info['backend']} test label to "
              f"{info['where']}")
        return
    if args.cmd == "inventory-label":
        cmd_inventory_label(cfg, args)
        return

    if args.cmd == "supvan-probe":
        # Same reasoning as selftest: this is a hardware test, and a
        # hardware test must not need the database.
        cmd_supvan_probe(cfg, args)
        return
    if args.cmd == "supvan-test-print":
        cmd_supvan_test_print(cfg, args)
        return
    if args.cmd == "file":
        cmd_file(cfg, args)
        return
    if args.cmd == "passwd":
        cmd_passwd()
        return
    if args.cmd == "status":
        cmd_status(cfg)
        return
    if args.cmd == "printd":
        # No database: this half knows about paper, not orders.
        from . import printd as printd_mod
        printd_mod.serve(cfg, bind=args.bind, port=args.port)
        return

    conn = connect_db(cfg["home"])

    if args.cmd == "check":
        poll_once(cfg, conn, do_print=False)
    elif args.cmd == "run":
        do_print = truthy(cfg["auto_print"])
        if args.loop:
            loop(cfg, conn, do_print)
        else:
            poll_once(cfg, conn, do_print)
    elif args.cmd == "list":
        cmd_list(cfg, conn, args)
    elif args.cmd == "reprint":
        cmd_reprint(cfg, conn, args)
    elif args.cmd == "ship":
        cmd_ship(cfg, conn, args)
    elif args.cmd == "cancel":
        cmd_cancel(cfg, conn, args)
    elif args.cmd == "verify":
        cmd_verify(cfg, conn, args)
    elif args.cmd == "inventory":
        cmd_inventory(cfg, conn, args)
    elif args.cmd == "test-print":
        cmd_test_print(cfg, conn, args)
    elif args.cmd == "scan":
        backfill_mod.scan(cfg, args.limit)
    elif args.cmd == "backfill":
        backfill_mod.run(cfg, conn, args.limit, resume=not args.restart)
    elif args.cmd == "sheets":
        sync_sheets(cfg, conn, dry_run=args.dry_run)
    elif args.cmd == "serve":
        # Imported here rather than at the top: web imports cli back, and
        # a module-level import either way is a cycle.
        from . import web as web_mod
        web_mod.serve(cfg, bind=args.bind, port=args.port)
    elif args.cmd == "import":
        listings_mod.refresh(conn)
        if args.format == "saved":
            n, stats = savedpage_mod.import_saved(conn, args.path,
                                                  verbose=not args.quiet,
                                                  state=args.state)
            print(f"imported {n} listing(s) "
                  f"({stats['json_blocks']} JSON blocks scanned)")
        elif args.format == "dyi":
            n, examined = listings_mod.import_dyi(conn, args.path)
            print(f"imported {n} listing(s) from {examined} marketplace file(s)")
            if examined == 0:
                print("No marketplace files found in that export. Meta only "
                      "includes what you tick when requesting the download - "
                      "re-request it with the Marketplace section selected.")
        else:
            print(f"imported {listings_mod.import_csv(conn, args.path)} row(s)")
        listings_mod.refresh(conn)
    elif args.cmd == "pending":
        cmd_pending(cfg, conn, args)
    elif args.cmd == "stats":
        cmd_stats(cfg, conn, args)
    elif args.cmd == "reconcile":
        cmd_reconcile(cfg, conn, args)
