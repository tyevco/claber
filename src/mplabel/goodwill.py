"""
goodwill.py - ShopGoodwill auction mail -> inventory she already owns.

The other half of the mailbox. Facebook mail says what *sold*;
ShopGoodwill mail says what she *bought*, which until now was the half
nobody could automate: `listings.paid` had a schema and a phone screen
and nothing that filled it without a person typing, so every margin in
`v_listing_perf` was null on anything she did not triage by hand.

An auction purchase is the one sourcing event that arrives as a
document. Two mails per item, and they mean different things:

    "You Were Awarded The Winning Bid!"   she owes for it
    "Online Payment Received"             she paid for it, and how much

Both are parsed; only the second one is money. A win is a commitment -
payment is due within seven days and she has not made it yet - so the
win creates the thing on the shelf and leaves `paid` null, and the
payment mail is what fills it. Either can arrive first and either can be
missing; both key on the ShopGoodwill item number, so they land on one
row whichever way round they turn up.

WHAT `paid` MEANS HERE, because it is the whole point of the module.
The payment mail itemises four numbers: item subtotal, sales tax,
shipping and handling, and the order total. What left her account is the
order total. On an $8.99 teapot the shipping was $9.41 - so recording
the hammer price as the cost would report less than half of what the
object really cost, and every margin computed from it would be wrong by
more than 100%. `paid` is therefore the *landed* cost.

That is only unambiguous when the order holds one line. An order with
three items has one shipping charge and one tax line covering all three,
and splitting them - pro rata by price, by weight, evenly - is arithmetic
this system would be inventing. Same rule as `estimate_postage`: a
derived figure that gets written down is indistinguishable from a
measured one a week later. So a multi-item order attributes each item its
own price and leaves the rest as the trip's `unassigned`, which is
already the number the triage screen exists to chase and is the one
question only she can answer.

The order itself becomes a `trip`. That is not a stretch of the word: a
trip is "a shop, a day, and what the receipt came to", and an order is
exactly that with the shop online. Reusing it is what makes the money
reconcile through machinery that already works, rather than giving cost
basis a second place to live.

stdlib only, like `mailparse` and `savedpage` - the Pi dependency list
stays short.
"""

import re
from datetime import datetime
from email.utils import parsedate_to_datetime

from . import listings as listings_mod
from . import mailparse

# Both mails come from ShopGoodwill, but not from the same host: the win
# is `no-reply@shopgoodwill.com` and the payment receipt is
# `no-reply@txemail.shopgoodwill.com`, which is their transactional
# sender. Matching the registrable domain with a boundary covers both -
# and, exactly as with Facebook, `shopgoodwill.com.example.net` is not
# ShopGoodwill.
SENDER_DOMAINS = ("shopgoodwill.com",)

# The item number is minted by ShopGoodwill, so unlike a saved-page
# listing there is a real key to point at. It is namespaced because it
# shares a shape with a Facebook listing id - both are nine-ish digit
# strings - and an unprefixed collision would silently merge two
# different objects. `title_key` sets the precedent with `saved:`.
KEY_PREFIX = "goodwill:"

# Bought, not yet listed. The vocabulary had no word for this because
# until now nothing entered the database before it was for sale: a row
# was born when Facebook mentioned it, by which time it was live. An
# auction win arrives weeks earlier.
#
# It is a state rather than a null `listed_at` because `v_price_band`
# measures sell-through as sold-over-COUNT(*), so a box of things she
# has bought and not yet photographed would go straight into the
# denominator and make sell-through fall every time she wins an auction.
ACQUIRED = "acquired"

# Every ShopGoodwill subject seen in the real mailbox. The first five
# came out of one `mplabel scan`, and the survey is why they are here
# rather than guessed: the two this module was built for were 11 of the
# messages, and the other ~60 were being reported as unrecognised.
#
# Order matters only in that `classify` returns the first match; these
# do not overlap.
SUBJECT_PATTERNS = [
    ("goodwill_won",       r"you were awarded the winning bid"),
    # Buy It Now, which is a second way she acquires things and was
    # invisible until the survey. The win mail's own seller message
    # mentions BIN sales, so this was always there to be found.
    ("goodwill_bought",    r"buy now confirmation"),
    ("goodwill_paid",      r"online payment received"),
    # Money going back the other way. Nine of them in one survey, which
    # is the finding: a refund makes a recorded cost basis wrong, and
    # nothing here can act on one yet - see `import_order`.
    ("goodwill_refund",    r"refund issued"),
    ("goodwill_retracted", r"bid has been retracted"),
    # Her own customer-service correspondence - "broken item", "missing
    # part", "please cancel the order". Thirty-odd messages, which is
    # what was drowning the survey. Classified so `scan` stays readable
    # and recorded so the history is complete; nothing reads them.
    ("goodwill_ticket",    r"ticket id\s*#"),
    ("goodwill_reminder",  r"payment reminder"),
]

# The kinds that mean a thing is now hers. Both are commitments rather
# than money: BIN is bought-then-paid exactly as an auction is
# won-then-paid, so `goodwill_paid` is still the only mail that fills a
# cost.
ACQUISITION_KINDS = frozenset({"goodwill_won", "goodwill_bought"})

# The kinds that carry an order and its items. Everything else is
# recorded as an event and touches no listing - deliberately, because
# a ticket body can quote an item number and a refund body probably
# names one, and either would otherwise mint inventory out of
# correspondence.
ORDER_KINDS = ACQUISITION_KINDS | {"goodwill_paid"}


def is_from_goodwill(msg):
    """True only if the message really came from ShopGoodwill.

    Same reasoning as `mailparse.is_from_facebook`, and the same teeth:
    an IMAP `FROM` search matches the header as text, so a display name
    is enough to get a message fetched. What is downstream of this one
    is money - a spoofed payment receipt would write a cost basis for an
    object that does not exist - so the address domain is matched with a
    boundary rather than as a substring."""
    header = (mailparse._decode(msg.get("From")) + " "
              + mailparse._decode(msg.get("Reply-To"))).lower()
    domains = re.findall(r"[^\s<>@]+@([a-z0-9.\-]+)", header)
    return any(d == known or d.endswith("." + known)
               for d in domains for known in SENDER_DOMAINS)


def classify(subject):
    """Return the event kind for a ShopGoodwill subject, or None.

    Deliberately separate from `listings.classify`: that one is asked
    about Facebook mail and answering "sold" to a ShopGoodwill subject
    would put one of her purchases into the sell-through numerator."""
    s = (subject or "").lower()
    for kind, pattern in SUBJECT_PATTERNS:
        if re.search(pattern, s):
            return kind
    return None


def _money(text):
    """A dollar figure out of a label's value, or None."""
    if text is None:
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", str(text))
    return float(m.group(1).replace(",", "")) if m else None


def _us_date(text):
    """'9/9/2026' -> '2026-09-09'. Their own field, in their own format."""
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", str(text or ""))
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _labelled(blocks):
    """Fold `Label: value` blocks into a dict, lower-cased keys.

    Every field in the payment mail is a `<strong>Label:</strong> value`
    pair, and the block extractor hands each one over whole because the
    `<strong>` is inside the same element as its value."""
    out = {}
    for blk in blocks:
        m = re.match(r"^([A-Za-z][A-Za-z '()/]{1,40}?)\s*:\s*(.+)$", blk)
        if m:
            out.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return out


def _items_from_blocks(blocks):
    """Every item line in an order.

    Anchored on `Item: <number>` rather than on position, because the
    number of items in an order is not fixed and the surrounding table
    is a marketing template that will be redesigned. The title is the
    nearest preceding block that is not itself a label - it sits in the
    same cell, one `<br>` above.

    `Item Subtotal:` does not match: the pattern requires the whole
    block to be `Item:` followed by digits and nothing else."""
    items = []
    for i, blk in enumerate(blocks):
        m = re.match(r"^item\s*:\s*(\d{4,})$", blk, re.I)
        if not m:
            continue
        item = {"item_id": m.group(1), "title": None,
                "price": None, "quantity": None}
        for nxt in blocks[i + 1:i + 4]:
            m2 = re.match(r"^(price|quantity)\s*:\s*(.+)$", nxt, re.I)
            if not m2:
                break
            if m2.group(1).lower() == "price":
                item["price"] = _money(m2.group(2))
            else:
                digits = re.search(r"\d+", m2.group(2))
                item["quantity"] = int(digits.group()) if digits else None
        for prev in range(i - 1, max(-1, i - 4), -1):
            cand = blocks[prev]
            if re.match(r"^[A-Za-z][A-Za-z '()/]{1,40}\s*:", cand):
                continue        # another label, not a title
            if 3 < len(cand) < 300:
                item["title"] = cand
                break
        items.append(item)
    return items


def parse(msg):
    """Everything a ShopGoodwill mail can tell us, or {} if it is not one.

    Every kind shares a return shape so a caller does not have to branch
    before it knows what it has: `kind` says which fields to expect, and
    the money fields are simply absent on a win.

    Only the order kinds are looked in for items. That is a guard, not
    an optimisation: a return ticket's body quotes the item it is about,
    and a refund's almost certainly names one, so running the item
    extractor over them would mint inventory out of correspondence -
    rows for things she is trying to send *back*."""
    kind = classify(mailparse._decode(msg.get("Subject")))
    if not kind:
        return {}

    blocks, _links = mailparse.body_blocks(msg)
    flat = " ".join(blocks)
    out = {"kind": kind,
           "subject": mailparse._decode(msg.get("Subject")),
           "message_id": mailparse._decode(msg.get("Message-ID"))}

    try:
        received = parsedate_to_datetime(msg.get("Date"))
    except Exception:
        received = None
    if received:
        out["received_at"] = received.isoformat()

    if kind not in ORDER_KINDS:
        # A refund, a retracted bid, a ticket update, a payment
        # reminder. Recorded so the history is complete and so `scan`
        # stops calling them unrecognised; read for nothing but the
        # labels they might carry, because what these mean has never
        # been seen - only their subject lines have.
        fields = _labelled(blocks)
        out["order_id"] = fields.get("order number")
        out["total"] = _money(fields.get("refund amount")
                              or fields.get("order total"))
        return out

    if kind == "goodwill_won":
        # "...winning bid at ShopGoodwill.com for item #911100022 -
        #  Pair Of Painted Tin Toy Banks 1930s.!"
        #
        # Matched against one block rather than the flattened body,
        # because the only thing marking the end of the title is the
        # `<br>` after it. Flattened, `(.+)$` runs happily to the end of
        # the bidder agreement and the whole email becomes the title.
        #
        # The trailing "!" belongs to the template, not to the title -
        # but only one of them does: a title that ends in a full stop
        # arrives as "...Albums.!" and keeping the stop is right.
        title_re = re.compile(r"for item\s*#?\s*(\d{4,})\s*[-\u2013]\s*(.+)$")
        price = None
        for blk in blocks:
            m = title_re.search(blk)
            if m and "items" not in out:
                out["items"] = [{
                    "item_id": m.group(1),
                    "title": re.sub(r"!\s*$", "", m.group(2)).strip(),
                    "price": None, "quantity": 1}]
            m = re.search(r"final price is\s*:?\s*(\$[\d,.]+)", blk, re.I)
            if m and price is None:
                price = _money(m.group(1))
            m = re.match(r"The Seller of this Item\s+(.+)$", blk)
            if m and "seller" not in out:
                out["seller"] = m.group(1).strip()
        if out.get("items") and price is not None:
            out["items"][0]["price"] = price
        return out

    fields = _labelled(blocks)
    if kind == "goodwill_bought":
        # ASSUMED. A Buy Now confirmation has never been read - two of
        # them turned up in the survey and that is all that is known. It
        # is treated as an acquisition because BIN is bought-then-paid
        # the way an auction is won-then-paid, and it is parsed with the
        # *payment* mail's label extractor on the guess that ShopGoodwill
        # reuses its own template. If that guess is wrong there are no
        # items, and `import_order` records the event and creates
        # nothing rather than inventing a row. `mplabel goodwill <eml>`
        # is how to settle it against a real one.
        out["items"] = _items_from_blocks(blocks)
        out["order_id"] = fields.get("order number")
        out["seller"] = fields.get("goodwill name")
        return out

    out["items"] = _items_from_blocks(blocks)
    out["order_id"] = fields.get("order number")
    out["seller"] = fields.get("goodwill name")
    out["seller_city"] = fields.get("city")
    out["subtotal"] = _money(fields.get("item subtotal"))
    out["tax"] = _money(fields.get("sales tax (if applicable)"))
    out["shipping"] = _money(fields.get("shipping and handling"))
    out["total"] = _money(fields.get("order total"))
    # Their own words for when the money moved. Falls back to the
    # message date, which is within minutes of it.
    out["paid_on"] = _us_date(fields.get("payment was received on")) or (
        received.date().isoformat() if received else None)
    return out


def landed_cost(order):
    """What each item in a paid order is judged to have cost.

    Returns {item_id: dollars}. The whole order total goes on the item
    when the order holds exactly one of them, because then there is
    nothing to divide and the tax and the shipping are unarguably part
    of what that object cost. With more than one line the split is
    unknowable, so each item carries its own price and the remainder
    stays visible as the trip's unassigned money rather than being
    quietly apportioned - see the module docstring.

    A line with a quantity above one is treated as several objects under
    one price and therefore as the ambiguous case, even when it is the
    only line: the total covers all of them and which one is on the
    shelf is not a question this can answer."""
    items = [i for i in order.get("items") or [] if i.get("item_id")]
    total = order.get("total")
    if len(items) == 1 and total is not None \
            and (items[0].get("quantity") or 1) == 1:
        return {items[0]["item_id"]: round(total, 2)}
    return {i["item_id"]: i.get("price") for i in items}


def store_name(order):
    """What to call the shop on a trip.

    The selling Goodwill is a different organisation each time - they
    are autonomous members, with their own postage and their own return
    policy - so the name is worth keeping rather than flattening every
    order into one "ShopGoodwill". Prefixed so a row is recognisable as
    an online order beside `GOODWILL ON MAIN ST`."""
    seller = (order.get("seller") or "").strip()
    return f"ShopGoodwill - {seller}" if seller else "ShopGoodwill"


def import_mail(conn, msg):
    """Record one ShopGoodwill mail. Returns a summary, or None.

    The gate is the sender domain, not the subject: what this writes is
    money against an object, and a subject line is something anyone can
    type."""
    if not is_from_goodwill(msg):
        return None
    order = parse(msg)
    if not order:
        return None
    return import_order(conn, order)


def import_order(conn, order):
    """Fold a parsed ShopGoodwill mail into listings, trips and events.

    Idempotent through `mail_events.message_id`, which is UNIQUE, and
    through `upsert_listing`, which fills blanks rather than overwriting
    - so a re-run of `backfill` cannot double-count an order, and a cost
    she corrected by hand survives the next import. The mail arrives
    once; her correction is the later observation and the more likely to
    be right.

    **The event is recorded first and unconditionally.** This used to
    return early when a mail carried no items, which was fine while the
    only two kinds both did - and became a real problem the moment a
    mailbox survey found sixty that do not. `backfill` marks a message
    seen on a truthy return, so a ticket update was re-fetched on every
    single run, for ever, and counted as unmatched each time."""
    kind = order.get("kind")
    items = ([i for i in order.get("items") or [] if i.get("item_id")]
             if kind in ORDER_KINDS else [])
    occurred = order.get("paid_on") or order.get("received_at")
    # What the mail said the money was: the order total once it is paid,
    # the hammer price while it is only won. Recorded on the event rather
    # than on the listing because a win is a debt, not a cost - `paid` is
    # what left the account, and on a win nothing has.
    amount = order.get("total")
    if amount is None:
        amount = next((i.get("price") for i in items
                       if i.get("price") is not None), None)
    listings_mod.record_event(
        conn, order.get("message_id"), occurred, kind, order.get("subject"),
        listing_id=None,               # theirs, not hers - see BUYER_KINDS
        amount=amount,
        counterparty=order.get("seller"))

    if not items:
        # A refund, a retracted bid, a ticket, a reminder - or a Buy Now
        # confirmation whose body did not look the way it was guessed to.
        #
        # A refund in particular *should* do something: it makes a
        # recorded `paid` wrong, and nine of them turned up in one
        # survey. It deliberately does not, because correcting a cost
        # basis means knowing which order the money came back on and
        # nothing here has ever read one of those bodies. Writing that
        # from the subject line would be inventing the link, and the
        # failure would be silent and in the direction that flatters the
        # margins.
        conn.commit()
        return {"kind": kind, "order_id": order.get("order_id"),
                "trip_id": None, "listing_ids": [],
                "total": order.get("total")}

    trip_id = None
    if kind == "goodwill_paid":
        trip_id = _trip_for_order(conn, order)

    costs = landed_cost(order) if kind == "goodwill_paid" else {}
    added = []
    for item in items:
        key = KEY_PREFIX + item["item_id"]
        listings_mod.upsert_listing(
            conn, key, "goodwill",
            title=item.get("title"),
            paid=costs.get(item["item_id"]),
            trip_id=trip_id,
            state=ACQUIRED,
            notes=_note(order, item))
        added.append(key)

    # Minted now rather than at the next refresh, for the same reason
    # `create_item` mints one: the thing is about to turn up in a parcel
    # and the label maker is the next step.
    from . import cli as cli_mod
    cli_mod.ensure_inventory_codes(conn)
    conn.commit()
    return {"kind": kind, "order_id": order.get("order_id"),
            "trip_id": trip_id, "listing_ids": added,
            "total": order.get("total")}


def _note(order, item):
    """A one-line provenance stamp, so a row says where it came from."""
    bits = ["ShopGoodwill item " + item["item_id"]]
    if order.get("order_id"):
        bits.append("order " + order["order_id"])
    if order.get("seller"):
        bits.append(order["seller"])
    return "; ".join(bits)


def _trip_for_order(conn, order):
    """The trip for this order, created once.

    Keyed on the order number rather than on the day: she pays for
    several auctions in one sitting, each with its own shipping charge
    and its own selling Goodwill, and rolling them into one trip would
    lose exactly the numbers this exists to keep. Matched through the
    notes stamp because `trips` has no natural place for a foreign
    order number and inventing a column for one importer is how a
    schema stops being about the business."""
    stamp = _order_stamp(order.get("order_id"))
    if stamp:
        row = conn.execute("SELECT id FROM trips WHERE notes LIKE ?",
                           (f"%{stamp}%",)).fetchone()
        if row:
            return row["id"]
    trip = listings_mod.create_trip(
        conn,
        store=store_name(order),
        occurred_at=order.get("paid_on"),
        receipt_total=order.get("total"),
        notes=stamp or "ShopGoodwill order")
    return trip["id"]


def _order_stamp(order_id):
    return f"ShopGoodwill order {order_id}" if order_id else None
