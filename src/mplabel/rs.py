"""Reed-Solomon over GF(256), encode and decode.

Factored out of `qr.py` when `marker.py` needed the same field. The QR
encoder only ever *makes* error-correction bytes; a marker we scan
ourselves has to *use* them, so this carries the decoding half too -
syndromes, Berlekamp-Massey, a Chien search and Forney's formula.

The field is the one both formats name: generator polynomial
x^8 + x^4 + x^3 + x^2 + 1 = 0x11d, with the code's roots at
alpha^0 .. alpha^(nsym-1).

**Correction is not detection.** A clean return means some valid
codeword was reached, not that it was the right one: handed more errors
than the parity can carry, the decoder can in principle converge on a
different valid codeword and hand back the wrong bytes without
complaint. Measured on the marker's own dimensions - 4 data bytes and 8
parity - it raised on all 4000 over-capacity trials rather than
mis-correcting, so this is a guard against something not yet observed
here rather than something seen. It stays because the consequence is a
label naming the wrong object, which is silent and expensive, and a
checksum costs one byte: `marker.py` carries one inside the data and
re-checks it after correction.
"""

_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables():
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


class RSError(ValueError):
    """The codeword carries more damage than the parity can repair."""


def mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def div(a, b):
    if b == 0:
        raise ZeroDivisionError("division by zero in GF(256)")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] + 255 - _LOG[b]) % 255]


def inv(a):
    return _EXP[255 - _LOG[a]]


# Polynomials are lists of coefficients, highest power first - the order
# every published generator table is written in.

def poly_scale(poly, x):
    return [mul(c, x) for c in poly]


def poly_add(a, b):
    out = [0] * max(len(a), len(b))
    for i, c in enumerate(a):
        out[i + len(out) - len(a)] = c
    for i, c in enumerate(b):
        out[i + len(out) - len(b)] ^= c
    return out


def poly_mul(a, b):
    out = [0] * (len(a) + len(b) - 1)
    for i, ca in enumerate(a):
        for j, cb in enumerate(b):
            out[i + j] ^= mul(ca, cb)
    return out


def poly_eval(poly, x):
    y = 0
    for coef in poly:
        y = mul(y, x) ^ coef
    return y


def generator(nsym):
    """The generator polynomial for `nsym` error-correction symbols."""
    g = [1]
    for i in range(nsym):
        g = poly_mul(g, [1, _EXP[i]])
    return g


def encode(data, nsym):
    """The `nsym` error-correction bytes for `data`."""
    gen = generator(nsym)
    rem = [0] * nsym
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i, coef in enumerate(gen[1:]):
            rem[i] ^= mul(coef, factor)
    return bytes(rem)


def syndromes(codeword, nsym):
    return [poly_eval(list(codeword), _EXP[i]) for i in range(nsym)]


def _error_locator(synd, nsym):
    """Berlekamp-Massey: the polynomial whose roots locate the errors."""
    err_loc, old_loc = [1], [1]
    for i in range(nsym):
        delta = synd[i]
        for j in range(1, len(err_loc)):
            delta ^= mul(err_loc[len(err_loc) - 1 - j], synd[i - j])
        old_loc = old_loc + [0]
        if delta != 0:
            if len(old_loc) > len(err_loc):
                new_loc = poly_scale(old_loc, delta)
                old_loc = poly_scale(err_loc, inv(delta))
                err_loc = new_loc
            err_loc = poly_add(err_loc, poly_scale(old_loc, delta))
    while err_loc and err_loc[0] == 0:
        err_loc.pop(0)
    return err_loc


def _error_positions(err_loc, length):
    """Chien search: which positions the locator's roots point at."""
    errs = len(err_loc) - 1
    positions = []
    for i in range(length):
        if poly_eval(err_loc, _EXP[255 - i]) == 0:
            positions.append(length - 1 - i)
    if len(positions) != errs:
        raise RSError(
            f"error locator has degree {errs} but {len(positions)} roots; "
            f"the codeword is damaged beyond repair")
    return positions


def _correct(codeword, synd, positions):
    """Forney: how much each located byte is wrong by.

    Written with the polynomials ascending - index i is the coefficient
    of x^i - even though everything else in this module runs highest
    power first. The two conventions met in the middle of this function
    in a first attempt and produced magnitudes that were wrong for every
    case with more than one error, which surfaces as "correction did not
    clear the syndromes" and reads like a symbol too damaged to fix.

    With the code's roots at alpha^0 the X_k factor in the usual
    statement of Forney's formula cancels against the derivative, so it
    is absent here on purpose:

        Y_k = omega(X_k^-1) / prod over j != k of (1 + X_j * X_k^-1)
    """
    n = len(codeword)
    nsym = len(synd)
    coef_pos = [n - 1 - p for p in positions]
    xs = [_EXP[cp % 255] for cp in coef_pos]

    # lambda(x) = product of (1 + X_k x), ascending.
    lam = [1]
    for x in xs:
        nxt = lam + [0]
        for i, c in enumerate(lam):
            nxt[i + 1] ^= mul(c, x)
        lam = nxt

    # omega(x) = S(x) * lambda(x) mod x^nsym, ascending.
    omega = [0] * nsym
    for j in range(nsym):
        acc = 0
        for i in range(min(j, len(lam) - 1) + 1):
            acc ^= mul(synd[j - i], lam[i])
        omega[j] = acc

    magnitudes = [0] * n
    for k, xk in enumerate(xs):
        xk_inv = inv(xk)
        num, power = 0, 1
        for j in range(nsym):
            num ^= mul(omega[j], power)
            power = mul(power, xk_inv)
        den = 1
        for j, xj in enumerate(xs):
            if j != k:
                den = mul(den, 1 ^ mul(xj, xk_inv))
        if den == 0:
            raise RSError("Forney denominator vanished; not correctable")
        magnitudes[positions[k]] = div(num, den)

    return bytes(a ^ b for a, b in zip(codeword, magnitudes))


def decode(codeword, nsym):
    """Repair `codeword` in place and return it.

    Raises RSError when the damage is past what `nsym` parity bytes can
    carry. Note the warning in the module docstring: a *clean* return is
    not proof the answer is right, only that some valid codeword was
    reached. Check a payload checksum afterwards."""
    codeword = bytes(codeword)
    synd = syndromes(codeword, nsym)
    if not any(synd):
        return codeword
    err_loc = _error_locator(synd, nsym)
    if len(err_loc) - 1 > nsym // 2:
        raise RSError(
            f"{len(err_loc) - 1} errors found, {nsym // 2} correctable")
    positions = _error_positions(err_loc, len(codeword))
    fixed = _correct(codeword, synd, positions)
    if any(syndromes(fixed, nsym)):
        raise RSError("correction did not clear the syndromes")
    return fixed
