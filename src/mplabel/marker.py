"""A strip code for the three- and four-character codes this system uses.

Why not a QR, which `qr.py` already draws: a QR version 1 holds 152 bits
and we have 20. Paying for 132 bits we do not want costs module size,
and module size is the whole game on thermal paper. Bigger modules
survive bleed, a smeared roll, a bad angle and a phone that will not
focus.

What it costs: nothing off the shelf reads it. A QR is scanned by the
camera app; this is scanned by our own. That is the trade, and it is
only worth it because the phone app already exists and is the thing she
has open when she is standing at the shelf.

Six modules tall by twenty-four wide - one by four - so it sits under
the text as a band rather than stealing a square out of the middle of a
label that is mostly text:

    # # # # # # # # # # # #     top row and right column alternate: the
    #......................#    clock track, which says how many
    #.       data         .#    modules across and catches a scale that
    #.                    .#    has drifted
    #......................#
    ########################    left column and bottom row solid: the
                                L, which gives position, rotation and
                                the module pitch in one feature

The interior is 4 x 22 = 88 modules and the codeword is exactly 88 bits:
4 data bytes and 7 Reed-Solomon parity bytes, so any 3 of the 11 can be
wrong and the code still reads. The 4 data bytes are a 4-bit format, a
20-bit payload and an 8-bit checksum. Nothing is spare, which is the
point of choosing this shape over a taller one - every module left over
would have been a smaller module.

Bits are written in raster order, and on a strip that choice matters
more than it did on a square. Along the rows, a byte is eight
neighbouring modules and a scratch down the length of the strip damages
three of eleven bytes - just inside what the parity carries. Down the
columns it would be one bit from each of eleven bytes, which is the same
damage spread so thin that nothing is recoverable.

The checksum is the part that matters most. Reed-Solomon corrects; it
does not certify. A code that decodes to the *wrong* four characters
sends a parcel to a stranger or puts the wrong label on a box in the
loft, and neither announces itself - so the payload is checked again
after correction and a mismatch is a refusal, not a best guess.
"""

from . import rs

# The alphabet the codes are already drawn from: digits and capitals
# minus I, L, O and U, which get misread as 1, 1, 0 and V on thermal
# stock. cli.CODE_ALPHABET is the same string; it is repeated here so
# this module can be read, and tested, on its own.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# One by four. The interior is (ROWS-2) x (COLS-2) = 88 modules and the
# codeword is 88 bits, so the fit is exact in both directions.
ROWS = 6
COLS = 24
DATA_BYTES = 4
ECC_BYTES = 7
PAYLOAD_BITS = 20            # four characters at five bits each

# Border modules, which is what a finder score is out of.
BORDER = 2 * ROWS + 2 * COLS - 4

# Numbered from 1 so that format 0 is invalid, which makes the all-zero
# codeword unreadable. That matters more than it looks: all-zero is a
# perfectly valid Reed-Solomon codeword and crc8(b"\0\0\0") is 0, so a
# blank crop used to satisfy every check and decode cleanly to "000" -
# a real code, returned confidently, from a picture of nothing.
FORMAT_3CHAR = 1
FORMAT_4CHAR = 2

# How much of the border has to be right before a grid is worth decoding
# at all. A blank region matches every light module and no dark one, so
# it scores the light count and nothing more; noise scores about the
# same. This is the difference between "no marker here" and a confident
# answer drawn from the wallpaper. 82% of the border, the same fraction
# the square version used.
MIN_FINDER_SCORE = int(BORDER * 0.82)


class MarkerError(ValueError):
    """The code will not encode, or the image will not decode."""


def _crc8(data):
    """CRC-8, polynomial 0x07. Eight bits is enough: it is guarding a
    20-bit payload that Reed-Solomon has already had a go at, and the
    job is to catch a mis-correction rather than to detect noise."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def encode_payload(code):
    """The 12 codeword bytes for `code`."""
    code = code.upper()
    if len(code) not in (3, 4):
        raise MarkerError(
            f"{code!r} is {len(code)} characters; this carries 3 or 4")
    value = 0
    for ch in code:
        if ch not in ALPHABET:
            raise MarkerError(
                f"{ch!r} is not in the code alphabet {ALPHABET!r}")
        value = value * len(ALPHABET) + ALPHABET.index(ch)

    fmt = FORMAT_4CHAR if len(code) == 4 else FORMAT_3CHAR
    word = (fmt << PAYLOAD_BITS) | value
    body = word.to_bytes(3, "big")
    data = body + bytes([_crc8(body)])
    return data + rs.encode(data, ECC_BYTES)


def decode_payload(codeword):
    """The code a 12-byte codeword names, repairing it if it can.

    Raises MarkerError when the damage is past the parity, or when the
    checksum says the repair landed somewhere plausible and wrong."""
    if len(codeword) != DATA_BYTES + ECC_BYTES:
        raise MarkerError(
            f"{len(codeword)} bytes, expected {DATA_BYTES + ECC_BYTES}")
    try:
        fixed = rs.decode(bytes(codeword), ECC_BYTES)
    except rs.RSError as exc:
        raise MarkerError(f"too damaged to read: {exc}")

    body, crc = fixed[:3], fixed[3]
    if _crc8(body) != crc:
        raise MarkerError(
            "checksum failed after correction - the parity was satisfied "
            "by the wrong codeword, so this is refused rather than "
            "guessed at")

    word = int.from_bytes(body, "big")
    fmt = word >> PAYLOAD_BITS
    if fmt not in (FORMAT_3CHAR, FORMAT_4CHAR):
        raise MarkerError(
            f"format {fmt} is not one this draws; format 0 in particular "
            f"is the all-zero codeword, which is what a blank picture "
            f"decodes to")
    length = 4 if fmt == FORMAT_4CHAR else 3

    value = word & ((1 << PAYLOAD_BITS) - 1)
    chars = []
    for _ in range(length):
        value, rem = divmod(value, len(ALPHABET))
        chars.append(ALPHABET[rem])
    if value:
        raise MarkerError("payload has bits set past its declared length")
    return "".join(reversed(chars))


# ------------------------------------------------------------- drawing

def _blank():
    return [[0] * COLS for _ in range(ROWS)]


def _finder(grid):
    """The L and the clock track.

    Both corners where they meet are dark, and the two clock tracks are
    out of phase with each other by construction - the top starts dark
    at column 0 and the right ends dark at the bottom row - so an image
    that has been mirrored does not read as a valid finder."""
    for r in range(ROWS):
        grid[r][0] = 1                       # left column, solid
    for c in range(COLS):
        grid[ROWS - 1][c] = 1                # bottom row, solid
    for c in range(COLS):
        grid[0][c] = 1 if c % 2 == 0 else 0  # top clock
    for r in range(ROWS):
        grid[r][COLS - 1] = 1 if r % 2 else 0  # right clock


def _cells():
    """Interior module positions, in the order the bits are written.

    Raster order, deliberately. Each byte then lands as eight modules
    that span at most two rows, so a smudge damages one or two whole
    bytes - which is what Reed-Solomon over bytes is good at - rather
    than one bit from each of eight, which is the same damage spread so
    thin that it exhausts the parity."""
    return [(r, c) for r in range(1, ROWS - 1) for c in range(1, COLS - 1)]


def encode(code):
    """The finished ROWS x COLS grid, as rows of 0/1."""
    grid = _blank()
    _finder(grid)
    codeword = encode_payload(code)
    bits = [(byte >> i) & 1 for byte in codeword for i in range(7, -1, -1)]
    for (r, c), bit in zip(_cells(), bits):
        grid[r][c] = bit
    # Any interior module past the codeword stays light; the decoder
    # reads only as many as the codeword needs.
    return grid


def render(code, scale=1, quiet=2):
    """`encode`, with a quiet zone and an integer scale.

    The quiet zone is not optional: the finder is found by looking for
    the darkest edges in the crop, and ink butted against the L reads as
    part of it."""
    grid = encode(code)
    out = [[0] * (COLS + 2 * quiet) for _ in range(ROWS + 2 * quiet)]
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            out[r + quiet][c + quiet] = cell
    if scale == 1:
        return out
    return [[cell for cell in row for _ in range(scale)]
            for row in out for _ in range(scale)]


# ------------------------------------------------------------- reading

def _otsu(hist, total):
    """The threshold that best splits the histogram in two.

    A fixed threshold fails on the two things a phone actually does:
    underexpose the whole frame, and light one side of the label more
    than the other. This handles the first. The second is why the
    sampler below votes over a window rather than reading one pixel."""
    sum_all = sum(i * n for i, n in enumerate(hist))
    sum_b = w_b = 0
    best, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, best_t = between, t
    return best_t


def _binarize(image):
    """A grayscale image to a list of rows of 0/1, 1 meaning ink."""
    gray = image.convert("L")
    hist = gray.histogram()
    threshold = _otsu(hist, gray.width * gray.height)
    px = gray.load()
    return [[1 if px[x, y] <= threshold else 0 for x in range(gray.width)]
            for y in range(gray.height)]


def _despeckle(bits):
    """Clear dark pixels that have almost no dark neighbours.

    A single speck decides the bounding box, because the box is a min
    and a max over every dark pixel - so one dust mote in a corner
    stretches the grid and every module afterwards is sampled in the
    wrong place. That is not a hypothetical: 5% salt-and-pepper noise
    took the finder from 44/44 to 11/44 with the picture otherwise
    perfect.

    A real module is a solid block at least two pixels across, so every
    one of its pixels has several dark neighbours. Requiring two is
    enough to drop isolated noise and safe even where a module is only
    two pixels wide."""
    h, w = len(bits), len(bits[0])
    out = [row[:] for row in bits]
    for y in range(h):
        for x in range(w):
            if not bits[y][x]:
                continue
            neighbours = 0
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == dx == 0:
                        continue
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w:
                        neighbours += bits[ny][nx]
            if neighbours < 2:
                out[y][x] = 0
    return out


def _ink_bounds(bits):
    """The bounding box of the ink, which is the marker plus nothing.

    Relies on the crop holding the marker and little else - which is
    what the phone app's aiming rectangle is for. A general search of a
    whole camera frame is a different and much larger problem, and
    pretending otherwise here would be the kind of thing that works on
    a rendered fixture and never on a photograph.

    Two dark pixels, not one, decide whether a line counts as inked.
    Every row of the marker crosses the solid left column and every
    column crosses the solid bottom row, so at any scale worth reading
    they clear that easily - while a surviving pair of noise specks
    does not, and a single one certainly does not."""
    def inked(line):
        return sum(line) >= 2

    rows = [y for y, row in enumerate(bits) if inked(row)]
    cols = [x for x in range(len(bits[0]))
            if inked([row[x] for row in bits])]
    if not rows or not cols:
        raise MarkerError("no ink in the image")
    return min(cols), min(rows), max(cols), max(rows)


def _sample(bits, box, rows, cols):
    """Read a rows x cols grid out of the boxed region.

    Each module is decided by a vote over the middle of its cell rather
    than by its centre pixel: one pixel is a coin toss wherever the
    threshold landed near the ink, and the middle half is still well
    inside the module even if the box is a little off."""
    x0, y0, x1, y1 = box
    w = (x1 - x0 + 1) / cols
    h = (y1 - y0 + 1) / rows
    if w < 1 or h < 1:
        raise MarkerError("the marker is too small in this image to read")

    grid = []
    for r in range(rows):
        row = []
        for c in range(cols):
            cx0 = x0 + c * w + w * 0.25
            cx1 = x0 + c * w + w * 0.75
            cy0 = y0 + r * h + h * 0.25
            cy1 = y0 + r * h + h * 0.75
            dark = seen = 0
            for y in range(int(cy0), max(int(cy1), int(cy0) + 1)):
                if not 0 <= y < len(bits):
                    continue
                line = bits[y]
                for x in range(int(cx0), max(int(cx1), int(cx0) + 1)):
                    if 0 <= x < len(line):
                        seen += 1
                        dark += line[x]
            row.append(1 if seen and dark * 2 >= seen else 0)
        grid.append(row)
    return grid


def _finder_score(grid):
    """How many finder modules are where they should be, out of BORDER."""
    want = _blank()
    _finder(want)
    score = 0
    for r in range(ROWS):
        for c in range(COLS):
            if r in (0, ROWS - 1) or c in (0, COLS - 1):
                score += grid[r][c] == want[r][c]
    return score


def _rot180(grid):
    return [list(reversed(row)) for row in reversed(grid)]


def _rot90(grid):
    """One quarter turn clockwise. An h x w grid becomes w x h."""
    return [list(row) for row in zip(*grid[::-1])]


def _rot270(grid):
    return _rot180(_rot90(grid))


def read_grid(grid):
    """The code a ROWS x COLS grid names, trying both ways up.

    A rectangle can only be read at 0 or 180 degrees - at 90 it would
    not be this shape - so the quarter turns are handled by `read_image`,
    which samples transposed when the ink is taller than it is wide.
    That is a real simplification over the square: the aspect ratio
    itself rules out half the orientations before any decoding starts."""
    candidates = []
    for turn, candidate in ((0, grid), (1, _rot180(grid))):
        candidates.append((_finder_score(candidate), turn, candidate))
    candidates.sort(key=lambda t: (-t[0], t[1]))

    if candidates[0][0] < MIN_FINDER_SCORE:
        raise MarkerError(
            f"no marker here: the best orientation matches only "
            f"{candidates[0][0]}/{BORDER} of the border")

    errors = []
    for score, _turn, candidate in candidates:
        if score < MIN_FINDER_SCORE:
            continue
        bits = [candidate[r][c] for r, c in _cells()]
        need = (DATA_BYTES + ECC_BYTES) * 8
        if len(bits) < need:
            raise MarkerError("grid is too small to hold a codeword")
        codeword = bytes(
            int("".join(str(b) for b in bits[i:i + 8]), 2)
            for i in range(0, need, 8))
        try:
            return decode_payload(codeword)
        except MarkerError as exc:
            errors.append(f"finder {score}/{BORDER}: {exc}")
    raise MarkerError("; ".join(errors))


def read_image(image):
    """Read a marker out of a PIL image that mostly contains one.

    *Mostly contains one* is load bearing. The grid is located by the
    bounding box of the ink, so a whole label - with a code, a title and
    a price on it - stretches that box across everything and samples the
    marker at the wrong pitch. Crop first. The phone app's aiming
    rectangle is how that happens there; `MIN_FINDER_SCORE` is what
    stops a bad crop returning an answer anyway.

    The box's own shape settles the quarter turns. A strip photographed
    upright has a box four times wider than tall, so it is sampled
    ROWS x COLS; one photographed on its side has the opposite, so it is
    sampled COLS x ROWS and turned. Either way `read_grid` is then left
    with only the two ways up."""
    bits = _despeckle(_binarize(image))
    box = _ink_bounds(bits)
    wide = (box[2] - box[0]) >= (box[3] - box[1])

    if wide:
        return read_grid(_sample(bits, box, ROWS, COLS))

    sideways = _sample(bits, box, COLS, ROWS)
    errors = []
    for turned in (_rot90(sideways), _rot270(sideways)):
        try:
            return read_grid(turned)
        except MarkerError as exc:
            errors.append(str(exc))
    raise MarkerError("; ".join(errors))
