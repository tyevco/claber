"""
label.py - turn a letter-size Marketplace/USPS label PDF into a PDF that is
exactly 4.00 x 6.00 in, upright.

Exactness matters. At 203 dpi a 4 x 6 label is 812 x 1218 dots, and a raw
ZPL or TSPL printer will clip or wrap anything wider than its print head.
So rather than cropping to the ink plus a margin, this snaps to the nominal
label size and centres the ink inside it.
"""

import io

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

PT_PER_IN = 72.0
TARGET_W = 4.0 * PT_PER_IN     # 288
TARGET_H = 6.0 * PT_PER_IN     # 432
TOL = 4.0                      # pt of slop before we complain

# Helvetica advance widths, in ems. Digits are all one width in this face,
# but letters are not - W is nearly twice M's neighbours - so the white
# patch behind the code has to be measured, not assumed, or a code like
# WXY spills out past its background.
_ASCENT = 0.72
_DESCENT = 0.22
_HELVETICA_W = {
    "0": .556, "1": .556, "2": .556, "3": .556, "4": .556,
    "5": .556, "6": .556, "7": .556, "8": .556, "9": .556,
    "A": .667, "B": .667, "C": .722, "D": .722, "E": .667, "F": .611,
    "G": .778, "H": .722, "I": .278, "J": .500, "K": .667, "L": .556,
    "M": .833, "N": .722, "O": .778, "P": .667, "Q": .778, "R": .722,
    "S": .667, "T": .611, "U": .722, "V": .667, "W": .944, "X": .667,
    "Y": .667, "Z": .611,
}


def _text_width(text, size):
    return sum(_HELVETICA_W.get(ch, 0.6) for ch in text) * size


def inspect(pdf_path, page_index=0):
    """Locate the ink on a page and work out which way the text runs.

    Returns (bbox, rotation, page_size) with bbox as (x0, y0, x1, y1) in
    PDF points measured from the bottom-left, and rotation as the
    clockwise turn needed to make the text read left-to-right.

    This is the whole page's ink, which is the right answer only when the
    label has the page to itself. `find_label` is what handles the rest."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        objs = _page_objects(page)
        if not objs:
            raise ValueError("no drawable content on that page")
        extent = _extent(objs)
        rot, _source = _rotation(page.chars, extent)
        page_size = (page.width, page.height)

    return _to_pdf_space(extent, page_size), rot, page_size


# --------------------------------------------------------------- regions
#
# A Marketplace label has its page to itself, so the ink's own bounding
# box *is* the label and `_snap` can work straight off it. Almost nothing
# else arrives that way. eBay, PirateShip and the carriers' own sites
# hand out a US Letter page with the 4x6 label on the top half and a
# packing slip or a fold-here strip below it, and the ink then spans the
# whole sheet - which `_snap` refuses, correctly, because a page of ink
# is not a 4x6 label. The label has to be *found* before it can be
# cropped to.
#
# This is a recursive XY-cut, the oldest trick in document layout:
# project the ink onto an axis, split it wherever there is a blank band
# wide enough to be a deliberate separator, and repeat on the pieces. It
# runs only when the whole page does not already fit, so the one path
# with real Marketplace labels behind it is not touched by any of it.

# 0.25in. Narrower than this is line spacing and the gaps inside a
# barcode, not a gutter somebody put there to separate two things.
MIN_GUTTER = 18.0
# 1.5in. Below this a block is furniture - a page number, "fold here", a
# cut line's caption - rather than something that could be a label.
MIN_REGION = 108.0
# A runner-up whose area is at least this fraction of the winner's is a
# coin toss, and a coin toss here prints the packing slip and throws the
# label away. Refuse, and make the caller say which.
AMBIGUOUS_AT = 0.60


def _page_objects(page):
    return (page.chars + page.lines + page.rects
            + page.curves + page.images)


def _extent(objs):
    """Bounding box of some objects, in pdfplumber's top-down space."""
    return (min(o["x0"] for o in objs), min(o["top"] for o in objs),
            max(o["x1"] for o in objs), max(o["bottom"] for o in objs))


def _to_pdf_space(extent, page_size):
    """pdfplumber measures down from the top; pypdf measures up from the
    bottom, and the mediabox this ends up as is pypdf's."""
    x0, top, x1, bottom = extent
    _pw, ph = page_size
    return (x0, ph - bottom, x1, ph - top)


def _runs(spans, min_gap):
    """Contiguous runs of `spans`, broken at every blank band >= min_gap.

    Spans closer together than that become one run: the gaps inside a
    barcode and between two lines of an address are not separators, and
    splitting on them would shatter a label into its individual words."""
    out = []
    for lo, hi in sorted(spans):
        if out and lo - out[-1][1] < min_gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [tuple(r) for r in out]


def _cut(objs, axis, min_gap):
    """Split objects into the bands one axis's blank gutters leave."""
    lo_key, hi_key = ("top", "bottom") if axis == "y" else ("x0", "x1")
    runs = _runs([(o[lo_key], o[hi_key]) for o in objs], min_gap)
    if len(runs) < 2:
        return [objs]
    pieces = [[] for _ in runs]
    for o in objs:
        # An object cannot straddle a gutter: a gutter is by construction
        # a band no object's own span reaches into, so its midpoint
        # settles which run it belongs to.
        mid = (o[lo_key] + o[hi_key]) / 2
        for i, (lo, hi) in enumerate(runs):
            if lo <= mid <= hi:
                pieces[i].append(o)
                break
    return [p for p in pieces if p]


def _carve(objs, min_gap, depth=0):
    """Recursive XY-cut. Horizontal gutters first, because stacked is how
    a label and its packing slip come out of every site that makes one."""
    if len(objs) < 2 or depth >= 6:
        return [objs]
    for axis in ("y", "x"):
        pieces = _cut(objs, axis, min_gap)
        if len(pieces) > 1:
            out = []
            for piece in pieces:
                out.extend(_carve(piece, min_gap, depth + 1))
            return out
    return [objs]


def _char_rotation(ch):
    """The clockwise turn that makes one character read left-to-right."""
    a, b, _c, d = ch["matrix"][:4]
    if abs(a) < 1e-6 and abs(d) < 1e-6:
        # b > 0 means the baseline runs upward, so turn it clockwise.
        return 90 if b > 0 else 270
    return 180 if a < 0 else 0


def _rotation(chars, extent):
    """Which way this block's text runs, and how good that answer is.

    A majority, not `chars[0]`. The first character is whatever the
    content stream happened to draw first, and on a page carrying a
    rotated label above an upright packing slip that is a coin toss
    between the two orientations - which is exactly the page this
    function was added for."""
    votes = {}
    for ch in chars:
        r = _char_rotation(ch)
        votes[r] = votes.get(r, 0) + 1
    if votes:
        # Ties go to the smaller turn, so the answer is at least stable.
        return max(votes.items(), key=lambda kv: (kv[1], -kv[0]))[0], "text"

    # Nothing but barcodes and images: some carriers' labels carry no
    # extractable text at all, and those used to be called rotation 0 and
    # then refused for being "6.00 x 4.00 in, larger than the 4 x 6 in
    # target" - a measurement that is right attached to a diagnosis that
    # is wrong. The shape is the only evidence left. It says the label is
    # lying on its side; it cannot say which way up, so this is a guess,
    # `--rotate` overrides it, and the answer says which it was.
    x0, top, x1, bottom = extent
    return (90 if (x1 - x0) > (bottom - top) else 0), "aspect"


def _fits(width, height):
    """Does a block fit inside 4x6, either way round?"""
    return ((width <= TARGET_W + TOL and height <= TARGET_H + TOL)
            or (width <= TARGET_H + TOL and height <= TARGET_W + TOL))


def _describe(extent):
    w, h = extent[2] - extent[0], extent[3] - extent[1]
    return (f"{w / PT_PER_IN:.2f} x {h / PT_PER_IN:.2f} in at "
            f"({extent[0] / PT_PER_IN:.2f}, {extent[1] / PT_PER_IN:.2f}) in "
            f"from the top left")


def find_label(pdf_path, page_index=0, region=None):
    """Find the 4x6 label on a page and say which way up it is.

    Returns (extent, rotation, page_size, info), extent in pdfplumber's
    top-down space. `region` is a 1-based index into the candidates, for
    the page where more than one block could be the label."""
    with pdfplumber.open(pdf_path) as pdf:
        if not 0 <= page_index < len(pdf.pages):
            raise ValueError(
                f"there is no page {page_index + 1} in this PDF - it has "
                f"{len(pdf.pages)}")
        page = pdf.pages[page_index]
        objs = _page_objects(page)
        if not objs:
            raise ValueError("no drawable content on that page")
        page_size = (page.width, page.height)
        chars = page.chars

        whole = _extent(objs)
        # The label alone on its page: every Marketplace label, and any
        # PDF that is already a 4x6. Take it without carving, so the one
        # path with real labels behind it cannot be moved by a layout
        # heuristic.
        if _fits(whole[2] - whole[0], whole[3] - whole[1]):
            rot, source = _rotation(chars, whole)
            return whole, rot, page_size, {
                "regions_found": 1, "region": 1, "rotation_source": source}

        found = []
        for block in _carve(objs, MIN_GUTTER):
            ext = _extent(block)
            w, h = ext[2] - ext[0], ext[3] - ext[1]
            if _fits(w, h) and max(w, h) >= MIN_REGION:
                found.append((w * h, ext))
        found.sort(key=lambda t: -t[0])

        if not found:
            w, h = whole[2] - whole[0], whole[3] - whole[1]
            raise ValueError(
                f"ink is {w / PT_PER_IN:.2f} x {h / PT_PER_IN:.2f} in and "
                f"no block on this page is a 4 x 6 label on its own - this "
                f"may not be a shipping label, or the label may not be "
                f"separated from the rest of the page by a clear margin")

        if region is not None:
            if not 1 <= region <= len(found):
                raise ValueError(
                    f"there is no region {region} on this page - it has "
                    f"{len(found)}")
            pick = region - 1
        elif len(found) > 1 and found[1][0] >= found[0][0] * AMBIGUOUS_AT:
            # Two blocks this alike could both be the label, and guessing
            # wrong prints the packing slip and spends the stock anyway.
            # Say where they are and let the caller choose.
            raise ValueError(
                f"{len(found)} blocks on this page could be the label and "
                f"they are too alike to choose between - say which with "
                f"--region. "
                + "; ".join(f"{i + 1}: {_describe(e)}"
                            for i, (_a, e) in enumerate(found)))
        else:
            pick = 0

        _area, ext = found[pick]
        inside = [c for c in chars
                  if ext[0] <= (c["x0"] + c["x1"]) / 2 <= ext[2]
                  and ext[1] <= (c["top"] + c["bottom"]) / 2 <= ext[3]]
        rot, source = _rotation(inside, ext)
        return ext, rot, page_size, {
            "regions_found": len(found), "region": pick + 1,
            "rotation_source": source}


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


def _unrotate(box, source_rot, page_size):
    """A box in pdfplumber's (rotated) view, as pypdf's mediabox sees it.

    pdfplumber applies a page's own /Rotate before reporting coordinates;
    `page.mediabox` is the unrotated page. Marketplace labels carry no
    /Rotate, so the two agreed and nothing here had to know this - but an
    arbitrary label PDF may well carry one, and a mediabox written in the
    wrong space crops some other corner of the page entirely."""
    source_rot %= 360
    if source_rot == 0:
        return box
    x0, y0, x1, y1 = box
    pw, ph = page_size          # as pdfplumber sees it, i.e. already turned
    if source_rot == 180:
        return (pw - x1, ph - y1, pw - x0, ph - y0)
    # At 90 and 270 the page's own axes are swapped, so pdfplumber's
    # width is the unrotated page's height. /Rotate 90 displays the sheet
    # turned clockwise, which sends the unrotated (X, Y) to (Y, ph - X);
    # this is that inverted. The two turns are easy to write down the
    # wrong way round and the result is a crop of some other corner of
    # the page, so they are pinned by rendering rather than by argument.
    if source_rot == 90:
        return (ph - y1, x0, ph - y0, x1)
    return (y0, pw - x1, y1, pw - x0)


def to_4x6(src, dst, page_index=0, force_rotation=None, region=None):
    """Write a 4 x 6 in, upright version of src to dst.

    Handles a label alone on its page, a label sharing a US Letter sheet
    with a packing slip, and a label with no extractable text at all.
    Anything it cannot resolve is refused with what it measured rather
    than cropped to a guess: a wrong crop spends the stock either way,
    and a plausible-looking one is worse than an error.

    Returns a dict describing what was done, for logging."""
    extent, detected_rot, page_size, info = find_label(src, page_index,
                                                       region=region)
    rot = force_rotation if force_rotation is not None else detected_rot
    if force_rotation is not None:
        info["rotation_source"] = "forced"
    bbox = _to_pdf_space(extent, page_size)
    box = _snap(bbox, rot, page_size)

    reader = PdfReader(src)
    page = reader.pages[page_index]

    source_rot = int(page.get("/Rotate") or 0) % 360
    rect = RectangleObject(_unrotate(box, source_rot, page_size))
    page.mediabox = rect
    page.cropbox = rect
    page.trimbox = rect
    page.artbox = rect
    page.bleedbox = rect
    if rot:
        # `rotate` adds to whatever the page already carried, which is
        # what is wanted: the detected turn was measured in the rotated
        # view a reader will already be applying.
        page.rotate(rot)

    writer = PdfWriter()
    writer.add_page(page)
    with open(dst, "wb") as fh:
        writer.write(fh)

    w, h = box[2] - box[0], box[3] - box[1]
    if rot in (90, 270):
        w, h = h, w
    info.update({"rotation": rot,
                 "page": page_index + 1,
                 "source_rotation": source_rot,
                 "ink_bbox": tuple(round(v, 1) for v in bbox),
                 "crop_bbox": tuple(round(v, 1) for v in box),
                 "size_in": (round(w / PT_PER_IN, 3), round(h / PT_PER_IN, 3))})
    return info


def _code_placement(mediabox, rot, code, size, margin):
    """Where the code goes so that it lands top-right *as printed*.

    The page is not stored upright: `to_4x6` leaves a landscape mediabox
    with /Rotate 90, so page space and printed space disagree. Under a 90
    degree clockwise display rotation, page +y runs to the right of the
    print and page -x runs up it - which puts the printed top-right corner
    at the page's top-*left*, with the text on a +90 (CCW) matrix. That is
    the same convention the label's own text uses.

    Returns (matrix, tx, ty, box) in page coordinates, where box is
    (x, y, w, h) for the white patch behind the digits."""
    x0, y0, x1, y1 = mediabox
    textw = _text_width(code, size)
    asc, desc = _ASCENT * size, _DESCENT * size
    pad = 0.35 * size
    rot = rot % 360

    if rot == 90:
        tx, ty = x0 + margin + asc, y1 - margin - textw
        return ((0, 1, -1, 0), tx, ty,
                (tx - asc - pad, ty - pad, asc + desc + 2 * pad,
                 textw + 2 * pad))
    if rot == 180:
        tx, ty = x0 + margin + textw, y0 + margin + asc
        return ((-1, 0, 0, -1), tx, ty,
                (tx - textw - pad, ty - desc - pad, textw + 2 * pad,
                 asc + desc + 2 * pad))
    if rot == 270:
        tx, ty = x1 - margin - asc, y0 + margin + textw
        return ((0, -1, 1, 0), tx, ty,
                (tx - desc - pad, ty - textw - pad, asc + desc + 2 * pad,
                 textw + 2 * pad))
    tx, ty = x1 - margin - textw, y1 - margin - asc
    return ((1, 0, 0, 1), tx, ty,
            (tx - pad, ty - desc - pad, textw + 2 * pad,
             asc + desc + 2 * pad))


def _overlay_pdf(mediabox, content):
    """A one-page PDF holding nothing but `content`, sized to match.

    Written out by hand rather than with reportlab, which is a test-only
    dependency here and would otherwise have to be installed on the Pi -
    the short dependency list is deliberate. Helvetica is one of the 14
    faces every PDF reader carries, so nothing needs embedding."""
    x0, y0, x1, y1 = mediabox
    stream = content.encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox "
         f"[{x0:.4f} {y0:.4f} {x1:.4f} {y1:.4f}] /Resources << /Font << "
         f"/MPCode 5 0 R >> >> /Contents 4 0 R >>").encode("ascii"),
        (b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
         + stream + b"\nendstream"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    size = len(objs) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode("ascii")
    return bytes(out)


def stamp_code(src, dst, code, size=8.0, margin=6.0):
    """Write src to dst with `code` printed small in the top right.

    Deliberately not applied to the archived label: `cli.print_label`
    stamps a throwaway copy on its way to the printer, so re-printing can
    never double-stamp and the file on disk stays as Facebook sent it.

    The white patch behind the digits is not decoration - the top right of
    a USPS label is not reliably blank, and black on black would be
    useless."""
    code = str(code).upper()
    if not code or not set(code) <= set(_HELVETICA_W):
        raise ValueError(
            f"code must be digits and capital letters, got {code!r}")

    # clone_from, so the page is attached to the writer before the merge.
    # Merging into a detached page is deprecated in pypdf and documented as
    # unreliable; it removes silently in pypdf 7.
    writer = PdfWriter(clone_from=str(src))
    page = writer.pages[0]
    mediabox = [float(v) for v in page.mediabox]
    rot = int(page.get("/Rotate") or 0)
    (a, b, c, d), tx, ty, (bx, by, bw, bh) = _code_placement(
        mediabox, rot, code, size, margin)

    content = (
        "q\n"
        "1 1 1 rg\n"
        f"{bx:.3f} {by:.3f} {bw:.3f} {bh:.3f} re f\n"
        "0 0 0 rg\n"
        f"BT /MPCode {size:.3f} Tf "
        f"{a} {b} {c} {d} {tx:.3f} {ty:.3f} Tm ({code}) Tj ET\n"
        "Q\n")

    overlay = PdfReader(io.BytesIO(_overlay_pdf(mediabox, content))).pages[0]
    page.merge_page(overlay)

    with open(dst, "wb") as fh:
        writer.write(fh)
    return {"code": code, "rotation": rot,
            "origin": (round(tx, 1), round(ty, 1))}


def extract_label_fields(pdf_4x6):
    """Read the bits that only exist on the label, never in the email:
    tracking number, the buyer's postal address, weight, service."""
    import re

    with pdfplumber.open(pdf_4x6) as pdf:
        page = pdf.pages[0]
        # Confined to the crop. Cropping a PDF sets the boxes and leaves
        # the content stream alone - a rasteriser honours that and prints
        # only the window, but pdfplumber goes on listing every object on
        # the sheet. On a Marketplace label there is nothing else there,
        # which is why this never mattered; on a label sharing a page
        # with a packing slip the last CITY ST ZIP on the page belongs to
        # the slip, and `ship_to` would come back as an address that is
        # not on the parcel.
        text = (page.within_bbox(page.bbox).extract_text() or "")
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
