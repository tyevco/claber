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
import socket
import sqlite3
import sys
import time
from datetime import datetime
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
    "printer_backend": "tspl",
    "printer_queue": "",
    "printer_device": "/dev/usb/lp0",
    "printer_dpi": "203",
    "printer_darkness": "8",
    "printer_speed": "4",
    "media_tracking": "gap",
    "gap_inches": "0.12",
    "settle_seconds": "2.0",
    "poll_seconds": "120",
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
    conn = sqlite3.connect(home / "sales.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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


def already_seen(conn, message_id, listing_id):
    if message_id and conn.execute(
            "SELECT 1 FROM sales WHERE message_id=?", (message_id,)).fetchone():
        return True
    if listing_id and conn.execute(
            "SELECT 1 FROM sales WHERE listing_id=?", (listing_id,)).fetchone():
        return True
    return False


# ---------------------------------------------------------------- printing

def print_label(cfg, pdf_path):
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
    else:
        kwargs = {"device": cfg["printer_device"], "dpi": dpi,
                  "darkness": darkness}

    log.info("printing %s via %s", Path(pdf_path).name, backend)
    printers.send(pdf_path, backend, **kwargs)


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
            print_label(cfg, out_pdf)
            mark_printed(conn, rec.get("message_id"))
        except Exception as exc:
            log.error("print failed for %s: %s", ref, exc)
            conn.execute("UPDATE sales SET notes=? WHERE message_id=?",
                         (f"print failed: {exc}", rec.get("message_id")))
            conn.commit()
    return rec


def poll_once(cfg, conn, do_print):
    host, port = cfg["imap_host"], int(cfg["imap_port"])
    user, pw = cfg["imap_user"], cfg["imap_password"]
    if not user or not pw:
        raise SystemExit("imap_user / imap_password not configured")

    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(user, pw)
        imap.select(cfg["imap_folder"])
        typ, data = imap.search(None, '(UNSEEN FROM "facebook")')
        ids = data[0].split() if data and data[0] else []
        log.info("%d unread candidate(s)", len(ids))

        handled = 0
        for num in ids:
            typ, raw = imap.fetch(num, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            if not mailparse.is_label_email(msg):
                # Leave unrelated Facebook mail unread and untouched.
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
        if handled and truthy(cfg.get("sheets_after_poll")) and cfg.get("sheets_key"):
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

def cmd_file(cfg, conn, args):
    src = Path(args.pdf)
    out = Path(args.output) if args.output else src.with_name(src.stem + "_4x6.pdf")
    info = label.to_4x6(src, out, force_rotation=args.rotate)
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
        "printed_at FROM sales WHERE status != 'shipped' ORDER BY ship_by"
    ).fetchall()
    if not rows:
        print("nothing outstanding")
        return
    for r in rows:
        printed = "printed" if r["printed_at"] else "NOT PRINTED"
        print(f"{r['ship_by'] or '?':<12} ${r['price'] or 0:>7.2f}  "
              f"{(r['item'] or '?')[:38]:<40} {r['buyer'] or '?':<18} "
              f"{printed}")


def cmd_reprint(cfg, conn, args):
    row = conn.execute(
        "SELECT * FROM sales WHERE listing_id=? OR order_id=? OR tracking=?",
        (args.ref, args.ref, args.ref)).fetchone()
    if not row:
        raise SystemExit(f"no record matching {args.ref}")
    print_label(cfg, row["label_pdf"])
    mark_printed(conn, row["message_id"])
    print(f"reprinted {row['item']}")


def cmd_test_print(cfg, conn, args):
    row = conn.execute("SELECT * FROM sales WHERE label_pdf IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no labels on file yet - run `check` first")
    print_label(cfg, row["label_pdf"])
    print(f"sent {Path(row['label_pdf']).name} to {cfg['printer_backend']}")


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
    conn = connect_db(cfg["home"])

    if args.cmd == "check":
        poll_once(cfg, conn, do_print=False)
    elif args.cmd == "run":
        do_print = truthy(cfg["auto_print"])
        if args.loop:
            loop(cfg, conn, do_print)
        else:
            poll_once(cfg, conn, do_print)
    elif args.cmd == "file":
        cmd_file(cfg, conn, args)
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
                                                  verbose=not args.quiet)
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
    elif args.cmd == "stats":
        cmd_stats(cfg, conn, args)
    elif args.cmd == "selftest":
        printers.tspl_selftest(cfg["printer_device"],
                               cfg.get("media_tracking", "gap"),
                               float(cfg.get("gap_inches", 0.12)))
        print("sent text-only TSPL test label to " + cfg["printer_device"])
