"""
backfill.py - walk the whole mailbox once and reconstruct her history
from every Facebook Marketplace and ShopGoodwill email she has received.

**The whole mailbox, which is not the inbox.** `survey_folders` prefers
the `\All` special-use mailbox - Gmail's archive - over the configured
`imap_folder`, and the reason is a real survey: INBOX held 99 messages
and not one of them was a win or a payment receipt, the two kinds the
auction importer exists for. They had been archived. Walking the inbox
would have imported no acquisitions and no costs at all, and said
nothing was wrong.

**And Trash, because she deletes this mail often.** `\All` excludes it,
so the archive alone still misses purchases. A deleted receipt does not
undo the purchase it recorded - the money left her account either way.
Gmail empties Trash after 30 days, which makes this a *rescue window*
rather than a second archive, and means "reconstruct her history" is
only true of the history that has not been purged yet.

**This is the half that may read the bin, and the poller is not.**
Nothing here prints: it records events and fills in listings. The poller
prints, and reprocessing a label email she deleted could put a parcel
back on the printer - so `poll_once` stays on the configured folder
deliberately. The asymmetry is the safety property, not an oversight.

This is the only way to get a listing catalogue without an API. It gives
you listing dates, sale dates, inquiry counts and payouts going back as
far as her mail does - and, since the ShopGoodwill importer landed, what
the things cost, which is the half no amount of Facebook mail can say.

Two commands matter:

    mplabel.py scan       report what is in the mailbox, change nothing
    mplabel.py backfill   parse it into the listings table

Run `scan` first. It prints a histogram of subject lines and, crucially,
lists the ones no pattern matched. Facebook's wording varies by locale
and changes over time, so rather than guess the subjects once and let it
rot, add what you actually see to EVENT_PATTERNS in listings.py -
SUBJECT_PATTERNS in goodwill.py for the auction side.

The two senders are handled by different code on purpose. Facebook mail
is about things she is selling and reconciles through `mail_events`;
ShopGoodwill mail is about things she has bought and writes a cost
straight onto a listing. Asking one classifier about both is how a
purchase ends up in the sell-through numerator.
"""

import email
import imaplib
import logging
import re
from collections import Counter
from email.utils import parsedate_to_datetime

from . import goodwill
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


def _special_folder(imap, flag):
    """The mailbox carrying an RFC 6154 special-use flag, or None.

    Asked for by the flag rather than by name: Gmail calls these
    `[Gmail]/All Mail` and `[Gmail]/Trash` in English and something
    else in every other locale, and a hardcoded name would silently
    find nothing and fall back without saying so."""
    try:
        typ, data = imap.list()
    except imaplib.IMAP4.error:
        return None
    if typ != "OK" or not data:
        return None
    for line in data:
        if isinstance(line, tuple):
            line = line[0]
        if not line:
            continue
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        flags = text[1:text.index(")")] if ")" in text else ""
        if flag not in flags:
            continue
        # `(\HasNoChildren \All) "/" "[Gmail]/All Mail"` - the name is
        # the last field, quoted when it has a space in it, which this
        # one does.
        rest = text[text.index(")") + 1:].strip()
        parts = rest.split(" ", 1)
        name = parts[1].strip() if len(parts) > 1 else rest
        return name[1:-1] if name.startswith('"') and name.endswith('"') else name
    return None


def _all_mail_folder(imap):
    """Everything, archived included - but **not** deleted."""
    return _special_folder(imap, "\\All")


def _trash_folder(imap):
    """Where deleted mail waits to be purged.

    Gmail's `\All` mailbox deliberately excludes Trash, and she deletes
    ShopGoodwill mail often - so the archive alone still misses
    purchases. Gmail empties Trash after 30 days, which makes this a
    rescue window rather than a second archive: what is in there now is
    recoverable and what was in there last month is gone."""
    return _special_folder(imap, "\\Trash")


def survey_folder(imap, cfg):
    """Which mailbox to walk, and why.

    **Not the configured `imap_folder`, where that is the inbox.** A
    real `mplabel scan` against INBOX found 99 messages and *none* of
    them were a win or a payment receipt - the two kinds this whole
    importer is built on - while turning up 47 return tickets and 9
    refunds. Those mails exist; they had been archived. So `backfill`,
    whose own first line says it walks the whole mailbox once, would
    have imported zero acquisitions and zero costs from that account
    and reported success.

    A configured folder that is *not* the inbox is a deliberate answer
    to "her mail is filtered into a label", so it wins."""
    configured = cfg.get("imap_folder") or "INBOX"
    if configured.upper() != "INBOX":
        return configured
    return _all_mail_folder(imap) or configured


def survey_folders(imap, cfg):
    """Every mailbox worth walking, in order, deduplicated.

    **Trash is on this list, and that is the point.** She deletes
    ShopGoodwill mail often, and a deleted receipt does not undo the
    purchase it recorded - the money left her account either way, and
    the cost basis is still true. Gmail's `\All` mailbox excludes
    Trash, so walking the archive alone still misses whatever she
    cleared out.

    Message numbers are per-mailbox, so these are walked one at a time
    rather than searched together; `mail_events.message_id` is what
    stops a message counted twice.

    The poller deliberately does *not* use this. Reprocessing a message
    she put in the bin is the opposite of what deleting it meant, and
    the poller is about what happens next rather than what already
    did."""
    folders = [survey_folder(imap, cfg)]
    trash = _trash_folder(imap)
    if trash and trash not in folders:
        folders.append(trash)
    return folders


def quote_mailbox(name):
    """A mailbox name as IMAP wants it, quoted when it has to be.

    **imaplib does not do this for you.** `select` passes the name
    straight into the command line, so `EXAMINE [Gmail]/All Mail` goes
    out as two arguments and Gmail answers `BAD Could not parse
    command`. That shipped the moment the survey started preferring the
    archive, because every mailbox this had ever selected - `INBOX`, a
    label she typed - happened to be one word.

    Quoted only when it needs to be, so the names that already worked
    go out byte-identical."""
    if name.startswith('"') and name.endswith('"'):
        return name
    if re.search(r'[\s(){%*"\\]', name):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


def open_folder(imap, preferred, fallback="INBOX"):
    """Select `preferred`, falling back rather than raising.

    A survey must not die because the archive could not be opened - the
    configured folder is still worth walking, and saying which one was
    used is the whole point of naming it. Returns the folder actually
    selected."""
    for name in (preferred, fallback):
        if not name:
            continue
        try:
            typ, _data = imap.select(quote_mailbox(name), readonly=True)
        except imaplib.IMAP4.error as exc:
            log.debug("could not open %s: %s", name, exc)
            continue
        if typ == "OK":
            return name
        log.debug("could not open %s: %s", name, typ)
    raise imaplib.IMAP4.error(
        f"could not open {preferred!r} or {fallback!r}")


def _search_all(imap, folder, since=None):
    """Every message from a sender we parse, read or unread.

    Both halves of the mailbox: Facebook for what she sold, ShopGoodwill
    for what she bought. Gmail's X-GM-RAW is far better at this than
    plain IMAP SEARCH, so use it when available - and note the plain
    fallback has to nest its ORs, because IMAP's OR takes exactly two
    arguments and a server rejects the whole search otherwise."""
    from . import cli

    open_folder(imap, folder)
    doms = " OR ".join(cli.MAIL_DOMAINS)
    queries = [
        f'(X-GM-RAW "from:({doms})")',
        f'({cli.imap_or_from(cli.MAIL_DOMAINS)})',
    ]
    for q in queries:
        try:
            if since and not q.startswith("(X-GM-RAW"):
                q = q[:-1] + f' SINCE {since})'
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
        # Name every folder and count them apart. "99 messages" means
        # one thing out of an inbox and another out of everything she
        # has ever received, and the first survey of this mailbox was
        # read as the second.
        folders = survey_folders(imap, cfg)
        trash = _trash_folder(imap)
        msgs, found = [], []
        for folder in folders:
            nums = _search_all(imap, folder)
            found.append((folder, len(nums)))
            if nums:
                msgs.extend(_headers_only(imap, nums[-limit:]))
        for folder, n in found:
            note = "  <- deleted; Gmail purges this after 30 days" \
                if folder == trash and n else ""
            print(f"{n} Facebook/ShopGoodwill message(s) in {folder}{note}")
        if not msgs:
            print("\nNothing found. If her Facebook mail is filtered into a "
                  "label rather than the inbox, set imap_folder to that "
                  "label name.")
            return
    finally:
        try:
            imap.close()
        except Exception:
            pass
        imap.logout()

    known, unknown = Counter(), Counter()
    for m in msgs:
        subj = mailparse._decode(m.get("Subject"))
        kind = listings.classify(subj) or goodwill.classify(subj)
        if kind:
            known[kind] += 1
        else:
            # Collapse the variable part so the histogram stays readable.
            generic = re.sub(r"[\"'\u201c\u201d].*?[\"'\u201c\u201d]", '"..."', subj)
            # Long digit runs are the other variable part, and the one
            # that actually bit: a real survey came back with twenty
            # `Ticket ID # 9333904 Updated - ...` lines, each its own
            # subject, which ate most of the list and pushed whole
            # families below the cut. Quoting was the only thing being
            # collapsed and nothing here is quoted.
            generic = re.sub(r"\d{4,}", "#", generic)
            generic = re.sub(r"\s+", " ", generic).strip()[:70]
            unknown[generic] += 1

    print("\n=== recognised ===")
    for kind, n in known.most_common():
        print(f"  {n:>5}  {kind}")
    if not known:
        print("  none")

    print("\n=== unrecognised subjects ===")
    shown = 25
    for subj, n in unknown.most_common(shown):
        print(f"  {n:>5}  {subj}")
    # A gauge has to say when it is off-scale. This printed its top 25
    # and nothing else, so a survey whose first twenty lines were all
    # one noisy family looked like a complete answer and was not.
    if len(unknown) > shown:
        print(f"  ... and {len(unknown) - shown} more distinct subject(s) "
              f"not shown. Classify the noisy ones and run this again - "
              f"they are what is hiding the rest.")
    if unknown:
        print("\nIf any of those are listing/sale/inquiry notifications, add "
              "a pattern for them to EVENT_PATTERNS in listings.py - that is "
              "how the backfill learns them. An auction subject goes in "
              "SUBJECT_PATTERNS in goodwill.py instead.")


def run(cfg, conn, limit=None, resume=True):
    """Parse the mailbox into mail_events and listings, then rebuild."""
    conn.executescript(listings.SCHEMA)

    seen = set()
    if resume:
        seen = {r[0] for r in conn.execute("SELECT message_id FROM mail_events")}

    imap = _connect(cfg)
    added = skipped = unmatched = bought = noted = 0
    try:
        # One folder at a time: a message number means nothing outside
        # the mailbox it was searched in. `mail_events.message_id` is
        # what stops the same message being counted twice.
        work = []
        for folder in survey_folders(imap, cfg):
            nums = _search_all(imap, folder)
            if limit:
                nums = nums[-limit:]
            log.info("%d message(s) to consider in %s", len(nums), folder)
            work.append((folder, nums))

        for folder, nums in work:
            _search_all(imap, folder)   # re-select; the last loop moved on
            for i in range(0, len(nums), BATCH):
                chunk = nums[i:i + BATCH]
                for num in chunk:
                    typ, raw = imap.fetch(num, "(RFC822)")
                    if typ != "OK" or not raw or not raw[0]:
                        continue
                    msg = email.message_from_bytes(raw[0][1])
                    mid = mailparse._decode(msg.get("Message-ID"))
                    if mid and mid in seen:
                        skipped += 1
                        continue

                    # ShopGoodwill first, because it is decided by the sender
                    # and cannot be confused with the seller side. Its own
                    # importer writes the row: a purchase has a cost and no
                    # Facebook listing id, so there is nothing for
                    # `apply_events` to replay it into.
                    if goodwill.is_from_goodwill(msg):
                        try:
                            result = goodwill.import_mail(conn, msg)
                            if result:
                                # A classified mail is recorded either way,
                                # but only some of them are a purchase.
                                # Counting a return ticket as an order would
                                # report sixty acquisitions from a mailbox
                                # that had eleven.
                                if result["listing_ids"]:
                                    bought += 1
                                else:
                                    noted += 1
                                if mid:
                                    seen.add(mid)
                            else:
                                unmatched += 1
                        except Exception:
                            log.exception("could not import ShopGoodwill mail")
                            unmatched += 1
                        continue

                    # The server-side search is by From domain, but X-GM-RAW
                    # and IMAP SEARCH both match loosely. Verify per message.
                    if not mailparse.is_from_facebook(msg):
                        unmatched += 1
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
    log.info("added %d event(s), %d ShopGoodwill order(s), skipped %d "
             "already seen, %d unmatched subjects",
             added + noted, bought, skipped, unmatched)
    log.info("listings now: %s", stats or "none")
    if unmatched:
        log.info("run `scan` to see the unmatched subject lines")
    return added + bought
