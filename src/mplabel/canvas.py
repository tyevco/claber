"""A freeform label: put what you like where you like, and print it.

The two tags this repo drew before this one are *forms*. An inventory
label is a code, a title and a price in fixed places, and a shelf tag is
a code and a name - the layout is the design, and the caller supplies
only the words. That covers the labels this system mints for itself and
nothing else: a warning to go in a parcel, a price ticket, a return
address, a "fragile" strip, a QR pointing at something that is not an
inventory code at all.

So this kind carries its own layout. A spec is a size and a list of
elements, each of which says where it goes, and `render_canvas` draws
them into the same 1-bit raster the other two produce. Everything
downstream - `build_job`, the print buffers, the signed `/print-tag`
body, printd's journal - is unchanged, because a tag kind is exactly the
seam this needed.

Two decisions are load bearing and are the reason this module exists at
all rather than being a few more arguments on `render_label`:

**Coordinates are in millimetres, and the origin is the drawable box.**
Not dots, because dots are the printer's unit and a spec is written by
whoever has the words rather than whoever has the roll. Not the label's
own corner, because the head does not mark the whole label: 40 dots are
lost on the left of a 48mm one and 32 on the right, the firmware never
sends the feed margin at either end, and ink in any of that is **dropped
rather than printed small** with nothing reporting it. `inventory._
geometry` already computes the box that actually burns, for the rotated
case as well as the flat one, so this lays out inside that box and mm
(0, 0) is its top-left corner. `canvas_mm` is how a client asks how big
that box is, and it is the same function - a canvas laid out against a
second opinion of where the ink lands is the bug this repo has already
paid for once, when a QR drawn from x=22 lost its left finder column and
would not scan while looking intact in a photograph.

**A canvas spec always carries its own size**, which is the opposite of
the rule the other two tags follow. There, `size_mm` unset means "the
host with the roll decides", and that is right for a form: the layout
adapts to whatever paper is loaded. Here the layout *is* millimetres
from a corner, and a coordinate has no meaning without the box it was
measured in - the same design sent to a 48x30 roll and a 4x1in one is
not two sizes of one label, it is one label and one pile of ink in the
wrong places. So the size travels with the design, and the mismatch that
matters instead - a design longer than the die-cut stock - is reported
as `feed_mm` for a person to check against the paper, which is the
number both other tag commands already print for the same reason.
"""

from . import inventory as inventory_mod
from . import qr

# What an element can be. Named rather than inferred so that a typo in a
# hand-written spec is a refusal that says what it expected, instead of
# an element that silently does not appear on the label - which on a
# write-only printer costs a piece of stock to discover.
ELEMENT_TYPES = ("text", "rect", "ellipse", "line", "qr")

# Quiet modules around a QR. Four is the specification's number; the
# inventory label uses two because it is boxed by its own layout and the
# space is worth more there. A freeform canvas has no such guarantee -
# an element can be put hard against another one - so this is the full
# quiet zone rather than the tight one.
QR_QUIET = 4

# Dots per module below which a printed QR is not worth the stock.
#
# Measured rather than chosen: `inventory-label --qr` prints at 5 and a
# photograph of one read first time in the iPhone's stock Camera app,
# which is the only evidence in this repo that anything survives thermal
# bleed. Below it nothing has ever been read, so a small QR is reported
# rather than refused - the person holding the label is the one who can
# say whether a 3mm code on a 4mm label is an experiment or a mistake,
# and refusing would make the experiment impossible.
QR_MIN_SCALE = 5

# Default stroke for an outlined shape, in mm. Thin enough to look like
# a rule, thick enough to survive a burn: one dot at 8 dots/mm is 0.125mm
# and comes out broken.
DEFAULT_STROKE_MM = 0.4

# Default text size in mm, which is the em size Pillow is asked for. The
# inventory label's title runs 18-22 dots, so this is deliberately in the
# same range rather than a web-ish default that would print enormous.
DEFAULT_TEXT_MM = 3.0


class CanvasError(ValueError):
    """A spec that cannot be drawn.

    A ValueError subclass on purpose: `printd.h_print_tag` and
    `web.h_tag_*` both already turn a ValueError from the render path
    into a 400 carrying its sentence, and that sentence - "element 3 is
    a text with no text in it" - is the whole answer. Introducing a new
    exception type would make every one of those refusals a 500."""


def _num(value, what, default=None):
    """A number off a spec, or a refusal naming the field."""
    if value is None:
        if default is None:
            raise CanvasError(f"{what} is missing")
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise CanvasError(f"{what} wants a number, not {value!r}")


def _dots(mm):
    return int(round(mm * inventory_mod.DOTS_PER_MM))


def _box(label_mm, feed_margin=None):
    """The drawable box: (geom, origin_x, origin_y, width, height) in dots.

    One function, because `canvas_mm` and `render_canvas` disagreeing
    about where mm (0, 0) is would put every element on the label a fixed
    distance from where the editor drew it - and it would look right on
    screen the entire time."""
    from .supvan import DEFAULT_MARGIN_DOTS

    if feed_margin is None:
        feed_margin = DEFAULT_MARGIN_DOTS
    geom = inventory_mod._geometry(tuple(label_mm), feed_margin)
    return (geom, geom["left"], geom["top"],
            geom["right"] - geom["left"], geom["bottom"] - geom["top"])


def canvas_mm(label_mm=inventory_mod.DEFAULT_LABEL_MM, feed_margin=None):
    """How big the drawable box is, in mm, for a label of this size.

    This is the co-ordinate space a spec is written in, so it is what a
    client has to be told before it can lay anything out. Deliberately
    derived rather than published as a constant: it depends on the label
    size, on which way round the head forces the label to print, and on
    the measured printable window - three things a browser has no
    business knowing separately."""
    _geom, _x, _y, w, h = _box(label_mm, feed_margin)
    return (round(w / inventory_mod.DOTS_PER_MM, 2),
            round(h / inventory_mod.DOTS_PER_MM, 2))


def _normalise(elements):
    """Check the element list and hand back a clean copy.

    Every refusal names the element by index, because a canvas comes out
    of an editor where they are a list and "element 3" is something a
    person can point at. A message that says only what was wrong, and not
    which of eleven boxes it was wrong about, is a message that sends you
    back to the screen to guess."""
    if elements is None:
        raise CanvasError("a canvas needs an `elements` list")
    if not isinstance(elements, (list, tuple)):
        raise CanvasError("`elements` wants a list")
    if not elements:
        # A blank label is a piece of stock spent on nothing, and this
        # printer gives nothing back that would say it happened.
        raise CanvasError("a canvas with nothing on it would print a "
                          "blank label")

    out = []
    for i, raw in enumerate(elements):
        if not isinstance(raw, dict):
            raise CanvasError(f"element {i} is not an object")
        kind = raw.get("type")
        if kind not in ELEMENT_TYPES:
            raise CanvasError(
                f"element {i} is a {kind!r}; expected one of "
                f"{', '.join(ELEMENT_TYPES)}")
        el = {"type": kind,
              "x": _num(raw.get("x"), f"element {i} x", 0),
              "y": _num(raw.get("y"), f"element {i} y", 0)}

        if kind == "text":
            text = raw.get("text")
            if text is None or str(text) == "":
                raise CanvasError(f"element {i} is a text with no text in it")
            el["text"] = str(text)
            el["size"] = _num(raw.get("size"), f"element {i} size",
                              DEFAULT_TEXT_MM)
            if el["size"] <= 0:
                raise CanvasError(f"element {i} has a size of zero")
            el["w"] = (None if raw.get("w") in (None, "")
                       else _num(raw.get("w"), f"element {i} w"))
            align = raw.get("align") or "left"
            if align not in ("left", "center", "right"):
                raise CanvasError(
                    f"element {i} aligns {align!r}; expected left, center "
                    f"or right")
            el["align"] = align
        elif kind in ("rect", "ellipse"):
            el["w"] = _num(raw.get("w"), f"element {i} w")
            el["h"] = _num(raw.get("h"), f"element {i} h")
            if el["w"] <= 0 or el["h"] <= 0:
                raise CanvasError(
                    f"element {i} is a {kind} with no area")
            el["fill"] = bool(raw.get("fill"))
            el["stroke"] = _num(raw.get("stroke"), f"element {i} stroke",
                                DEFAULT_STROKE_MM)
        elif kind == "line":
            el["x2"] = _num(raw.get("x2"), f"element {i} x2")
            el["y2"] = _num(raw.get("y2"), f"element {i} y2")
            el["stroke"] = _num(raw.get("stroke"), f"element {i} stroke",
                                DEFAULT_STROKE_MM)
        elif kind == "qr":
            text = raw.get("text")
            if text is None or str(text) == "":
                raise CanvasError(
                    f"element {i} is a QR with nothing to carry")
            el["text"] = str(text)
            el["size"] = _num(raw.get("size"), f"element {i} size")
            if el["size"] <= 0:
                raise CanvasError(f"element {i} is a QR with no size")
            ecl = (raw.get("ecl") or "M").upper()
            if ecl not in ("L", "M", "Q", "H"):
                raise CanvasError(
                    f"element {i} asks for error correction {ecl!r}; "
                    f"expected L, M, Q or H")
            el["ecl"] = ecl
        out.append(el)
    return out


def _draw_text(draw, el, ox, oy, cw, ch):
    """Draw one text element and return its inked bbox in canvas dots."""
    font = inventory_mod._font(max(6, _dots(el["size"])))
    x, y = _dots(el["x"]), _dots(el["y"])
    width = cw - x if el["w"] is None else _dots(el["w"])
    width = max(1, width)

    # Wrapped to the box rather than to the label, and the line count is
    # bounded by what is left below `y` - a line drawn past the bottom is
    # in the feed margin, where it is not printed small but dropped.
    line_h = draw.textbbox((0, 0), "Ay", font=font)[3] + 2
    room = max(0, (ch - y) // line_h) if line_h else 0
    lines = inventory_mod._wrap(draw, el["text"], font, width,
                                max_lines=max(1, room or 1))

    x0, y0, x1, y1 = x + width, y, x, y
    cursor = y
    for line in lines:
        lw = inventory_mod._text_width(draw, line, font)
        if el["align"] == "center":
            lx = x + (width - lw) // 2
        elif el["align"] == "right":
            lx = x + width - lw
        else:
            lx = x
        draw.text((ox + lx, oy + cursor), line, font=font, fill=1)
        x0, x1 = min(x0, lx), max(x1, lx + lw)
        y1 = cursor + line_h
        cursor += line_h
    if not lines:
        x0, x1 = x, x
    return (x0, y0, x1, y1)


def _draw_shape(draw, el, ox, oy):
    x0, y0 = _dots(el["x"]), _dots(el["y"])
    x1, y1 = x0 + _dots(el["w"]) - 1, y0 + _dots(el["h"]) - 1
    fn = draw.rectangle if el["type"] == "rect" else draw.ellipse
    if el["fill"]:
        fn([ox + x0, oy + y0, ox + x1, oy + y1], fill=1)
    else:
        fn([ox + x0, oy + y0, ox + x1, oy + y1], outline=1,
           width=max(1, _dots(el["stroke"])))
    return (x0, y0, x1, y1)


def _draw_line(draw, el, ox, oy):
    x0, y0 = _dots(el["x"]), _dots(el["y"])
    x1, y1 = _dots(el["x2"]), _dots(el["y2"])
    width = max(1, _dots(el["stroke"]))
    draw.line([ox + x0, oy + y0, ox + x1, oy + y1], fill=1, width=width)
    half = width // 2
    return (min(x0, x1) - half, min(y0, y1) - half,
            max(x0, x1) + half, max(y0, y1) + half)


def _draw_qr(draw, el, ox, oy, index, notes):
    """Draw one QR, and say how many dots each module got.

    The module size is the whole question of whether a printed QR reads,
    and it is decided here by an integer division that can quietly land
    on 1. So it is reported rather than left to be discovered off the
    paper: `qr_modules` in the notes carries what each one actually got,
    and anything under `QR_MIN_SCALE` is flagged."""
    matrix = qr.render(el["text"], ecl=el["ecl"], quiet=QR_QUIET)
    modules = len(matrix)
    side = _dots(el["size"])
    scale = side // modules
    if scale < 1:
        # A fractional module does not scale into uneven blocks here - it
        # scales into nothing at all. Name the size that would work,
        # because "too small" without a number is a guess-and-reprint
        # loop on a printer that cannot report anything.
        need = round(modules / inventory_mod.DOTS_PER_MM, 1)
        raise CanvasError(
            f"element {index} is a {el['size']:g}mm QR carrying "
            f"{len(el['text'])} characters, which needs {modules} modules "
            f"- at least {need}mm at one dot each, and {need * QR_MIN_SCALE:g}"
            f"mm to be worth printing")

    block = modules * scale
    x0, y0 = _dots(el["x"]), _dots(el["y"])
    for r, row in enumerate(matrix):
        for c, cell in enumerate(row):
            if cell:
                draw.rectangle(
                    [ox + x0 + c * scale, oy + y0 + r * scale,
                     ox + x0 + (c + 1) * scale - 1,
                     oy + y0 + (r + 1) * scale - 1], fill=1)
    notes.setdefault("qr_modules", []).append(
        {"element": index, "dots_per_module": scale, "modules": modules,
         "mm": round(block / inventory_mod.DOTS_PER_MM, 2)})
    if scale < QR_MIN_SCALE:
        want = round(modules * QR_MIN_SCALE / inventory_mod.DOTS_PER_MM, 1)
        notes.setdefault("warnings", []).append(
            f"element {index}: the QR gets {scale} dot"
            f"{'' if scale == 1 else 's'} per module, and nothing under "
            f"{QR_MIN_SCALE} has ever been read off this paper - it wants "
            f"{want:g}mm, or less text in it")
    return (x0, y0, x0 + block - 1, y0 + block - 1)


def render_canvas(spec, label_mm=inventory_mod.DEFAULT_LABEL_MM,
                  feed_margin=None, notes=None):
    """Draw a freeform canvas. Returns (raster, stride, rows).

    Same contract as `inventory.render_label`: MSB-first, a set bit is a
    black dot, head-width lines, drawn in reading orientation and rotated
    once at the end if the label is wider than the head.

    `notes` is a dict the caller owns, filled with what only the renderer
    can know - which elements ran off the label, what each QR's module
    size came out at. It is an out-parameter rather than a return value
    for the reason `ebay.ensure_policies` is: the interesting cases are
    the ones where something is wrong, and a caller that raises on the
    way past never receives a returned value.
    """
    from PIL import Image, ImageDraw

    notes = {} if notes is None else notes
    elements = _normalise(spec.get("elements"))
    geom, ox, oy, cw, ch = _box(label_mm, feed_margin)

    img = Image.new("1", (geom["w"], geom["h"]), 0)   # 0 = no dot
    draw = ImageDraw.Draw(img)

    for i, el in enumerate(elements):
        if el["type"] == "text":
            bbox = _draw_text(draw, el, ox, oy, cw, ch)
        elif el["type"] in ("rect", "ellipse"):
            bbox = _draw_shape(draw, el, ox, oy)
        elif el["type"] == "line":
            bbox = _draw_line(draw, el, ox, oy)
        else:
            bbox = _draw_qr(draw, el, ox, oy, i, notes)

        x0, y0, x1, y1 = bbox
        if x1 < 0 or y1 < 0 or x0 >= cw or y0 >= ch:
            # Nothing of it would reach the paper. That is never what
            # anybody meant, and the printer cannot report a label that
            # came out missing a third of its design - so it is refused
            # here, where there is still something to say about it.
            raise CanvasError(
                f"element {i} is a {el['type']} entirely off the label: it "
                f"sits at {el['x']:g}, {el['y']:g}mm on a canvas that is "
                f"{cw / inventory_mod.DOTS_PER_MM:g} x "
                f"{ch / inventory_mod.DOTS_PER_MM:g}mm")
        if x0 < 0 or y0 < 0 or x1 >= cw or y1 >= ch:
            # Partly off. Printed, because dragging something a
            # millimetre past an edge while laying out is normal and a
            # refusal there would make the editor unusable - but said out
            # loud, because the part that runs off is dropped silently
            # and an instrument has to report when it is off its scale.
            notes.setdefault("clipped", []).append(i)
            notes.setdefault("warnings", []).append(
                f"element {i} ({el['type']}) runs off the label and the "
                f"part that does will not print")

    # Everything above is in the orientation a person holds the label.
    # One rotation at the end, exactly as `render_label` does it, so the
    # sideways case never needs thinking about twice.
    if geom["rotate"]:
        img = img.rotate(90, expand=True)

    canvas = Image.new("1", (inventory_mod.HEAD_DOTS, geom["rows"]), 0)
    canvas.paste(img, (geom["x_off"], 0))

    stride = inventory_mod.HEAD_DOTS // 8
    return canvas.tobytes(), stride, geom["rows"]
