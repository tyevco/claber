"""
listings.py - the listing side of the picture.

There is no Marketplace API. Meta has never published one for individual
sellers, and the Commerce Platform API is a closed alpha for approved
business partners. So a listing catalogue has to be assembled from data
she already owns:

  1. her mailbox   - Facebook emails on listing, sale, payout, expiry
  2. Facebook's "Download Your Information" export
  3. manual CSV    - for anything the first two miss

Scraping Marketplace is the obvious fourth option and this deliberately
does not do it. Beyond the terms-of-service question, automated access is
what gets accounts flagged, and losing her account means losing the
selling channel and the order history along with it. Not worth it for a
sell-through chart.
"""

import csv
import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

# Indexes on columns that arrived by migration. They cannot live in
# SCHEMA: `executescript(SCHEMA)` runs *before* the ALTER TABLE loop, so
# on a database that predates the column the CREATE INDEX fails and takes
# `connect_db` down with it - every command, not just the new feature.
# CLAUDE.md says a new column needs a migration; the index needs one too.
POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_listing_bin ON listings(bin_code)",
    "CREATE INDEX IF NOT EXISTS idx_listing_trip ON listings(trip_id)",
    "CREATE INDEX IF NOT EXISTS idx_photo_listing ON photos(listing_id)",
    "CREATE INDEX IF NOT EXISTS idx_photo_trip ON photos(trip_id)",
)

SCHEMA = """
-- A place something can be. The first real relation in this schema, and
-- it earns one: unlike a listing_id, a bin's identity is minted here, so
-- there is an actual key to point at rather than a title to match on.
--
-- Two halves on purpose. `name` is what is written on the shelf and read
-- across the room - FLOOR, ATTIC - and it can change without anything
-- following it. `code` is three characters from the code alphabet, minted
-- rather than typed, and is what a tag carries and a phone reads. Renaming
-- a bin moves neither the tag nor the things in it.
-- One sourcing trip: a shop, a day, and what the receipt came to.
--
-- Its identity is minted here, so like a bin it earns a real key. Unlike
-- a bin it is never printed on anything, so the id is an ordinary
-- integer rather than a code from the alphabet - nothing has to read it
-- off thermal paper.
--
-- `receipt_total` is what the till said, and it is deliberately not the
-- sum of what the items cost: a receipt has tax and things that never
-- became listings on it, and the difference between the two numbers is
-- itself worth seeing.
CREATE TABLE IF NOT EXISTS trips (
    id            INTEGER PRIMARY KEY,
    store         TEXT NOT NULL,
    occurred_at   TEXT,
    receipt_total REAL,
    notes         TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS bins (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    listing_id    TEXT UNIQUE,
    title         TEXT,
    price         REAL,
    category      TEXT,
    condition     TEXT,
    -- Roughly when the thing is from - "c. 1910", "mid-century", "1970s".
    -- Free text and deliberately not a year: her titles say "Antique
    -- 1900-1915 American Edwardian", which is a range, a guess and a
    -- selling point all at once, and pinning it to an integer would
    -- force a precision the object does not have. Nothing computes on
    -- it; it is there because it is most of what a buyer asks.
    era           TEXT,
    listed_at     TEXT,
    sold_at       TEXT,
    removed_at    TEXT,
    renewed_count INTEGER DEFAULT 0,
    inquiries     INTEGER DEFAULT 0,
    state         TEXT DEFAULT 'active',   -- active | sold | expired | removed
    source        TEXT,                    -- email | dyi | csv | manual
    first_seen    TEXT,
    last_seen     TEXT,
    inventory_code TEXT,
    -- Where the thing physically is. ON DELETE SET NULL because deleting
    -- a bin should put its contents back on no shelf, not leave them
    -- pointing at one that is gone - and a dangling code is the failure
    -- this table exists to make impossible.
    --
    -- Enforced: `connect_db` turns foreign keys on. SQLite has them off
    -- per connection by default, so a declared reference without that
    -- pragma is documentation, not a constraint.
    --
    -- Still no move history. This records where a thing *is*, which is
    -- what is being asked - "where is this?", "what is in ATTIC?". If
    -- "where has this been?" becomes a real question it is a new table
    -- beside this, not a different shape of it.
    bin_code      TEXT REFERENCES bins(code) ON DELETE SET NULL,

    -- What it cost, in dollars, like `price`. NOT cents.
    --
    -- `amount_with_offset` is already the trap on record for this: read
    -- raw it turns $15 into $1500 and silently corrupts every average.
    -- A second money column is a second chance to make that mistake, so
    -- it uses the same unit as the first one and a test says so.
    --
    -- Null means unknown, which is the honest state for everything that
    -- predates this column - and margin has to stay null rather than
    -- becoming the whole price.
    paid          REAL,

    -- Which trip it came home from. SET NULL because deleting a trip is
    -- tidying up a record of a shop visit, not disowning the things.
    trip_id       INTEGER REFERENCES trips(id) ON DELETE SET NULL,

    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_listing_state ON listings(state);
CREATE INDEX IF NOT EXISTS idx_listing_sold  ON listings(sold_at);

-- Every Facebook email we have classified, so backfill is resumable and
-- we can report on subject lines we do not yet recognise.
-- A photograph. The row points at a file; the bytes are on disk under
-- `home/photos/`, exactly as `labels/` works.
--
-- Not a blob: this is an SD card in a Raspberry Pi, and a database that
-- grows by three megabytes per photo is a database that stops being
-- copyable. `verify` is already the pattern for checking a row still
-- matches its file.
--
-- A photo with no `listing_id` is a capture awaiting triage. That is
-- why there is no `captures` table and no state column: "not yet turned
-- into an item" is the absence of a reference, and inventing a state to
-- say the same thing gives two places to disagree.
CREATE TABLE IF NOT EXISTS photos (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL,
    sha256     TEXT,
    taken_at   TEXT,
    created_at TEXT,
    listing_id INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    trip_id    INTEGER REFERENCES trips(id)    ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS mail_events (
    id          INTEGER PRIMARY KEY,
    message_id  TEXT UNIQUE,
    occurred_at TEXT,
    kind        TEXT,          -- see EVENT_PATTERNS
    listing_id  TEXT,
    subject     TEXT,
    amount      REAL,
    counterparty TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_kind ON mail_events(kind);
"""


# Facebook changes subject lines without warning, and they differ by
# locale and by how the listing was created. Rather than guess once and
# have it rot, these are patterns you can extend - and `mplabel.py scan`
# reports every unmatched Facebook subject so you know what to add.
EVENT_PATTERNS = [
    ("shipping_label", r"shipping label for your marketplace order"),

    # Buyer side: mail about things SHE bought. These sit above the seller
    # patterns because classify() returns the first match, and because
    # getting the direction wrong is the expensive mistake here - see
    # BUYER_KINDS. Real subjects: "You placed an order: <item>",
    # "Confirm if you received your order: <item>", "Offer submitted:
    # <item>". The same item turned up under both "Offer submitted" and
    # "Confirm if you received", which is what settled the direction.
    ("purchase",       r"\b(you placed an order|confirm if you received your order|offer submitted)\b"),

    # Seller side. "New Marketplace order for <item>" is the real wording
    # for a sale - it arrives first, without the label; the shipping_label
    # mail follows separately with the PDF attached.
    ("sold",           r"\b(new marketplace order for|you sold|your item sold|sold your|congratulations on your sale)\b"),
    ("order_placed",   r"\b(new order|order confirmation|you have a new order)\b"),
    ("listed",         r"\b(your (listing|item) is (now )?live|you listed|congrats.*listed|your listing was published)\b"),
    ("renewed",        r"\b(listing (was )?renewed|we renewed your listing)\b"),
    ("expired",        r"\b(listing (has )?expired|your listing is no longer)\b"),
    # "📬 Tyler sent you a message" - the emoji is part of the subject.
    ("inquiry",        r"\b(new message about|is interested in|asked about your|sent you a message)\b"),
    ("payout",         r"\b(payout|payment (sent|on its way|initiated)|you.ve been paid)\b"),
    ("rating",         r"\b(left you a rating|rate your)\b"),
]


# Kinds that describe HER buying something, not selling it. They are
# classified so `scan` stops reporting them as unrecognised, and recorded
# so the history is complete - but they must never reach the listings
# table. A purchase email carries the *seller's* listing id, so treating
# one as a listing would invent a row for an item that was never for sale,
# inflating the listing count and dragging sell-through down with it.
BUYER_KINDS = {"purchase"}


# The sale subjects carry the item name, and for a local-pickup sale that
# subject is the only record that exists - no label email ever arrives.
SUBJECT_TITLE_RES = [
    re.compile(r"new marketplace order for\s+(.+)$", re.I),
    re.compile(r"you sold\s+(.+)$", re.I),
    re.compile(r"your item sold[:\-\s]+(.+)$", re.I),
    re.compile(r"congratulations on your sale of\s+(.+)$", re.I),
]


def title_from_subject(subject):
    """Pull the item name out of a sale subject, or None."""
    s = (subject or "").strip()
    for rx in SUBJECT_TITLE_RES:
        m = rx.search(s)
        if m:
            return m.group(1).strip().strip('"“”')
    return None


def classify(subject):
    """Return the event kind for a subject line, or None if unrecognised."""
    s = (subject or "").lower()
    for kind, pattern in EVENT_PATTERNS:
        if re.search(pattern, s):
            return kind
    return None


def record_event(conn, message_id, occurred_at, kind, subject,
                 listing_id=None, amount=None, counterparty=None):
    conn.execute(
        "INSERT OR IGNORE INTO mail_events "
        "(message_id, occurred_at, kind, listing_id, subject, amount, counterparty) "
        "VALUES (?,?,?,?,?,?,?)",
        (message_id, occurred_at, kind, listing_id, subject, amount, counterparty))


def upsert_listing(conn, listing_id, source, **fields):
    """Create or enrich a listing row without clobbering what we know.

    Later observations fill in blanks; they do not overwrite a value that
    is already set, except for state and the *_at timestamps where a more
    definite state wins."""
    if not listing_id:
        return
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute("SELECT * FROM listings WHERE listing_id=?",
                       (listing_id,)).fetchone()
    if row is None:
        cols = ["listing_id", "source", "first_seen", "last_seen"]
        vals = [listing_id, source, now, now]
        for k, v in fields.items():
            if v is not None:
                cols.append(k)
                vals.append(v)
        conn.execute(f"INSERT INTO listings ({','.join(cols)}) "
                     f"VALUES ({','.join('?' * len(cols))})", vals)
        return

    updates, vals = {"last_seen": now}, []
    existing = dict(row)
    # 'sold' is terminal and beats anything else we might later infer.
    rank = {"active": 0, "expired": 1, "removed": 2, "sold": 3}
    for k, v in fields.items():
        if v is None:
            continue
        if k == "state":
            if rank.get(v, 0) >= rank.get(existing.get("state") or "active", 0):
                updates[k] = v
        elif not existing.get(k):
            updates[k] = v
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE listings SET {sets} WHERE listing_id=?",
                     list(updates.values()) + [listing_id])


# A bin is a place something is. Two halves on purpose, and the split is
# the whole design:
#
# `name` is what is written on the shelf and read across the room -
# `FLOOR`, `ATTIC`, `B5`. It is folded rather than validated into a code
# because those are real answers, and it can change without anything
# having to follow it.
#
# `code` is the machine half: three characters from the same alphabet as
# a parcel code, minted rather than typed, and what a shelf tag carries.
# Renaming a bin moves neither the tag on the shelf nor the things in it.
BIN_NAME_MAX = 24
BIN_CODE_LENGTH = 3


def normalise_bin_name(name):
    """Fold a typed bin name.

    Upper-cased and space-collapsed because `attic`, `ATTIC ` and
    `AT TIC` are one shelf in the room, and a name is how a person finds
    a thing - two spellings of one shelf is two shelves on the screen."""
    folded = " ".join(str(name or "").split()).upper()
    if not folded:
        raise ValueError("a bin needs a name - it is what you read across "
                         "the room; the code is for the phone")
    if len(folded) > BIN_NAME_MAX:
        raise ValueError(
            f"{folded!r} is {len(folded)} characters; a bin name is at most "
            f"{BIN_NAME_MAX}. It goes on a shelf and gets read across a room")
    return folded


def allocate_bin_code(conn):
    """Mint a code no bin has ever had.

    Checked against every bin *ever*, not the ones in use - the same rule
    as an inventory code and for the same reason. A bin's code is printed
    on a tag stuck to a shelf, and that tag outlives the row: recycling
    the code leaves a label in the loft naming somewhere else. Which is
    the opposite of a parcel code, released the moment the parcel ships.
    Three code spaces, three lifetimes; do not merge them."""
    import random

    from .marker import ALPHABET

    taken = {r[0] for r in conn.execute("SELECT code FROM bins")}
    for _ in range(2000):
        code = "".join(random.choice(ALPHABET)
                       for _ in range(BIN_CODE_LENGTH))
        if code not in taken:
            return code
    raise ValueError("no free bin code; every combination is taken")


def create_bin(conn, name, code=None, notes=None):
    """Name a place. Returns the row.

    The code is minted here rather than typed, because it exists to be
    printed and scanned and nobody should have to invent one."""
    name = normalise_bin_name(name)
    clash = conn.execute("SELECT code FROM bins WHERE name=?",
                         (name,)).fetchone()
    if clash:
        raise ValueError(
            f"there is already a bin called {name} ({clash['code']}). "
            f"Two shelves with one name is how a thing gets lost")
    code = (code or allocate_bin_code(conn)).upper()
    conn.execute(
        "INSERT INTO bins (code, name, created_at, notes) VALUES (?,?,?,?)",
        (code, name, datetime.now().isoformat(timespec="seconds"), notes))
    conn.commit()
    return dict(conn.execute("SELECT * FROM bins WHERE code=?",
                             (code,)).fetchone())


def rename_bin(conn, code, name):
    """Change what a bin is called. The code does not move.

    This is the whole reason the two are separate: the tag already stuck
    to the shelf stays valid, and everything in the bin follows the
    rename for free because it references the code."""
    name = normalise_bin_name(name)
    code = (code or "").upper()
    cur = conn.execute("UPDATE bins SET name=? WHERE code=?", (name, code))
    conn.commit()
    if not cur.rowcount:
        raise ValueError(f"no bin {code}")
    return dict(conn.execute("SELECT * FROM bins WHERE code=?",
                             (code,)).fetchone())


def find_bin(conn, needle):
    """A bin by code or by name, whichever was typed.

    The phone has the code and a person has the name, and neither should
    have to know which one the other used."""
    needle = " ".join(str(needle or "").split()).upper()
    if not needle:
        return None
    row = conn.execute("SELECT * FROM bins WHERE code=? OR name=?",
                       (needle, needle)).fetchone()
    return dict(row) if row else None


def delete_bin(conn, needle):
    """Retire a bin. Whatever was in it goes back on no shelf.

    That last part is the foreign key doing the work: `ON DELETE SET
    NULL` empties the reference rather than leaving rows pointing at a
    bin that is gone. It only fires because `connect_db` turns foreign
    keys on - SQLite ignores a declared reference otherwise."""
    found = find_bin(conn, needle)
    if not found:
        raise ValueError(f"no bin {needle!r}")
    conn.execute("DELETE FROM bins WHERE code=?", (found["code"],))
    conn.commit()
    return found


def set_bin(conn, listing_id, needle):
    """Put one thing in a bin, or take it out of one.

    Takes a code or a name. An empty value clears it - "not set" on the
    shelf screen is the absence of a bin, not a bin called nothing."""
    if needle in (None, ""):
        code = None
    else:
        found = find_bin(conn, needle)
        if not found:
            raise ValueError(
                f"no bin {needle!r}. Make it first - a thing cannot be "
                f"somewhere that has no name")
        code = found["code"]
    cur = conn.execute("UPDATE listings SET bin_code=? WHERE id=?",
                       (code, listing_id))
    conn.commit()
    if not cur.rowcount:
        raise ValueError(f"no listing {listing_id}")
    return code


def bins_in_use(conn, include_sold=False):
    """Every bin, with what is in it.

    A LEFT JOIN, so a bin that has just been made or has just emptied
    still appears: it exists because someone named it and printed a tag
    for it, not because something is in it. That is the difference the
    table buys over deriving the list from the items.

    Sold items are excluded by default - a bin's useful count is what is
    still on the shelf. `include_sold` answers what *was* there."""
    where = "" if include_sold else " AND l.state != 'sold'"
    rows = conn.execute(
        f"SELECT b.code, b.name, b.created_at, b.notes, "
        f"       COUNT(l.id) AS n "
        f"FROM bins b LEFT JOIN listings l "
        f"  ON l.bin_code = b.code{where} "
        f"GROUP BY b.code, b.name, b.created_at, b.notes "
        f"ORDER BY b.name").fetchall()
    return [{"code": r["code"], "name": r["name"], "notes": r["notes"],
             "created_at": r["created_at"], "count": r["n"]} for r in rows]


def bin_contents(conn, needle, include_sold=False):
    """What is in one bin, by code or by name."""
    found = find_bin(conn, needle)
    if not found:
        raise ValueError(f"no bin {needle!r}")
    where = "" if include_sold else " AND state != 'sold'"
    rows = conn.execute(
        f"SELECT id, listing_id, title, price, state, inventory_code, "
        f"bin_code FROM listings WHERE bin_code=?{where} ORDER BY title",
        (found["code"],)).fetchall()
    return {"bin": found, "items": [dict(r) for r in rows]}


def title_key(title):
    """Stable id for a listing we only know by name.

    Used by the saved-page import and by link_sales, which have to agree
    or a sale will not find the listing it belongs to. The digest is of
    the full title because her titles are long and share their first
    sixty characters."""
    slug = re.sub(r"\W+", "-", str(title).lower()).strip("-")[:48]
    digest = hashlib.sha1(str(title).encode("utf-8")).hexdigest()[:8]
    return f"saved:{slug}-{digest}"


def _norm_title(title):
    """Loose form for matching a sale against a listing."""
    return re.sub(r"\W+", " ", (title or "").lower()).strip()


def link_sales(conn):
    """Fold the sales table into listings, so a sold item has both a
    listing date and a sale date and we can measure time-to-sell.

    Matching on listing_id alone is not enough. A saved-page import keys
    its listings by title, because the cards carry no Facebook id; and
    some label emails carry no listing id either. In both cases the sold
    item stayed 'active' while a second, duplicate row appeared beside
    it - so a sale today would not show up as sold, and the listing count
    would grow instead."""
    by_title = {}
    for r in conn.execute(
            "SELECT listing_id, title FROM listings WHERE title IS NOT NULL"):
        by_title.setdefault(_norm_title(r["title"]), r["listing_id"])

    for row in conn.execute(
            "SELECT listing_id, item, price, received_at FROM sales"):
        lid = row["listing_id"]
        known = lid and conn.execute(
            "SELECT 1 FROM listings WHERE listing_id=?", (lid,)).fetchone()
        if not known:
            # Prefer an existing listing with the same title; failing that
            # key it the way the saved-page import would, so a later
            # capture of the same item lands on this row rather than
            # creating a twin.
            lid = by_title.get(_norm_title(row["item"])) or lid \
                or (title_key(row["item"]) if row["item"] else None)
        if not lid:
            continue
        upsert_listing(conn, lid, "email",
                       title=row["item"], price=row["price"],
                       sold_at=row["received_at"], state="sold")
    conn.commit()


def apply_events(conn):
    """Replay mail_events into the listings table."""
    kind_to_state = {"sold": "sold", "expired": "expired",
                     "listed": "active", "renewed": "active"}
    by_title = {}
    for r in conn.execute(
            "SELECT listing_id, title FROM listings WHERE title IS NOT NULL"):
        by_title.setdefault(_norm_title(r["title"]), r["listing_id"])

    for ev in conn.execute("SELECT * FROM mail_events ORDER BY occurred_at"):
        if ev["kind"] in BUYER_KINDS:
            continue
        lid = ev["listing_id"]
        if not lid:
            # A sale email often carries no listing id, and a local-pickup
            # sale never produces a label - so the subject line is the only
            # record of it. Reconcile by the item name in that subject, and
            # for a sale with no match at all create the listing, or the
            # sale simply would not be counted.
            name = title_from_subject(ev["subject"])
            if name:
                lid = by_title.get(_norm_title(name))
                if not lid and ev["kind"] == "sold":
                    lid = title_key(name)
                    by_title[_norm_title(name)] = lid
                    upsert_listing(conn, lid, "email", title=name)
        if not lid:
            continue
        fields = {"state": kind_to_state.get(ev["kind"])}
        if ev["kind"] == "listed":
            fields["listed_at"] = ev["occurred_at"]
        elif ev["kind"] == "sold":
            fields["sold_at"] = ev["occurred_at"]
        elif ev["kind"] == "expired":
            fields["removed_at"] = ev["occurred_at"]
        if ev["amount"]:
            fields["price"] = ev["amount"]
        upsert_listing(conn, lid, "email", **fields)

    # Inquiry counts and renewals are aggregates, not single observations.
    conn.execute("""
        UPDATE listings SET inquiries = COALESCE((
            SELECT COUNT(*) FROM mail_events e
             WHERE e.listing_id = listings.listing_id AND e.kind='inquiry'
        ), 0)""")
    conn.execute("""
        UPDATE listings SET renewed_count = COALESCE((
            SELECT COUNT(*) FROM mail_events e
             WHERE e.listing_id = listings.listing_id AND e.kind='renewed'
        ), 0)""")
    conn.commit()


# ------------------------------------------------------------- importers

def import_dyi(conn, path):
    """Import Facebook's 'Download Your Information' export.

    Accepts the zip or an unpacked directory. Meta reshuffles this export
    regularly and does not document it, so rather than assume a schema
    this walks the JSON looking for objects that carry marketplace-ish
    keys. Returns (imported, files_examined) so you can tell the
    difference between 'no listings' and 'did not recognise the format'."""
    path = Path(path)
    imported = 0
    examined = 0

    def handle(blob):
        nonlocal imported
        for obj in _walk_for_listings(blob):
            lid = str(obj.get("id") or obj.get("listing_id") or "").strip()
            title = obj.get("title") or obj.get("name") or obj.get("marketplace_listing_title")
            if not (lid or title):
                continue
            if not lid:
                lid = "dyi:" + re.sub(r"\W+", "-", title.lower())[:60]
            price = _coerce_price(obj.get("price") or obj.get("listing_price"))
            created = _coerce_time(obj.get("created_timestamp")
                                   or obj.get("creation_timestamp")
                                   or obj.get("timestamp"))
            upsert_listing(conn, lid, "dyi", title=title, price=price,
                           listed_at=created,
                           category=obj.get("category") or obj.get("category_name"),
                           condition=obj.get("condition"))
            imported += 1

    if path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith(".json"):
                    continue
                if "marketplace" not in name.lower() and "selling" not in name.lower():
                    continue
                examined += 1
                try:
                    handle(json.loads(zf.read(name)))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    else:
        for jf in path.rglob("*.json"):
            if "marketplace" not in str(jf).lower() and "selling" not in str(jf).lower():
                continue
            examined += 1
            try:
                handle(json.loads(jf.read_text(errors="replace")))
            except json.JSONDecodeError:
                continue

    conn.commit()
    return imported, examined


LISTING_KEYS = {"title", "listing_id", "marketplace_listing_title",
                "listing_price", "price"}


def _walk_for_listings(node):
    """Yield every dict in a nested structure that looks like a listing."""
    if isinstance(node, dict):
        if LISTING_KEYS & set(node.keys()):
            yield node
        for v in node.values():
            yield from _walk_for_listings(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_for_listings(item)


def _coerce_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return _coerce_price(v.get("amount") or v.get("value"))
    m = re.search(r"([\d,]+(?:\.\d{2})?)", str(v))
    return float(m.group(1).replace(",", "")) if m else None


def parse_money(value):
    """The importers' price reader, for callers outside this module.

    `web.py` needs exactly this leniency - "$4.99", "4.99" and 4.99 are
    all things a person types into a phone - and reaching for the private
    name would make a refactor here a silent break there."""
    return _coerce_price(value)


def _coerce_time(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        # Facebook uses unix seconds throughout the export.
        try:
            return datetime.fromtimestamp(float(v)).isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            return None
    return str(v)


def import_csv(conn, path):
    """Import a hand-maintained CSV. Any subset of these columns:
    listing_id,title,price,category,condition,listed_at,sold_at,state"""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): (v.strip() or None)
                   for k, v in row.items() if k}
            lid = row.get("listing_id") or (
                "csv:" + re.sub(r"\W+", "-", (row.get("title") or "")[:60].lower()))
            upsert_listing(conn, lid, "csv",
                           title=row.get("title"),
                           price=_coerce_price(row.get("price")),
                           category=row.get("category"),
                           condition=row.get("condition"),
                           listed_at=row.get("listed_at"),
                           sold_at=row.get("sold_at"),
                           state=row.get("state") or ("sold" if row.get("sold_at") else "active"))
            n += 1
    conn.commit()
    return n


# ------------------------------------------------------------- analytics

ANALYTICS_VIEWS = """
DROP VIEW IF EXISTS v_listing_perf;
CREATE VIEW v_listing_perf AS
SELECT
    listing_id, title, category, price, state, inquiries, renewed_count,
    listed_at, sold_at, paid, trip_id,
    -- Null unless both halves are known. A missing cost must not read as
    -- a cost of zero, which would report the whole price as profit.
    CASE WHEN price IS NOT NULL AND paid IS NOT NULL
         THEN ROUND(price - paid, 2) END AS margin,
    CASE WHEN sold_at IS NOT NULL AND listed_at IS NOT NULL
         THEN CAST(julianday(sold_at) - julianday(listed_at) AS INTEGER)
    END AS days_to_sell,
    CASE WHEN sold_at IS NULL AND listed_at IS NOT NULL
         THEN CAST(julianday('now') - julianday(listed_at) AS INTEGER)
    END AS days_listed,
    CASE
        WHEN price IS NULL      THEN 'unknown'
        WHEN price <  10        THEN 'under $10'
        WHEN price <  25        THEN '$10-25'
        WHEN price <  50        THEN '$25-50'
        WHEN price < 100        THEN '$50-100'
        ELSE '$100+'
    END AS price_band
FROM listings;

DROP VIEW IF EXISTS v_price_band;
CREATE VIEW v_price_band AS
SELECT price_band,
       COUNT(*)                                        AS listed,
       SUM(state='sold')                               AS sold,
       ROUND(100.0 * SUM(state='sold') / COUNT(*), 1)  AS sell_through_pct,
       ROUND(AVG(CASE WHEN state='sold' THEN days_to_sell END), 1) AS avg_days_to_sell,
       ROUND(AVG(price), 2)                            AS avg_price
FROM v_listing_perf
GROUP BY price_band;

DROP VIEW IF EXISTS v_monthly;
CREATE VIEW v_monthly AS
SELECT strftime('%Y-%m', sold_at) AS month,
       COUNT(*)             AS orders,
       ROUND(SUM(price), 2) AS gross,
       -- Only over rows that have a cost. A month with one costed item
       -- reports that item's margin, not the month's - which is why the
       -- count comes with it.
       ROUND(SUM(margin), 2) AS net,
       SUM(margin IS NOT NULL) AS costed,
       ROUND(AVG(price), 2) AS avg_order,
       ROUND(AVG(days_to_sell), 1) AS avg_days_to_sell
FROM v_listing_perf
WHERE sold_at IS NOT NULL
GROUP BY month ORDER BY month DESC;

DROP VIEW IF EXISTS v_aging;
CREATE VIEW v_aging AS
SELECT listing_id, title, price, days_listed, inquiries, renewed_count
FROM v_listing_perf
WHERE state='active' AND days_listed IS NOT NULL
ORDER BY days_listed DESC;
"""


def build_views(conn):
    conn.executescript(ANALYTICS_VIEWS)
    conn.commit()


def refresh(conn):
    """Rebuild the derived listing picture from sales + mail events."""
    conn.executescript(SCHEMA)
    link_sales(conn)
    apply_events(conn)
    build_views(conn)


# ------------------------------------------------------------ the sourcing
#
# A trip is one visit to one shop, and it is where cost basis enters the
# system. Everything else here already knew what a thing *sold* for; this
# is the half that knows what it cost, and without it every margin in the
# analytics is null and the profit screen can only report gross.
#
# The shape follows the receipt rather than the till. `receipt_total` is
# what was paid at the counter; `paid` on each listing is what one object
# is judged to have cost. They do not have to agree - a thrift receipt
# itemises by department ("HOUSEWARES $4.99"), tax is on it, and some of
# what came home never becomes a listing. The difference between the two
# is the unassigned money, and it is worth seeing rather than forcing to
# zero: a $4 lot of four things is one line on the receipt and four rows
# here.


def create_trip(conn, store, occurred_at=None, receipt_total=None,
                notes=None):
    """One shop visit. `store` is the only thing that must be there."""
    store = (store or "").strip()
    if not store:
        raise ValueError("a trip needs a store")
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO trips (store, occurred_at, receipt_total, notes, "
        "created_at) VALUES (?,?,?,?,?)",
        (store, occurred_at or now[:10], _coerce_price(receipt_total),
         notes, now))
    conn.commit()
    return trip_summary(conn, cur.lastrowid)


def trip_summary(conn, trip_id=None):
    """A trip and what happened to the money.

    `unassigned` is null rather than zero when the receipt total is
    unknown, because "nothing left to attribute" and "we never recorded
    what the till said" are different answers and the triage screen shows
    one of them as a number to chase."""
    sql = ("SELECT t.*, "
           "  (SELECT ROUND(SUM(paid), 2) FROM listings "
           "     WHERE trip_id = t.id) AS assigned, "
           "  (SELECT ROUND(SUM(price), 2) FROM listings "
           "     WHERE trip_id = t.id) AS listed_for, "
           "  (SELECT COUNT(*) FROM listings WHERE trip_id = t.id) AS items "
           "FROM trips t")
    args = []
    if trip_id is not None:
        sql += " WHERE t.id=?"
        args.append(int(trip_id))
    sql += " ORDER BY t.occurred_at DESC, t.id DESC"

    out = []
    for row in conn.execute(sql, args):
        trip = dict(row)
        total, assigned = trip.get("receipt_total"), trip.get("assigned")
        trip["unassigned"] = (None if total is None
                              else round(total - (assigned or 0), 2))
        out.append(trip)
    if trip_id is not None:
        return out[0] if out else None
    return out


def create_item(conn, title, **fields):
    """Add something by hand: a local pickup, or a thing off a shelf.

    Keyed with `title_key`, the same derivation the saved-page import and
    `link_sales` use. That is not a detail: a local pickup sale produces
    no label email, so the sale arrives later knowing only the item's
    name, and a manual item under any other scheme would sit beside its
    own sale instead of being it."""
    from . import cli as cli_mod

    title = (title or "").strip()
    if not title:
        raise ValueError("an item needs a title")

    allowed = ("price", "paid", "category", "condition", "era", "notes",
               "state", "listed_at", "trip_id")
    clean = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
    for money in ("price", "paid"):
        if money in clean:
            # Raise rather than drop. `_coerce_price` returns None for
            # anything it cannot read, and silently storing "unknown"
            # for a figure she typed is the same class of quiet loss as
            # reading the offset field as dollars.
            value = _coerce_price(clean[money])
            if value is None:
                raise ValueError(f"{money} must be a number")
            clean[money] = value
    if "trip_id" in clean:
        clean["trip_id"] = int(clean["trip_id"])

    key = title_key(title)
    upsert_listing(conn, key, "manual", title=title, **clean)
    # The code is minted here rather than left for the next `refresh`,
    # because the thing is in her hand now and the label maker is the
    # next step. `ensure_inventory_codes` never reissues one.
    cli_mod.ensure_inventory_codes(conn)
    conn.commit()
    row = conn.execute("SELECT * FROM listings WHERE listing_id=?",
                       (key,)).fetchone()
    return dict(row) if row else None


def set_cost(conn, listing_id, paid):
    """What one object cost. Null clears it back to unknown.

    Deliberately a plain overwrite where `upsert_listing` would not
    clobber: this is a person answering the question, and correcting a
    figure she typed yesterday is the normal case."""
    value = None if paid in (None, "") else _coerce_price(paid)
    if paid not in (None, "") and value is None:
        raise ValueError("paid must be a number")
    conn.execute("UPDATE listings SET paid=? WHERE id=?",
                 (value, int(listing_id)))
    conn.commit()
    return value


def add_photo(conn, path, sha256=None, trip_id=None, listing_id=None,
              taken_at=None):
    """Record a photograph. The bytes are already on disk.

    Idempotent on the digest, and that is the point rather than a nicety:
    she is uploading from a shop on a phone with one bar, the client
    retries, and a second row would put the same receipt in the triage
    pile twice."""
    now = datetime.now().isoformat(timespec="seconds")
    if sha256:
        row = conn.execute("SELECT * FROM photos WHERE sha256=?",
                           (sha256,)).fetchone()
        if row is not None:
            return dict(row)
    cur = conn.execute(
        "INSERT INTO photos (path, sha256, taken_at, created_at, "
        "listing_id, trip_id) VALUES (?,?,?,?,?,?)",
        (str(path), sha256, taken_at or now, now,
         int(listing_id) if listing_id else None,
         int(trip_id) if trip_id else None))
    conn.commit()
    return dict(conn.execute("SELECT * FROM photos WHERE id=?",
                             (cur.lastrowid,)).fetchone())


def attach_photo(conn, photo_id, listing_id=None, trip_id=None):
    """Point a capture at the thing it turned out to be about."""
    sets, args = [], []
    if listing_id is not None:
        sets.append("listing_id=?")
        args.append(int(listing_id) if listing_id else None)
    if trip_id is not None:
        sets.append("trip_id=?")
        args.append(int(trip_id) if trip_id else None)
    if not sets:
        raise ValueError("nothing to attach")
    args.append(int(photo_id))
    conn.execute(f"UPDATE photos SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()
    row = conn.execute("SELECT * FROM photos WHERE id=?",
                       (int(photo_id),)).fetchone()
    return dict(row) if row else None


def untriaged(conn, limit=200):
    """Captures that are not yet about anything - *either* thing.

    There is no state column and no `captures` table on purpose; the
    schema note says why. But "about something" is two references, not
    one. The first version of this asked only for `listing_id IS NULL`,
    on the schema comment's reading that a capture becomes triaged by
    turning into an item - and that is true of a photograph of an object
    and false of the thing she actually photographs most, which is a
    receipt. A receipt is never about one listing: it is the record of a
    trip, several of whose items it paid for. Filed against a trip, it
    would have sat in this pile for ever, and the pile is the one number
    on the capture screen.

    So the pile is a photo with neither reference. That keeps the
    property worth keeping - triaged is still an absence rather than a
    flag - and stops the queue from lying. Money still needing a home is
    a different question with a different answer: `trip.unassigned`."""
    rows = conn.execute(
        "SELECT p.*, t.store AS store, t.occurred_at AS trip_date "
        "FROM photos p LEFT JOIN trips t ON t.id = p.trip_id "
        "WHERE p.listing_id IS NULL AND p.trip_id IS NULL "
        "ORDER BY p.created_at DESC, p.id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]


# ------------------------------------------------------------- postage
#
# What a parcel cost to send, and the reason this is careful.
#
# **No email carries the charge.** The label email is a *prepaid* label -
# Facebook pays the carrier and takes it out of the payout - so the one
# document this system reliably receives says what the parcel weighs and
# what service it went by, and not what it cost. The payout email is the
# only plausible carrier and none has ever been seen (see the verified /
# assumed table). Until one is, every figure here comes from a person.
#
# So there is no rate card in this file. Inventing one would produce a
# number for every parcel, none of them observed, all of them looking
# exactly like the ones she typed - and postage on a heavy item is
# routinely the difference between a good margin and none. An estimate is
# only offered once there is something real to derive it from, and it is
# labelled for as long as it stays an estimate.


def parse_weight(text):
    """Pounds, from what the label says - "2 lb 3 oz", "11 lbs", "16 oz".

    Returns None rather than a guess. The weight is on the label and the
    label is verified; a weight this cannot read is a weight nothing
    should be derived from.
    """
    if not text:
        return None
    raw = str(text).lower()
    pounds = re.search(r"([\d.]+)\s*(?:lbs?|pounds?)\b", raw)
    ounces = re.search(r"([\d.]+)\s*(?:oz|ounces?)\b", raw)
    total = 0.0
    if pounds:
        total += float(pounds.group(1))
    if ounces:
        total += float(ounces.group(1)) / 16.0
    if total:
        return round(total, 3)
    bare = re.fullmatch(r"\s*([\d.]+)\s*", raw)
    # A bare number on a shipping label is pounds; every carrier this
    # system has seen writes ounces with a unit.
    return float(bare.group(1)) if bare else None


def confirmed_postage(conn):
    """(pounds, dollars) for every parcel whose postage a person typed."""
    rows = conn.execute(
        "SELECT weight, postage FROM sales "
        "WHERE postage IS NOT NULL AND postage_source = 'confirmed' "
        "AND weight IS NOT NULL").fetchall()
    out = []
    for row in rows:
        pounds = parse_weight(row["weight"])
        if pounds:
            out.append((pounds, float(row["postage"])))
    return sorted(out)


def estimate_postage(conn, weight):
    """What this one probably cost, from what the others actually did.

    Returns `(dollars, "estimated")`, or `(None, None)` when there is no
    basis - which is the answer for a database that has never had a real
    charge typed into it, and it is the right one. A number produced from
    nothing would be indistinguishable from a measured one a week later.

    Linear between the two nearest confirmed weights, flat outside them.
    That is a crude model of a rate card and it is meant to be: it exists
    to be visibly an estimate until she corrects it, not to be right.
    """
    pounds = parse_weight(weight)
    known = confirmed_postage(conn)
    if pounds is None or not known:
        return None, None
    if len(known) == 1:
        return known[0][1], "estimated"

    below = [k for k in known if k[0] <= pounds]
    above = [k for k in known if k[0] >= pounds]
    if not below:
        return above[0][1], "estimated"
    if not above:
        return below[-1][1], "estimated"
    (w1, p1), (w2, p2) = below[-1], above[0]
    if w2 == w1:
        return round((p1 + p2) / 2, 2), "estimated"
    share = (pounds - w1) / (w2 - w1)
    return round(p1 + (p2 - p1) * share, 2), "estimated"


def kept(price, postage, paid=None):
    """What the sale actually leaves behind.

    Null in, null out - and deliberately so. A missing postage read as
    zero reports the whole price as kept, which is the same failure as a
    missing cost reading as free, and both flatter the numbers in the
    same direction.
    """
    if price is None or postage is None:
        return None
    total = float(price) - float(postage)
    if paid is not None:
        total -= float(paid)
    return round(total, 2)


# ------------------------------------------------------- what it is worth
#
# Two different questions get asked in a shop, and they want different
# evidence:
#
#   what would this sell for      -> what things like it actually sold for
#   what should I pay for it      -> that, less the margin she usually gets
#
# Both are answered from her own history and neither is invented. Where
# there is no comparable the answer is "nothing to compare it with",
# which is a useful thing to be told while holding a $40 lamp.
#
# The *model* also has an opinion, and it is kept somewhere else on
# purpose - see `OnDevice.Suggested.estimate`. It has no market data, no
# comps and no idea what a thing goes for in her county, so its number
# and these numbers must never be added, averaged or shown as one
# figure.


def _words(title):
    """Significant words, for finding something like this one."""
    stop = {"the", "a", "an", "and", "of", "with", "in", "for", "vintage",
            "antique", "large", "small", "set", "pair", "old"}
    return {w for w in re.split(r"\W+", (title or "").lower())
            if len(w) > 2 and w not in stop}


def comparables(conn, category=None, title=None, limit=12):
    """What things like this actually sold for.

    Category first, then title overlap - a "Home" category covers half
    the house, so two shared words in the title is the stronger signal
    when it is there. Sold rows only, and only with a price: an active
    listing at $45 is an asking price nobody has agreed to.
    """
    rows = conn.execute(
        "SELECT title, category, price, paid, listed_at, sold_at "
        "FROM listings WHERE state='sold' AND price IS NOT NULL").fetchall()
    wanted = _words(title)
    scored = []
    for row in rows:
        overlap = len(wanted & _words(row["title"])) if wanted else 0
        same_category = bool(
            category and row["category"]
            and row["category"].lower() == category.lower())

        # **A shared category is not a comparison.** Nearly everything
        # she sells is "Home", so matching on it alone put a tumbler, a
        # cabinet and a doorway in one another's comparables and produced
        # "6 like it sold for $5.00-$235.00" - a range so wide it is
        # worse than saying nothing, because it looks like evidence. Seen
        # on the first real trip with the phone.
        #
        # So a title, when there is one, has to share a word. Category
        # only breaks ties and only helps where no title was offered at
        # all.
        if wanted and not overlap:
            continue
        score = 2 * overlap + (1 if same_category else 0)
        if score:
            scored.append((score, dict(row)))
    scored.sort(key=lambda pair: -pair[0])
    return [row for _score, row in scored[:limit]]


def _median(values):
    """The middle one, unrounded.

    Rounding here was wrong: this serves money, day counts and a margin
    *fraction*, and two decimal places turns a margin of 0.625 into 0.62.
    Rounding belongs where the number is shown, which knows what kind of
    number it is.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def usual_margin(conn):
    """The fraction of a sale she has actually been keeping.

    The median rather than the mean: one lamp bought for a pound and sold
    for eighty would drag an average into fantasy, and the number is here
    to price the next ordinary thing.

    None when nothing sold has a cost against it - and then there is no
    ceiling, because a ceiling from an assumed margin is a number this
    system made up about her business.
    """
    fractions = []
    for row in conn.execute(
            "SELECT price, paid FROM listings WHERE state='sold' "
            "AND price IS NOT NULL AND paid IS NOT NULL AND price > 0"):
        fractions.append((row["price"] - row["paid"]) / row["price"])
    return _median(fractions)


def worth(conn, category=None, title=None):
    """What it might sell for, and what to pay - or why neither is known.

    Everything here is null unless her own history supports it. That is
    the whole design: standing in a shop being told "no idea" is worth
    more than being told a number that came from nowhere, because she
    will act on the number.
    """
    found = comparables(conn, category=category, title=title)
    prices = [row["price"] for row in found if row["price"] is not None]
    days = []
    for row in found:
        if row["listed_at"] and row["sold_at"]:
            try:
                start = datetime.fromisoformat(row["listed_at"][:10])
                end = datetime.fromisoformat(row["sold_at"][:10])
                days.append((end - start).days)
            except ValueError:
                continue

    margin = usual_margin(conn)
    middle = _median(prices)
    # The ceiling is the median comparable less the margin she usually
    # takes. Deliberately built on the median rather than the top of the
    # range: pricing the next thing off the best day she ever had is how
    # a shelf fills up with things that do not move.
    ceiling = None
    if middle is not None and margin is not None:
        ceiling = round(middle * (1 - margin), 2)

    # A range whose top is several times its bottom is not guidance, and
    # presenting it as one is the failure this whole module is trying to
    # avoid. Say it is wide rather than quietly averaging it away.
    low = min(prices) if prices else None
    high = max(prices) if prices else None
    wide = bool(low and high and low > 0 and high > 3 * low)

    return {
        "comparables": len(prices),
        "low": low,
        "high": high,
        "wide": wide,
        "median": round(middle, 2) if middle is not None else None,
        "typical_days": round(_median(days)) if days else None,
        "usual_margin": round(margin, 3) if margin is not None else None,
        "pay_under": ceiling,
        "examples": [{"title": row["title"], "price": row["price"],
                      "paid": row["paid"]} for row in found[:3]],
    }
