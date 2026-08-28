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
import email
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
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import backfill as backfill_mod
from . import label
from . import listings as listings_mod
from . import mailparse
from . import printers
from . import savedpage as savedpage_mod
from . import sheets as sheets_mod

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
    # A 3-digit code printed small in the top right, so a stack of parcels
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing
    ON sales(listing_id) WHERE listing_id IS NOT NULL;
"""


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
        conn.executescript(SCHEMA)
        # listings owns its own tables, but the poll loop writes mail_events
        # as it goes, so they have to exist from the start. Both scripts are
        # CREATE TABLE IF NOT EXISTS.
        conn.executescript(listings_mod.SCHEMA)
        # ...which is exactly why a new column needs saying separately: the
        # database already holds real sales and CREATE TABLE IF NOT EXISTS
        # will not touch them.
        have = {r[1] for r in conn.execute("PRAGMA table_info(sales)")}
        for column, decl in (("code", "TEXT"),):
            if column not in have:
                conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {decl}")
                log.info("added sales.%s to the existing database", column)
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


def already_seen(conn, message_id, listing_id):
    if message_id and conn.execute(
            "SELECT 1 FROM sales WHERE message_id=?", (message_id,)).fetchone():
        return True
    if listing_id and conn.execute(
            "SELECT 1 FROM sales WHERE listing_id=?", (listing_id,)).fetchone():
        return True
    return False


# ---------------------------------------------------------------- printing

def allocate_code(conn):
    """A 3-digit code that no unshipped parcel is already using.

    Scoped to unshipped deliberately: the code exists to tell apart the
    boxes waiting to go out, so once a parcel ships its code is free
    again. Random rather than sequential, so a re-run cannot silently
    hand out a code that is still on a box in the hall."""
    taken = {r[0] for r in conn.execute(
        "SELECT code FROM sales WHERE code IS NOT NULL "
        "AND status != 'shipped'")}
    free = [f"{n:03d}" for n in range(1000) if f"{n:03d}" not in taken]
    if not free:
        # A thousand parcels open at once. Repeat rather than refuse to
        # print: an ambiguous code beats a parcel that cannot ship.
        log.warning("all 1000 codes are in use by unshipped parcels")
        return f"{random.randrange(1000):03d}"
    return random.choice(free)


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


def print_label(cfg, pdf_path, code=None):
    backend = cfg["printer_backend"]
    dpi = int(cfg["printer_dpi"])
    darkness = int(cfg["printer_darkness"]) if cfg["printer_darkness"] else None

    if backend.startswith("cups"):
        kwargs = {"printer": cfg["printer_queue"] or None}
        if backend == "cups-raster":
            kwargs["dpi"] = dpi
    elif backend == "tspl":
        kwargs = {"device": cfg["printer_device"], "dpi": dpi,
                  "darkness": darkness,
                  "speed": int(cfg["printer_speed"]) if cfg.get("printer_speed") else None,
                  "media": cfg.get("media_tracking", "gap"),
                  "gap_in": float(cfg.get("gap_inches", 0.12)),
                  "settle": float(cfg.get("settle_seconds", 2.0))}
    elif backend == "escpos":
        # ESC/POS has no gap-distance command - the printer finds the gap
        # itself on a form feed - so gap_inches does not apply here.
        kwargs = {"device": cfg["printer_device"], "dpi": dpi,
                  "media": cfg.get("media_tracking", "gap"),
                  "band_rows": int(cfg.get("escpos_band_rows", 128)),
                  "settle": float(cfg.get("settle_seconds", 2.0))}
    else:
        kwargs = {"device": cfg["printer_device"], "dpi": dpi,
                  "darkness": darkness}

    # Stamp a throwaway copy rather than the archive, so a reprint cannot
    # double-stamp and labels/<ref>_4x6.pdf stays as Facebook sent it.
    tmp = None
    if code and truthy(cfg.get("label_code", "yes")):
        tmp = Path(tempfile.gettempdir()) / f"mplabel_{code}_{os.getpid()}.pdf"
        label.stamp_code(pdf_path, tmp,
                         code, size=float(cfg.get("label_code_size", 8)))
        pdf_path = tmp

    log.info("printing %s via %s%s", Path(pdf_path).name, backend,
             f" [{code}]" if code else "")
    try:
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
    if already_seen(conn, parsed.get("message_id"), parsed.get("listing_id")):
        log.debug("skipping, already recorded")
        return None

    fname, blob = mailparse.attachment(msg, ".pdf")
    if not blob:
        log.warning("no PDF attached to %s", parsed.get("subject"))
        return None

    ref = (parsed.get("listing_id")
           or parsed.get("order_id")
           or datetime.now().strftime("%Y%m%d%H%M%S"))
    labels = Path(cfg["home"]) / "labels"
    raw_pdf = labels / f"{ref}_source.pdf"
    out_pdf = labels / f"{ref}_4x6.pdf"
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
        "printed_at, code FROM sales WHERE status != 'shipped' "
        "ORDER BY ship_by"
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


def cmd_reprint(cfg, conn, args):
    row = conn.execute(
        "SELECT * FROM sales WHERE listing_id=? OR order_id=? OR tracking=?",
        (args.ref, args.ref, args.ref)).fetchone()
    if not row:
        raise SystemExit(f"no record matching {args.ref}")
    code = ensure_code(conn, row["message_id"])
    print_label(cfg, row["label_pdf"], code)
    mark_printed(conn, row["message_id"])
    print(f"reprinted {row['item']}" + (f"  [{code}]" if code else ""))


def cmd_test_print(cfg, conn, args):
    row = conn.execute("SELECT * FROM sales WHERE label_pdf IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no labels on file yet - run `check` first")
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
    sql = ("SELECT message_id, item, price, code, label_pdf, received_at "
           "FROM sales WHERE printed_at IS NULL AND status != 'shipped' "
           "AND label_pdf IS NOT NULL")
    params = []
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
        if not Path(r["label_pdf"]).exists():
            log.error("label file missing for %s: %s", r["item"],
                      r["label_pdf"])
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
    n = conn.execute("UPDATE sales SET status='shipped' WHERE listing_id=? "
                     "OR order_id=? OR tracking=?",
                     (args.ref, args.ref, args.ref)).rowcount
    conn.commit()
    print(f"{n} record(s) marked shipped")


def main():
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
    p = sub.add_parser("reprint"); p.add_argument("ref")
    p = sub.add_parser("ship", help="mark as shipped"); p.add_argument("ref")
    sub.add_parser("test-print", help="reprint the newest label")
    sub.add_parser("probe", help="show printers and USB devices")
    sub.add_parser("selftest", help="print a tiny text-only TSPL test label")
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

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if args.cmd == "probe":
        printers.probe()
        return

    cfg = load_config(args.config)

    # These touch the printer and the filesystem but never the database.
    # Keep them above connect_db: a missing or unwritable home directory
    # must not stop you testing the printer or converting a PDF by hand.
    if args.cmd == "selftest":
        # Send the test label in whatever language the printer is set to
        # speak; a TSPL selftest on an ESC/POS printer just prints the
        # commands as literal text.
        backend = cfg["printer_backend"]
        if backend == "escpos":
            printers.escpos_selftest(cfg["printer_device"])
        else:
            printers.tspl_selftest(cfg["printer_device"],
                                   cfg.get("media_tracking", "gap"),
                                   float(cfg.get("gap_inches", 0.12)))
        print(f"sent text-only {backend} test label to "
              + cfg["printer_device"])
        return
    if args.cmd == "file":
        cmd_file(cfg, args)
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
    elif args.cmd == "test-print":
        cmd_test_print(cfg, conn, args)
    elif args.cmd == "scan":
        backfill_mod.scan(cfg, args.limit)
    elif args.cmd == "backfill":
        backfill_mod.run(cfg, conn, args.limit, resume=not args.restart)
    elif args.cmd == "sheets":
        sync_sheets(cfg, conn, dry_run=args.dry_run)
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
