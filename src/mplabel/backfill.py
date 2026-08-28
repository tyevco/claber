"""
backfill.py - walk the whole mailbox once and reconstruct listing history
from every Facebook Marketplace email she has ever received.

This is the only way to get a listing catalogue without an API. It gives
you listing dates, sale dates, inquiry counts and payouts going back as
far as her mail does.

Two commands matter:

    mplabel.py scan       report what is in the mailbox, change nothing
    mplabel.py backfill   parse it into the listings table

Run `scan` first. It prints a histogram of Facebook subject lines and,
crucially, lists the ones no pattern matched. Facebook's wording varies
by locale and changes over time, so rather than guess the subjects once
and let it rot, add what you actually see to EVENT_PATTERNS in
listings.py.
"""

import email
import imaplib
import logging
import re
from collections import Counter
from email.utils import parsedate_to_datetime

from . import listings
from . import mailparse

log = logging.getLogger("mplabel.backfill")

# Cap on messages fetched per run, so a first pass over a decade-old
# mailbox does not run for an hour or trip Gmail's throttling.
BATCH = 200


def _connect(cfg):
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]))
    imap.login(cfg["imap_user"], cfg["imap_password"])
    return imap


def _search_all(imap, folder, since=None):
    """Every Facebook message, read or unread. Gmail's X-GM-RAW is far
    better at this than plain IMAP SEARCH, so use it when available."""
    imap.select(folder, readonly=True)
    queries = [
        '(X-GM-RAW "from:(facebookmail.com OR marketplace.facebook.com)")',
        '(OR (FROM "facebookmail.com") (FROM "marketplace.facebook.com"))',
    ]
    for q in queries:
        try:
            if since:
                q = q[:-1] + f' SINCE {since})' if q.startswith("(OR") else q
            typ, data = imap.search(None, q)
            if typ == "OK" and data and data[0]:
                return data[0].split()
        except imaplib.IMAP4.error:
            continue
    return []


def _headers_only(imap, nums):
    """Fetch just headers for the survey pass - much faster than RFC822."""
    out = []
    for i in range(0, len(nums), 100):
        chunk = b",".join(nums[i:i + 100])
        typ, data = imap.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")
        if typ != "OK":
            continue
        for part in data:
            if isinstance(part, tuple) and part[1]:
                out.append(email.message_from_bytes(part[1]))
    return out


def scan(cfg, limit=2000):
    """Survey the mailbox without changing anything."""
    imap = _connect(cfg)
    try:
        nums = _search_all(imap, cfg["imap_folder"])
        print(f"{len(nums)} Facebook message(s) in {cfg['imap_folder']}")
        if not nums:
            print("\nNothing found. If her Facebook mail is filtered into a "
                  "label rather than the inbox, set imap_folder to that "
                  "label name.")
            return
        nums = nums[-limit:]
        msgs = _headers_only(imap, nums)
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()

    known, unknown = Counter(), Counter()
    for m in msgs:
        subj = mailparse._decode(m.get("Subject"))
        kind = listings.classify(subj)
        if kind:
            known[kind] += 1
        else:
            # Collapse the variable part so the histogram stays readable.
            generic = re.sub(r"[\"'\u201c\u201d].*?[\"'\u201c\u201d]", '"..."', subj)
            generic = re.sub(r"\s+", " ", generic).strip()[:70]
            unknown[generic] += 1

    print("\n=== recognised ===")
    for kind, n in known.most_common():
        print(f"  {n:>5}  {kind}")
    if not known:
        print("  none")

    print("\n=== unrecognised subjects ===")
    for subj, n in unknown.most_common(25):
        print(f"  {n:>5}  {subj}")
    if unknown:
        print("\nIf any of those are listing/sale/inquiry notifications, add "
              "a pattern for them to EVENT_PATTERNS in listings.py - that is "
              "how the backfill learns them.")


def run(cfg, conn, limit=None, resume=True):
    """Parse Facebook mail into mail_events, then rebuild listings."""
    conn.executescript(listings.SCHEMA)

    seen = set()
    if resume:
        seen = {r[0] for r in conn.execute("SELECT message_id FROM mail_events")}

    imap = _connect(cfg)
    added = skipped = unmatched = 0
    try:
        nums = _search_all(imap, cfg["imap_folder"])
        if limit:
            nums = nums[-limit:]
        log.info("%d Facebook message(s) to consider", len(nums))

        for i in range(0, len(nums), BATCH):
            chunk = nums[i:i + BATCH]
            for num in chunk:
                typ, raw = imap.fetch(num, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                # The server-side search is by From domain, but X-GM-RAW
                # and IMAP SEARCH both match loosely. Verify per message.
                if not mailparse.is_from_facebook(msg):
                    unmatched += 1
                    continue
                mid = mailparse._decode(msg.get("Message-ID"))
                if mid and mid in seen:
                    skipped += 1
                    continue

                subject = mailparse._decode(msg.get("Subject"))
                kind = listings.classify(subject)
                if not kind:
                    unmatched += 1
                    continue

                try:
                    occurred = parsedate_to_datetime(msg.get("Date")).isoformat()
                except Exception:
                    occurred = None

                parsed = mailparse.parse(msg)
                # Her own purchases carry the *seller's* listing id. Drop it
                # rather than record it: everything downstream treats a
                # listing id as one of her listings, so keeping it would
                # invent rows for items that were never for sale.
                buyer_side = kind in listings.BUYER_KINDS
                listings.record_event(
                    conn, mid, occurred, kind, subject,
                    listing_id=None if buyer_side else parsed.get("listing_id"),
                    amount=parsed.get("price"),
                    counterparty=parsed.get("buyer"))
                # The title is worth capturing on any event type, not just
                # sales - it is how an unsold listing gets a name.
                if not buyer_side and parsed.get("listing_id"):
                    listings.upsert_listing(
                        conn, parsed["listing_id"], "email",
                        title=parsed.get("item"), price=parsed.get("price"))
                added += 1
                if mid:
                    seen.add(mid)
            conn.commit()
            log.info("  %d/%d processed", min(i + BATCH, len(nums)), len(nums))
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()

    conn.commit()
    listings.refresh(conn)

    stats = dict(conn.execute(
        "SELECT state, COUNT(*) FROM listings GROUP BY state").fetchall())
    log.info("added %d event(s), skipped %d already seen, %d unmatched subjects",
             added, skipped, unmatched)
    log.info("listings now: %s", stats or "none")
    if unmatched:
        log.info("run `scan` to see the unmatched subject lines")
    return added
