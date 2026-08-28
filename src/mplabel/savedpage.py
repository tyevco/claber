"""
savedpage.py - pull her listing history out of a saved Marketplace page.

Why this rather than a scraper:

A crawler cannot reach the useful data anyway. facebook.com/robots.txt
disallows automated access, and an unauthenticated fetch of a seller
profile returns a login wall - the listing history, sale dates and prices
only exist inside her logged-in session. Driving that session with
automation is what trips Meta's bot detection, and the cost of a false
positive is her account.

So: she opens the page herself, in her own browser, already logged in,
and saves it. One human page load, zero automated requests, and the file
lands on disk with the same JSON the page used to render itself. This is
a one-time job, which is exactly what you wanted.

    1. Open  facebook.com/marketplace/you/selling
    2. Scroll to the bottom until everything has loaded - the list is
       lazy-loaded, so anything not scrolled into view is not in the file
    3. Ctrl-S / Cmd-S, "Web page, HTML only", save it somewhere
    4. mplabel.py import --format saved ~/Downloads/selling.html

Facebook's markup is machine-generated: class names are rotating hashes
and the DOM shape changes constantly. So the parser reads the JSON that
Facebook embeds in <script> tags to hydrate the page rather than the DOM.
Field names there are far more stable than CSS classes, and where they do
change, the walker matches on any of several known key spellings rather
than one fixed path.

On a real selling page that JSON turned out not to be there any more -
the cards are rendered from data that never lands in a parseable script
tag. So CONSOLE_SNIPPET (`--snippet`) reads the rendered page instead and
downloads clean JSON, which this module also imports. It anchors on the
price text rather than on links, because her own listings do not
necessarily link to /marketplace/item/<id>. A listing that arrives with
no id is keyed by its title as `saved:<slug>`.
"""

import html as htmllib
import json
import re
from datetime import datetime
from pathlib import Path

# A dict is listing-shaped if it carries any of these. Several spellings
# because Facebook uses different ones in different GraphQL responses.
TITLE_KEYS = ("marketplace_listing_title", "listing_title", "title")
PRICE_KEYS = ("listing_price", "price", "formatted_price")
ID_KEYS = ("id", "listing_id", "product_id")

# Guards against false positives: plenty of unrelated blobs on the page
# have a "title" or a "price". Require an id plus something price-like or
# a marketplace-specific key.
STRONG_KEYS = ("marketplace_listing_title", "listing_price",
               "marketplace_listing_category_id", "is_sold", "is_live",
               "marketplace_listing_seller")

SCRIPT_RE = re.compile(
    r"<script[^>]*type=[\"']application/json[\"'][^>]*>(.*?)</script>",
    re.S | re.I)
ANY_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)


def _json_blobs(text, failures=None):
    """Yield every parseable JSON object embedded in the page.

    Blocks that will not parse are counted rather than silently dropped:
    a page saved mid-load has truncated script tags, and a run that
    reports 3 listings and 40 skipped blocks means something went wrong
    with the save, not that she only has 3 listings."""
    # A bare .json file - what CONSOLE_SNIPPET downloads - has no script
    # tags at all. Without this it parsed to zero listings and told her to
    # scroll further, which was never the problem.
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            yield json.loads(stripped)
            return
        except (json.JSONDecodeError, ValueError):
            pass

    seen = 0
    for pattern in (SCRIPT_RE, ANY_SCRIPT_RE):
        for raw in pattern.findall(text):
            raw = raw.strip()
            if not raw or raw[0] not in "{[":
                # Some blocks are `window.X = {...};` - salvage the object.
                m = re.search(r"=\s*(\{.*\})\s*;?\s*$", raw, re.S)
                if not m:
                    continue
                raw = m.group(1)
            try:
                yield json.loads(raw)
                seen += 1
            except (json.JSONDecodeError, ValueError):
                if failures is not None:
                    failures.append(len(raw))
                continue
        if seen:
            return


def _walk(node):
    """Depth-first over nested dicts/lists, yielding every dict."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _looks_like_listing(d):
    if not any(k in d for k in STRONG_KEYS):
        return False
    return any(k in d for k in TITLE_KEYS) or any(k in d for k in PRICE_KEYS)


def _first(d, keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _price(value):
    """Facebook gives price several ways. amount_with_offset is in minor
    units (cents), so it must be divided - reading it raw turns $15 into
    $1500, which would quietly wreck every average in the sheet."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if value.get("amount") not in (None, ""):
            try:
                return float(str(value["amount"]).replace(",", ""))
            except ValueError:
                pass
        if value.get("amount_with_offset") not in (None, ""):
            try:
                offset = float(value.get("offset", 100) or 100)
                return float(value["amount_with_offset"]) / offset
            except (ValueError, ZeroDivisionError):
                pass
        return _price(value.get("formatted_amount") or value.get("text"))
    m = re.search(r"([\d,]+(?:\.\d+)?)", str(value))
    return float(m.group(1).replace(",", "")) if m else None


def _time(value):
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if n > 1e11:            # milliseconds
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


def _state(d):
    if d.get("is_sold") is True:
        return "sold"
    if d.get("is_live") is False:
        return "expired"
    if d.get("is_live") is True:
        return "active"
    return None


def extract(path):
    """Parse a saved page. Returns (listings, stats)."""
    text = Path(path).read_text(errors="replace")
    # Saved pages are sometimes entity-escaped by the browser.
    if "&quot;" in text[:5000] and '"' not in text[:200]:
        text = htmllib.unescape(text)

    found, blobs, candidates = {}, 0, 0
    failures = []
    for blob in _json_blobs(text, failures):
        blobs += 1
        for d in _walk(blob):
            if not _looks_like_listing(d):
                continue
            candidates += 1
            title = _first(d, TITLE_KEYS)
            lid = _first(d, ID_KEYS)
            if not title and not lid:
                continue
            lid = str(lid) if lid else "saved:" + re.sub(
                r"\W+", "-", str(title).lower())[:60]

            rec = {
                "listing_id": lid,
                "title": str(title) if title else None,
                "price": _price(_first(d, PRICE_KEYS)),
                "listed_at": _time(d.get("creation_time")
                                   or d.get("created_time")
                                   or d.get("creation_timestamp")
                                   # what CONSOLE_SNIPPET writes
                                   or d.get("listed_at")),
                "state": _state(d),
                "category": d.get("marketplace_listing_category_id")
                            or d.get("category_name"),
                "condition": d.get("condition"),
            }
            # The same listing appears in several blobs with differing
            # completeness; merge rather than letting the last one win.
            prev = found.get(lid)
            if prev:
                for k, v in rec.items():
                    if v is not None and prev.get(k) is None:
                        prev[k] = v
            else:
                found[lid] = rec

    stats = {"json_blocks": blobs, "candidates": candidates,
             "listings": len(found), "unparseable_blocks": len(failures)}
    return list(found.values()), stats


def import_saved(conn, path, verbose=False):
    """Load a saved page into the listings table."""
    from . import listings as listings_mod

    rows, stats = extract(path)
    for r in rows:
        listings_mod.upsert_listing(
            conn, r["listing_id"], "saved-page",
            title=r["title"], price=r["price"], listed_at=r["listed_at"],
            state=r["state"], category=r["category"],
            condition=r["condition"])
    conn.commit()

    if stats["listings"] == 0:
        print("No listings found. Most likely causes, in order:\n"
              "  1. The page was saved before scrolling to the bottom -\n"
              "     the list lazy-loads, so unscrolled items are absent.\n"
              "  2. It was saved as 'complete' rather than 'HTML only',\n"
              "     which can rewrite the script tags.\n"
              "  3. Facebook changed its field names - run\n"
              "     `python3 savedpage.py <file>` to inspect what parsed.")
    elif stats["unparseable_blocks"] > stats["json_blocks"]:
        print(f"note: {stats['unparseable_blocks']} script block(s) would "
              f"not parse against {stats['json_blocks']} that did - the "
              f"page may have been saved mid-load, so this list is likely "
              f"incomplete.")

    if verbose:
        for r in sorted(rows, key=lambda x: x["listed_at"] or ""):
            print(f"  {str(r['listed_at'])[:10]:<12} "
                  f"${r['price'] or 0:>8.2f}  {(r['state'] or '?'):<8} "
                  f"{(r['title'] or '?')[:44]}")
    return len(rows), stats


# The alternative to saving the file: paste this into the browser console
# on the selling page. Same idea - reads what the page already loaded,
# makes no requests of its own - but downloads clean JSON instead of HTML.
CONSOLE_SNIPPET = r"""
// Paste into DevTools console on facebook.com/marketplace/you/selling
// AFTER scrolling to the bottom so every listing has loaded.
// Reads only what the page has already rendered; sends nothing anywhere.
//
// Anchored on prices, not on links: her own listings do not necessarily
// link to /marketplace/item/<id>, so an href-based scan finds nothing.
// A listing with no id still imports - extract() keys it by title.
(() => {
  const money = (s) => {
    if (/^free$/i.test(s.trim())) return 0;
    const m = String(s).replace(/,/g, '').match(/\$\s*(\d+(?:\.\d+)?)/);
    return m ? parseFloat(m[1]) : null;
  };
  const when = (all) => {
    const rel = all.match(/listed\s+(?:about\s+)?(\d+)\s+(minute|hour|day|week|month)s?\s+ago/i);
    if (rel) {
      const mult = {minute: 60, hour: 3600, day: 86400, week: 604800, month: 2592000};
      return Math.floor(Date.now() / 1000) - parseInt(rel[1], 10) * mult[rel[2].toLowerCase()];
    }
    const abs = all.match(/listed\s+(?:on\s+)?([A-Za-z]{3,9})\s+(\d{1,2})/i);
    if (abs) {
      const now = new Date();
      let d = new Date(abs[1] + ' ' + abs[2] + ', ' + now.getFullYear());
      if (isNaN(d.getTime())) return null;
      if (d > now) d = new Date(abs[1] + ' ' + abs[2] + ', ' + (now.getFullYear() - 1));
      return Math.floor(d.getTime() / 1000);
    }
    return null;
  };

  // --- structure report ------------------------------------------------
  const hrefs = [...document.querySelectorAll('a[href]')]
    .map(a => a.getAttribute('href') || '').filter(h => /marketplace/i.test(h));
  const shapes = {};
  for (const h of hrefs) {
    const k = h.split('?')[0].replace(/\d{6,}/g, '<id>');
    shapes[k] = (shapes[k] || 0) + 1;
  }
  console.log('marketplace anchors:', hrefs.length);
  console.log('anchor shapes:', Object.entries(shapes).sort((a, b) => b[1] - a[1]).slice(0, 12));

  // --- find the repeating card, anchored on the price ------------------
  // Smallest element whose whole text starts with a price. Its card is the
  // nearest ancestor that also carries a title line.
  const priceEls = [...document.querySelectorAll('span,div')].filter(e => {
    const t = (e.innerText || '').trim();
    return t && /^(\$[\d,]|free$)/i.test(t) && t.length < 40 && e.children.length === 0;
  });
  console.log('price elements:', priceEls.length);

  const out = new Map();
  for (const pe of priceEls) {
    let node = pe, lines = [];
    for (let i = 0; i < 6 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      lines = (node.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      if (lines.length >= 2 && lines.length <= 12) break;
    }
    if (lines.length < 2) continue;
    const all = lines.join(' ');
    const priceLine = lines.find(l => /^(\$[\d,]|free$)/i.test(l));
    const status = lines.find(l => /^(sold|pending|out of stock)$/i.test(l)) || '';
    const title = lines
      .filter(l => l !== priceLine && l !== status)
      .filter(l => !/^\d+\s+(view|watch|interested|save|message)/i.test(l))
      .filter(l => !/^(listed|renewed|shipping|free shipping|boost|edit|share|mark as)/i.test(l))
      .filter(l => l.length > 3)
      .sort((x, y) => y.length - x.length)[0] || null;
    if (!title) continue;
    let id = null;
    const link = node.querySelector && node.querySelector('a[href*="/marketplace/item/"]');
    if (link) {
      const m = (link.getAttribute('href') || '').match(/\/marketplace\/item\/(\d+)/);
      if (m) id = m[1];
    }
    const key = id || 'title:' + title.toLowerCase();
    if (out.has(key)) continue;
    const rec = {title: title, price: priceLine ? money(priceLine) : null,
                 listed_at: when(all), is_sold: /^sold$/i.test(status),
                 is_live: !/^sold$/i.test(status)};
    if (id) rec.listing_id = id;
    out.set(key, rec);
  }

  const rows = [...out.values()];
  console.log('LISTINGS FOUND:', rows.length);
  if (!rows.length) {
    console.log('--- first 800 chars of page text, to see the shape ---');
    console.log((document.body.innerText || '').slice(0, 800));
    return;
  }
  console.table(rows.slice(0, 10));
  const blob = new Blob([JSON.stringify(rows, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'marketplace-listings.json';
  a.click();
  console.log('downloaded marketplace-listings.json');
})();
"""


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--snippet":
        print(CONSOLE_SNIPPET)
    elif len(sys.argv) > 1:
        rows, stats = extract(sys.argv[1])
        print(json.dumps(stats, indent=2))
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    else:
        print(__doc__)
