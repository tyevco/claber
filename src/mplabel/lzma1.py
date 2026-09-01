"""lzma1.py - a minimal LZMA1 encoder that emits no end-of-stream marker.

The T50M Pro accepts exactly one shape of stream: the ".lzma" (alone)
container with the uncompressed size **declared** in the header and **no
end-of-stream marker** in the body. Python's `lzma` module cannot produce
that - it always writes a marker, offers no way to suppress one, and the
marker is entropy-coded so it cannot be trimmed off afterwards. Both
halves of that were proved against a captured print: the vendor's stream
fails to decode as unknown-size (no marker), ours succeeds (has one), and
the printer refuses ours either way.

Rather than add a dependency to a project that deliberately keeps the Pi
list short, this encodes literals only. No match finding, no length or
distance coders, no marker - just the range coder and the literal
context model, stopping when the last byte is written. The decoder knows
the size from the header and stops there.

That costs compression: with no matches, long runs are cheap but not
free. It does not matter here. A 48x256 label is 12288 bytes, the
announce field carries 16 bits, and the transfer is 64 bytes a report
over USB - so even a poor ratio is comfortably inside every limit.

Correctness is checkable without hardware, which nothing else in this
corner of the project has been: liblzma must decode what this produces,
with the declared size, back to the original bytes. `test_mplabel.py`
does exactly that.

Reference: the LZMA specification's range coder and literal decoder.
Written from that, not from any implementation.
"""

import struct

# Probabilities are 11-bit, so 2048 is "certain" and 1024 is even odds.
_PROB_BITS = 11
_PROB_INIT = (1 << _PROB_BITS) // 2
_MOVE_BITS = 5
_TOP = 1 << 24

# lc/lp/pb, the literal-context / literal-position / position bits. These
# are the LZMA defaults and match the properties byte 0x5D that the
# captured print carries.
LC, LP, PB = 3, 0, 2

_STATES = 12


class _RangeEncoder:
    """The LZMA binary range coder.

    `shift_low` carrying into already-emitted bytes is why output is
    buffered rather than streamed: a carry can ripple backwards through a
    run of 0xFF bytes."""

    def __init__(self):
        self.low = 0
        self.range = 0xFFFFFFFF
        self.cache = 0
        self.cache_size = 1
        self.out = bytearray()

    def _shift_low(self):
        if self.low < 0xFF000000 or self.low > 0xFFFFFFFF:
            carry = self.low >> 32
            temp = self.cache
            while True:
                self.out.append((temp + carry) & 0xFF)
                temp = 0xFF
                self.cache_size -= 1
                if self.cache_size == 0:
                    break
            self.cache = (self.low >> 24) & 0xFF
        self.cache_size += 1
        self.low = (self.low << 8) & 0xFFFFFFFF

    def encode_bit(self, probs, index, bit):
        prob = probs[index]
        bound = (self.range >> _PROB_BITS) * prob
        if bit == 0:
            self.range = bound
            probs[index] = prob + (((1 << _PROB_BITS) - prob) >> _MOVE_BITS)
        else:
            self.low += bound
            self.range -= bound
            probs[index] = prob - (prob >> _MOVE_BITS)
        while self.range < _TOP:
            self.range = (self.range << 8) & 0xFFFFFFFF
            self._shift_low()

    def flush(self):
        # Five shifts push out the whole of `low` plus the cached byte.
        for _ in range(5):
            self._shift_low()
        return bytes(self.out)


def compress(data, dict_size=8192):
    """Encode `data` as a .lzma stream with a declared size and no marker.

    Returns the 13-byte header followed by the range-coded body."""
    data = bytes(data)
    if not data:
        raise ValueError("nothing to compress")

    rc = _RangeEncoder()
    # is_match[state][pos_state]: 0 selects a literal. Nothing here ever
    # encodes a 1, but the bit still has to be written for each symbol.
    is_match = [_PROB_INIT] * (_STATES << 4)
    literals = [_PROB_INIT] * (0x300 << (LC + LP))

    pos_mask = (1 << PB) - 1
    lp_mask = (1 << LP) - 1
    state = 0
    prev = 0

    for pos, byte in enumerate(data):
        rc.encode_bit(is_match, (state << 4) + (pos & pos_mask), 0)

        ctx = (((pos & lp_mask) << LC) + (prev >> (8 - LC))) * 0x300
        symbol = 1
        for shift in range(7, -1, -1):
            bit = (byte >> shift) & 1
            rc.encode_bit(literals, ctx + symbol, bit)
            symbol = (symbol << 1) | bit

        prev = byte
        # The literal state transition. With no matches this settles at 0
        # immediately, but keep the rule rather than hard-coding the
        # result - it is the spec's, and a future match coder needs it.
        state = 0 if state < 4 else (state - 3 if state < 10 else state - 6)

    body = rc.flush()
    props = (PB * 5 + LP) * 9 + LC
    return (bytes([props]) + struct.pack("<I", dict_size)
            + struct.pack("<Q", len(data)) + body)
