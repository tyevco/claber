"""Draw the inventory label the T50M Pro prints.

One label names one thing on a shelf: its inventory code, enough of the
title to recognise it by, and what it was listed at. The code is the
handle - `listings.inventory_code`, four characters, never reused - so it
is the biggest thing on the label and everything else is context.

Drawn straight into a 1-bit raster rather than rendered from a PDF. The
printhead is 384 dots across and burns a dot or does not, so there is no
grey to preserve and nothing gained by going through a rasteriser: at
this size the thresholding is the hard part, and doing it here means the
layout can be measured in the same dots it prints in.

The QR variant carries the same code as the printed characters. That is
the point of it - a phone reads the box in the loft without anyone
squinting at four characters on thermal paper - so the two must never
disagree, and `render_label` derives both from the same string rather
than taking them separately.
"""

from . import marker as marker_mod
from . import qr

# 8 dots/mm on a 48mm head. Both are the printer's, not ours to choose.
DOTS_PER_MM = 8
HEAD_DOTS = 384

# Label size in *reading* orientation - the way round you hold it - as
# (across, down) millimetres. The printer's own axes are derived from it
# below, and are not always the same two.
DEFAULT_LABEL_MM = (48, 30)
DEFAULT_HEIGHT_MM = 30          # the short axis of the default label

# What the head actually marks on the label, measured with
# `supvan-test-print --style edges`: the left 40 dots and the right 32
# never reach the paper, leaving 312 - 39mm, not 48 - and the window is
# **not centred** under the head.
#
# This was guessed at first, as a symmetric 12-dot margin, on the reading
# that "the media runs centred under the bar and 48mm stock is not
# exactly 48mm". Both halves of that were wrong, and the cost was a QR
# drawn from x=22: it lost its left finder column and would not scan,
# while looking intact in a photograph. Unequal insets are the tell that
# the media sits off-centre rather than the head being narrow.
#
# Measured on one roll. Different stock will differ, and the edge test is
# how to find out rather than a thing to assume.
PRINTABLE_LEFT_DOTS = 40
PRINTABLE_RIGHT_DOTS = 32
PRINTABLE_DOTS = HEAD_DOTS - PRINTABLE_LEFT_DOTS - PRINTABLE_RIGHT_DOTS

# A cosmetic breathing space inside that window, not a guard against the
# edge loss - the window above is what handles that.
SIDE_MARGIN_DOTS = 8

# Quiet modules around the shelf marker, on all four sides.
MARKER_QUIET = 2

# The fraction of the content height the marker band may take. The rest
# is for the words it captions.
MARKER_HEIGHT = 0.42


def _font(size):
    """Pillow's built-in scalable face.

    Deliberately not a system TrueType path: the Pi has no font package
    guaranteed, and a label that renders in the test suite and comes out
    blank on the device is the failure this whole project is organised
    against. `load_default` ships inside Pillow, which is already a hard
    dependency for the rasteriser."""
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


def _text_width(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def _fit(draw, text, font_for, max_width, start, floor=10):
    """The largest size at which `text` fits `max_width`, and its font."""
    size = start
    while size > floor:
        font = font_for(size)
        if _text_width(draw, text, font) <= max_width:
            return font, size
        size -= 1
    return font_for(floor), floor


def _wrap(draw, text, font, max_width, max_lines):
    """Greedy word wrap, ellipsing whatever will not fit.

    Her titles run long and share their first sixty characters, so the
    tail is often the only thing that tells two apart - but a label is
    30mm and something has to give. The code above it is what actually
    identifies the item; this is only here to be recognised by."""
    if max_lines <= 0:
        return []
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if _text_width(draw, trial, font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
            if len(lines) == max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and words:
        # Did everything actually land? If not, mark the truncation.
        used = sum(len(x.split()) for x in lines)
        if used < len(words):
            last = lines[-1]
            while last and _text_width(draw, last + "...", font) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = last + "..."
    return lines


def _geometry(label_mm, feed_margin):
    """Turn a label size into everything the drawing needs.

    The printhead is 384 dots and does not turn, so a label wider than
    48mm can only be printed with its long axis running down the *feed*.
    That is not a preference - it is the only orientation the hardware
    allows, and it is why a 4x1in label is drawn sideways and rotated at
    the end rather than laid out directly.

    The consequence worth spelling out: after that rotation the feed axis
    is the reading orientation's **width**, so the feed margin - whose
    columns the firmware never sends - has to be inset on left and right
    rather than top and bottom. Insetting the wrong pair puts ink in the
    dead band, where it is dropped rather than printed small.
    """
    w = round(label_mm[0] * DOTS_PER_MM)
    h = round(label_mm[1] * DOTS_PER_MM)
    rotate = w > HEAD_DOTS
    across = h if rotate else w

    # Only PRINTABLE_DOTS of the head reach the label, and not centred.
    # A label whose across dimension is wider than that is drawn to the
    # printable width instead: the paper is still 48mm, but the part of
    # it this printer can mark is 39mm, so laying out to 48 puts ink
    # where nothing burns.
    if rotate:
        # Rotated already means the long axis runs down the feed, so
        # `across` is the label's *short* side. If that does not fit the
        # printable window, no orientation does - refuse rather than
        # silently crop a label to the middle of itself.
        if across > PRINTABLE_DOTS:
            raise ValueError(
                f"a {label_mm[0]}x{label_mm[1]}mm label needs {across} dots "
                f"across the head and only {PRINTABLE_DOTS} of them reach "
                f"the paper; neither way round fits")
    elif w > PRINTABLE_DOTS:
        # Not rotated, so the label is at most head-width and the excess
        # is the edge loss itself: the paper is still 48mm, but the part
        # of it this printer marks is 39mm. Lay out to what burns.
        w = PRINTABLE_DOTS
        across = PRINTABLE_DOTS

    if rotate:
        left, right = feed_margin + 4, w - feed_margin - 4
        top, bottom = SIDE_MARGIN_DOTS, h - SIDE_MARGIN_DOTS
    else:
        left, right = SIDE_MARGIN_DOTS, w - SIDE_MARGIN_DOTS
        top, bottom = feed_margin + 4, h - feed_margin - 4

    return {"w": w, "h": h, "rotate": rotate, "across": across,
            "left": left, "right": right, "top": top, "bottom": bottom,
            "x_off": PRINTABLE_LEFT_DOTS + (PRINTABLE_DOTS - across) // 2,
            "rows": w if rotate else h}


def _to_raster(box, geom):
    """A reading-orientation box, in final raster coordinates.

    `Image.rotate(90, expand=True)` turns anticlockwise, so a point
    (x, y) on a w-wide canvas lands at (y, w - 1 - x). Settled by
    rotating a marked pixel and looking at where it went, because this is
    exactly the arithmetic that looks right and is off by a reflection.
    """
    x0, y0, x1, y1 = box
    if geom["rotate"]:
        x0, y0, x1, y1 = y0, geom["w"] - 1 - x1, y1, geom["w"] - 1 - x0
    return x0 + geom["x_off"], y0, x1 + geom["x_off"], y1


def _price_text(price):
    return f"${price:.2f}" if isinstance(price, (int, float)) else str(price)


def _symbol_placement(geom, modules, cap=140):
    """Where a square symbol of `modules` modules goes, in reading
    coordinates, and how many dots each module gets.

    Returns (x0, y0, side_in_dots, dots_per_module). One function owns
    this so that `render_label` and `marker_box` cannot drift: a caller
    cropping a photograph to the wrong rectangle reads the marker at the
    wrong pitch and gets either nothing or, worse, something."""
    top, bottom = geom["top"], geom["bottom"]
    side = min(bottom - top, cap)
    scale = max(1, side // modules)
    block = modules * scale
    return (geom["left"], top + (bottom - top - block) // 2, block, scale)


def _marker_band(geom):
    """Where the marker band goes, in reading coordinates.

    Returns (x0, y0, w, h, scale). The marker is one by four, so it sits
    *under the text* as a band rather than taking a square bite out of a
    label that is mostly words - which is what the square version did,
    and it pushed the title into three cramped lines.

    Sized by height first. Filling the width would be the obvious rule
    and it is the wrong one: at full width on a 48mm label the band
    would be a third of the label tall, and the text it is supposed to
    caption has nowhere left to go."""
    rows = marker_mod.ROWS + 2 * MARKER_QUIET
    cols = marker_mod.COLS + 2 * MARKER_QUIET
    room_w = geom["right"] - geom["left"]
    room_h = geom["bottom"] - geom["top"]
    scale = max(1, min(room_w // cols, int(room_h * MARKER_HEIGHT) // rows))
    w, h = cols * scale, rows * scale
    return geom["left"], geom["bottom"] - h, w, h, scale


def marker_box(label_mm=DEFAULT_LABEL_MM, feed_margin=None):
    """The marker's rectangle in the final raster: (x0, y0, x1, y1).

    Exposed because reading one back needs a crop that holds the marker
    and not the title above it - `marker.read_image` locates the grid
    from the bounding box of the ink, so a crop that catches a letter
    samples the whole thing at the wrong pitch."""
    from .supvan import DEFAULT_MARGIN_DOTS
    if feed_margin is None:
        feed_margin = DEFAULT_MARGIN_DOTS
    geom = _geometry(label_mm, feed_margin)
    x0, y0, w, h, _scale = _marker_band(geom)
    return _to_raster((x0, y0, x0 + w - 1, y0 + h - 1), geom)


def media_box(label_mm=DEFAULT_LABEL_MM, feed_margin=None):
    """Where the label itself sits in the raster: (x0, y0, x1, y1).

    Not the same as the raster. Every printhead line is 384 dots because
    the bar is, but a 1in-wide label only covers 203 of them and the rest
    is head hanging off the media. A preview of the whole raster shows
    those as broad empty margins, which reads as a badly laid out label
    and is nothing of the sort."""
    from .supvan import DEFAULT_MARGIN_DOTS
    if feed_margin is None:
        feed_margin = DEFAULT_MARGIN_DOTS
    geom = _geometry(label_mm, feed_margin)
    return (geom["x_off"], 0,
            geom["x_off"] + geom["across"] - 1, geom["rows"] - 1)


def reads_sideways(label_mm=DEFAULT_LABEL_MM):
    """Whether this size has to be printed with its long axis down the
    feed - which is to say, whether the raster is a quarter turn from the
    way you hold the label."""
    return round(label_mm[0] * DOTS_PER_MM) > HEAD_DOTS


def render_label(code, title=None, price=None, with_qr=False,
                 label_mm=DEFAULT_LABEL_MM, ecl="M", feed_margin=None,
                 with_marker=False):
    """Draw one label and return (raster, stride, rows).

    Raster is MSB-first with a set bit meaning a black dot, which is the
    convention everywhere else in this codebase; `supvan.build_job`
    repacks it for the printhead. Returning the raster rather than an
    image keeps this function testable without a device or a viewer.

    `label_mm` is (across, down) in the orientation you hold the label,
    not the printer's. A label wider than the 48mm head is drawn sideways
    and rotated at the end, because the head does not turn - see
    `_geometry`.

    `feed_margin` is the blank run the firmware feeds at each end of the
    label, and its columns are **not sent** - so anything drawn there is
    silently dropped rather than printed small. Defaulting it to the same
    constant `build_job` uses is what keeps the two agreeing; the price
    sat in that dead band and came out with its bottom sheared off.
    """
    from PIL import Image, ImageDraw
    from .supvan import DEFAULT_MARGIN_DOTS

    if feed_margin is None:
        feed_margin = DEFAULT_MARGIN_DOTS

    geom = _geometry(label_mm, feed_margin)
    img = Image.new("1", (geom["w"], geom["h"]), 0)   # 0 = no dot
    draw = ImageDraw.Draw(img)

    left, right = geom["left"], geom["right"]
    top, bottom = geom["top"], geom["bottom"]

    if with_marker:
        # The marker carries 20 bits where a QR version 1 carries 152, so
        # it buys far bigger modules - and module size is what survives
        # thermal bleed. Being one by four, it goes along the bottom and
        # the text keeps the full width above it.
        grid = marker_mod.render(code, quiet=MARKER_QUIET)
        bx, by, _w, band_h, scale = _marker_band(geom)
        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                if cell:
                    draw.rectangle(
                        [bx + c * scale, by + r * scale,
                         bx + (c + 1) * scale - 1, by + (r + 1) * scale - 1],
                        fill=1)
        bottom -= band_h + 6
        text_left = left
    elif with_qr:
        # Sized to the space, then rounded down to a whole number of dots
        # per module - a fractional module scales into uneven blocks and
        # is the classic reason a printed code will not scan.
        matrix = qr.render(code, ecl=ecl, quiet=2)
        _x, y0, block, scale = _symbol_placement(geom, len(matrix))
        for r, row in enumerate(matrix):
            for c, cell in enumerate(row):
                if cell:
                    draw.rectangle(
                        [left + c * scale, y0 + r * scale,
                         left + (c + 1) * scale - 1, y0 + (r + 1) * scale - 1],
                        fill=1)
        text_left = left + block + 14
    else:
        text_left = left

    text_width = right - text_left

    # The code, as large as the space allows - and the space is now
    # vertical as often as horizontal. Fitting on width alone sized it
    # for the whole label while the marker band was taking two fifths of
    # the height, and the title underneath was pushed into the band.
    room_h = bottom - top
    share = 0.38 if with_marker else 0.45
    start = min(60 if with_qr else 90, max(20, int(room_h * share)))
    code_font, _ = _fit(draw, code, _font, text_width, start=start)
    code_h = draw.textbbox((0, 0), code, font=code_font)[3]
    draw.text((text_left, top), code, font=code_font, fill=1)

    # With a marker band along the bottom the price shares the code's
    # line instead of sitting under the title. It is the same two facts
    # either way, and on a label this size the alternative is a title
    # with nowhere to go.
    price_inline = with_marker and price is not None
    if price_inline:
        ptext = _price_text(price)
        pfont = _font(max(14, int(code_h * 0.42)))
        pw = _text_width(draw, ptext, pfont)
        ph = draw.textbbox((0, 0), ptext, font=pfont)[3]
        draw.text((right - pw, top + code_h - ph), ptext, font=pfont, fill=1)

    y = top + code_h + 8

    # A rule under the code, so the eye separates it from the title.
    draw.rectangle([text_left, y, right, y + 1], fill=1)
    y += 8

    if title:
        title_font = _font(18 if with_qr else 22)
        line_h = draw.textbbox((0, 0), "Ay", font=title_font)[3] + 2
        limit = bottom - (0 if price_inline or price is None else 26)
        room = max(0, (limit - y) // line_h)
        for line in _wrap(draw, title, title_font, text_width,
                          max_lines=min(room, 3)):
            # Never past `bottom`: `max(1, ...)` used to force a line
            # even where there was no room for one, and on a label with
            # a marker band that line landed *inside* the marker. The
            # code still read - the parity carried it - which is exactly
            # how it would have reached paper unnoticed.
            if y + line_h > limit:
                break
            draw.text((text_left, y), line, font=title_font, fill=1)
            y += line_h

    if price is not None and not price_inline:
        price_text = _price_text(price)
        price_font = _font(26)
        w = _text_width(draw, price_text, price_font)
        h = draw.textbbox((0, 0), price_text, font=price_font)[3]
        draw.text((right - w, bottom - h), price_text,
                  font=price_font, fill=1)

    # Sideways until now if the label is wider than the head. Rotate
    # once, at the end, so every measurement above is in the orientation
    # a person reads.
    if geom["rotate"]:
        img = img.rotate(90, expand=True)

    # The media runs centred under a fixed 384-dot bar, so a narrower
    # label is centred in a head-width raster rather than pushed left.
    # Every printhead line is still 48 bytes; `build_job` needs that.
    canvas = Image.new("1", (HEAD_DOTS, geom["rows"]), 0)
    canvas.paste(img, (geom["x_off"], 0))

    stride = HEAD_DOTS // 8
    return canvas.tobytes(), stride, geom["rows"]


# Ruler spacing, in dots. 8 dots is 1mm on this head, so a minor tick is a
# millimetre and a numbered one is a centimetre.
RULER_MINOR = 8
RULER_MAJOR = 40
RULER_LABEL = 80

# Insets for the edge gauge, in dots out from each edge of the sent
# area. The outermost complete rectangle is the printable inset.
#
# It stopped at 32 first, and the reported loss was about 40 - so every
# rectangle was gone on that side and the gauge could only say "more than
# 32". A gauge has to outrange the thing it measures, which is the same
# mistake as drawing one inside the band that is never sent.
RULER_INSETS = (0, 8, 16, 24, 32, 40, 48)


def render_ruler(width_dots=HEAD_DOTS, rows=DEFAULT_HEIGHT_MM * DOTS_PER_MM,
                 feed_margin=None):
    """Draw a calibration target and return (raster, stride, rows).

    Answers, off one label: how wide the print really is, how far down
    the feed it goes, how much each edge loses, and which end is fed
    first. Nothing here is symmetric in either axis, so a mirror or a
    flip shows without being measured for.

    **The first version put its graduations where they could never
    appear.** `split_into_buffers` starts the image at row `margin_top`
    and stops `margin_bottom` short, and the firmware feeds blank for
    both - so on a 240-row label with the default 8-dot margins, rows
    0-7 and 232-239 are not sent at all. The edge rules and every minor
    tick (5 dots deep) lived inside that dead band. The scale that did
    survive was the one whose numbers sat at y=19.

    So the rule this is built on: **the instrument must not live in the
    region it is measuring.** The graduations sit well inboard, where
    they print whatever the edges do, and the edges get a separate gauge
    - nested rectangles at 0, 8, 16, 24 and 32 dots in, each labelled
    just inside its own top line. The outermost *complete* rectangle is
    the printable inset, its label is the number, and a rectangle broken
    on one side only says which side.

    Drawn in **device coordinates** - full head width, no side margin, no
    rotation - unlike `render_label`, which centres a label of a given
    size under the head. A target that had been centred and rotated first
    would be measuring this module's arithmetic rather than the printer.
    """
    from PIL import Image, ImageDraw

    if width_dots <= 0 or rows <= 0:
        raise ValueError("a ruler needs a positive width and height")
    if width_dots > HEAD_DOTS:
        raise ValueError(
            f"{width_dots} dots is wider than the {HEAD_DOTS}-dot head")
    if feed_margin is None:
        from .supvan import DEFAULT_MARGIN_DOTS
        feed_margin = DEFAULT_MARGIN_DOTS

    # The rows that are actually transmitted. Drawing outside these is
    # drawing into a band the firmware replaces with blank feed.
    top = feed_margin
    bottom = rows - feed_margin - 1
    if bottom - top < 40:
        raise ValueError(
            f"{rows} rows less {2 * feed_margin} of margin leaves too "
            f"little to measure")

    img = Image.new("1", (width_dots, rows), 0)
    draw = ImageDraw.Draw(img)
    font = _font(15)
    small = _font(13)
    last_x = width_dots - 1

    # --- the edge gauge: four combs, one per edge
    #
    # Nested rectangles read well but do not scale: past about 32 dots
    # they cut through the middle of the label and collide with
    # everything. A comb is compact and can reach any depth, which
    # matters - the gauge stopped at 32, the reported loss was around 40,
    # and a gauge that cannot outrange what it measures says only "more
    # than 32".
    #
    # Each mark is a small filled square whose near edge sits exactly at
    # its inset, with its number alongside. Both are lost together when
    # that column or row is lost, so **the smallest number still fully
    # printed on a side is that side's printable inset**. One frame at
    # inset 0 is kept as the overall witness.
    draw.rectangle((0, top, last_x, bottom), outline=1)

    for idx, inset in enumerate(RULER_INSETS):
        tag = str(inset)
        tw = _text_width(draw, tag, small)
        # top and bottom: stepped along x so the numbers never stack
        ax = 60 + idx * 20
        # `top + inset`, not `inset`. Measured from the first row the
        # device is actually given - an inset-0 mark at row 0 would sit
        # in the band that is never sent, and would read as the printer
        # losing an edge it was never shown.
        ty = top + inset
        draw.rectangle((ax, ty, ax + 5, ty + 5), fill=1)
        draw.text((ax + 9, ty), tag, font=small, fill=1)
        by = bottom - inset
        draw.rectangle((ax, by - 5, ax + 5, by), fill=1)
        draw.text((ax + 9, by - 15), tag, font=small, fill=1)
        # left and right: stepped down y for the same reason
        ay = top + 80 + idx * 18
        draw.rectangle((inset, ay, inset + 5, ay + 5), fill=1)
        draw.text((inset + 9, ay - 4), tag, font=small, fill=1)
        rx = last_x - inset
        draw.rectangle((rx - 5, ay, rx, ay + 5), fill=1)
        draw.text((rx - 9 - tw, ay - 4), tag, font=small, fill=1)

    # --- the scales, far enough in to survive whatever the edges do,
    #     and on opposite sides so their numbers never share a corner.
    #     Both were crowded into the top left first and the labels
    #     overprinted each other, which on thermal paper is the same as
    #     not printing them.
    sy = top + 72
    sx = last_x - 96

    for pos in range(0, width_dots, RULER_MINOR):
        depth = 15 if pos % RULER_LABEL == 0 else (
            10 if pos % RULER_MAJOR == 0 else 5)
        draw.line((pos, sy, pos, sy + depth), fill=1)
        if pos % RULER_LABEL == 0:
            tag = str(pos)
            # Not if it would run into the feed scale's line. An
            # overprinted number is not a smaller number, it is an
            # unreadable one.
            if pos + 2 + _text_width(draw, tag, font) < sx - 44:
                draw.text((pos + 2, sy + 17), tag, font=font, fill=1)
    draw.line((0, sy, last_x, sy), fill=1)

    for pos in range(top, bottom, RULER_MINOR):
        depth = 15 if (pos - top) % RULER_LABEL == 0 else (
            10 if (pos - top) % RULER_MAJOR == 0 else 5)
        draw.line((sx - depth, pos, sx, pos), fill=1)
        if (pos - top) % RULER_LABEL == 0:
            tag = str(pos)
            draw.text((sx - 19 - _text_width(draw, tag, font), pos + 2),
                      tag, font=font, fill=1)
    draw.line((sx, top, sx, bottom), fill=1)

    # --- origin marker and feed direction, at 0,0 and nowhere else
    # Clear of the bottom comb, which sweeps up from the left corner
    # through where this used to sit.
    ox, oy = 200, sy + 40
    if oy + 80 < bottom:
        draw.rectangle((ox, oy, ox + 19, oy + 19), fill=1)
        draw.text((ox + 25, oy + 2), "0,0", font=font, fill=1)
        draw.line((ox + 9, oy + 30, ox + 9, oy + 62), fill=1)
        draw.polygon((ox + 2, oy + 52, ox + 16, oy + 52, ox + 9, oy + 62),
                     fill=1)
        draw.text((ox + 22, oy + 38), "FEED", font=font, fill=1)
        draw.text((ox, oy + 68), f"{width_dots}x{rows}", font=font, fill=1)

    stride = (width_dots + 7) // 8
    if width_dots != stride * 8:
        canvas = Image.new("1", (stride * 8, rows), 0)
        canvas.paste(img, (0, 0))
        img = canvas
    return img.tobytes(), stride, rows


# The edge test's staircase: 8 steps of 8 dots, so it reads a loss of up
# to 56 dots on any side.
EDGE_STEPS = 8
EDGE_PITCH = 8
EDGE_BAR = 6
EDGE_UNIT = 12


def render_edge_test(width_dots=HEAD_DOTS, rows=DEFAULT_HEIGHT_MM * DOTS_PER_MM,
                     feed_margin=None):
    """Draw the edge test and return (raster, stride, rows).

    One question only: **where does each edge actually start printing?**

    The ruler answers it with a 5x5 dot square per inset, which is about
    0.6mm, and a reading off one came back saying the left 40 dots are
    lost - flatly contradicted by a QR on another label that printed
    whole starting at x=22. Both cannot be right, and a mark that small
    on thermal stock, photographed at an angle, is not the thing to
    settle it with.

    So: eight bars per edge, marching in at 8 dots each, and each bar is
    a different **length** - the innermost is longest. That makes every
    bar self-identifying. Count in from the long end, or measure any
    single bar you can see, and you know which one it is without needing
    a number beside it to survive as well. High contrast, no small type,
    nothing to lose to glare.

    Read it: the shortest bar still fully printed is the printable inset
    on that side. All eight present means nothing is lost.
    """
    from PIL import Image, ImageDraw

    if width_dots <= 0 or rows <= 0:
        raise ValueError("an edge test needs a positive width and height")
    if width_dots > HEAD_DOTS:
        raise ValueError(
            f"{width_dots} dots is wider than the {HEAD_DOTS}-dot head")
    if feed_margin is None:
        from .supvan import DEFAULT_MARGIN_DOTS
        feed_margin = DEFAULT_MARGIN_DOTS

    top = feed_margin
    bottom = rows - feed_margin - 1
    longest = EDGE_STEPS * EDGE_UNIT
    if bottom - top < longest + 60 or width_dots < 2 * longest + 120:
        raise ValueError(
            f"{width_dots}x{rows} is too small for the edge test")

    img = Image.new("1", (width_dots, rows), 0)
    draw = ImageDraw.Draw(img)
    font = _font(15)
    last_x = width_dots - 1

    ay = top + 26                      # where the side staircases hang from
    ax = 132                           # where the top/bottom ones start

    for i in range(EDGE_STEPS):
        near = i * EDGE_PITCH
        length = (i + 1) * EDGE_UNIT

        # left and right: vertical bars, hanging down from a common line
        draw.rectangle((near, ay, near + EDGE_BAR - 1, ay + length), fill=1)
        far = last_x - near
        draw.rectangle((far - EDGE_BAR + 1, ay, far, ay + length), fill=1)

        # top and bottom: horizontal bars, running in from a common line
        ty = top + near
        draw.rectangle((ax, ty, ax + length, ty + EDGE_BAR - 1), fill=1)
        by = bottom - near
        draw.rectangle((ax, by - EDGE_BAR + 1, ax + length, by), fill=1)

    # A legend where nothing can clip it. No per-bar numbers on purpose:
    # a number is exactly as losable as the mark it names, which is what
    # went wrong with the comb.
    # In the clear band between the top and bottom staircases - the
    # obvious spot below them is where the bottom one lives.
    cx, cy = ax + 4, top + EDGE_STEPS * EDGE_PITCH + 20
    draw.text((cx, cy), "EDGE TEST", font=font, fill=1)
    draw.text((cx, cy + 18), f"{EDGE_STEPS} bars, {EDGE_PITCH} dots apart",
              font=font, fill=1)
    draw.text((cx, cy + 36), "longest = innermost", font=font, fill=1)
    draw.text((cx, cy + 54), f"{width_dots}x{rows}", font=font, fill=1)

    stride = (width_dots + 7) // 8
    if width_dots != stride * 8:
        canvas = Image.new("1", (stride * 8, rows), 0)
        canvas.paste(img, (0, 0))
        img = canvas
    return img.tobytes(), stride, rows

def to_image(raster, stride, rows, scale=1):
    """A viewable image of a raster, black on white.

    Inverted relative to the raster's own convention on purpose: a set
    bit is a burnt dot, and a burnt dot is black on white paper."""
    from PIL import Image
    img = Image.frombytes("1", (stride * 8, rows), raster)
    img = img.point(lambda v: 0 if v else 255, "L")
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale),
                         Image.NEAREST)
    return img
