/* Reading the shelf marker in the browser.
 *
 * A port of src/mplabel/marker.py, and it has to stay one: the printer
 * writes these and the phone reads them, so a drift between the two ends
 * is a code that prints and cannot be scanned. The Python side is the
 * reference and `test_marker_js_port_agrees_with_python` runs this file
 * under node against vectors generated from it.
 *
 * No library, for the same reason there is no framework here: the whole
 * decoder is a few hundred lines of arithmetic, and a dependency with a
 * build step would be the odd one out next to a stdlib Python backend.
 *
 * Deliberately not a general scanner. It reads a crop that mostly holds
 * one marker - the aiming rectangle in the scan view is what makes that
 * true - rather than searching a whole camera frame, which is a much
 * larger problem and one this does not pretend to solve.
 */

var MK = (function () {
  'use strict';

  var ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
  var SIZE = 12, DATA_BYTES = 4, ECC_BYTES = 8, PAYLOAD_BITS = 20;
  /* Formats are numbered from 1 so the all-zero codeword has no valid
     format: all-zero satisfies Reed-Solomon and its CRC is zero too, so
     a camera pointed at a blank wall used to decode confidently to the
     real code "000". Keep in step with marker.py. */
  var FORMAT_3CHAR = 1, FORMAT_4CHAR = 2;
  /* How much of the 44-module border must match before a grid is worth
     decoding. Blank and noise both score about 22. */
  var MIN_FINDER_SCORE = 36;

  /* ------------------------------------------------------- GF(256) */

  var EXP = new Uint8Array(512), LOG = new Uint8Array(256);
  (function () {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      EXP[i] = x; LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  }());

  function mul(a, b) { return (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]]; }
  function inv(a) { return EXP[255 - LOG[a]]; }
  function div(a, b) {
    if (b === 0) throw new Error('divide by zero');
    return a === 0 ? 0 : EXP[(LOG[a] + 255 - LOG[b]) % 255];
  }

  function polyEval(poly, x) {
    var y = 0;
    for (var i = 0; i < poly.length; i++) y = mul(y, x) ^ poly[i];
    return y;
  }

  function syndromes(cw, nsym) {
    var s = [];
    for (var i = 0; i < nsym; i++) s.push(polyEval(cw, EXP[i]));
    return s;
  }

  function polyScale(p, x) { return p.map(function (c) { return mul(c, x); }); }

  function polyAdd(a, b) {
    var n = Math.max(a.length, b.length), out = new Array(n).fill(0), i;
    for (i = 0; i < a.length; i++) out[i + n - a.length] = a[i];
    for (i = 0; i < b.length; i++) out[i + n - b.length] ^= b[i];
    return out;
  }

  function errorLocator(synd, nsym) {
    var errLoc = [1], oldLoc = [1], i, j;
    for (i = 0; i < nsym; i++) {
      var delta = synd[i];
      for (j = 1; j < errLoc.length; j++) {
        delta ^= mul(errLoc[errLoc.length - 1 - j], synd[i - j]);
      }
      oldLoc = oldLoc.concat([0]);
      if (delta !== 0) {
        if (oldLoc.length > errLoc.length) {
          var newLoc = polyScale(oldLoc, delta);
          oldLoc = polyScale(errLoc, inv(delta));
          errLoc = newLoc;
        }
        errLoc = polyAdd(errLoc, polyScale(oldLoc, delta));
      }
    }
    while (errLoc.length && errLoc[0] === 0) errLoc.shift();
    return errLoc;
  }

  function errorPositions(errLoc, length) {
    var errs = errLoc.length - 1, positions = [];
    for (var i = 0; i < length; i++) {
      if (polyEval(errLoc, EXP[(255 - i) % 255]) === 0) {
        positions.push(length - 1 - i);
      }
    }
    if (positions.length !== errs) throw new Error('damaged beyond repair');
    return positions;
  }

  /* Forney, ascending - see the note in rs.py on why this is not the
     derivative form. */
  function correct(cw, synd, positions) {
    var n = cw.length, nsym = synd.length, i, j;
    var coefPos = positions.map(function (p) { return n - 1 - p; });
    var xs = coefPos.map(function (cp) { return EXP[cp % 255]; });

    var lam = [1];
    for (i = 0; i < xs.length; i++) {
      var nxt = lam.concat([0]);
      for (j = 0; j < lam.length; j++) nxt[j + 1] ^= mul(lam[j], xs[i]);
      lam = nxt;
    }

    var omega = new Array(nsym).fill(0);
    for (j = 0; j < nsym; j++) {
      var acc = 0;
      for (i = 0; i <= Math.min(j, lam.length - 1); i++) {
        acc ^= mul(synd[j - i], lam[i]);
      }
      omega[j] = acc;
    }

    var out = cw.slice();
    for (var k = 0; k < xs.length; k++) {
      var xkInv = inv(xs[k]), num = 0, power = 1;
      for (j = 0; j < nsym; j++) { num ^= mul(omega[j], power); power = mul(power, xkInv); }
      var den = 1;
      for (j = 0; j < xs.length; j++) {
        if (j !== k) den = mul(den, 1 ^ mul(xs[j], xkInv));
      }
      if (den === 0) throw new Error('not correctable');
      out[positions[k]] ^= div(num, den);
    }
    return out;
  }

  function rsDecode(cw, nsym) {
    var synd = syndromes(cw, nsym);
    if (!synd.some(function (v) { return v !== 0; })) return cw.slice();
    var errLoc = errorLocator(synd, nsym);
    if (errLoc.length - 1 > nsym >> 1) throw new Error('too many errors');
    var fixed = correct(cw, synd, errorPositions(errLoc, cw.length));
    if (syndromes(fixed, nsym).some(function (v) { return v !== 0; })) {
      throw new Error('correction did not clear');
    }
    return fixed;
  }

  /* -------------------------------------------------------- payload */

  function crc8(bytes) {
    var crc = 0;
    for (var i = 0; i < bytes.length; i++) {
      crc ^= bytes[i];
      for (var b = 0; b < 8; b++) {
        crc = (crc & 0x80) ? ((crc << 1) ^ 0x07) & 0xff : (crc << 1) & 0xff;
      }
    }
    return crc;
  }

  function decodePayload(cw) {
    if (cw.length !== DATA_BYTES + ECC_BYTES) throw new Error('wrong length');
    var fixed = rsDecode(cw, ECC_BYTES);
    var body = fixed.slice(0, 3);
    if (crc8(body) !== fixed[3]) throw new Error('checksum failed');

    var word = (body[0] << 16) | (body[1] << 8) | body[2];
    var fmt = word >>> PAYLOAD_BITS;
    if (fmt !== FORMAT_3CHAR && fmt !== FORMAT_4CHAR) {
      throw new Error('unknown format');
    }
    var length = fmt === FORMAT_4CHAR ? 4 : 3;

    var value = word & ((1 << PAYLOAD_BITS) - 1), chars = [];
    for (var i = 0; i < length; i++) {
      chars.push(ALPHABET[value % ALPHABET.length]);
      value = Math.floor(value / ALPHABET.length);
    }
    if (value !== 0) throw new Error('payload overruns its length');
    return chars.reverse().join('');
  }

  /* --------------------------------------------------------- pixels */

  function otsu(hist, total) {
    var sumAll = 0, i;
    for (i = 0; i < 256; i++) sumAll += i * hist[i];
    var sumB = 0, wB = 0, best = -1, bestT = 128;
    for (i = 0; i < 256; i++) {
      wB += hist[i];
      if (wB === 0) continue;
      var wF = total - wB;
      if (wF === 0) break;
      sumB += i * hist[i];
      var mB = sumB / wB, mF = (sumAll - sumB) / wF;
      var between = wB * wF * (mB - mF) * (mB - mF);
      if (between > best) { best = between; bestT = i; }
    }
    return bestT;
  }

  /* `gray` is a Uint8Array of w*h luminance values. */
  function binarize(gray, w, h) {
    var hist = new Uint32Array(256), i;
    for (i = 0; i < gray.length; i++) hist[gray[i]]++;
    var t = otsu(hist, w * h);
    var bits = new Uint8Array(w * h);
    for (i = 0; i < gray.length; i++) bits[i] = gray[i] <= t ? 1 : 0;
    return bits;
  }

  function despeckle(bits, w, h) {
    var out = bits.slice();
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        if (!bits[y * w + x]) continue;
        var n = 0;
        for (var dy = -1; dy <= 1; dy++) {
          for (var dx = -1; dx <= 1; dx++) {
            if (dx === 0 && dy === 0) continue;
            var ny = y + dy, nx = x + dx;
            if (ny >= 0 && ny < h && nx >= 0 && nx < w) n += bits[ny * w + nx];
          }
        }
        if (n < 2) out[y * w + x] = 0;
      }
    }
    return out;
  }

  function inkBounds(bits, w, h) {
    var x, y, rows = [], cols = [], c;
    for (y = 0; y < h; y++) {
      c = 0;
      for (x = 0; x < w; x++) c += bits[y * w + x];
      if (c >= 2) rows.push(y);
    }
    for (x = 0; x < w; x++) {
      c = 0;
      for (y = 0; y < h; y++) c += bits[y * w + x];
      if (c >= 2) cols.push(x);
    }
    if (!rows.length || !cols.length) throw new Error('no ink');
    return [cols[0], rows[0], cols[cols.length - 1], rows[rows.length - 1]];
  }

  function sample(bits, w, h, box) {
    var x0 = box[0], y0 = box[1], x1 = box[2], y1 = box[3];
    var mw = (x1 - x0 + 1) / SIZE, mh = (y1 - y0 + 1) / SIZE;
    if (mw < 1 || mh < 1) throw new Error('marker too small');
    var grid = [];
    for (var r = 0; r < SIZE; r++) {
      var row = [];
      for (var c = 0; c < SIZE; c++) {
        var cx0 = Math.floor(x0 + c * mw + mw * 0.25);
        var cx1 = Math.max(Math.floor(x0 + c * mw + mw * 0.75), cx0 + 1);
        var cy0 = Math.floor(y0 + r * mh + mh * 0.25);
        var cy1 = Math.max(Math.floor(y0 + r * mh + mh * 0.75), cy0 + 1);
        var dark = 0, seen = 0;
        for (var y = cy0; y < cy1; y++) {
          if (y < 0 || y >= h) continue;
          for (var x = cx0; x < cx1; x++) {
            if (x < 0 || x >= w) continue;
            seen++; dark += bits[y * w + x];
          }
        }
        row.push(seen && dark * 2 >= seen ? 1 : 0);
      }
      grid.push(row);
    }
    return grid;
  }

  function finderWant() {
    var g = [], r, c;
    for (r = 0; r < SIZE; r++) g.push(new Array(SIZE).fill(0));
    for (var i = 0; i < SIZE; i++) { g[i][0] = 1; g[SIZE - 1][i] = 1; }
    for (c = 0; c < SIZE; c++) g[0][c] = c % 2 === 0 ? 1 : 0;
    for (r = 0; r < SIZE; r++) g[r][SIZE - 1] = r % 2 ? 1 : 0;
    return g;
  }

  function finderScore(grid) {
    var want = finderWant(), score = 0;
    for (var r = 0; r < SIZE; r++) {
      for (var c = 0; c < SIZE; c++) {
        if (r === 0 || r === SIZE - 1 || c === 0 || c === SIZE - 1) {
          if (grid[r][c] === want[r][c]) score++;
        }
      }
    }
    return score;
  }

  function rotate(grid) {
    var out = [];
    for (var r = 0; r < SIZE; r++) {
      var row = [];
      for (var c = 0; c < SIZE; c++) row.push(grid[SIZE - 1 - c][r]);
      out.push(row);
    }
    return out;
  }

  function cells() {
    var out = [];
    for (var r = 1; r < SIZE - 1; r++) {
      for (var c = 1; c < SIZE - 1; c++) out.push([r, c]);
    }
    return out;
  }

  function readGrid(grid) {
    var cands = [], i;
    for (i = 0; i < 4; i++) {
      cands.push({ score: finderScore(grid), turn: i, grid: grid });
      grid = rotate(grid);
    }
    cands.sort(function (a, b) { return (b.score - a.score) || (a.turn - b.turn); });
    if (cands[0].score < MIN_FINDER_SCORE) return null;

    var cs = cells(), need = (DATA_BYTES + ECC_BYTES) * 8;
    for (i = 0; i < cands.length; i++) {
      if (cands[i].score < MIN_FINDER_SCORE) continue;
      var g = cands[i].grid, bits = cs.map(function (rc) { return g[rc[0]][rc[1]]; });
      var cw = [];
      for (var b = 0; b < need; b += 8) {
        var byte = 0;
        for (var k = 0; k < 8; k++) byte = (byte << 1) | bits[b + k];
        cw.push(byte);
      }
      try { return decodePayload(cw); } catch (e) { /* next orientation */ }
    }
    return null;
  }

  /* Read a marker from a region of an ImageData. Returns the code, or
     null - null is the normal case, called many times a second on frames
     that hold nothing. Only exceptions are exceptional. */
  function readImageData(img, rx, ry, rw, rh) {
    rx = rx | 0; ry = ry | 0; rw = rw | 0; rh = rh | 0;
    if (rw < SIZE || rh < SIZE) return null;
    var gray = new Uint8Array(rw * rh), d = img.data;
    for (var y = 0; y < rh; y++) {
      for (var x = 0; x < rw; x++) {
        var p = ((ry + y) * img.width + (rx + x)) * 4;
        /* Rec. 601 luma, integer - the same weighting a phone's own
           preview uses, so what the eye lines up is what gets read. */
        gray[y * rw + x] = (d[p] * 77 + d[p + 1] * 150 + d[p + 2] * 29) >> 8;
      }
    }
    try {
      var bits = despeckle(binarize(gray, rw, rh), rw, rh);
      return readGrid(sample(bits, rw, rh, inkBounds(bits, rw, rh)));
    } catch (e) {
      return null;
    }
  }

  return {
    ALPHABET: ALPHABET, SIZE: SIZE,
    crc8: crc8, rsDecode: rsDecode, decodePayload: decodePayload,
    binarize: binarize, despeckle: despeckle, inkBounds: inkBounds,
    sample: sample, readGrid: readGrid, readImageData: readImageData
  };
}());

if (typeof module !== 'undefined' && module.exports) module.exports = MK;
