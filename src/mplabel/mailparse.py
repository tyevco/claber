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

# The other selling channel's mail. She sells on eBay as well, and an
# eBay sale sends "Your shipping label is ready" with the label as a PDF
# attachment - the same shape of event as a Marketplace label email, so
# it belongs on the same print path rather than in a second one.
#
# ASSUMED: no eBay .eml has been read. The PDF has - it crops, rotates
# and parses correctly through the existing pipeline - but the envelope
# around it has not, so the sending domain is inference. `ebay.com` with
# the usual boundary match covers the subdomains eBay actually sends
# from (`reply.ebay.com`, `members.ebay.com`); if her mail arrives from
# an eBay domain that is not under it, `mplabel scan` is what says so.
EBAY_SENDER_DOMAINS = ("ebay.com",)

# An eBay order number: two digits, five, five. This *is* verified - it
# is the only part of a real eBay label email that has been seen, and it
# was on the attachment's own filename (`ebay-label-17-15142-59571.pdf`).
# Which makes the filename the reliable source for it and the body the
# speculative one, the opposite way round from Facebook.
EBAY_ORDER_RE = re.compile(r"\b(\d{2}-\d{5}-\d{5})\b")

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


def _headers(msg):
    """The three fields every channel's mail carries in the same place."""
    out = {"subject": _decode(msg.get("Subject")),
           "message_id": _decode(msg.get("Message-ID"))}
    try:
        received = parsedate_to_datetime(msg.get("Date"))
    except Exception:
        received = None
    if received:
        out["received_at"] = received.isoformat()
    return out, received


def parse_ebay(msg):
    """What an eBay label email can be trusted for, which is not much.

    No eBay .eml has ever been read, so the body's shape is unknown and
    nothing here walks it looking for an item title, a buyer or a price.
    That reads like a gap and is the deliberate half of this: an eBay
    label-ready email has several dollar amounts in it - item, postage,
    total - and picking one blind would write a number into `price` that
    is indistinguishable from a parsed one the moment it lands, and from
    there into revenue and into the Sheet. The same rule
    `estimate_postage` and `landed_cost` are built on: no basis, no
    figure. She can type the price on the order screen, where it is
    marked as hers.

    What *is* trustworthy is the label, which has been read: tracking,
    weight, service and the recipient all come off the PDF through
    `extract_label_fields`, and on a real eBay label all four were
    right. So the usual precedence is inverted here - on Facebook mail
    the email outranks the label, and on eBay mail the label is very
    nearly all there is.

    The order number is the one body field worth reaching for, because
    it is cheap to recognise (NN-NNNNN-NNNNN) and wrong only if eBay
    stops using that format. `process_message` also reads it off the
    attachment's filename, which is where it has actually been seen."""
    out, _ = _headers(msg)
    out["channel"] = "ebay"
    blocks, links = body_blocks(msg)
    haystack = out["subject"] + " " + " ".join(blocks) + " " + " ".join(links)
    m = EBAY_ORDER_RE.search(haystack)
    if m:
        out["order_id"] = m.group(1)
    return out


def parse(msg):
    """Extract every field the email can give us.

    Dispatches on the sender: the two selling channels write completely
    different mail and share only their headers. Merging the two
    parsers would mean Facebook's regexes running over eBay's body,
    which is how a phrase that means one thing on one channel gets read
    as another - the same reason `listings.classify` and
    `goodwill.classify` were never merged."""
    if is_from_ebay(msg) and not is_from_facebook(msg):
        return parse_ebay(msg)
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


def _sender_domains(msg):
    """Every address domain in From and Reply-To, lowercased."""
    header = (_decode(msg.get("From")) + " "
              + _decode(msg.get("Reply-To"))).lower()
    return re.findall(r"[^\s<>@]+@([a-z0-9.\-]+)", header)


def _from_any(msg, known_domains):
    """Does this message come from one of these domains?

    Match on the address domain with a boundary, not as a substring:
    `noreply@marketplace.facebook.com.example.net` contains
    "marketplace.facebook.com" and is not Facebook."""
    return any(d == known or d.endswith("." + known)
               for d in _sender_domains(msg) for known in known_domains)


def is_from_facebook(msg):
    """True only if the message really came from Facebook.

    The IMAP search is `FROM "facebook"`, which matches the whole From
    header - so a display name is enough to get a message fetched, and
    anyone can set one. Everything downstream trusts this: a subject
    reading "New Marketplace order for <item>" is taken as a sale and
    creates a sold listing, so a spoof would quietly poison the numbers."""
    return _from_any(msg, SENDER_DOMAINS)


def is_from_ebay(msg):
    """True only if the message really came from eBay.

    Same teeth as `is_from_facebook` and needed more, not less: this one
    gates a *printer*. A label email is fetched, cropped and burned onto
    physical stock without anyone looking at it, so a display name
    reading "eBay" must not be enough to get a PDF printed."""
    return _from_any(msg, EBAY_SENDER_DOMAINS)


def classify_ebay(subject):
    """Name an eBay subject, for the mailbox survey.

    Subject-only, because `scan` fetches headers rather than bodies and
    deliberately does not know the sender - so this has to be a phrase
    nothing else says, and "shipping label" is. Kept separate from
    `listings.classify` and `goodwill.classify` for the reason those two
    were never merged: one classifier answering for every channel is how
    a purchase ends up in the sell-through numerator.

    Only the one kind, because only the one kind has ever been seen. The
    rest of eBay's mail stays in the survey's unrecognised list, which is
    the honest place for it and the reason the survey widened."""
    if subject and "shipping label" in subject.lower():
        return "ebay_shipping_label"
    return None


def is_label_email(msg):
    """Is this a shipping label to crop and print?

    The two channels get different subject rules, and the asymmetry is
    deliberate rather than an oversight.

    Facebook's mailbox traffic is almost entirely about her own selling,
    so "label" or "shipping" anywhere in the subject is a safe net and
    has been in production for months.

    eBay's is not. eBay mails about everything - her purchases included -
    and "Your order has shipped" would sail through the loose rule while
    being about a parcel coming *to* her. So eBay needs the two words
    together: "Your shipping label is ready" is the subject that carries
    an attachment, and nothing else claims to."""
    if is_from_facebook(msg):
        subject = _decode(msg.get("Subject")).lower()
        return "label" in subject or "shipping" in subject
    if is_from_ebay(msg):
        return "shipping label" in _decode(msg.get("Subject")).lower()
    return False
