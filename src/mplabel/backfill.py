r"""
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

# What `scan` looks for, which is wider than what `run` imports. eBay is
# in here and not in `cli.MAIL_DOMAINS` because the survey's job is to
# say what is in the mailbox - including the mail nothing can act on -
# while the import's job is to act, and fetching a sender with no
# classifier behind it would count every message as unmatched and do it
# again next run.
#
# It is also the only way to check the one inference the eBay label path
# rests on: that her label mail comes from a domain under `ebay.com`. If
# that is wrong, nothing prints and nothing says why.
SURVEY_DOMAINS = (tuple(mailparse.SENDER_DOMAINS)
                  + tuple(goodwill.SENDER_DOMAINS)
                  + tuple(mailparse.EBAY_SENDER_DOMAINS))


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
    r"""Where deleted mail waits to be purged.

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
    r"""Every mailbox worth walking, in order, deduplicated.

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


def _search_all(imap, folder, since=None, domains=None):
    """Every message from a sender we parse, read or unread.

    Both halves of the mailbox: Facebook for what she sold, ShopGoodwill
    for what she bought. Gmail's X-GM-RAW is far better at this than
    plain IMAP SEARCH, so use it when available - and note the plain
    fallback has to nest its ORs, because IMAP's OR takes exactly two
    arguments and a server rejects the whole search otherwise.

    `domains` widens that for the survey, which asks a different question
    from the import. `scan` wants to know what is *there*, eBay included;
    `run` wants the mail it can act on, and fetching a sender it has no
    classifier for would pull hundreds of messages down by RFC822, count
    every one as unmatched, and do it again on the next run."""
    from . import cli

    open_folder(imap, folder)
    domains = tuple(domains or cli.MAIL_DOMAINS)
    doms = " OR ".join(domains)
    queries = [
        f'(X-GM-RAW "from:({doms})")',
        f'({cli.imap_or_from(domains)})',
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
    """Fetch just headers for the survey pass - much faster than RFC822.

    FROM and REPLY-TO are in the list because a subject on its own stopped
    being enough the moment there were three senders. A real scan came
    back with thirty-one `has been listed` notifications and no way to
    say whether they were Facebook's `listed` event - a pattern that has
    been in `EVENT_PATTERNS` and never matched anything - or eBay's, and
    the two want opposite handling. An instrument that cannot attribute
    its reading is the same failure as one that stops at 32 dots."""
    out = []
    for i in range(0, len(nums), 100):
        chunk = b",".join(nums[i:i + 100])
        typ, data = imap.fetch(
            chunk, "(BODY.PEEK[HEADER.FIELDS "
                   "(SUBJECT DATE MESSAGE-ID FROM REPLY-TO)])")
        if typ != "OK":
            continue
        for part in data:
            if isinstance(part, tuple) and part[1]:
                out.append(email.message_from_bytes(part[1]))
    return out


def generic_subject(subject):
    """Collapse the variable parts so the histogram stays readable."""
    generic = re.sub(r"[\"'\u201c\u201d].*?[\"'\u201c\u201d]", '"..."',
                     subject)
    # Long digit runs are the other variable part, and the one that
    # actually bit: a real survey came back with twenty
    # `Ticket ID # 9333904 Updated - ...` lines, each its own subject,
    # which ate most of the list and pushed whole families below the
    # cut. Quoting was the only thing being collapsed and nothing here
    # is quoted.
    generic = re.sub(r"\d{4,}", "#", generic)
    # A truncated title is the third variable part, and the one eBay
    # uses for nearly everything: it cuts the item name and ends it with
    # an ellipsis, so `Vintage...`, `Vintage #...`, `Antique...` and
    # `Andrea by Sadek...` are four lines of a histogram describing one
    # notification. A real scan spent eight of its twenty-five rows on
    # that single family and hid fifty-four other subjects behind them.
    #
    # The span is bounded at a colon so a label keeps its own words -
    # `Order update: ...` and `You have a new offer: ...` stay apart,
    # which is the whole point of reading the list.
    # Starts at a word character or a `$`, so the emoji eBay prefixes
    # these with survives - it is half of what identifies the family.
    generic = re.sub(r"[\w$][^:]*?(?:\.\.\.|\u2026)", "\u2026", generic)
    return re.sub(r"\s+", " ", generic).strip()[:70]


def classify_for(who, subject):
    """Ask the classifier that belongs to the sender, and only that one.

    The three are separate on purpose - one classifier answering "sold"
    to a ShopGoodwill subject puts one of her own purchases into the
    sell-through numerator - and until the survey fetched FROM it had to
    try all three and take the first answer, which is the merge it was
    trying to avoid, one layer out. An unknown sender still tries all
    three, because a wrong guess there is a line in a report rather than
    a row in a table."""
    if who == "facebook":
        return listings.classify(subject)
    if who == "shopgoodwill":
        return goodwill.classify(subject)
    if who == "ebay":
        return mailparse.classify_ebay(subject)
    return (listings.classify(subject) or goodwill.classify(subject)
            or mailparse.classify_ebay(subject))


def sender_of(msg):
    """Which of the three this came from, by address domain.

    The same predicates the importers gate on rather than a fourth
    reading of the From header - a survey that disagrees with the poller
    about who sent something is worse than one that cannot tell."""
    if mailparse.is_from_facebook(msg):
        return "facebook"
    if goodwill.is_from_goodwill(msg):
        return "shopgoodwill"
    if mailparse.is_from_ebay(msg):
        return "ebay"
    return "?"


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
            # eBay as well, and only here. The whole eBay path rests on
            # one inference - that her label mail comes from a domain
            # under `ebay.com` - and if that is wrong the symptom is
            # silent: nothing prints and nothing says why. A survey is
            # the cheap place to find out, because it changes nothing.
            nums = _search_all(imap, folder, domains=SURVEY_DOMAINS)
            found.append((folder, len(nums)))
            if nums:
                msgs.extend(_headers_only(imap, nums[-limit:]))
        for folder, n in found:
            note = "  <- deleted; Gmail purges this after 30 days" \
                if folder == trash and n else ""
            print(f"{n} Facebook/ShopGoodwill/eBay message(s) "
                  f"in {folder}{note}")
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

    from_sender = Counter()
    known, unknown = Counter(), Counter()
    for m in msgs:
        subj = mailparse._decode(m.get("Subject"))
        who = sender_of(m)
        from_sender[who] += 1
        kind = classify_for(who, subj)
        if kind:
            known[(who, kind)] += 1
        else:
            unknown[(who, generic_subject(subj))] += 1

    print("\n=== who sent them ===")
    for who, n in from_sender.most_common():
        print(f"  {n:>5}  {who}")

    print("\n=== recognised ===")
    for (who, kind), n in known.most_common():
        print(f"  {n:>5}  {who:<13}{kind}")
    if not known:
        print("  none")

    # Grouped by sender and capped *per sender*, because the cut is what
    # keeps going wrong. A flat top 25 let one noisy family eat the list
    # once already, and with three senders it would happily spend the
    # whole budget on the loudest one while the sender whose shapes
    # nobody has read printed nothing at all.
    print("\n=== unrecognised subjects ===")
    if not unknown:
        print("  none")
    per_sender = 12
    for who, _n in from_sender.most_common():
        rows = [(subj, n) for (w, subj), n in unknown.items() if w == who]
        if not rows:
            continue
        rows.sort(key=lambda r: (-r[1], r[0]))
        print(f"\n  -- {who} --")
        for subj, n in rows[:per_sender]:
            print(f"  {n:>5}  {subj}")
        if len(rows) > per_sender:
            hidden = sum(n for _s, n in rows[per_sender:])
            print(f"  ... and {len(rows) - per_sender} more distinct "
                  f"subject(s) from {who}, {hidden} message(s) in total.")

    if unknown:
        print("\nWhere a pattern goes depends on who sent it, which is why "
              "the sender is printed beside every line. A Facebook "
              "listing/sale/inquiry notification goes in EVENT_PATTERNS in "
              "listings.py; an auction subject goes in SUBJECT_PATTERNS in "
              "goodwill.py. Guessing from the subject alone is how a "
              "Facebook `listed` pattern and an eBay one get confused for "
              "each other - they read almost identically and want "
              "opposite handling.")
        print("An eBay subject is a different question again: the only one "
              "with a printer behind it is 'Your shipping label is ready'. "
              "The rest are listed so it is visible what eBay actually "
              "sends rather than assumed, and nothing reads their bodies.")


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
