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
import json
import logging
import os
import random
import re
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.parse
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
# Safe at module level: ebay.py imports nothing from this package,
# unlike `web`, which imports cli back and has to be imported inside
# main() to avoid the cycle.
from . import ebay as ebay_mod
from . import goodwill as goodwill_mod
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
    # Push. Nothing here has a default that could accidentally work: an
    # unconfigured install must refuse to notify rather than half-try.
    # apns_topic is the app's bundle id, and the key is the .p8 from the
    # developer account - see docs/notifications.md.
    "apns_key_path": "",
    "apns_key_id": "",
    "apns_team_id": "",
    "apns_topic": "com.marchvector.Sellomatic",
    "apns_environment": "production",
    "supvan_device": "/dev/hidraw0",
    # The label maker's own backend, separate from printer_backend:
    # two devices, and one may be local while the other is not.
    "tag_backend": "supvan",
    # The roll in the machine, on the machine that has it.
    "supvan_label_mm": "48x30",
    "supvan_density": "4",
    # The tag sequence polls the device between every step, and the
    # deadline header only bounds *getting* the device - so the
    # socket has to outlast the print, not just the queue.
    "printd_tag_timeout": "90",
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
    # Where `mplabel send` posts a label from another machine. The same
    # address the phone app uses - a tunnel hostname, or http://pi:8080
    # on the LAN. Empty on the Pi itself, which has no reason to send
    # anything to itself.
    "web_url": "",
    # yes | no | auto. auto sets the cookie's Secure flag when the request
    # arrived over HTTPS, which behind cloudflared means trusting
    # X-Forwarded-Proto. Forcing yes on a plain-HTTP LAN test makes the
    # browser drop the cookie and the login fails with no visible reason.
    "web_secure_cookie": "auto",
    # Send a browser to the front end built for it when it asks for `/`:
    # a phone gets the PWA, a laptop gets the desk portal. `?ui=phone` or
    # `?ui=desk` overrides it and is remembered, which is what the link
    # each one carries to the other actually does. Set to no to serve the
    # phone app at `/` to everybody, as it was before.
    "web_auto_route": "yes",
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
    # eBay, the other selling channel. Every value is empty by default
    # and `mplabel ebay` exits 78 rather than half-trying, like notify.
    #
    # sandbox or production. It picks the host *and* which credentials
    # are meant, because the two keysets are different strings that look
    # alike, and sending a sandbox key to production is a 401 that says
    # nothing about environments.
    "ebay_environment": "sandbox",
    # From the developer portal's Application Keys page, per environment.
    # eBay calls the first two "Client ID" and "Client Secret" on some
    # pages and App ID / Cert ID on others; they are the same values.
    "ebay_app_id": "",
    "ebay_cert_id": "",
    # The RuName, not a URL - eBay resolves it to the redirect configured
    # against the keyset, and sending the URL itself is rejected.
    "ebay_ru_name": "",
    "ebay_marketplace": "EBAY_US",
    # Needed to *publish* an offer, not to draft one. Set up in My eBay
    # and the Account API; see docs/ebay.md.
    "ebay_merchant_location": "",
    "ebay_fulfillment_policy": "",
    "ebay_payment_policy": "",
    "ebay_return_policy": "",
    # Ours, not eBay's: 32-80 characters that we invent and eBay echoes
    # back in the account-deletion challenge. See web.py.
    "ebay_verification_token": "",
    # The public HTTPS URL eBay sends account-deletion notices to. It has
    # to be the exact string eBay is configured with - the challenge hash
    # covers it, so a trailing slash difference fails the handshake with
    # no explanation.
    "ebay_notification_endpoint": "",
    # The outside of the tunnel, without a path: eBay fetches listing
    # photographs from here, because the Sell REST APIs have no image
    # upload and `imageUrls` must be a public https URL. Only photos
    # attached to a listing already pushed to eBay are served; see
    # `web.h_ebay_photo`.
    "ebay_photo_base": "",
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
    notes        TEXT,
    -- What the postage cost, in dollars like `price`. NULL means nobody
    -- knows, which is the honest state for almost every row: the label
    -- email is a *prepaid* label and does not carry a charge.
    postage      REAL,
    -- 'confirmed' if a person typed it, 'estimated' if it was derived
    -- from other confirmed rows. Never inferred from the presence of a
    -- number: the two are indistinguishable once written down, and it is
    -- the estimate that must not be able to pass for a fact.
    postage_source TEXT
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
# `decl` is pasted into `ALTER TABLE ... ADD COLUMN`, so it carries the
# REFERENCES clause too - and it has to.
#
# A column added as plain TEXT gets **no foreign key at all**, while the
# same column in SCHEMA gets one, so a migrated database and a fresh one
# quietly end up with different constraints. `bin_code` shipped that way
# and the tests missed it, because the fixture builds from SCHEMA.
# `test_a_migrated_database_has_the_same_foreign_keys` is the guard.
#
# Note this only helps a database that has not migrated yet: SQLite
# cannot add a constraint to a column that already exists without
# rebuilding the table.
MIGRATIONS = [
    ("sales", "code", "TEXT"),
    ("listings", "inventory_code", "TEXT"),
    ("listings", "bin_code",
     "TEXT REFERENCES bins(code) ON DELETE SET NULL"),
    ("listings", "paid", "REAL"),
    ("listings", "trip_id",
     "INTEGER REFERENCES trips(id) ON DELETE SET NULL"),
    ("listings", "era", "TEXT"),
    # What the postage actually cost, and whether that figure was
    # measured or guessed. Two columns rather than one because an
    # estimate silently hardening into a fact is the whole trap here -
    # see the note in `listings.estimate_postage`.
    ("sales", "postage", "REAL"),
    ("sales", "postage_source", "TEXT"),
    # The listing copy, for the desk's writer screen. Separate from
    # `notes` on purpose - see the comment on the column in
    # listings.SCHEMA.
    ("listings", "description", "TEXT"),
    # Postage on a listing, mirroring the pair on `sales`. A spreadsheet
    # of old sales is the only place this has ever been recorded for
    # something that never came through the mailbox.
    ("listings", "postage", "REAL"),
    ("listings", "postage_source", "TEXT"),
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
        # Off by default, per connection, so a declared REFERENCES is
        # documentation until this runs. Safe to switch on: nothing in
        # this schema declared one before `bins`, so it constrains only
        # the relation that actually has a key. sales -> listings stays
        # matched on title in `link_sales`, because the source data has
        # no shared id to key on - see the note there.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        # listings owns its own tables, but the poll loop writes mail_events
        # as it goes, so they have to exist from the start. Both scripts are
        # CREATE TABLE IF NOT EXISTS.
        conn.executescript(listings_mod.SCHEMA)
        # The in-store half: candidates and the receipt read as lines.
        # Declared here with the rest so a fresh database and a migrated
        # one agree, and so nothing has to remember to create them.
        from . import shopping as shopping_mod

        conn.executescript(shopping_mod.SCHEMA)
        # Where a listing is posted on eBay. After listings', because it
        # references that table - and declared with the rest so a fresh
        # database and a migrated one agree about the constraint.
        conn.executescript(ebay_mod.SCHEMA)
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

        # Only now: an index on a migrated column has to wait for the
        # column. In SCHEMA it would run first and fail on every existing
        # database, taking every command down with it.
        for stmt in listings_mod.POST_MIGRATION_INDEXES:
            conn.execute(stmt)

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


# Every sender the poller cares about. Facebook mail is what she sold;
# ShopGoodwill mail is what she bought, and it is the only sourcing
# event that arrives as a document rather than as a receipt in a bag.
MAIL_DOMAINS = tuple(mailparse.SENDER_DOMAINS) + tuple(goodwill_mod.SENDER_DOMAINS)


def imap_or_from(domains):
    """`OR` over a FROM per domain, as plain IMAP wants it.

    IMAP's OR is binary and prefix, so three terms is `OR (OR a b) c`
    rather than a list - and getting that wrong is not a soft failure:
    the server rejects the whole SEARCH and `candidate_ids` falls
    through to the UNSEEN query, which is the one that hid eight
    labels."""
    terms = [f'(FROM "{d}")' for d in domains]
    expr = terms[0]
    for term in terms[1:]:
        expr = f"(OR {expr} {term})"
    return expr


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
    doms = " OR ".join(MAIL_DOMAINS)
    queries = []
    if "gmail" in host:
        queries.append(f'(X-GM-RAW "from:({doms}) newer_than:{days}d")')
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    queries.append(f'({imap_or_from(MAIL_DOMAINS)} SINCE {since})')
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


def print_label(cfg, pdf_path, code=None, job=None):
    """Print one 4x6 PDF, whatever it is a label for.

    `job` names the print for printd's journal. Left alone it is random
    per call, which is right for a label off a sale: the parcel code is
    already the handle and a reprint is a deliberate second label. A
    caller with a *stable* id - the digest of an uploaded PDF, say - can
    pass it here, and printd then answers a retry with 409 instead of
    printing again. It reaches only the backends that keep a journal;
    writing straight to a device records nothing either way."""
    backend = cfg["printer_backend"]
    kwargs = printers.backend_kwargs(cfg, backend, code=code)
    if job and backend in printers.REMOTE_BACKENDS:
        kwargs["job"] = job

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


def record_purchase(conn, msg):
    """Note a ShopGoodwill auction mail: what she bought, and what it cost.

    The mirror of `record_event`. That one reconciles mail about things
    she is selling; this one is the only automatic route cost basis has
    into the database - `listings.paid` had a schema and a phone screen
    for months and nothing that filled it, which is why every margin in
    the analytics was null.

    Returns 1 if an order was recorded, else 0."""
    try:
        result = goodwill_mod.import_mail(conn, msg)
    except Exception:
        log.exception("could not record ShopGoodwill mail")
        return 0
    if not result:
        return 0
    log.info("ShopGoodwill %s: %d item(s)%s",
             result["kind"], len(result["listing_ids"]),
             f", ${result['total']:.2f}" if result.get("total") else "")
    return 1


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

        handled = noted = skipped = bought = 0
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
                # A ShopGoodwill mail is not Facebook mail, so
                # `record_event` refuses it on the sender check and it
                # would otherwise fall out of the poll unrecorded.
                bought += record_purchase(conn, msg)
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
        if bought:
            log.info("%d ShopGoodwill order(s) recorded", bought)
        if noted or bought:
            # Refresh after the purchases as well as the sales: an
            # auction item she has already listed is reconciled by title
            # in `link_sales`, so its cost only reaches the sale it
            # belongs to once the rebuild has run.
            listings_mod.refresh(conn)
        if (handled or noted or bought) and truthy(cfg.get("sheets_after_poll")) \
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
    info = label.to_4x6(src, out, page_index=getattr(args, "page", 1) - 1,
                        force_rotation=args.rotate,
                        region=getattr(args, "region", None))
    if getattr(args, "code", None):
        # Handy for checking placement on a real label without printing.
        label.stamp_code(out, out, args.code,
                         size=float(cfg.get("label_code_size", 8)))
    print(f"{out}  {info['size_in'][0]} x {info['size_in'][1]} in  "
          f"(rotated {info['rotation']}, from {info['rotation_source']})")
    if info["regions_found"] > 1:
        # Said out loud: the page held more than one thing that could
        # have been the label, and which one this is matters.
        print(f"  page held {info['regions_found']} candidate blocks; "
              f"this is region {info['region']}")
    for k, v in label.extract_label_fields(out).items():
        print(f"  {k}: {v}")
    if args.print_it:
        print_label(cfg, out)


def token_path():
    """Where `send` keeps its bearer token.

    Not in mplabel.conf. This command runs on a workstation rather than
    on the Pi, `config` is a file that gets pasted into a chat window
    when something is wrong, and a session token is a credential with
    nothing else in that file's class. It is also disposable: delete it
    and the next send logs in again."""
    return Path.home() / ".config" / "mplabel.token"


def _api(url, path, token=None, data=None, ctype=None, timeout=120):
    """One request to the phone app's server, as the phone makes it.

    urllib rather than requests, like every other HTTP call in this
    project. Returns (status, parsed-or-raw)."""
    import urllib.error
    import urllib.request

    headers = {"X-Mplabel": "1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url.rstrip("/") + path, data=data,
                                 headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return exc.code, body
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"could not reach {url}: {exc.reason}. The label did not print.")


def _login(url):
    """Swap the password for a token and keep the token.

    The password is asked for interactively unless the environment
    carries one, because the alternative is a password in shell history
    on a machine that is not the Pi."""
    import getpass

    password = os.environ.get("MPLABEL_WEB_PASSWORD")
    if not password:
        password = getpass.getpass(f"password for {url}: ")
    status, body = _api(url, "/api/login",
                        data=json.dumps({"password": password}).encode(),
                        ctype="application/json", timeout=30)
    if status != 200 or not isinstance(body, dict) or not body.get("token"):
        detail = body.get("error") if isinstance(body, dict) else body
        raise SystemExit(f"login failed: {detail}")
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body["token"], encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Windows, where the mode is not the mechanism. Not worth failing
        # a print over; the file is under the user's own profile either way.
        pass
    return body["token"]


def cmd_send(cfg, args):
    """Send one PDF to the Pi to be printed, from anywhere.

    The label is converted *there*, not here: `to_4x6` needs pdfplumber
    and pypdf, the print needs the roll and the darkness and the gap
    distance, and every one of those is a fact about the machine with the
    printer attached. What crosses is the PDF and what to do with it -
    the same rule that keeps the raster off the wire in `print_tag`.

    Nothing is recorded for this label beyond printd's journal. It is not
    a sale, and it does not become one."""
    url = args.url or cfg.get("web_url") or ""
    if not url:
        raise SystemExit(
            "no server to send to - pass --url https://... or set web_url "
            "in mplabel.conf. That is the same address the phone app uses.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    pdf = Path(args.pdf)
    if not pdf.is_file():
        raise SystemExit(f"no such file: {pdf}")
    body = pdf.read_bytes()
    if not body.startswith(b"%PDF"):
        raise SystemExit(f"{pdf} is not a PDF")

    query = []
    if args.rotate is not None:
        query.append(f"rotate={args.rotate}")
    if args.page != 1:
        query.append(f"page={args.page}")
    if args.region:
        query.append(f"region={args.region}")
    if args.dry_run:
        query.append("dry_run=1")
    if args.force:
        query.append("force=1")
    path = "/api/v1/print/label" + ("?" + "&".join(query) if query else "")

    token = None
    try:
        token = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not token:
        token = _login(url)

    status, reply = _api(url, path, token=token, data=body,
                         ctype="application/pdf")
    if status == 401:
        # Expired, or the password changed - which invalidates every
        # token, by design. One retry, then give up rather than loop.
        token = _login(url)
        status, reply = _api(url, path, token=token, data=body,
                             ctype="application/pdf")

    if status != 200:
        detail = reply.get("error") if isinstance(reply, dict) else reply
        raise SystemExit(f"{url} said {status}: {detail}")

    info = (reply or {}).get("label") or {}
    w, h = info.get("size_in", ("?", "?"))
    print(f"{pdf.name} -> {w} x {h} in, rotated {info.get('rotation')} "
          f"({info.get('rotation_source')})")
    if info.get("regions_found", 1) > 1:
        print(f"  page held {info['regions_found']} candidate blocks; "
              f"printed region {info['region']} (--region to choose)")
    if info.get("dry_run"):
        print("  dry run - nothing printed")
    else:
        print(f"  printed. job {info.get('job')}, recorded in "
              f"{info.get('recorded')}")


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
    if args.qr and args.marker:
        raise SystemExit("--qr and --marker both want the same square; "
                         "pick one")
    spec = _tag_spec(args, "inventory-label")
    carrier = ("  (+ QR carrying the same code)" if args.qr else
               "  (+ shelf marker carrying the same code)" if args.marker
               else "")
    print(f"code   : {args.code}{carrier}")
    return _emit_tag(cfg, args, spec)



def cmd_shelf_tag(cfg, args):
    """Draw one shelf tag: a code for a *place*, not a thing.

    Three characters where an item code is four, which is what lets a
    scanner tell "put things here" from "this is a thing" - the marker's
    payload already carries a format bit for 3-char codes against 4-char
    ones, so nothing new goes on the wire.

    Above `connect_db` with the other printer commands: printing a tag
    for a shelf needs no database, and the labels want making before
    there is anything to record against them."""
    if args.qr and args.marker:
        raise SystemExit("--qr and --marker both want the same space; "
                         "pick one")
    spec = _tag_spec(args, "shelf-tag")
    carrier = ("  (+ QR carrying the same code)" if args.qr else
               "  (+ shelf marker carrying the same code)" if args.marker
               else "")
    print(f"shelf  : {args.code.upper()}{carrier}")
    if args.name:
        print(f"name   : {args.name}")
    return _emit_tag(cfg, args, spec)



def cmd_bin(conn, cfg, args):
    """Make, list and fill the places things live.

    Needs the database, so unlike `shelf-tag` it sits below connect_db.
    That is the split: `shelf-tag` draws a tag for a code you already
    have, and this is where the code comes from.

    `new --print` does both in one go, because a bin whose tag never got
    printed is a code nobody can read off the shelf."""
    action = args.action
    if action == "new":
        row = listings_mod.create_bin(conn, args.name, notes=args.notes)
        print(f"bin    : {row['name']}")
        print(f"code   : {row['code']}")
        if args.print or args.preview:
            # The tag carries the code, and the name under it: the code
            # is what a scanner reads and the name is what she does.
            spec = {"kind": "shelf-tag", "code": row["code"],
                    "name": row["name"], "qr": bool(args.qr),
                    "marker": not args.qr, "ecl": "M"}
            _emit_tag(cfg, args, spec)
        return

    if action == "ls":
        rows = listings_mod.bins_in_use(conn, include_sold=args.all)
        if not rows:
            print("no bins yet - `mplabel bin new \"ATTIC\"` makes one")
            return
        for r in rows:
            print(f"{r['code']}  {r['name']:<24} {r['count']:>4} item"
                  f"{'' if r['count'] == 1 else 's'}")
        return

    if action == "show":
        found = listings_mod.bin_contents(conn, args.bin,
                                          include_sold=args.all)
        print(f"{found['bin']['code']}  {found['bin']['name']}")
        for item in found["items"]:
            print(f"  {item['inventory_code'] or '----'}  "
                  f"{(item['title'] or '')[:56]}")
        if not found["items"]:
            print("  (empty)")
        return

    if action == "rename":
        row = listings_mod.rename_bin(
            conn, (listings_mod.find_bin(conn, args.bin) or {}).get("code"),
            args.name)
        # The code deliberately does not move, so the tag already stuck
        # to the shelf is still right - say so, because the obvious
        # worry after a rename is whether it needs reprinting.
        print(f"{row['code']}  {row['name']}  (code unchanged - the tag on "
              f"the shelf is still correct)")
        return

    if action == "put":
        row = conn.execute(
            "SELECT id, title FROM listings WHERE UPPER(inventory_code)=? "
            "OR id=?", (str(args.item).upper(), _as_int(args.item))).fetchone()
        if row is None:
            raise SystemExit(f"no item {args.item!r} - that is an inventory "
                             f"code off the label, or a listing id")
        code = listings_mod.set_bin(conn, row["id"], args.bin)
        where = listings_mod.find_bin(conn, code) if code else None
        print(f"{row['title'][:56]} -> "
              + (f"{where['name']} ({where['code']})" if where
                 else "no bin"))
        return

    if action == "rm":
        row = listings_mod.delete_bin(conn, args.bin)
        # Not a cascade: the things come back out onto no shelf rather
        # than disappearing with the bin.
        print(f"removed {row['name']} ({row['code']}); anything in it is "
              f"now in no bin")
        return


def _as_int(value):
    """`put` takes an inventory code or a row id and does not ask which."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _tag_spec(args, kind):
    """The spec both tag commands send. What to put on the label, only.

    Deliberately carries no geometry unless the operator typed some:
    `--size` and `--density` default to None so that "the user asked for
    this" is distinguishable from "the default fired". Unset means the
    host with the roll decides, which is the whole point - it is the one
    that can see the paper."""
    spec = {"kind": kind, "code": args.code,
            "qr": bool(args.qr), "marker": bool(args.marker),
            "ecl": args.ecl}
    if kind == "shelf-tag":
        spec["name"] = args.name
    else:
        spec["title"] = args.title
        price = args.price
        if price is not None:
            try:
                price = float(price)
            except ValueError:
                pass
        spec["price"] = price
    if getattr(args, "size", None):
        try:
            spec["size_mm"] = list(printers.parse_label_size(args.size))
        except ValueError as exc:
            raise SystemExit(str(exc))
    if getattr(args, "density", None) is not None:
        spec["density"] = int(args.density)
    return spec


def _emit_tag(cfg, args, spec):
    """Dispatch a tag spec and report what came back.

    The output is the same whether the label came out of this machine or
    one on the other side of the house, because both ends answer in the
    same schema and this only renders it. That is the ergonomic
    requirement: nobody should have to learn a second set of words for
    the remote case."""
    backend = cfg.get("tag_backend") or "supvan"
    dry = not args.print
    kwargs = printers.tag_backend_kwargs(cfg, backend)
    if backend == "supvan" and getattr(args, "device", None):
        kwargs["device"] = args.device

    try:
        result = printers.print_tag(spec, backend, dry_run=dry, **kwargs)
    except (ValueError, printers.PrinterUnavailable) as exc:
        raise SystemExit(str(exc))

    label, payload = result.get("label") or {}, result.get("payload") or {}
    if backend != "supvan":
        print(f"printd : {cfg.get('printd_url')}")
    if label:
        print(f"label  : {label['mm'][0]:g} x {label['mm'][1]:g}mm"
              + (" - printed sideways, long axis down the feed"
                 if label.get("sideways") else ""))
        wide, tall = label["dots"]
        media_wide = label["media_box"][2] - label["media_box"][0] + 1
        # The raster is head-width because the *bar* is; a 1in label
        # covers 203 of those dots and the rest is head hanging off the
        # edge, which in a preview reads as a badly laid out label and is
        # nothing of the sort.
        print(f"raster : {wide} x {tall} dots"
              + (f", of which {media_wide} across is media"
                 if label.get("sideways") else ""))
        print(f"         {label['ink_pct']:.2f}% ink")
    if payload:
        print(f"job    : {payload['buffer_count']} x "
              f"{supvan_mod.PRINT_BUF_SIZE} = {payload['raw_len']} bytes, "
              f"{payload['compressed_len']} compressed, "
              f"speed {payload['speed']} (derived), "
              f"density {payload['density']}")
        # Millimetres, because that is the number that has to match the
        # paper. A tag longer than the die-cut label prints straight
        # across the gap onto the next one, and the first anybody knows
        # is a label with a third of the design on it.
        print(f"feed   : {label['feed_mm']:.1f}mm down the feed - the "
              f"die-cut label has to be at least this long")
        print(f"decoded: {payload['decoded_columns']} printhead lines, "
              f"{payload['decoded_stride']} bytes each, every checksum valid")

    if args.preview and result.get("compressed_b64"):
        import base64
        back, back_stride, cols = supvan_mod.decode_job(
            base64.b64decode(result["compressed_b64"]))
        img = inventory_mod.to_image(back, back_stride, cols,
                                     scale=args.scale)
        # Cropped with the box the *renderer* reported, never this
        # host's own PRINTABLE_* - on the remote path those describe a
        # roll this machine cannot see, and cropping a server-rendered
        # raster with client-side constants is the precise class of bug
        # the split exists to prevent.
        mx0, _my0, mx1, _my1 = label["media_box"]
        img = img.crop((mx0 * args.scale, 0,
                        (mx1 + 1) * args.scale, img.height))
        if label.get("sideways"):
            img = img.rotate(-90, expand=True)
        img.save(args.preview)
        print(f"\nwrote {args.preview} - the payload, decoded back"
              + (", turned the way you hold it"
                 if label.get("sideways") else ""))

    if dry:
        print("\nnothing sent, no paper moved. Add --print to try it.")
        return

    for entry in result.get("trace") or []:
        flags = ", ".join(entry.get("flags") or []) or "no flags"
        print(f"  . {entry['step']}: {flags}")
    final = result.get("final") or {}
    if result.get("printed"):
        print(f"\npages printed now reads {final.get('pages_printed')}.")
    else:
        print("\nit did not report a finished print.")


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
    if args.style == "edges":
        if clip or args.invert:
            raise SystemExit(
                "--style edges is a measurement, so --clip and --invert "
                "are not allowed with it")
        from . import inventory as inventory_mod
        raw, stride, rows = inventory_mod.render_edge_test(args.width,
                                                            args.height)
    elif args.style == "ruler":
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
        # Only 4x6 parcel labels. A shelf tag's job id is the same shape
        # - {code}-{hex} - and a 3-character location code could match a
        # parcel code by coincidence, which would mark a live parcel
        # printed because a shelf tag came out. Checked structurally on
        # `kind`, not by prefixing job ids. Rows written before the field
        # existed have no `kind` and are parcel labels by definition.
        if row.get("kind", "pdf") != "pdf":
            continue
        # And only rows that actually printed. A stall is journaled so a
        # retry of the same id is refused, but paper moving is not the
        # same as a label coming out.
        if row.get("outcome", "printed") != "printed":
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
    assuming either way.

    Refuses on a host whose backend is remote, rather than querying the
    local device name that `DEFAULTS` always supplies. Without this it
    reads a node that is not there and prints the "answered no, printing
    stays at-least-once" finding - a finding-shaped answer, about the
    highest-value open experiment in the project, delivered by a machine
    with no printer attached."""
    if cfg.get("printer_backend") in printers.REMOTE_BACKENDS:
        raise SystemExit(
            f"printer_backend is {cfg['printer_backend']!r}, so the printer "
            f"is on another host and there is nothing here to ask.\nRun this "
            f"on the printd host, or `mplabel probe --remote` from here.")
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


# Anything whose value must not be echoed to a terminal or a paste.
# `mplabel config` is the command you run *and paste* when something is
# broken, which is exactly when a credential leaks.
#
# `ebay_cert_id` is the OAuth client secret under one of the two names
# eBay gives it, and `ebay_verification_token` is what proves an
# account-deletion notice came from eBay rather than from anyone who
# found the URL. The app id and the RuName are deliberately not here:
# both are public halves and seeing them is how you check the right
# keyset is loaded.
SECRET_KEYS = ("imap_password", "printd_secret", "web_password_hash",
               "sheets_key", "sheets_key_json",
               "ebay_cert_id", "ebay_verification_token")


def config_sources(path=None):
    """Where every resolved config value actually came from.

    Returns (rows, config_path). Each row is (key, value, origin) with
    origin one of `default`, `file`, `env`.

    Written because three things decide a value and none of them is
    visible from the others: `DEFAULTS`, the *first* config file that
    exists - the search stops there, so `~/.config/mplabel.conf` is never
    read while `/etc/mplabel.conf` is present - and `MPLABEL_*`, which
    wins, is invisible in the file, and comes from an `EnvironmentFile`
    nobody remembers. This is the only way to answer "which value is in
    force" without reading three places and guessing."""
    defaults = dict(DEFAULTS)
    from_file, used = {}, None
    candidates = [Path(path)] if path else [
        Path("/etc/mplabel.conf"),
        Path.home() / ".config" / "mplabel.conf",
    ]
    for cand in candidates:
        if cand and cand.exists():
            parser = configparser.ConfigParser()
            parser.read(cand)
            if parser.has_section("mplabel"):
                from_file = dict(parser["mplabel"])
            used = cand
            break

    rows = []
    for key in sorted(set(defaults) | set(from_file)):
        env = os.environ.get("MPLABEL_" + key.upper())
        if env:
            rows.append((key, env, "env"))
        elif key in from_file:
            rows.append((key, from_file[key], "file"))
        else:
            rows.append((key, defaults[key], "default"))
    return rows, used


def cmd_config(args):
    """Show the resolved config, and where each value came from.

    Above `connect_db` with the printer commands: the reason to run this
    is usually that something is not working, and needing a writable data
    directory to find out which config file is in force would be exactly
    the wrong dependency."""
    rows, used = config_sources(args.config)

    print(f"host   {socket.gethostname()}")
    print(f"file   {used or 'none found - every value is a built-in default'}")
    if used and not args.config:
        # The search stops at the first hit, so say what was skipped.
        skipped = [c for c in (Path("/etc/mplabel.conf"),
                               Path.home() / ".config" / "mplabel.conf")
                   if c != used and c.exists()]
        for other in skipped:
            print(f"       (ignoring {other} - the search stops at the "
                  f"first file that exists, values do not merge)")
    print()

    width = max(len(k) for k, _v, _o in rows)
    for key, value, origin in rows:
        if not args.all and origin == "default":
            continue
        shown = "<set>" if (value and key in SECRET_KEYS) else value
        print(f"  {key:<{width}}  {shown or '':<28}  {origin}")

    # An inline comment is not a comment. `configparser` keeps it, so
    # `apns_environment = sandbox ; production later` is a value that is
    # not "sandbox" - and that one chose the wrong APNs host and produced
    # a refusal that named nothing. Flagged for every key, because the
    # next one will be somewhere else.
    suspect = [(key, value) for key, value, origin in rows
               if origin == "file" and value
               and re.search(r"\s[;#]", str(value))]
    if suspect:
        print("\nThese values contain what looks like an inline comment,"
              " and configparser keeps it:", file=sys.stderr)
        for key, value in suspect:
            head = str(value).split(None, 1)[0]
            print(f"  {key} = {value!r}\n    -> put the comment on its own"
                  f" line, or this stays {head!r} plus the rest",
                  file=sys.stderr)

    if not args.all:
        n = sum(1 for _k, _v, o in rows if o == "default")
        print(f"\n{n} more at their built-in default; --all shows them.")

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


def cmd_notify(cfg, conn, args):
    """Send what is worth sending, once each.

    `--dry-run` is the one to reach for first: it goes through the whole
    decision - what is due, what never printed, what money is loose - and
    prints it without touching Apple or the `notices` table, so running
    it twice says the same thing."""
    from . import notify as notify_mod

    recorded = []

    def dry(token, payload):
        alert = payload["aps"]["alert"]
        recorded.append((alert["title"], alert["body"]))
        return True, "dry run"

    if getattr(args, "check", False):
        # Separates "our request is wrong" from "Apple is unhappy", which
        # a refusal cannot do on its own: APNs answers an unclassifiable
        # request with InternalServerError and says no more.
        import base64 as _b64
        import shutil as _shutil
        import subprocess as _subprocess

        conn.executescript(notify_mod.SCHEMA)
        conn.commit()
        print(f"host        : {notify_mod._host(cfg)}")
        print(f"topic       : {cfg.get('apns_topic') or '(unset)'}")
        # As the sender resolves it, not as it was typed: the two
        # differed, and the difference chose the wrong host.
        resolved = notify_mod.environment(cfg)
        typed = str(cfg.get("apns_environment") or "production")
        print(f"environment : {resolved}"
              + (f"   (config says {typed!r})" if typed.strip() != resolved
                 else ""))

        key_path = cfg.get("apns_key_path") or ""
        key_id = cfg.get("apns_key_id") or ""
        if key_path and os.path.exists(key_path):
            mode = oct(os.stat(key_path).st_mode & 0o777)
            print(f"key         : {key_path} ({mode})")
            # Apple names both kinds of key `AuthKey_<KEYID>.p8`, so the
            # id is in the filename and can be checked against the
            # configured one. A mismatch is usually two keys on the
            # machine and the wrong id typed.
            named = re.search(r"AuthKey_([A-Z0-9]{10})\.p8$",
                              os.path.basename(key_path))
            if named and key_id and named.group(1) != key_id:
                print(f"              ^ the filename says "
                      f"{named.group(1)} and apns_key_id says {key_id}",
                      file=sys.stderr)
            head = ""
            try:
                with open(key_path) as fh:
                    head = fh.readline().strip()
            except OSError:
                pass
            if "PRIVATE KEY" not in head:
                print(f"              ^ does not look like a .p8 key "
                      f"(first line: {head[:40]!r})", file=sys.stderr)
            print("              note an App Store Connect API key is "
                  "also AuthKey_*.p8 and looks identical - APNs needs "
                  "the one made under Keys with push enabled")
        else:
            print(f"key         : {key_path or '(unset)'} - NOT FOUND")

        curl = _shutil.which("curl")
        print(f"curl        : {curl or 'MISSING'}")
        if curl:
            version = _subprocess.run([curl, "--version"],
                                      capture_output=True)
            has_h2 = b"HTTP2" in version.stdout or b"nghttp2" in version.stdout
            print(f"              HTTP/2 support: "
                  + ("yes" if has_h2 else "NO - APNs requires it"))
        print(f"openssl     : {_shutil.which('openssl') or 'MISSING'}")

        try:
            token = notify_mod.provider_token(cfg, now=time.time())
        except notify_mod.NotifyError as exc:
            print(f"jwt         : cannot be signed - {exc}", file=sys.stderr)
            return 78
        head, payload, signature = token.split(".")

        def unpad(part):
            return _b64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

        print(f"jwt header  : {unpad(head).decode()}")
        print(f"jwt payload : {unpad(payload).decode()}")
        # 64 bytes is the whole of an ES256 signature; anything else
        # means the DER unpack is wrong and Apple will refuse it without
        # explaining which part it disliked.
        print(f"jwt sig     : {len(unpad(signature))} bytes "
              + ("(correct for ES256)" if len(unpad(signature)) == 64
                 else "- WRONG, ES256 is 64"))

        devices = notify_mod.devices(conn)
        print(f"devices     : {len(devices)}")
        for device in devices:
            print(f"              {device['token'][:8]}... "
                  f"({device['environment']}, {len(device['token'])} chars)")
            if len(device["token"]) != 64:
                print("              ^ an APNs token is 64 hex characters",
                      file=sys.stderr)
            if device["environment"] != notify_mod.environment(cfg):
                print("              ^ registered against a different "
                      "environment than apns_environment", file=sys.stderr)
        return 0

    if getattr(args, "test", False):
        # The equivalent of `selftest` for the printer: nothing else here
        # can prove the path works, because every real trigger waits on
        # something happening first. Deliberately ignores the notices
        # table - this is a wire check, not one of the three things, and
        # recording it would silence the real notification about the same
        # parcel. It reports what Apple said per device rather than a
        # summary, because the useful refusals name themselves and each
        # points at a different mistake.
        conn.executescript(notify_mod.SCHEMA)
        conn.commit()
        targets = notify_mod.devices(conn)
        if not targets:
            print("no devices are registered, so there is nothing to send "
                  "to.\nOpen the app on the phone, go to Settings, and "
                  "turn notifications on.")
            return 0
        failed = 0
        for device in targets:
            try:
                ok, detail = notify_mod.send_one(
                    cfg, device["token"],
                    "mplabel is wired up",
                    "This is the test notification. The three real ones "
                    "are: a parcel is due, a label never printed, money "
                    "has no home.")
            except notify_mod.NotifyError as exc:
                print(f"push is not configured: {exc}", file=sys.stderr)
                return 78
            where = f"{device['token'][:8]}... ({device['environment']})"
            if ok:
                print(f"sent to {where}")
                continue
            failed += 1
            print(f"REFUSED for {where}: {detail}", file=sys.stderr)
            if "InternalServerError" in str(detail):
                # Apple's word for "no, and I will not say why". It is
                # retried once already, so a second one is either their
                # bad day or a request they cannot classify - and the
                # only thing left to do is check our own side rather
                # than ask for another command to be run.
                print("  APNs says that when it cannot classify a "
                      "request, and when it is simply having a bad "
                      "moment. Our side, for comparison:", file=sys.stderr)
                checked = argparse.Namespace(dry_run=False, test=False,
                                             check=True)
                cmd_notify(cfg, conn, checked)
            hint = ""
            if "BadDeviceToken" in str(detail):
                hint = ("apns_environment does not match the build the "
                        "token came from - a development build gives a "
                        "sandbox token")
            elif "InvalidProviderToken" in str(detail):
                hint = "apns_key_id or apns_team_id does not match the .p8"
            elif "TopicDisallowed" in str(detail):
                hint = "apns_topic must be the app's bundle id"
            if hint:
                print(f"  that usually means {hint}.", file=sys.stderr)
        return 1 if failed else 0

    if args.dry_run:
        # `remember_sent=False`, not a rollback: `remember` commits, so a
        # rollback here would leave the notices written and report that
        # they were not.
        result = notify_mod.run(cfg, conn, sender=dry, remember_sent=False)
        if not recorded:
            print("nothing to say"
                  + (" (no devices registered)" if not result["devices"]
                     else ""))
        for title, body in recorded:
            print(f"{title}\n    {body}")
        return 0

    try:
        result = notify_mod.run(cfg, conn)
    except notify_mod.NotifyError as exc:
        # A configuration refusal, the same shape printd uses: exit 78 so
        # a timer or a unit does not retry a permanent error for ever.
        print(f"push is not configured: {exc}", file=sys.stderr)
        return 78
    if not result["devices"]:
        print("no devices are registered; nothing was sent")
        return 0
    print(f"sent {len(result['sent'])} to {result['devices']} device"
          + ("" if result["devices"] == 1 else "s"))
    return 0


def cmd_ebay(cfg, args):
    """`ebay auth` and `ebay check`. Neither touches the database.

    Above `connect_db` for the same reason `probe` and `selftest` are: a
    credential test must not need a writable home directory. It is the
    one thing you want working when nothing else is.
    """
    from . import ebay as ebay_mod

    if args.ebaycmd == "check":
        blocking, notes = 0, 0
        for label, value, problem, is_blocking in ebay_mod.check(cfg):
            print(f"{label:20}: {value}")
            if not problem:
                continue
            if is_blocking:
                blocking += 1
                print(f"{'':20}  ^ {problem}", file=sys.stderr)
            else:
                # A note, not a fault. Printed on stdout beside the row
                # it belongs to, and it does not reach the exit code.
                notes += 1
                print(f"{'':20}    {problem}")
        if blocking:
            # 78, not 1: an unconfigured install is a permanent error and
            # a timer must not retry it for ever. Same refusal printd
            # makes for a missing secret - which is why only a *blocking*
            # problem earns it. The publish-time policies are notes.
            noun = "thing needs" if blocking == 1 else "things need"
            print(f"\n{blocking} {noun} attention - see docs/ebay.md",
                  file=sys.stderr)
            return 78
        if notes:
            print(f"\nnothing broken. {notes} thing(s) are only needed to "
                  f"publish, which nothing here does yet.")
        else:
            print("\nnothing to fix")
        return 0

    if args.ebaycmd == "setup":
        try:
            tokens = ebay_mod.load_tokens(cfg)
            if not ebay_mod.has_scope(tokens, ebay_mod.ACCOUNT_SCOPE):
                # Said here rather than letting eBay answer 403, which
                # reads as the account lacking a permission rather than
                # the token lacking a scope. `refresh_access` replays
                # the granted scopes deliberately, so this cannot fix
                # itself - it needs consent again.
                print("ebay: this token was granted before `sell.account` "
                      "was asked for, so it\ncannot create a policy. Run "
                      "`mplabel ebay auth` again to re-consent.",
                      file=sys.stderr)
                return 78

            # Before anything else: an account that has not joined the
            # business-policies programme cannot have policies at all,
            # and eBay's refusal for that is its own template with an
            # empty field name - `20403: Invalid .` - which reads as a
            # malformed request rather than an account that never opted
            # in. Ask the question whose answer is legible.
            joined = ebay_mod.opted_in_programs(cfg)
            if ebay_mod.POLICY_PROGRAM not in joined:
                if not args.opt_in:
                    print(f"this account has not joined "
                          f"{ebay_mod.POLICY_PROGRAM}, so it cannot have\n"
                          f"business policies at all. It is in: "
                          f"{', '.join(joined) or '(no programmes)'}\n\n"
                          f"Re-run with --opt-in to join it. That is an "
                          f"account-level change affecting\nevery listing, "
                          f"which is why it is not done for you - and eBay "
                          f"can take\n24 hours to process it, so `setup` "
                          f"may need running again tomorrow.",
                          file=sys.stderr)
                    return 78
                ebay_mod.opt_in_to_program(cfg)
                print(f"asked eBay to join {ebay_mod.POLICY_PROGRAM}.\n"
                      f"This can take up to 24 hours to take effect. Run "
                      f"`mplabel ebay setup` again\nonce it has, to create "
                      f"the policies.")
                return 0

            policies = ebay_mod.ensure_policies(cfg, dry_run=args.dry_run)
            key, what = ebay_mod.ensure_location(cfg, dry_run=args.dry_run)
        except ebay_mod.EbayConfigError as exc:
            print(f"ebay: {exc}", file=sys.stderr)
            return 78
        except ebay_mod.EbayError as exc:
            print(f"ebay: {exc}", file=sys.stderr)
            return 1

        print(f"{'location':22}: {key}  ({what})")
        for kind, (pid, said) in sorted(policies.items()):
            print(f"{kind + ' policy':22}: {pid or '-'}  ({said})")
        if args.dry_run:
            print("\nnothing created. Drop --dry-run to apply.")
            return 0
        print("\nPut these in /etc/mplabel.conf, or an offer will be "
              "refused:\n")
        print(f"ebay_merchant_location  = {key}")
        for kind in ("fulfillment", "payment", "return"):
            print(f"ebay_{kind}_policy{'':{max(0, 9 - len(kind))}} = "
                  f"{policies[kind][0] or ''}")
        return 0

    if args.ebaycmd == "skus":
        try:
            skus = ebay_mod.existing_skus(cfg)
        except ebay_mod.EbayConfigError as exc:
            print(f"ebay: {exc}", file=sys.stderr)
            return 78
        except ebay_mod.EbayError as exc:
            print(f"ebay: {exc}", file=sys.stderr)
            return 1
        if not skus:
            print("no SKUs on the account yet - nothing to collide with")
            return 0
        for sku in sorted(skus):
            print(f"  {sku}")
        print(f"\n{len(skus)} SKU(s) already in use. Reusing one does not "
              f"error:\nit re-points that inventory item at a new object, "
              f"so a live listing\nwould start describing something else.")
        return 0

    # auth
    try:
        if not args.code:
            url = ebay_mod.consent_url(cfg)
            print("Open this on a machine with a browser, sign in as the "
                  "seller, and agree:\n")
            print(f"  {url}\n")
            print("eBay then redirects to the URL configured against the "
                  "RuName with ?code=... on the end. That code is "
                  "url-encoded and expires in a few minutes, so paste it "
                  "back promptly:\n")
            print("  mplabel ebay auth --code '<the code>'")
            return 0
        # The code arrives url-encoded in a browser's address bar and is
        # routinely pasted that way. Unquoting an already-clean code is a
        # no-op, so this is safe in both directions.
        tokens = ebay_mod.exchange_code(
            cfg, urllib.parse.unquote(args.code))
    except ebay_mod.EbayConfigError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 78
    except ebay_mod.EbayError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 1

    print(f"stored {ebay_mod.token_path(cfg)} (0600)")
    days = ebay_mod.refresh_days_left(tokens)
    if days is None:
        print("eBay did not say how long the refresh token lasts, which "
              "means the expiry cannot be recorded - `ebay check` will "
              "not be able to warn you before it dies.")
    else:
        print(f"the refresh token lasts {days} days. `ebay check` counts "
              f"it down; there is no second warning from eBay.")
    return 0


def find_listing(conn, needle):
    """One listing by id, inventory code or title. Refuses on ambiguity."""
    needle = (needle or "").strip()
    if not needle:
        raise SystemExit("which listing?")
    if needle.isdigit():
        row = conn.execute("SELECT * FROM listings WHERE id=?",
                           (int(needle),)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT * FROM listings WHERE inventory_code=?",
                       (needle.upper(),)).fetchone()
    if row:
        return row
    rows = conn.execute(
        "SELECT * FROM listings WHERE title LIKE ? ORDER BY id",
        (f"%{needle}%",)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise SystemExit(f"no listing matches {needle!r}")
    # Refused rather than guessed: pushing the wrong object to eBay puts
    # someone else's thing on sale under this one's price.
    raise SystemExit(
        f"{len(rows)} listings match {needle!r} - name one by id:\n"
        + "\n".join(f"  {r['id']}  {r['title']}" for r in rows[:10]))


def cmd_ebay_push(cfg, conn, args):
    """One listing to eBay: an inventory item, an offer, and maybe live.

    The order of operations here is not arbitrary and not tidiness. eBay
    fetches `imageUrls` from our tunnel itself, and `/ebay/photo/<sha>`
    serves a digest **only** when it is attached to a listing with a row
    in `ebay_offers` - so that row has to exist *before* the publish
    call, or the allowlist 404s eBay's own fetch and the publish fails
    naming the image field. The row is a precondition, not a record of
    what happened.
    """
    from . import ebay as ebay_mod
    from . import listings as listings_mod

    # Publishing is refused on production by policy, and the refusal is
    # here rather than in `publish_offer` so that a future caller cannot
    # reach the mechanism without meeting the policy. Going live is a
    # decision made in eBay's own UI, where the whole listing is visible.
    if args.publish and ebay_mod.environment(cfg) == "production":
        print("refusing: ebay_environment is production.\n"
              "Publishing is a decision made in eBay's own UI, where you "
              "can see the whole\nlisting. Drop --publish; the offer is "
              "created and waiting in your drafts.", file=sys.stderr)
        return 2

    aspects = {}
    for pair in args.aspect or []:
        name, _, value = pair.partition("=")
        if not name or not value:
            raise SystemExit(f"--aspect wants NAME=VALUE, got {pair!r}")
        aspects[name.strip()] = value.strip()

    # Lazily minted, so a listing that has never had a label has no code
    # and therefore no SKU.
    ensure_inventory_codes(conn)
    listing = find_listing(conn, args.listing)
    row = dict(listing)

    try:
        sku = ebay_mod.sku_for(row.get("inventory_code"))
        photos = listings_mod.photos_for(conn, row["id"])
        digests = [p["sha256"] for p in photos if p.get("sha256")]
        image_urls = [ebay_mod.photo_url(cfg, d) for d in digests] \
            if digests else []

        suggestions = ebay_mod.suggest_categories(cfg, row.get("title"))
        category = args.category or (suggestions[0]["id"]
                                     if suggestions else None)
        needed = ebay_mod.required_aspects(cfg, category) if category else []
    except ebay_mod.EbayConfigError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 78
    except ebay_mod.EbayError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 1

    print(f"{row['title']}")
    sent_title = ebay_mod.ebay_title(row.get("title"))
    if sent_title != (row.get("title") or "").strip():
        # Said out loud because the desk shows her full title and eBay
        # would show 80 characters of it, with nothing anywhere saying
        # they differ.
        print(f"  title      cut to {len(sent_title)} chars for eBay:")
        print(f"             {sent_title}")
    print(f"  sku        {sku}")
    print(f"  price      "
          + ("?" if row.get("price") is None else f"{row['price']:.2f}"))
    print(f"  photos     {len(image_urls)}")
    for url in image_urls:
        print(f"             {url}")
    if suggestions:
        print("  eBay suggests:")
        for i, guess in enumerate(suggestions):
            mark = "*" if str(guess["id"]) == str(category) else " "
            print(f"           {mark} {guess['id']}  {guess['path']}")
    missing = [a for a in needed if a not in aspects]
    if needed:
        print(f"  required aspects for {category}: " + ", ".join(needed))
        if missing:
            print(f"             missing: " + ", ".join(missing))

    if args.dry_run:
        print("\n--- inventory item ---")
        print(json.dumps(
            ebay_mod.inventory_item_body(row, image_urls, aspects), indent=2))
        if category:
            print("--- offer ---")
            print(json.dumps(
                ebay_mod.offer_body(cfg, sku, row, category), indent=2))
        print("\nnothing sent.")
        return 0

    if args.publish:
        if not args.category:
            # The suggestion is a guess from a title, and a wrong
            # category is a listing nobody searching for the thing will
            # ever see - a silent failure. Confirm it or do not publish.
            print("\nrefusing to publish on a suggested category. Name one "
                  "with --category;\nthe suggestions above are eBay's guess "
                  "from the title.", file=sys.stderr)
            return 2
        if missing:
            print("\nrefusing to publish without: " + ", ".join(missing)
                  + "\neBay would refuse it too, one aspect per round trip.",
                  file=sys.stderr)
            return 2
        if not image_urls:
            print("\nrefusing to publish with no photographs: eBay requires "
                  "at least one,\nand it fetches them from "
                  f"{cfg.get('ebay_photo_base') or '(ebay_photo_base unset)'}.",
                  file=sys.stderr)
            return 2

    try:
        ebay_mod.put_inventory_item(
            cfg, sku, ebay_mod.inventory_item_body(row, image_urls, aspects))
        print(f"\ninventory item {sku} sent")

        offer_id, what = ebay_mod.create_or_update_offer(
            cfg, sku, ebay_mod.offer_body(cfg, sku, row, category))
        print(f"offer {offer_id} {what} (unpublished)")

        # Written before the publish call, deliberately: this row is
        # what makes the photographs fetchable. See the docstring.
        record_ebay_offer(conn, row["id"], sku, offer_id, state="draft")

        if not args.publish:
            print("\nnot published. Review it in eBay's UI, or re-run with "
                  "--category and --publish on sandbox.")
            return 0

        # Only now, with the allowlist row in place, can eBay fetch the
        # images - so this is the first moment the check means anything.
        for url in image_urls:
            ok, why = ebay_mod.photo_reachable(cfg, url)
            if not ok:
                print(f"\nrefusing to publish: {url}\n  {why}\n"
                      "eBay fetches these itself and its refusal names the "
                      "field, not the reason.", file=sys.stderr)
                return 1
        print(f"photos reachable ({len(image_urls)})")

        item = ebay_mod.publish_offer(cfg, offer_id)
        record_ebay_offer(conn, row["id"], sku, offer_id,
                          state="published", ebay_item=item)
        print(f"published: item {item}")
    except ebay_mod.EbayConfigError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 78
    except ebay_mod.EbayError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 1
    return 0


def record_ebay_offer(conn, listing_row_id, sku, offer_id, state,
                      ebay_item=None):
    """Where this listing is on eBay. One row per listing, overwritten.

    `ebay_item` is only set once something is published, and an update
    must not blank it: a republish of an already-live listing passes
    None for it while the listing id is still true.
    """
    conn.execute(
        "INSERT INTO ebay_offers (listing_id, sku, offer_id, state, "
        "pushed_at, ebay_item) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(listing_id) DO UPDATE SET "
        "sku=excluded.sku, offer_id=excluded.offer_id, "
        "state=excluded.state, pushed_at=excluded.pushed_at, "
        "ebay_item=COALESCE(excluded.ebay_item, ebay_offers.ebay_item)",
        (listing_row_id, sku, offer_id, state,
         datetime.now().isoformat(timespec="seconds"), ebay_item))
    conn.commit()


def cmd_ebay_pull(cfg, conn, args):
    """eBay orders as `sales` rows - printed, not written.

    `--dry-run` is the only mode that exists yet, and that is the point
    of this slice rather than a limitation of it: it proves the OAuth
    path, the order JSON shape and the whole mapping against the real
    API without one write to a database that holds real orders. Every
    other survey in this project has the same shape - `scan` changes
    nothing at all, and `notify`, `pending`, `reconcile` and `sheets`
    each carry a `--dry-run` that was worth having.

    Running it twice says the same thing, because it records nothing.
    That honesty was got wrong once already: `notify --dry-run` ran the
    whole decision and then called `rollback()`, which did nothing
    because `remember` had already committed.
    """
    if not args.dry_run:
        # Refused rather than defaulted to dry: a command that silently
        # does less than its name says is worse than one that stops.
        print("ebay pull: writing is not built yet - this slice only "
              "surveys.\nRun it with --dry-run to see what it would "
              "record.", file=sys.stderr)
        return 2

    try:
        orders = ebay_mod.get_orders(cfg, since=args.since, limit=args.limit)
    except ebay_mod.EbayConfigError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 78
    except ebay_mod.EbayError as exc:
        print(f"ebay: {exc}", file=sys.stderr)
        return 1

    if not orders:
        print("no eBay orders in that window")
        return 0

    fresh, known = [], []
    for order in orders:
        rec = ebay_mod.order_to_sale(order)
        # The same guard the mail path uses, and for the same reason: the
        # unit of a sale is the order. A second `pull` over the same
        # window must not be able to produce a second row.
        seen = already_seen(conn, rec.get("message_id"), rec.get("order_id"))
        (known if seen else fresh).append(rec)

    for rec in fresh:
        print(f"\n  {rec['message_id']}  {rec['item'] or '(no title)'}")
        shown = "?" if rec["price"] is None else f"{rec['price']:.2f}"
        print(f"    price     {shown}   "
              f"(line items, not the order total)")
        print(f"    sold      {rec['received_at']}")
        print(f"    ship by   {rec['ship_by']}   (local date)")
        print(f"    ship to   {rec['ship_to']}")
        print(f"    service   {rec['service']}")
        print(f"    sku       {rec['sku']}")

    print(f"\nwould record {len(fresh)} sale(s); "
          f"{len(known)} already in the database.")
    # Said out loud because the columns do not exist yet, and a dry run
    # that quietly implies otherwise is the thing this command is for.
    print("nothing written - `sales.channel` and the write path are the "
          "next slice.")
    return 0


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


def cmd_goodwill(conn, args):
    """Read one saved ShopGoodwill email and say what it found.

    The `scan` of the auction half. Everything this parser knows was
    reconstructed from two forwarded mails, and ShopGoodwill redesigns
    that template like any other marketing department - so before a run
    writes a cost basis into the database there has to be a way to point
    it at a real message and *look*. Reads by default and writes only
    when asked, for the same reason `scan` changes nothing.

    Save the message as .eml from the mail client: 'Show original' in
    Gmail, then save."""
    raw = Path(args.path).read_bytes()
    msg = email.message_from_bytes(raw)
    if not goodwill_mod.is_from_goodwill(msg):
        sender = mailparse._decode(msg.get("From")) or "(no From header)"
        raise SystemExit(f"not a ShopGoodwill message - From: {sender}")

    order = goodwill_mod.parse(msg)
    if not order:
        subject = mailparse._decode(msg.get("Subject"))
        raise SystemExit(
            f"no pattern matched that subject:\n  {subject}\n\n"
            "If it is a win or a payment receipt, add it to "
            "SUBJECT_PATTERNS in goodwill.py.")

    print(f"kind    : {order['kind']}")
    if order.get("seller"):
        print(f"seller  : {order['seller']}")
    if order.get("order_id"):
        print(f"order   : {order['order_id']}   paid {order.get('paid_on')}")
    costs = goodwill_mod.landed_cost(order) if order["kind"] == "goodwill_paid" else {}
    for item in order.get("items") or []:
        print(f"\n  item  : {item['item_id']}")
        print(f"  title : {item.get('title')}")
        print(f"  price : {item.get('price')}")
        if item["item_id"] in costs:
            print(f"  paid  : {costs[item['item_id']]}  (landed)")
    if order["kind"] == "goodwill_paid":
        print(f"\nsubtotal {order.get('subtotal')}  tax {order.get('tax')}  "
              f"postage {order.get('shipping')}  total {order.get('total')}")
        if len(order.get("items") or []) > 1:
            print("More than one item, so the tax and the postage are not "
                  "split - each item carries its own price and the rest "
                  "stays as the trip's unassigned money.")

    if not args.write:
        print("\nNothing written. Re-run with --write to record it.")
        return
    result = goodwill_mod.import_order(conn, order)
    listings_mod.refresh(conn)
    print(f"\nrecorded {len(result['listing_ids'])} item(s)"
          + (f", trip {result['trip_id']}" if result.get("trip_id") else ""))


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
    # What has come home and is not yet for sale. A new question - until
    # the ShopGoodwill importer there was no way for a row to exist
    # before it was listed - and the one that decides what to photograph
    # next. Cost is shown because it is the money standing still.
    table("Bought, not yet listed",
          "SELECT title, paid, inventory_code FROM listings "
          "WHERE state = 'acquired' ORDER BY id DESC LIMIT 10",
          # Not `or 0`: a win that has not been paid for yet has a null
          # cost, and printing it as $0.00 says the thing was free. The
          # same distinction `v_listing_perf.margin` is built on.
          lambda r: f"{(r['title'] or '?')[:34]:<36} "
                    + ("      -" if r["paid"] is None else f"${r['paid']:>6.2f}")
                    + f"  {r['inventory_code'] or '----'}")
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
        # `return`, not a bare call. `notify` and `ebay check` answer a
        # configuration error with 78 (EX_CONFIG) so that systemd's
        # RestartPreventExitStatus=78 can keep a permanent error dead
        # where it can be seen - and dropping the value here turned every
        # one of those refusals into a success, which is the half of that
        # pair that has no unit test to notice.
        return _main()
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
    p.add_argument("--page", type=int, default=1,
                   help="which page of the PDF (default %(default)s)")
    p.add_argument("--region", type=int,
                   help="which block on the page is the label, when more "
                        "than one could be and it refused to guess")
    p.add_argument("--print", dest="print_it", action="store_true")
    p.add_argument("--code", help="stamp this parcel code on the label, to "
                                  "check placement without printing")
    p = sub.add_parser("send",
                       help="send a PDF to the Pi to be printed, from "
                            "anywhere - a label that is not a Marketplace "
                            "one")
    p.add_argument("pdf")
    p.add_argument("--url", help="the phone app's address; defaults to "
                                 "web_url in mplabel.conf")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270],
                   help="override the detected orientation. Needed when "
                        "the label carries no text to read one off, where "
                        "its shape says it is on its side but not which "
                        "way up")
    p.add_argument("--page", type=int, default=1,
                   help="which page of the PDF (default %(default)s)")
    p.add_argument("--region", type=int,
                   help="which block on the page is the label, when more "
                        "than one could be and the server refused to guess")
    p.add_argument("--dry-run", action="store_true",
                   help="convert and measure, print nothing. What to run "
                        "on a new seller's PDF before spending a label")
    p.add_argument("--force", action="store_true",
                   help="print it again. The same PDF twice is one job by "
                        "design, so a retry cannot double-print")
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
    p = sub.add_parser("goodwill",
                       help="read one saved ShopGoodwill email")
    p.add_argument("path", help="a .eml saved from the mail client")
    p.add_argument("--write", action="store_true",
                   help="record it; without this it only reports")
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
    p.add_argument("--size", default=None, metavar="WxH",
                   help="label size the way you hold it, in mm (default "
                        "%(default)s). Suffix `in` for inches, so a 4x1in "
                        "shelf label is --size 4x1in. Anything wider than "
                        "the 48mm head is printed with its long axis down "
                        "the feed - the head does not turn")
    p.add_argument("--density", type=int, default=None,
                   help="burn energy 0-15. Unset means the host with the "
                        "roll decides (supvan_density), which is the point "
                        "- it is the one that can see the paper")
    p.add_argument("--preview", metavar="PNG",
                   help="write what the payload decodes back to")
    p.add_argument("--scale", type=int, default=2,
                   help="preview magnification (default %(default)s)")
    p.add_argument("--print", action="store_true",
                   help="actually send it. Nothing built this way has "
                        "printed yet - see open work 1b")
    p.add_argument("--device", help="hidraw node, default supvan_device")

    p = sub.add_parser("shelf-tag",
                       help="draw a tag for a shelf, bin or area")
    p.add_argument("--code", required=True,
                   help="the location code: THREE characters, where an "
                        "item code is four. That is what tells a scanner "
                        "a shelf from a thing on it")
    p.add_argument("--name", help="what the place is called, under the code")
    p.add_argument("--marker", action="store_true",
                   help="add the shelf marker carrying the same code")
    p.add_argument("--qr", action="store_true",
                   help="add a QR carrying the same code instead")
    p.add_argument("--ecl", choices=["L", "M", "Q", "H"], default="M",
                   help="QR error correction (default %(default)s)")
    p.add_argument("--size", default=None, metavar="WxH",
                   help="tag size the way you hold it, mm unless suffixed "
                        "`in` (default %(default)s, the same stock as an "
                        "item label). A bigger tag reads better across a "
                        "room, but it must fit the die-cut label or it "
                        "prints across several of them")
    p.add_argument("--density", type=int, default=None,
                   help="burn energy 0-15. Unset means the host with the "
                        "roll decides (supvan_density), which is the point "
                        "- it is the one that can see the paper")
    p.add_argument("--preview", metavar="PNG",
                   help="write what the payload decodes back to")
    p.add_argument("--scale", type=int, default=2,
                   help="preview magnification (default %(default)s)")
    p.add_argument("--print", action="store_true", help="actually send it")
    p.add_argument("--device", help="hidraw node, default supvan_device")

    p = sub.add_parser("bin", help="the places things live: make one, see "
                                   "what is in it, put something in it")
    bsub = p.add_subparsers(dest="action", required=True)

    b = bsub.add_parser("new", help="name a place; the code is minted here")
    b.add_argument("name", help="what it is called - FLOOR, ATTIC, B5. "
                                "Read across a room, so it is a name and "
                                "not a code")
    b.add_argument("--notes")
    b.add_argument("--print", action="store_true",
                   help="print its shelf tag straight away, which is the "
                        "only way the code gets onto the shelf")
    b.add_argument("--qr", action="store_true",
                   help="carry the code as a QR instead of the marker")
    b.add_argument("--preview", metavar="PNG",
                   help="write what the tag decodes back to")
    b.add_argument("--scale", type=int, default=2)
    b.add_argument("--size", default=None, metavar="WxH")
    b.add_argument("--density", type=int, default=None)
    b.add_argument("--device")

    b = bsub.add_parser("ls", help="every bin and how much is in it")
    b.add_argument("--all", action="store_true",
                   help="count sold items too, i.e. what *was* there")

    b = bsub.add_parser("show", help="what is in one bin")
    b.add_argument("bin", help="its code or its name; either will do")
    b.add_argument("--all", action="store_true")

    b = bsub.add_parser("rename", help="change what a bin is called")
    b.add_argument("bin", help="its code or its current name")
    b.add_argument("name", help="the new name. The code does not move, so "
                                "the tag on the shelf stays correct")

    b = bsub.add_parser("put", help="put one thing in a bin")
    b.add_argument("item", help="the 4-character inventory code off the "
                                "label, or a listing id")
    b.add_argument("bin", help="the bin's code or name; empty takes the "
                               "thing off the shelf entirely")

    b = bsub.add_parser("rm", help="retire a bin; its contents come back "
                                   "out rather than going with it")
    b.add_argument("bin")

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
    p.add_argument("--style",
                   choices=["blocks", "sparse", "scatter", "ruler",
                            "edges"],
                   default="blocks",
                   help="test pattern (default %(default)s). 'sparse' draws "
                        "the same landmarks in outline, 0.66%% ink "
                        "against 7.54%%. 'scatter' is a diagnostic, "
                        "not a picture: working-print ink at "
                        "failing-print size, to say which of the two "
                        "the device objects to. 'ruler' is the "
                        "calibration target - scales on both axes, the "
                        "last dot's number at each far end, and an inset "
                        "comb to measure a clipped edge. 'edges' asks "
                        "only where each edge starts printing, in high "
                        "contrast: eight bars per side, 8 dots apart, each "
                        "a different length so it names itself. Use "
                        "--width and --height for a particular label")
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
    p = sub.add_parser("config",
                       help="show the resolved config and where each "
                            "value came from")
    p.add_argument("--all", action="store_true",
                   help="include keys still at their built-in default")
    sub.add_parser("passwd", help="hash a password for web_password_hash")
    sub.add_parser("status", help="ask the printer how it is (does it "
                                  "answer at all?)")
    p = sub.add_parser("reconcile",
                       help="ask printd what it printed and correct rows "
                            "that a timeout left looking unprinted")
    p.add_argument("--since", metavar="JOB", help="only jobs after this one")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("notify",
                       help="say the three things that earn a notification: "
                            "a parcel is due, a label never printed, money "
                            "has no home")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent and send nothing")
    p.add_argument("--test", action="store_true",
                   help="send one deliberate notification to every "
                        "registered device and report what Apple said")
    p.add_argument("--check", action="store_true",
                   help="check the push configuration and the JWT this "
                        "would sign, and send nothing")
    p = sub.add_parser("printd", help="run the print service")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)

    p = sub.add_parser("ebay", help="the other selling channel")
    esub = p.add_subparsers(dest="ebaycmd", required=True)
    e = esub.add_parser("auth",
                        help="grant this application access to the eBay "
                             "account; the Pi is headless, so consent "
                             "happens in a browser elsewhere")
    e.add_argument("--code",
                   help="the authorization code from the redirect URL. "
                        "Without it this prints the consent URL and stops")
    esub.add_parser("check",
                    help="configuration, tokens and how long they have "
                         "left. Changes nothing and sends nothing")
    esub.add_parser("skus",
                    help="every SKU already on the eBay account. Read-only, "
                         "and worth one call before the first push: eBay's "
                         "SKU uniqueness is permanent")
    e = esub.add_parser("setup",
                        help="create the three business policies and the "
                             "inventory location an offer has to name. "
                             "Needs the sell.account scope")
    e.add_argument("--dry-run", action="store_true",
                   help="say what it would create and create nothing")
    e.add_argument("--opt-in", action="store_true",
                   help="join the business-policies seller programme. An "
                        "account-level change affecting every listing, so "
                        "it is never done without this flag - and eBay can "
                        "take 24 hours to process it")
    e = esub.add_parser("push",
                        help="one listing as an eBay inventory item and an "
                             "unpublished offer")
    e.add_argument("listing", help="listing id, inventory code, or title")
    e.add_argument("--category", help="eBay category id. Required to "
                                      "publish; a draft takes the suggestion")
    e.add_argument("--aspect", action="append", metavar="NAME=VALUE",
                   help="an item specific, repeatable. eBay refuses a "
                        "publish without the required ones")
    e.add_argument("--publish", action="store_true",
                   help="make it a live listing. Sandbox only - refused "
                        "when ebay_environment is production")
    e.add_argument("--dry-run", action="store_true",
                   help="print the exact JSON and send nothing")
    e = esub.add_parser("pull",
                        help="eBay orders as sales rows. --dry-run is "
                             "currently the only mode: it prints what it "
                             "would record and writes nothing")
    e.add_argument("--since", metavar="YYYY-MM-DD",
                   help="orders created on or after this date "
                        f"(default: {ebay_mod.DEFAULT_SINCE_DAYS} days back)")
    e.add_argument("--limit", type=int, help="stop after this many orders")
    e.add_argument("--dry-run", action="store_true",
                   help="print what would be recorded and write nothing")

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
    if args.cmd == "shelf-tag":
        cmd_shelf_tag(cfg, args)
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
    if args.cmd == "send":
        # Above connect_db for the same reason as `file`: this talks to a
        # server over HTTP and has no business needing a local database.
        # It is also the command most likely to be run on a laptop that
        # has never had one.
        cmd_send(cfg, args)
        return
    if args.cmd == "passwd":
        cmd_passwd()
        return
    if args.cmd == "config":
        cmd_config(args)
        return
    if args.cmd == "status":
        cmd_status(cfg)
        return
    if args.cmd == "printd":
        # No database: this half knows about paper, not orders.
        from . import printd as printd_mod
        printd_mod.serve(cfg, bind=args.bind, port=args.port)
        return
    if args.cmd == "ebay" and args.ebaycmd in ("auth", "check", "skus",
                                               "setup"):
        # Same reasoning as probe and selftest: checking a credential
        # must not need the database. The subcommands that read or write
        # listings fall through to the block below.
        return cmd_ebay(cfg, args)

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
    elif args.cmd == "goodwill":
        cmd_goodwill(conn, args)
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
            n = listings_mod.import_csv(conn, args.path, state=args.state)
            print(f"imported {n} row(s)")
        listings_mod.refresh(conn)
    elif args.cmd == "pending":
        cmd_pending(cfg, conn, args)
    elif args.cmd == "bin":
        cmd_bin(conn, cfg, args)
    elif args.cmd == "stats":
        cmd_stats(cfg, conn, args)
    elif args.cmd == "reconcile":
        cmd_reconcile(cfg, conn, args)
    elif args.cmd == "ebay":
        # `auth`, `check`, `skus` and `setup` were handled above
        # connect_db; these two need the database.
        if args.ebaycmd == "push":
            return cmd_ebay_push(cfg, conn, args)
        return cmd_ebay_pull(cfg, conn, args)
    elif args.cmd == "notify":
        return cmd_notify(cfg, conn, args)
