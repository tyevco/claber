"""
mailparse.py - pull order data out of a Facebook Marketplace notification.

Most of what you want is in the email body, and the body is far more stable
than the label PDF. What is genuinely NOT in the email:

    tracking number      only on the label barcode block
    buyer postal address only on the label
    parcel weight        only on the label
    carrier + service    only on the label

Everything else - buyer name, item title, price, ship-by deadline, listing
id, order id - comes from here. The label is parsed as a fallback only.

Uses html.parser from the stdlib rather than BeautifulSoup, to keep the
dependency list short on a Pi.
"""

import html as htmllib
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

SENDER_DOMAINS = ("marketplace.facebook.com", "facebookmail.com")

# Blocks that are boilerplate in every Marketplace email, so never an item title
BOILERPLATE = re.compile(
    r"^(hi\b|your prepaid|please ship|how to |try reusing|you can also|"
    r"avoid usps|generate your|the package|payment to|this message|"
    r"meta platforms|to help keep|thanks|the facebook|see order|"
    r"to be shipped|shipped|\d+\.$)", re.I)


class _Extract(HTMLParser):
    """Collect visible text as discrete blocks, plus every href."""

    BREAKERS = {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3",
                "h4", "table", "ul", "ol", "span", "a"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.links = []
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)
        if tag in self.BREAKERS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BREAKERS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self):
        text = re.sub(r"[\s\xa0]+", " ", "".join(self._buf)).strip()
        if text:
            self.blocks.append(text)
        self._buf = []

    def close(self):
        super().close()
        self._flush()


def _decode(value):
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def body_blocks(msg):
    """Return (blocks, links) for the richest body part available."""
    plain = html = None
    for part in msg.walk():
        if part.get_filename() or part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/html" and html is None:
            html = text
        elif ctype == "text/plain" and plain is None:
            plain = text

    if html:
        p = _Extract()
        p.feed(html)
        p.close()
        return p.blocks, p.links
    if plain:
        blocks = [re.sub(r"\s+", " ", ln).strip()
                  for ln in htmllib.unescape(plain).splitlines() if ln.strip()]
        links = re.findall(r"https?://\S+", plain)
        return blocks, links
    return [], []


def _resolve_ship_by(fragment, received):
    """'Fri, Sep 4' carries no year. Anchor it to the received date and
    roll forward if that lands in the past."""
    if not fragment:
        return None
    frag = fragment.replace(",", " ")
    # Drop a leading weekday, but only a real one - a bare "Jan 2" must not
    # lose its month to a generic three-letter match.
    frag = re.sub(r"^(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)"
                  r"(?:day|sday|nesday|rsday|urday)?\s+", "",
                  frag.strip(), flags=re.I)
    frag = re.sub(r"\s+", " ", frag).strip()
    base = received or datetime.now()
    # Parse with the year already in the string, rather than parsing
    # year-less and patching the year in afterwards. A bare "%b %d"
    # defaults to 1900, which is not a leap year, so "Feb 29" raised
    # ValueError and the ship-by date was silently lost. Supplying the
    # year also sidesteps Python 3.15, which changes what year-less
    # parsing does. Try the received year first, then the next one, so a
    # label that crosses New Year still rolls forward.
    for fmt in ("%b %d", "%B %d"):
        for year in (base.year, base.year + 1):
            try:
                guess = datetime.strptime(f"{frag} {year}", f"{fmt} %Y")
            except ValueError:
                continue
            if guess.date() < base.date() - timedelta(days=1):
                continue        # already past; that means it is next year
            return guess.date().isoformat()
    return None


def parse(msg):
    """Extract every field the email can give us."""
    blocks, links = body_blocks(msg)
    flat = " ".join(blocks)
    out = {}

    try:
        received = parsedate_to_datetime(msg.get("Date"))
    except Exception:
        received = None
    if received:
        out["received_at"] = received.isoformat()

    out["subject"] = _decode(msg.get("Subject"))
    out["message_id"] = _decode(msg.get("Message-ID"))

    m = re.search(r"shipping label for (.+?)(?:'s|\u2019s) order", flat, re.I)
    if m:
        out["buyer"] = m.group(1).strip()

    m = re.search(r"ship this item by\s+((?:[A-Z][a-z]{2},?\s+)?"
                  r"[A-Z][a-z]{2,8}\s+\d{1,2})", flat)
    if not m:
        m = re.search(r"carrier by\s+((?:[A-Z][a-z]{2},?\s+)?"
                      r"[A-Z][a-z]{2,8}\s+\d{1,2})", flat)
    if m:
        out["ship_by_raw"] = m.group(1).strip()
        out["ship_by"] = _resolve_ship_by(m.group(1), received)

    # The price is its own block; the title is the block right before it.
    price_re = re.compile(r"^\$\s?([\d,]+(?:\.\d{2})?)$")
    for i, blk in enumerate(blocks):
        m = price_re.match(blk)
        if not m:
            continue
        out["price"] = float(m.group(1).replace(",", ""))
        for j in range(i - 1, max(-1, i - 4), -1):
            cand = blocks[j]
            if 3 < len(cand) < 200 and not BOILERPLATE.match(cand):
                out["item"] = cand
                break
        break

    if "price" not in out:
        m = re.search(r"\$\s?([\d,]+\.\d{2})", flat)
        if m:
            out["price"] = float(m.group(1).replace(",", ""))

    for url in links:
        m = re.search(r"/marketplace/item/(\d+)", url)
        if m:
            out["listing_id"] = m.group(1)
        m = re.search(r"[?&]order_id=(\d+)", url)
        if m:
            out["order_id"] = m.group(1)

    return out


def attachment(msg, suffix=".pdf"):
    """Return (filename, bytes) of the first matching attachment."""
    for part in msg.walk():
        fn = _decode(part.get_filename())
        if fn and fn.lower().endswith(suffix):
            payload = part.get_payload(decode=True)
            if payload:
                return fn, payload
    return None, None


def is_from_facebook(msg):
    """True only if the message really came from Facebook.

    The IMAP search is `FROM "facebook"`, which matches the whole From
    header - so a display name is enough to get a message fetched, and
    anyone can set one. Everything downstream trusts this: a subject
    reading "New Marketplace order for <item>" is taken as a sale and
    creates a sold listing, so a spoof would quietly poison the numbers.

    Match on the address domain with a boundary, not as a substring:
    `noreply@marketplace.facebook.com.example.net` contains
    "marketplace.facebook.com" but is not Facebook."""
    header = (_decode(msg.get("From")) + " "
              + _decode(msg.get("Reply-To"))).lower()
    domains = re.findall(r"[^\s<>@]+@([a-z0-9.\-]+)", header)
    return any(d == known or d.endswith("." + known)
               for d in domains for known in SENDER_DOMAINS)


def is_label_email(msg):
    if not is_from_facebook(msg):
        return False
    subject = _decode(msg.get("Subject")).lower()
    return "label" in subject or "shipping" in subject
