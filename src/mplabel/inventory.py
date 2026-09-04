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

DEFAULT_HEIGHT_MM = 30

# The label is drawn a little narrower than the head and centred, because
# the media runs centred under the bar and 48mm stock is not exactly 48mm.
SIDE_MARGIN_DOTS = 12

# Quiet modules around the shelf marker, on all four sides.
MARKER_QUIET = 2


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


def _symbol_placement(height, feed_margin, modules, cap=140):
    """Where a square symbol of `modules` modules goes, and how big.

    Returns (x0, y0, side_in_dots, dots_per_module). One function owns
    this so that `render_label` and `marker_box` cannot drift: a caller
    cropping a photograph to the wrong rectangle reads the marker at the
    wrong pitch and gets either nothing or, worse, something."""
    top = feed_margin + 4
    bottom = height - feed_margin - 4
    side = min(bottom - top, cap)
    scale = max(1, side // modules)
    block = modules * scale
    return (SIDE_MARGIN_DOTS, top + (bottom - top - block) // 2, block, scale)


def marker_box(height_mm=DEFAULT_HEIGHT_MM, feed_margin=None):
    """The marker's rectangle on the label, in dots: (x0, y0, x1, y1).

    Exposed because reading one back needs a crop that holds the marker
    and not the title beside it - `marker.read_image` locates the grid
    from the bounding box of the ink, so a crop that catches a letter
    samples the whole thing at the wrong pitch."""
    from .supvan import DEFAULT_MARGIN_DOTS
    if feed_margin is None:
        feed_margin = DEFAULT_MARGIN_DOTS
    modules = marker_mod.SIZE + 2 * MARKER_QUIET
    x0, y0, block, _scale = _symbol_placement(
        height_mm * DOTS_PER_MM, feed_margin, modules)
    return x0, y0, x0 + block, y0 + block


def render_label(code, title=None, price=None, with_qr=False,
                 height_mm=DEFAULT_HEIGHT_MM, ecl="M", feed_margin=None,
                 with_marker=False):
    """Draw one label and return (raster, stride, rows).

    Raster is MSB-first with a set bit meaning a black dot, which is the
    convention everywhere else in this codebase; `supvan.build_job`
    repacks it for the printhead. Returning the raster rather than an
    image keeps this function testable without a device or a viewer.

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

    width = HEAD_DOTS
    height = height_mm * DOTS_PER_MM
    img = Image.new("1", (width, height), 0)      # 0 = no dot
    draw = ImageDraw.Draw(img)

    left = SIDE_MARGIN_DOTS
    right = width - SIDE_MARGIN_DOTS
    top = feed_margin + 4
    bottom = height - feed_margin - 4

    if with_marker:
        # The shelf marker carries 20 bits where a QR version 1 carries
        # 152, so the same square buys far bigger modules - and module
        # size is what survives thermal bleed. Squared off to a whole
        # number of dots for the same reason the QR is.
        grid = marker_mod.render(code, quiet=MARKER_QUIET)
        _x, y0, block, scale = _symbol_placement(
            height, feed_margin, len(grid))
        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                if cell:
                    draw.rectangle(
                        [left + c * scale, y0 + r * scale,
                         left + (c + 1) * scale - 1, y0 + (r + 1) * scale - 1],
                        fill=1)
        text_left = left + block + 14
    elif with_qr:
        # Sized to the space, then rounded down to a whole number of dots
        # per module - a fractional module scales into uneven blocks and
        # is the classic reason a printed code will not scan.
        matrix = qr.render(code, ecl=ecl, quiet=2)
        _x, y0, block, scale = _symbol_placement(
            height, feed_margin, len(matrix))
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

    # The code, as large as the space allows.
    code_font, _ = _fit(draw, code, _font, text_width,
                        start=60 if (with_qr or with_marker) else 90)
    code_h = draw.textbbox((0, 0), code, font=code_font)[3]
    draw.text((text_left, top), code, font=code_font, fill=1)
    y = top + code_h + 8

    # A rule under the code, so the eye separates it from the title.
    draw.rectangle([text_left, y, right, y + 1], fill=1)
    y += 8

    if title:
        title_font = _font(18 if (with_qr or with_marker) else 22)
        room = max(1, (bottom - y - (26 if price else 0))
                   // (draw.textbbox((0, 0), "Ay", font=title_font)[3] + 2))
        for line in _wrap(draw, title, title_font, text_width,
                          max_lines=min(room, 3)):
            draw.text((text_left, y), line, font=title_font, fill=1)
            y += draw.textbbox((0, 0), "Ay", font=title_font)[3] + 2

    if price is not None:
        price_text = f"${price:.2f}" if isinstance(price, (int, float)) \
            else str(price)
        price_font = _font(26)
        w = _text_width(draw, price_text, price_font)
        h = draw.textbbox((0, 0), price_text, font=price_font)[3]
        draw.text((right - w, bottom - h), price_text,
                  font=price_font, fill=1)

    stride = (width + 7) // 8
    return img.tobytes(), stride, height


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
