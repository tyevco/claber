"""
shopping.py - the aisle, the receipt, and the gap between them.

The flow this serves: she photographs something in a shop, the phone
says what it thinks it is, she puts it in the cart or back on the shelf.
At the till she photographs the receipt. Afterwards the two are laid
side by side and what she actually bought becomes inventory.

**The receipt cannot close that gap on its own, and this module is built
around admitting it.** A thrift receipt itemises by *department* -
`HOUSEWARES 4.99` - so a line is not an object. Four lines and four
things in the cart is not four answers: it is four amounts and four
objects and a question about which is which. Where the departments
happen to be distinct the guess is obvious; where two things came from
HOUSEWARES it is a coin toss, and a coin toss written into `paid` is
indistinguishable from a fact she checked.

So nothing here writes a cost. `propose` returns what it thinks and says
how sure it is; `apply` writes only what a person handed back. That is
the same rule postage follows and for the same reason - an estimate that
can pass for a measurement corrupts every margin downstream, silently,
for ever.

There *is* a receipt-line table now, and CLAUDE.md previously said there
would not be. The reasoning has not changed - a line is still not an
object and a line never becomes inventory by itself. What changed is
that reconciliation needs the lines *as lines*: amounts to attribute,
kept beside the trip, pointing at nothing.
"""

import re
from datetime import datetime

SCHEMA = """
-- Something she pointed the camera at in a shop.
--
-- Not a listing. Most of these never become one: she photographs a
-- thing, the phone offers a title, and she puts it back. Keeping the
-- passed ones is deliberate - the same object turns up again next month
-- and "I saw this and passed on it at $40" is a note to herself that
-- nothing else in the system carries.
--
-- `listing_id` is the one that matters: null means this never became
-- inventory, and its presence is the record that it did. An absence
-- rather than a state column, like everywhere else here.
CREATE TABLE IF NOT EXISTS candidates (
    id          INTEGER PRIMARY KEY,
    trip_id     INTEGER REFERENCES trips(id)    ON DELETE SET NULL,
    photo_id    INTEGER REFERENCES photos(id)   ON DELETE SET NULL,
    -- What the model offered and she kept or edited. Never authoritative:
    -- it is a starting point for the form, not a fact about the object.
    title       TEXT,
    era         TEXT,
    condition   TEXT,
    category    TEXT,
    -- The price on the shelf ticket, if she typed it. Not what she paid:
    -- that comes off the receipt, later, with her help.
    asking      REAL,
    decision    TEXT,          -- carted | passed
    decided_at  TEXT,
    listing_id  INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidate_trip ON candidates(trip_id);

-- The receipt, as lines.
--
-- Stored because reconciliation needs amounts to attribute, and *only*
-- as lines: a line points at no object, becomes no inventory on its own,
-- and is never summed into anything that claims to be a cost. The
-- photograph remains the record; this is a reading of it.
CREATE TABLE IF NOT EXISTS receipt_lines (
    id        INTEGER PRIMARY KEY,
    trip_id   INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    position  INTEGER,
    label     TEXT,
    amount    REAL,
    kind      TEXT           -- item | total | tax | subtotal | ignored
);
CREATE INDEX IF NOT EXISTS idx_receipt_trip ON receipt_lines(trip_id);
"""

DECISIONS = ("carted", "passed")

# What a department on a thrift receipt tends to mean, against the broad
# categories the model offers. Deliberately small and deliberately loose:
# it exists to *order* a proposal, never to settle one. Every shop names
# its departments differently and this is a guess about one of them.
DEPARTMENTS = {
    "HOUSEWARES": ("Home", "Kitchen"),
    "HOUSEWARE": ("Home", "Kitchen"),
    "KITCHEN": ("Home", "Kitchen"),
    "GLASSWARE": ("Home",),
    "FURNITURE": ("Furniture",),
    "ART": ("Art", "Home"),
    "DECOR": ("Home", "Art"),
    "LINENS": ("Home", "Linens"),
    "BEDDING": ("Home", "Linens"),
    "CLOTHING": ("Clothing",),
    "SHOES": ("Clothing",),
    "TOYS": ("Toys",),
    "BOOKS": ("Books",),
    "TOOLS": ("Tools",),
    "ELECTRONICS": ("Electronics",),
}

# Lines that are arithmetic rather than goods.
_TOTAL = re.compile(r"\b(grand\s+)?total\b", re.I)
_SUBTOTAL = re.compile(r"\bsub\s*-?\s*total\b", re.I)
_TAX = re.compile(r"\b(tax|vat|hst|gst)\b", re.I)
_IGNORE = re.compile(
    r"\b(cash|change|card|visa|mastercard|debit|credit|tender|balance|"
    r"account|thank|receipt|store|cashier|register|survey|return)\b", re.I)

# "HOUSEWARES 4.99", "HOUSEWARES  $4.99", "HOUSEWARES....4.99"
_AMOUNT = re.compile(r"(-?\$?\s*\d{1,4}[.,]\d{2})\s*[A-Z]?\s*$")


def parse_receipt(text):
    """Lines out of whatever the OCR produced.

    Tolerant on purpose. This is a photograph of a curling thermal
    receipt taken at a kitchen table, so the text arrives with dropped
    characters, doubled spaces and the occasional invented one. Anything
    that does not end in something money-shaped is skipped rather than
    guessed at, and the caller sees what survived - a reading she can
    correct beats a parse that claims more than it read.
    """
    out, position = [], 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        found = _AMOUNT.search(line)
        if not found:
            continue
        amount = found.group(1).replace("$", "").replace(",", ".")
        amount = amount.replace(" ", "")
        try:
            value = float(amount)
        except ValueError:
            continue
        label = line[:found.start()].strip(" .:\t-")
        if not label:
            label = "(unlabelled)"

        if _SUBTOTAL.search(label):
            kind = "subtotal"
        elif _TOTAL.search(label):
            kind = "total"
        elif _TAX.search(label):
            kind = "tax"
        elif _IGNORE.search(label):
            kind = "ignored"
        else:
            kind = "item"
        position += 1
        out.append({"position": position, "label": label,
                    "amount": round(value, 2), "kind": kind})
    return out


def store_receipt(conn, trip_id, text):
    """Replace what we have read off this trip's receipt.

    Replace, not append: re-reading a receipt is what happens when the
    first photograph was blurry, and two readings of one piece of paper
    would double every amount on it.
    """
    conn.execute("DELETE FROM receipt_lines WHERE trip_id=?", (int(trip_id),))
    lines = parse_receipt(text)
    conn.executemany(
        "INSERT INTO receipt_lines (trip_id, position, label, amount, kind) "
        "VALUES (?,?,?,?,?)",
        [(int(trip_id), l["position"], l["label"], l["amount"], l["kind"])
         for l in lines])
    total = next((l["amount"] for l in reversed(lines)
                  if l["kind"] == "total"), None)
    if total is not None:
        # The till's own number beats anything summed from the lines: a
        # line the OCR dropped would otherwise quietly lower the total
        # and make the unattributed figure look better than it is.
        conn.execute("UPDATE trips SET receipt_total=? WHERE id=?",
                     (total, int(trip_id)))
    conn.commit()
    return lines


def receipt(conn, trip_id):
    return [dict(r) for r in conn.execute(
        "SELECT position, label, amount, kind FROM receipt_lines "
        "WHERE trip_id=? ORDER BY position", (int(trip_id),))]


# ---------------------------------------------------------- candidates

def add_candidate(conn, trip_id=None, photo_id=None, **fields):
    """Something she pointed the camera at. No decision yet."""
    now = datetime.now().isoformat(timespec="seconds")
    allowed = ("title", "era", "condition", "category", "asking")
    clean = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    if "asking" in clean:
        from . import listings as listings_mod

        clean["asking"] = listings_mod.parse_money(clean["asking"])
    cols = ["trip_id", "photo_id", "created_at"] + list(clean)
    vals = [int(trip_id) if trip_id else None,
            int(photo_id) if photo_id else None, now] + list(clean.values())
    cur = conn.execute(
        f"INSERT INTO candidates ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})", vals)
    conn.commit()
    return one(conn, cur.lastrowid)


def decide(conn, candidate_id, decision):
    """Cart it or put it back.

    A passed candidate is kept. The same object turns up again next
    month, and a photograph with a price she already rejected is worth
    more than the row costs.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    conn.execute(
        "UPDATE candidates SET decision=?, decided_at=? WHERE id=?",
        (decision, datetime.now().isoformat(timespec="seconds"),
         int(candidate_id)))
    conn.commit()
    return one(conn, candidate_id)


def update(conn, candidate_id, **fields):
    """Correct what the model offered. It was a starting point."""
    allowed = ("title", "era", "condition", "category", "asking")
    sets, params = [], []
    for key in allowed:
        if key not in fields:
            continue
        value = fields[key]
        if key == "asking" and value not in (None, ""):
            from . import listings as listings_mod

            value = listings_mod.parse_money(value)
            if value is None:
                raise ValueError("asking must be a number")
        sets.append(f"{key}=?")
        params.append(value if value != "" else None)
    if not sets:
        raise ValueError("nothing to change")
    params.append(int(candidate_id))
    conn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    return one(conn, candidate_id)


def one(conn, candidate_id):
    row = conn.execute("SELECT * FROM candidates WHERE id=?",
                       (int(candidate_id),)).fetchone()
    return dict(row) if row else None


def candidates(conn, trip_id=None, decision=None):
    sql = "SELECT * FROM candidates WHERE 1=1"
    args = []
    if trip_id is not None:
        sql += " AND trip_id=?"
        args.append(int(trip_id))
    if decision is not None:
        sql += " AND decision=?"
        args.append(decision)
    sql += " ORDER BY created_at, id"
    return [dict(r) for r in conn.execute(sql, args)]


# -------------------------------------------------------- reconciling

def _matches(category, label):
    """Does this department plausibly cover this category?"""
    if not category or not label:
        return False
    upper = label.upper()
    for department, categories in DEPARTMENTS.items():
        if department in upper:
            return any(c.lower() == category.lower() for c in categories)
    return False


def propose(conn, trip_id):
    """Lay the cart beside the receipt and say what it thinks.

    Returns a proposal per carted candidate, each with the line it would
    pick and **how sure that is**, plus whatever is left over on either
    side. Nothing is written. `confidence` is the whole point of the
    return value:

      matched   one unclaimed line whose department covers this category
      guessed   a line, but the department says nothing either way
      none      more things than lines, or nothing plausible left

    Four things and four lines is not four answers - it is four amounts,
    four objects and a question about which is which. Where two came out
    of HOUSEWARES the pick is a coin toss, and this says so rather than
    dressing it up.
    """
    carted = candidates(conn, trip_id=trip_id, decision="carted")
    carted = [c for c in carted if not c.get("listing_id")]
    lines = [l for l in receipt(conn, trip_id) if l["kind"] == "item"]

    taken = set()
    proposals = []
    # Department matches first, so an obvious pairing is not consumed by
    # an earlier candidate that would have taken any line at all.
    for wanted_confidence in ("matched", "guessed"):
        for candidate in carted:
            if any(p["candidate"] == candidate["id"] for p in proposals):
                continue
            pick = None
            for line in lines:
                if line["position"] in taken:
                    continue
                if wanted_confidence == "matched":
                    if _matches(candidate.get("category"), line["label"]):
                        pick = line
                        break
                else:
                    pick = line
                    break
            if pick is None:
                continue
            taken.add(pick["position"])
            proposals.append({"candidate": candidate["id"],
                              "title": candidate.get("title"),
                              "category": candidate.get("category"),
                              "line": pick["position"],
                              "label": pick["label"],
                              "amount": pick["amount"],
                              "confidence": wanted_confidence})

    for candidate in carted:
        if not any(p["candidate"] == candidate["id"] for p in proposals):
            proposals.append({"candidate": candidate["id"],
                              "title": candidate.get("title"),
                              "category": candidate.get("category"),
                              "line": None, "label": None, "amount": None,
                              "confidence": "none"})

    leftover = [l for l in lines if l["position"] not in taken]
    return {"proposals": proposals,
            "unclaimed_lines": leftover,
            "carted": len(carted),
            "item_lines": len(lines)}


def apply(conn, trip_id, assignments):
    """Turn what she confirmed into inventory.

    Only what was handed back. A proposal that never came through here
    writes nothing - which is the difference between a system that helps
    her attribute costs and one that invents them.
    """
    from . import listings as listings_mod

    created = []
    for assignment in assignments or []:
        candidate = one(conn, assignment.get("candidate"))
        if candidate is None:
            continue
        if candidate.get("listing_id"):
            continue          # already made; confirming twice is not two things
        title = (assignment.get("title") or candidate.get("title") or "").strip()
        if not title:
            raise ValueError("an item needs a title")
        paid = assignment.get("amount")
        item = listings_mod.create_item(
            conn, title,
            paid=paid,
            price=candidate.get("asking"),
            era=candidate.get("era"),
            condition=candidate.get("condition"),
            category=candidate.get("category"),
            trip_id=candidate.get("trip_id") or trip_id)
        conn.execute("UPDATE candidates SET listing_id=? WHERE id=?",
                     (item["id"], candidate["id"]))
        if candidate.get("photo_id"):
            conn.execute("UPDATE photos SET listing_id=? WHERE id=?",
                         (item["id"], candidate["photo_id"]))
        conn.commit()
        created.append(item)
    return created
