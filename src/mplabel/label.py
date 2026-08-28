"""
label.py - turn a letter-size Marketplace/USPS label PDF into a PDF that is
exactly 4.00 x 6.00 in, upright.

Exactness matters. At 203 dpi a 4 x 6 label is 812 x 1218 dots, and a raw
ZPL or TSPL printer will clip or wrap anything wider than its print head.
So rather than cropping to the ink plus a margin, this snaps to the nominal
label size and centres the ink inside it.
"""

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

PT_PER_IN = 72.0
TARGET_W = 4.0 * PT_PER_IN     # 288
TARGET_H = 6.0 * PT_PER_IN     # 432
TOL = 4.0                      # pt of slop before we complain


def inspect(pdf_path, page_index=0):
    """Locate the ink and work out which way the text runs.

    Returns (bbox, rotation, page_size) with bbox as (x0, y0, x1, y1) in
    PDF points measured from the bottom-left, and rotation as the clockwise
    turn needed to make the text read left-to-right."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        objs = (page.chars + page.lines + page.rects
                + page.curves + page.images)
        if not objs:
            raise ValueError("no drawable content on that page")

        x0 = min(o["x0"] for o in objs)
        x1 = max(o["x1"] for o in objs)
        top = min(o["top"] for o in objs)
        bottom = max(o["bottom"] for o in objs)
        pw, ph = page.width, page.height

        rot = 0
        if page.chars:
            a, b, _c, d = page.chars[0]["matrix"][:4]
            if abs(a) < 1e-6 and abs(d) < 1e-6:
                # b > 0 means the baseline runs upward, so turn it clockwise
                rot = 90 if b > 0 else 270
            elif a < 0:
                rot = 180

    # pdfplumber measures down from the top; pypdf measures up from the bottom
    return (x0, ph - bottom, x1, ph - top), rot, (pw, ph)


def _snap(bbox, rot, page_size):
    """Grow or shrink the crop to exactly 4 x 6 in, centred on the ink."""
    x0, y0, x1, y1 = bbox
    pw, ph = page_size

    # A 90 or 270 turn swaps which page axis becomes the label's width.
    want_w, want_h = (TARGET_H, TARGET_W) if rot in (90, 270) else (TARGET_W, TARGET_H)

    have_w, have_h = x1 - x0, y1 - y0
    if have_w > want_w + TOL or have_h > want_h + TOL:
        raise ValueError(
            f"ink is {have_w/72:.2f} x {have_h/72:.2f} in, larger than the "
            f"{want_w/72:.0f} x {want_h/72:.0f} in target - this may not be a "
            f"4x6 label")

    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    nx0, ny0 = cx - want_w / 2, cy - want_h / 2

    # Keep the window on the page; shift rather than clip if it would hang off.
    nx0 = min(max(nx0, 0), max(0, pw - want_w))
    ny0 = min(max(ny0, 0), max(0, ph - want_h))
    return (nx0, ny0, nx0 + want_w, ny0 + want_h)


def to_4x6(src, dst, page_index=0, force_rotation=None):
    """Write a 4 x 6 in, upright version of src to dst.

    Returns a dict describing what was done, for logging."""
    bbox, detected_rot, page_size = inspect(src, page_index)
    rot = force_rotation if force_rotation is not None else detected_rot
    box = _snap(bbox, rot, page_size)

    reader = PdfReader(src)
    page = reader.pages[page_index]

    rect = RectangleObject(box)
    page.mediabox = rect
    page.cropbox = rect
    page.trimbox = rect
    page.artbox = rect
    page.bleedbox = rect
    if rot:
        page.rotate(rot)

    writer = PdfWriter()
    writer.add_page(page)
    with open(dst, "wb") as fh:
        writer.write(fh)

    w, h = box[2] - box[0], box[3] - box[1]
    if rot in (90, 270):
        w, h = h, w
    return {"rotation": rot,
            "ink_bbox": tuple(round(v, 1) for v in bbox),
            "crop_bbox": tuple(round(v, 1) for v in box),
            "size_in": (round(w / PT_PER_IN, 3), round(h / PT_PER_IN, 3))}


def extract_label_fields(pdf_4x6):
    """Read the bits that only exist on the label, never in the email:
    tracking number, the buyer's postal address, weight, service."""
    import re

    with pdfplumber.open(pdf_4x6) as pdf:
        text = pdf.pages[0].extract_text() or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {}

    m = re.search(r"\b(9\d{3}(?:[ -]?\d{4}){4}[ -]?\d{2})\b", text)
    if m:
        out["tracking"] = re.sub(r"[ -]", "", m.group(1))

    m = re.search(r"(\d+\s*lbs?(?:\s*\d+\s*oz)?|\d+\s*oz)", text, re.I)
    if m:
        out["weight"] = m.group(1).strip()

    m = re.search(r"USPS ([A-Z][A-Z ]+?)(?:\u2122|TM|$)", text)
    if m:
        out["service"] = f"USPS {m.group(1).strip().title()}"

    # Sender block first, recipient second. Anchor on the last CITY ST ZIP.
    czip = re.compile(r"^(?:.*\s)?[A-Z][A-Z .'-]* [A-Z]{2} \d{5}(?:-\d{4})?$")
    idxs = [i for i, ln in enumerate(lines) if czip.match(ln)]
    if idxs:
        end = idxs[-1]
        block = lines[max(0, end - 2):end + 1]
        block = [re.sub(r"\s+(RDC|SCF|NDC)\s*\d+\s*$", "", b) for b in block]
        out["ship_to"] = ", ".join(b for b in block if b)

    return out
