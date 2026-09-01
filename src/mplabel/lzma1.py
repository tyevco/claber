"""lzma1.py - an LZMA1 encoder that emits no end-of-stream marker.

The T50M Pro accepts exactly one shape of stream, and it took a lot of
labels to establish both halves of it:

  - the ".lzma" (alone) container with the uncompressed size **declared**
    and **no end-of-stream marker**. Python's `lzma` always writes a
    marker, offers no way to suppress one, and the marker is entropy-coded
    so it cannot be trimmed off afterwards.
  - **at most 512 compressed bytes**, in a single buffer. Measured: 123
    and 419 bytes printed, 551, 695 and 724 were refused, and splitting a
    large image into several sub-512 buffers did not print either.

So the encoder has to be ours, and it has to compress well enough to fit a
whole label into 512 bytes. A 48x256 label is 12288 bytes, which is a
ratio of 24:1 - unremarkable for LZMA on a mostly-blank bitmap, and out of
reach without match coding. An earlier version of this module encoded
literals only and could not get under 551 bytes for any image with real
content in it, which is what those three refusals were.

This one has matches: a hash-chain finder, the rep-distance slots, the
length and distance coders, and the state machine that ties them together.
No optimal parse - greedy, preferring a repeat distance - because the
ratio needed here is nowhere near the edge of what LZMA can do.

Correctness is checkable without hardware, which very little else in this
corner of the project has been. liblzma must decode what this produces,
with the declared size, back to the original bytes, and the result must be
under 512 bytes for a full label. `test_mplabel.py` asserts both.

Reference: the LZMA specification's range coder, literal/length/distance
coders and state machine. Written from that, not from any implementation.
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
_MATCH_MIN_LEN = 2
_MATCH_MAX_LEN = 273

# Distances below this get their low bits from a context-coded reverse bit
# tree; above it, all but the bottom four bits go out as raw bits.
_START_POS_MODEL = 4
_END_POS_MODEL = 14
_FULL_DISTANCES = 1 << (_END_POS_MODEL >> 1)
_ALIGN_BITS = 4

# How far back the match finder will look along one hash chain. Our
# bitmaps are mostly one repeated byte, so the nearest candidate is
# usually already the best one; this only bounds the pathological case.
_CHAIN_DEPTH = 24


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

    def encode_direct(self, value, count):
        """Bits with no probability model - the high part of a distance."""
        for i in range(count - 1, -1, -1):
            self.range >>= 1
            if (value >> i) & 1:
                self.low += self.range
            while self.range < _TOP:
                self.range = (self.range << 8) & 0xFFFFFFFF
                self._shift_low()

    def encode_tree(self, probs, offset, bits, symbol):
        m = 1
        for i in range(bits - 1, -1, -1):
            bit = (symbol >> i) & 1
            self.encode_bit(probs, offset + m, bit)
            m = (m << 1) | bit

    def encode_tree_reverse(self, probs, offset, bits, symbol):
        m = 1
        for _ in range(bits):
            bit = symbol & 1
            symbol >>= 1
            self.encode_bit(probs, offset + m, bit)
            m = (m << 1) | bit

    def flush(self):
        # Five shifts push out the whole of `low` plus the cached byte.
        for _ in range(5):
            self._shift_low()
        return bytes(self.out)


class _LenCoder:
    """Match lengths: 2-9 cheap, 10-17 next, 18-273 in a shared tree."""

    def __init__(self):
        self.choice = [_PROB_INIT, _PROB_INIT]
        self.low = [_PROB_INIT] * ((1 << PB) * 8)
        self.mid = [_PROB_INIT] * ((1 << PB) * 8)
        self.high = [_PROB_INIT] * 256

    def encode(self, rc, length, pos_state):
        symbol = length - _MATCH_MIN_LEN
        if symbol < 8:
            rc.encode_bit(self.choice, 0, 0)
            rc.encode_tree(self.low, pos_state * 8, 3, symbol)
        elif symbol < 16:
            rc.encode_bit(self.choice, 0, 1)
            rc.encode_bit(self.choice, 1, 0)
            rc.encode_tree(self.mid, pos_state * 8, 3, symbol - 8)
        else:
            rc.encode_bit(self.choice, 0, 1)
            rc.encode_bit(self.choice, 1, 1)
            rc.encode_tree(self.high, 0, 8, symbol - 16)


def _pos_slot(dist):
    if dist < 4:
        return dist
    n = dist.bit_length() - 1
    return (n << 1) | ((dist >> (n - 1)) & 1)


class _MatchFinder:
    """Hash chains on three-byte keys.

    Deliberately plain. The images here are a few kilobytes of mostly one
    repeated byte, where the nearest candidate on a chain is almost always
    the best one, so cleverness buys nothing measurable and costs the
    ability to reason about what came out."""

    def __init__(self, data):
        self.data = data
        self.head = {}
        self.prev = [-1] * len(data)

    def _key(self, pos):
        d = self.data
        return d[pos] | (d[pos + 1] << 8) | (d[pos + 2] << 16)

    def insert(self, pos):
        if pos + 2 < len(self.data):
            key = self._key(pos)
            self.prev[pos] = self.head.get(key, -1)
            self.head[key] = pos

    def find(self, pos, max_len, dict_size):
        """Longest match at `pos`, as (length, distance), or (0, 0)."""
        data = self.data
        if pos + 2 >= len(data) or max_len < 3:
            return 0, 0
        best_len, best_dist = 0, 0
        candidate = self.head.get(self._key(pos), -1)
        for _ in range(_CHAIN_DEPTH):
            if candidate < 0:
                break
            dist = pos - candidate
            if dist > dict_size:
                break
            length = 0
            while length < max_len and data[candidate + length] == data[pos + length]:
                length += 1
            if length > best_len:
                best_len, best_dist = length, dist
                if length >= max_len:
                    break
            candidate = self.prev[candidate]
        if best_len < 3:
            return 0, 0
        return best_len, best_dist


def _run_length(data, pos, dist, max_len):
    """How far `pos` matches the bytes `dist` back. 0 if it does not."""
    if dist <= 0 or dist > pos:
        return 0
    length = 0
    while length < max_len and data[pos - dist + length] == data[pos + length]:
        length += 1
    return length


def compress(data, dict_size=8192):
    """Encode `data` as a .lzma stream with a declared size and no marker.

    Returns the 13-byte header followed by the range-coded body."""
    data = bytes(data)
    if not data:
        raise ValueError("nothing to compress")

    rc = _RangeEncoder()
    is_match = [_PROB_INIT] * (_STATES << 4)
    is_rep = [_PROB_INIT] * _STATES
    is_rep_g0 = [_PROB_INIT] * _STATES
    is_rep_g1 = [_PROB_INIT] * _STATES
    is_rep_g2 = [_PROB_INIT] * _STATES
    is_rep0_long = [_PROB_INIT] * (_STATES << 4)
    literals = [_PROB_INIT] * (0x300 << (LC + LP))
    pos_slots = [_PROB_INIT] * (4 * 64)
    spec_pos = [_PROB_INIT] * (_FULL_DISTANCES - _END_POS_MODEL)
    align = [_PROB_INIT] * (1 << _ALIGN_BITS)
    len_coder = _LenCoder()
    rep_len_coder = _LenCoder()

    finder = _MatchFinder(data)
    pos_mask = (1 << PB) - 1
    lp_mask = (1 << LP) - 1
    reps = [0, 0, 0, 0]
    state = 0
    pos = 0
    size = len(data)

    def literal_context(position, prev):
        return (((position & lp_mask) << LC) + (prev >> (8 - LC))) * 0x300

    while pos < size:
        pos_state = pos & pos_mask
        max_len = min(_MATCH_MAX_LEN, size - pos)

        # A repeat distance first: it is the cheapest thing to encode and,
        # on a bitmap of long identical runs, usually the longest too.
        rep_index, rep_len = -1, 0
        for i, dist in enumerate(reps):
            length = _run_length(data, pos, dist, max_len)
            if length > rep_len:
                rep_index, rep_len = i, length

        new_len, new_dist = finder.find(pos, max_len, dict_size)

        use_rep = rep_len >= _MATCH_MIN_LEN and rep_len + 1 >= new_len
        use_new = not use_rep and new_len >= 3

        if not use_rep and not use_new:
            # Literal. After a match the decoder uses the byte one match
            # distance back as context, so the encoder must too, or the
            # two walk different trees from here on.
            prev = data[pos - 1] if pos else 0
            ctx = literal_context(pos, prev)
            rc.encode_bit(is_match, (state << 4) + pos_state, 0)
            byte = data[pos]
            if state < 7:
                symbol = 1
                for shift in range(7, -1, -1):
                    bit = (byte >> shift) & 1
                    rc.encode_bit(literals, ctx + symbol, bit)
                    symbol = (symbol << 1) | bit
            else:
                # reps hold real distances, not the 0-based ones the
                # wire format carries - the conversion happens at
                # encode time (`dist - 1`). Mixing the two here put
                # the encoder one byte off the decoder's context and
                # corrupted only streams that place a literal after a
                # match, which most test bitmaps never do.
                match_byte = data[pos - reps[0]]
                # The match-bit term is gated by `offset`, not added
                # beside it. Once a bit disagrees with the match byte,
                # offset goes to zero and every later bit uses the plain
                # literal context - including its index, which must then
                # lose the match-bit half too. Adding it unconditionally
                # corrupts only streams where a literal follows a match
                # *and* the match byte has a set bit after the first
                # disagreement, so every uniform test bitmap passed.
                offset = 0x100
                symbol = 1
                for shift in range(7, -1, -1):
                    bit = (byte >> shift) & 1
                    gated = offset & ((match_byte >> shift & 1) << 8)
                    rc.encode_bit(literals, ctx + offset + gated + symbol,
                                  bit)
                    symbol = (symbol << 1) | bit
                    offset &= gated if bit else ~gated
            state = 0 if state < 4 else (state - 3 if state < 10 else state - 6)
            finder.insert(pos)
            pos += 1
            continue

        rc.encode_bit(is_match, (state << 4) + pos_state, 1)
        if use_rep:
            length = rep_len
            rc.encode_bit(is_rep, state, 1)
            if rep_index == 0:
                rc.encode_bit(is_rep_g0, state, 0)
                rc.encode_bit(is_rep0_long, (state << 4) + pos_state, 1)
            else:
                rc.encode_bit(is_rep_g0, state, 1)
                if rep_index == 1:
                    rc.encode_bit(is_rep_g1, state, 0)
                else:
                    rc.encode_bit(is_rep_g1, state, 1)
                    rc.encode_bit(is_rep_g2, state, 1 if rep_index == 3 else 0)
                dist = reps[rep_index]
                del reps[rep_index]
                reps.insert(0, dist)
            rep_len_coder.encode(rc, length, pos_state)
            state = 8 if state < 7 else 11
        else:
            length, dist = new_len, new_dist
            rc.encode_bit(is_rep, state, 0)
            len_coder.encode(rc, length, pos_state)

            slot = _pos_slot(dist - 1)
            len_to_pos = min(length - _MATCH_MIN_LEN, 3)
            rc.encode_tree(pos_slots, len_to_pos * 64, 6, slot)
            if slot >= _START_POS_MODEL:
                footer = (slot >> 1) - 1
                base = (2 | (slot & 1)) << footer
                rest = (dist - 1) - base
                if slot < _END_POS_MODEL:
                    rc.encode_tree_reverse(spec_pos, base - slot - 1,
                                           footer, rest)
                else:
                    rc.encode_direct(rest >> _ALIGN_BITS,
                                     footer - _ALIGN_BITS)
                    rc.encode_tree_reverse(align, 0, _ALIGN_BITS,
                                           rest & ((1 << _ALIGN_BITS) - 1))
            reps = [dist, reps[0], reps[1], reps[2]]
            state = 7 if state < 7 else 10

        for i in range(length):
            finder.insert(pos + i)
        pos += length

    body = rc.flush()
    props = (PB * 5 + LP) * 9 + LC
    return (bytes([props]) + struct.pack("<I", dict_size)
            + struct.pack("<Q", len(data)) + body)
