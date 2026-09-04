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

# The label is drawn a little narrower than the head and centred, because
# the media runs centred under the bar and 48mm stock is not exactly 48mm.
SIDE_MARGIN_DOTS = 12

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
    if across > HEAD_DOTS:
        raise ValueError(
            f"a {label_mm[0]}x{label_mm[1]}mm label needs {across} dots "
            f"across the head and it has {HEAD_DOTS}; neither way round "
            f"fits, so this size cannot be printed on a 48mm head")

    if rotate:
        left, right = feed_margin + 4, w - feed_margin - 4
        top, bottom = SIDE_MARGIN_DOTS, h - SIDE_MARGIN_DOTS
    else:
        left, right = SIDE_MARGIN_DOTS, w - SIDE_MARGIN_DOTS
        top, bottom = feed_margin + 4, h - feed_margin - 4

    return {"w": w, "h": h, "rotate": rotate, "across": across,
            "left": left, "right": right, "top": top, "bottom": bottom,
            "x_off": (HEAD_DOTS - across) // 2, "rows": w if rotate else h}


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


def render_ruler(width_dots=HEAD_DOTS, rows=DEFAULT_HEIGHT_MM * DOTS_PER_MM):
    """Draw a calibration target and return (raster, stride, rows).

    Answers, off one label, the things no amount of unit testing can:

      - **how wide the print really is.** The head is 384 dots on paper
        and the media is not exactly 48mm; the across-head scale runs to
        the last dot, so whichever tick is the last one visible is the
        answer.
      - **how far down the feed it really goes**, the same way.
      - **which end of the label is fed first.** The feed arrow and the
        `0,0` block sit at the origin corner and nowhere else, so the
        origin is wherever they came out.
      - **whether the line order is still right.** Nothing here is
        symmetric in either axis, so a mirror or a flip is visible at a
        glance rather than something you have to measure for.

    Deliberately drawn in **device coordinates** - full head width, no
    side margin, no rotation - unlike `render_label`, which centres a
    label of a given size under the head. A calibration target that had
    been centred and rotated first would be measuring this module's
    arithmetic rather than the printer.
    """
    from PIL import Image, ImageDraw

    if width_dots <= 0 or rows <= 0:
        raise ValueError("a ruler needs a positive width and height")
    if width_dots > HEAD_DOTS:
        raise ValueError(
            f"{width_dots} dots is wider than the {HEAD_DOTS}-dot head")

    img = Image.new("1", (width_dots, rows), 0)
    draw = ImageDraw.Draw(img)
    font = _font(13)

    last_x, last_y = width_dots - 1, rows - 1

    # A one-dot rule along all four edges. A missing side is the whole
    # point: it means that edge is outside what the printer will burn.
    draw.line((0, 0, last_x, 0), fill=1)
    draw.line((0, last_y, last_x, last_y), fill=1)
    draw.line((0, 0, 0, last_y), fill=1)
    draw.line((last_x, 0, last_x, last_y), fill=1)

    def scale(length, across):
        """Ticks hanging off one edge. `across` picks the axis."""
        for pos in range(0, length, RULER_MINOR):
            if pos % RULER_LABEL == 0:
                depth, label = 17, str(pos)
            elif pos % RULER_MAJOR == 0:
                depth, label = 11, None
            else:
                depth, label = 5, None
            if across:
                draw.line((pos, 1, pos, depth), fill=1)
                if label and pos:
                    draw.text((pos + 2, 19), label, font=font, fill=1)
            else:
                draw.line((1, pos, depth, pos), fill=1)
                if label and pos:
                    draw.text((19, pos - 6), label, font=font, fill=1)
        # The far end always gets a tick, whatever the spacing lands on:
        # the last dot is the number being looked for.
        if across:
            draw.line((last_x, 1, last_x, 17), fill=1)
        else:
            draw.line((1, last_y, 17, last_y), fill=1)

    scale(width_dots, across=True)
    scale(rows, across=False)

    # The last dot's own number, at the far end of each scale. "Is 383
    # there?" is the entire width question, and counting ticks back from
    # an edge that may itself be missing is exactly the sum nobody wants
    # to be doing while holding a warm label.
    w_lab = str(last_x)
    draw.text((last_x - 4 - _text_width(draw, w_lab, font), 19),
              w_lab, font=font, fill=1)
    draw.text((19, last_y - 17), str(last_y), font=font, fill=1)

    # Inset comb at the far corner, so a clipped edge can be *measured*
    # and not just noticed: the ticks stand 0, 8, 16, 24 and 32 dots in
    # from the corner, and whichever is the first one showing is how much
    # was lost.
    for inset in (0, 8, 16, 24, 32):
        x, y = last_x - inset, last_y - inset
        if x < 40 or y < 40:
            continue
        draw.line((x, y - 30, x, y - 6), fill=1)
        draw.line((x - 30, y, x - 6, y), fill=1)
        if inset % 16 == 0:
            tag = str(inset)
            draw.text((x - 3 - _text_width(draw, tag, font), y - 51),
                      tag, font=font, fill=1)

    # Origin marker and feed arrow, both only at 0,0. Put well inside the
    # scales so they cannot be confused with a tick.
    ox, oy = 46, 44
    draw.rectangle((ox, oy, ox + 21, oy + 21), fill=1)
    draw.text((ox + 27, oy + 3), "0,0", font=font, fill=1)

    ax, ay = ox + 4, oy + 34
    tip = min(ay + 46, last_y - 4)
    if tip > ay:
        draw.line((ax, ay, ax, tip), fill=1)
        draw.polygon((ax - 7, tip - 11, ax + 7, tip - 11, ax, tip), fill=1)
        draw.text((ax + 11, ay + 12), "FEED", font=font, fill=1)

    # What was asked for, printed on the thing itself, so a label found
    # loose in a drawer still says what it was measuring.
    draw.text((ox, min(oy + 92, last_y - 16)),
              f"{width_dots}x{rows}", font=font, fill=1)

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
