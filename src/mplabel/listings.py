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

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    listing_id    TEXT UNIQUE,
    title         TEXT,
    price         REAL,
    category      TEXT,
    condition     TEXT,
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
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_listing_state ON listings(state);
CREATE INDEX IF NOT EXISTS idx_listing_sold  ON listings(sold_at);

-- Every Facebook email we have classified, so backfill is resumable and
-- we can report on subject lines we do not yet recognise.
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
    listed_at, sold_at,
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
