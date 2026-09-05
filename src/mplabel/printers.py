"""
printers.py - output backends for 4x6 thermal label printers on a Pi.

Four backends, in rough order of how likely they are to just work:

  cups-pdf     hand the 4x6 PDF to CUPS. Rollo, Munbyn, iDPRT, Phomemo,
               Brother QL and anything with a real driver.
  cups-raster  render to PNG at the printer's dpi first, then hand that to
               CUPS. Use when the PDF path prints scaled or blank.
  zpl          raw Zebra ZPL II over /dev/usb/lp0. Zebra, and the many
               clones that speak ZPL.
  tspl         raw TSPL/TSPL2 over /dev/usb/lp0. TSC, and clones of it
               (some Munbyn, Beeprt, Xprinter models).

Pick one with PRINTER_BACKEND in the config. If you do not know which,
run `python3 printers.py --probe` to see what is attached.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import sys
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    # Windows has no fcntl. The Pi is the deployment target and the print
    # lock matters there, but importing this module has to work anywhere
    # or the tests cannot run off-target - which is where they are
    # written. `cli.py` has carried this guard since the last time an
    # unguarded import took the whole suite down on Windows; this is the
    # second time, so the note is here too.
    fcntl = None

log = logging.getLogger("mplabel.printers")

DEFAULT_DPI = 203          # 203 dpi is standard; some printers are 300
LABEL_W_IN = 4.0
LABEL_H_IN = 6.0

# Dots across the print head. The G4 is 812 at 203dpi, and anything wider
# is clipped by the hardware - see the note in CLAUDE.md about an 824-dot
# page ejecting a second, near-blank label. Configurable because a 300dpi
# printer has a wider head; checked by default because the failure is
# silent and costs a label every time.
DEFAULT_HEAD_DOTS = 812

# How long to wait on `lp`. Both CUPS backends run with check=True, and a
# CUPS queue that is disabled or waiting on a device can block forever -
# which, from the poll loop, is indistinguishable from a hung poller.
CUPS_TIMEOUT = 60


class PrinterUnavailable(Exception):
    """The printer cannot be reached: no device node, or CUPS has it.

    Deliberately an Exception and not SystemExit. SystemExit derives from
    BaseException, so it escaped every `except Exception` in this system -
    the poll loop's handler, the per-message handler, and web._dispatch's
    catch-all. The poller died on a switched-off printer instead of
    logging a failed print, and from the phone the error was swallowed by
    threading and arrived as a bare closed connection with no message.
    `cli.main` turns this back into a SystemExit so the command line still
    exits cleanly."""

# ESC/POS control sequences, spelled numerically so they survive being
# copied around and stay greppable against the command tables.
ESC_INIT = bytes((0x1B, 0x40))                  # ESC @   reset
GS_RASTER = bytes((0x1D, 0x76, 0x30, 0x00))     # GS v 0  raster, mode 0
ESC_FEED_DOTS = bytes((0x1B, 0x4A))             # ESC J n feed n dots
FORM_FEED = bytes((0x0C,))                      # FF      next label


# ------------------------------------------------------------ rasterising

def render_png(pdf_path, png_path, dpi=DEFAULT_DPI):
    """Render page 1 of a PDF to a 1-bit PNG at the printer's dot pitch."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        pil = doc[0].render(scale=dpi / 72).to_pil()
    finally:
        doc.close()

    from PIL import Image
    pil = pil.convert("L")
    want = (round(LABEL_W_IN * dpi), round(LABEL_H_IN * dpi))
    if pil.size != want:
        pil = pil.resize(want, Image.LANCZOS)
    # Fixed threshold, not dithering - barcodes must stay crisp.
    pil = pil.point(lambda p: 255 if p > 128 else 0).convert("1")
    pil.save(png_path, dpi=(dpi, dpi))
    return pil


def render_bitmap(pdf_path, dpi=DEFAULT_DPI, invert=False,
                  head_dots=DEFAULT_HEAD_DOTS):
    """Render to packed 1-bit-per-pixel rows for raw printer languages.

    Returns (data, width_px, width_bytes, height). A set bit means a black
    dot, unless invert=True, which is what TSPL wants.

    The dot count is pinned to exactly LABEL_W_IN x LABEL_H_IN at the given
    dpi. Rounding in the PDF rasteriser otherwise yields e.g. 1801 rows at
    300 dpi, and that row of overflow becomes a second, near-blank label."""
    from PIL import Image
    import pypdfium2 as pdfium

    want_w = round(LABEL_W_IN * dpi)
    want_h = round(LABEL_H_IN * dpi)

    # The dot count was pinned to the label size and never compared with
    # the head. printer_dpi = 300 is a plausible edit - the comment on
    # DEFAULT_DPI invites it - and renders 1200 dots onto an 812-dot head.
    # That is the same failure as the 824-dot page in CLAUDE.md, four
    # times over, and it is silent: the overflow just does not print.
    if head_dots and want_w > head_dots:
        raise ValueError(
            f"{dpi}dpi renders {want_w} dots across, wider than the "
            f"{head_dots}-dot print head - the overflow is clipped and "
            f"ejects a second, near-blank label. Lower printer_dpi, or "
            f"raise printer_head_dots if this printer really is wider.")

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        pil = doc[0].render(scale=dpi / 72).to_pil()
    finally:
        doc.close()

    pil = pil.convert("L")
    if pil.size != (want_w, want_h):
        pil = pil.resize((want_w, want_h), Image.LANCZOS)

    width_bytes = (want_w + 7) // 8
    px = pil.load()

    data = bytearray(width_bytes * want_h)
    for y in range(want_h):
        row = y * width_bytes
        for x in range(want_w):
            dark = px[x, y] <= 128
            if dark != invert:
                data[row + (x >> 3)] |= 0x80 >> (x & 7)

    if invert:
        # Pad bits past the right edge must read as white (set) for TSPL.
        spare = width_bytes * 8 - want_w
        if spare:
            mask = (1 << spare) - 1
            for y in range(want_h):
                data[y * width_bytes + width_bytes - 1] |= mask

    return bytes(data), want_w, width_bytes, want_h


# ----------------------------------------------------------------- backends

def print_cups_pdf(pdf_path, printer=None, media="Custom.4x6in",
                   fit=False, extra=()):
    cmd = ["lp"]
    if printer:
        cmd += ["-d", printer]
    cmd += ["-o", f"media={media}"]
    if fit:
        cmd += ["-o", "fit-to-page"]
    for opt in extra:
        cmd += ["-o", opt]
    cmd.append(str(pdf_path))
    subprocess.run(cmd, check=True, timeout=CUPS_TIMEOUT)


def print_cups_raster(pdf_path, printer=None, dpi=DEFAULT_DPI,
                      media="Custom.4x6in", extra=()):
    png = Path(pdf_path).with_suffix(f".{dpi}.png")
    render_png(pdf_path, png, dpi)
    cmd = ["lp"]
    if printer:
        cmd += ["-d", printer]
    cmd += ["-o", f"media={media}", "-o", "scaling=100"]
    for opt in extra:
        cmd += ["-o", opt]
    cmd.append(str(png))
    subprocess.run(cmd, check=True, timeout=CUPS_TIMEOUT)


def print_zpl(pdf_path, device="/dev/usb/lp0", dpi=DEFAULT_DPI, darkness=None,
              head_dots=DEFAULT_HEAD_DOTS, settle=2.0):
    # `settle` was the one raw backend that silently dropped it. The pause
    # exists because this class of firmware discards bytes that arrive
    # while the head is still moving, and that applies whatever language
    # the job was written in.
    data, width_px, width_bytes, height = render_bitmap(
        pdf_path, dpi, invert=False, head_dots=head_dots)
    total = width_bytes * height

    head = ["^XA", f"^PW{width_px}", f"^LL{height}", "^LH0,0"]
    if darkness is not None:
        head.append(f"~SD{int(darkness):02d}")   # 0-30
    zpl = ("\n".join(head)
           + f"\n^FO0,0^GFA,{total},{total},{width_bytes},"
           + data.hex().upper()
           + "^FS\n^XZ\n")
    _write_raw(device, zpl.encode("ascii"), settle=settle)


def build_tspl(pdf_path, dpi=DEFAULT_DPI, darkness=None, speed=None,
               media="gap", gap_in=0.12, copies=1,
               head_dots=DEFAULT_HEAD_DOTS):
    """Assemble a complete TSPL job as bytes.

    media: 'gap' for die-cut labels (the normal case for 4x6 shipping
    stock), 'blackmark' for black-mark stock, 'continuous' for gapless.

    Getting this wrong is the classic failure. GAP 0,0 means continuous:
    on die-cut labels the printer never finds the label edge, so prints
    drift down the roll a little further each time. Conversely a gap
    command on continuous stock makes the printer hunt for a gap it will
    never find and throw a media error."""
    # TSPL bitmaps are inverted relative to ZPL: a clear bit prints.
    data, _width_px, width_bytes, height = render_bitmap(
        pdf_path, dpi, invert=True, head_dots=head_dots)
    head = [f"SIZE {LABEL_W_IN},{LABEL_H_IN}"]

    if media == "gap":
        head.append(f"GAP {gap_in},0")
    elif media == "blackmark":
        head.append(f"BLINE {gap_in},0")
    elif media == "continuous":
        head.append("GAP 0,0")
    elif media != "printer":
        raise ValueError(f"unknown media tracking {media!r}")

    if darkness is not None:
        head.append(f"DENSITY {int(darkness)}")      # 0-15
    if speed is not None:
        head.append(f"SPEED {speed}")                # inches/sec
    head += ["DIRECTION 1", "REFERENCE 0,0", "CLS"]

    payload = ("\r\n".join(head) + "\r\n").encode("ascii")
    payload += f"BITMAP 0,0,{width_bytes},{height},0,".encode("ascii")
    payload += data
    payload += f"\r\nPRINT {int(copies)},1\r\n".encode("ascii")
    return payload


def print_tspl(pdf_path, device="/dev/usb/lp0", dpi=DEFAULT_DPI,
               darkness=None, speed=None, media="gap", gap_in=0.12,
               settle=2.0, head_dots=DEFAULT_HEAD_DOTS):
    payload = build_tspl(pdf_path, dpi, darkness, speed, media, gap_in,
                         head_dots=head_dots)
    _write_raw(device, payload, settle=settle)


def tspl_selftest(device="/dev/usb/lp0", media="gap", gap_in=0.12):
    """Print a tiny text-only label using the printer's built-in fonts.

    Worth trying before any bitmap: it is a few dozen bytes, so if this
    prints and a bitmap job does not, the problem is data transfer rather
    than the command language."""
    cmds = [f"SIZE {LABEL_W_IN},{LABEL_H_IN}"]
    cmds.append(f"GAP {gap_in},0" if media == "gap" else "GAP 0,0")
    cmds += ["DIRECTION 1", "CLS",
             'TEXT 40,80,"4",0,2,2,"TSPL OK"',
             'TEXT 40,160,"3",0,1,1,"If you can read this, the"',
             'TEXT 40,210,"3",0,1,1,"printer speaks TSPL."',
             "PRINT 1,1"]
    _write_raw(device, ("\r\n".join(cmds) + "\r\n").encode("ascii"))


# TSPL status queries. Neither prints anything: they ask the printer how
# it is and expect a byte back.
TSPL_STATUS = bytes((0x1B, 0x21, 0x3F))     # <ESC>!?  -> one status byte
TSPL_HOST_STATUS = b"~HS\r\n"               # TSC's multi-line variant

# Bit meanings for the one-byte reply, per the TSPL manual. Only useful if
# the printer answers at all - see ask_status.
TSPL_STATUS_BITS = [
    (0x01, "head open"),
    (0x02, "paper jam"),
    (0x04, "out of paper"),
    (0x08, "out of ribbon"),
    (0x10, "pause"),
    (0x20, "printing"),
    (0x40, "cover open"),
]


def ask_status(device="/dev/usb/lp0", timeout=0.5):
    """Ask the printer how it is, and see whether it answers at all.

    This is the experiment behind the one failure that actually loses a
    parcel. A successful `os.write` does not mean a label came out: out of
    paper, head open, jam and a wrong gap distance all accept the bytes
    happily and print nothing, after which `mark_printed` sets printed_at
    and the row leaves the Pending query. The parcel becomes invisible to
    the recovery path at the exact moment it needed to be visible - and
    the phone app removes the last defence, which was a person standing
    near the printer.

    If this unit answers, printd can refuse a job when the paper is out
    and the row stays where she can see it. **Whether it answers is
    unknown** - bidirectional reads have never been tested on this
    hardware, and the IEEE-1284 id has already been caught lying once.

    Returns a dict; never raises for a printer that simply says nothing.
    Non-destructive: neither query prints. Every read is bounded by
    `select`, because a blocking read on a device that will never answer
    is its own way to wedge the printer."""
    import select

    out = {"device": device, "answered": False, "raw": None,
           "flags": [], "note": ""}
    dev = Path(device)
    if not dev.exists():
        out["note"] = f"{device} does not exist"
        return out
    try:
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        # Read access is not a given: the udev rule grants the lp group
        # write, and a printer may be write-only regardless.
        out["note"] = f"cannot open read-write ({exc}); write-only is normal"
        return out
    try:
        # Drain anything stale first, so a leftover byte is not read as an
        # answer to a question we have not asked yet.
        try:
            while select.select([fd], [], [], 0)[0]:
                if not os.read(fd, 64):
                    break
        except OSError:
            pass

        for query, name in ((TSPL_STATUS, "<ESC>!?"),
                            (TSPL_HOST_STATUS, "~HS")):
            try:
                os.write(fd, query)
            except OSError as exc:
                out["note"] = f"write of {name} failed: {exc}"
                continue
            if select.select([fd], [], [], timeout)[0]:
                try:
                    data = os.read(fd, 64)
                except OSError as exc:
                    out["note"] = f"{name} read failed: {exc}"
                    continue
                if data:
                    out["answered"] = True
                    out["raw"] = data.hex()
                    out["query"] = name
                    first = data[0]
                    out["flags"] = [label for bit, label in TSPL_STATUS_BITS
                                    if first & bit]
                    if not out["flags"]:
                        out["flags"] = ["ready"]
                    return out
        out["note"] = out["note"] or (
            f"no reply within {timeout}s to either query - this printer is "
            f"probably write-only, so a failed print cannot be detected "
            f"from software")
    finally:
        os.close(fd)
    return out


def build_escpos(pdf_path, dpi=DEFAULT_DPI, media="gap", band_rows=128,
                 feed_dots=0, copies=1, head_dots=DEFAULT_HEAD_DOTS):
    """Assemble a complete ESC/POS raster job as bytes.

    The G4 self-describes as `COMMAND SET:ESC/POS` with no TSPL anywhere
    in its IEEE-1284 id, so this - not tspl - is the backend it needs.

    ESC/POS prints on a *set* bit, like ZPL and opposite TSPL, so the
    bitmap goes in un-inverted. That also leaves the four spare bits past
    the right edge of each row clear, which reads as white; setting them
    would draw a black stripe down every label.

    The image goes as a series of `GS v 0` blocks of band_rows each
    rather than one 1218-row block, because several of these budget
    firmwares cap a single raster command well below a full 6in label and
    silently print nothing when the cap is exceeded. Bands stack in the
    order sent, so the seam is invisible."""
    if media not in ("gap", "continuous"):
        raise ValueError(f"unknown media tracking {media!r}")
    if band_rows < 1:
        raise ValueError("band_rows must be at least 1")

    data, _width_px, width_bytes, height = render_bitmap(
        pdf_path, dpi, invert=False, head_dots=head_dots)
    job = bytearray()
    for _ in range(int(copies)):
        job += ESC_INIT
        y = 0
        while y < height:
            rows = min(band_rows, height - y)
            job += GS_RASTER
            job += bytes((width_bytes & 0xFF, width_bytes >> 8,
                          rows & 0xFF, rows >> 8))
            job += data[y * width_bytes:(y + rows) * width_bytes]
            y += rows
        if feed_dots:
            job += ESC_FEED_DOTS + bytes((min(int(feed_dots), 255),))
        if media == "gap":
            # Form feed advances to the next die-cut label using the gap
            # sensor. On continuous stock there is no gap to find, and
            # asking for one makes the printer hunt and throw a media error.
            job += FORM_FEED
    return bytes(job)


def print_escpos(pdf_path, device="/dev/usb/lp0", dpi=DEFAULT_DPI,
                 darkness=None, media="gap", band_rows=128, feed_dots=0,
                 settle=2.0, head_dots=DEFAULT_HEAD_DOTS):
    # darkness is accepted so every raw backend takes the same config keys,
    # but it is not sent: ESC/POS density is vendor-specific and the G4
    # documents no command for it. Set darkness on the unit itself.
    payload = build_escpos(pdf_path, dpi, media, band_rows, feed_dots,
                           head_dots=head_dots)
    _write_raw(device, payload, settle=settle)


def escpos_selftest(device="/dev/usb/lp0"):
    """Print a tiny text-only label using the printer's built-in font.

    ESC/POS prints plain ASCII as text, so this needs no raster at all.
    If it prints and a real label does not, the problem is the raster path
    or data transfer, not the command language."""
    # CR+LF, not bare LF: the only line ending this printer is known to
    # render is the \r\n the TSPL selftest happened to use when it printed
    # its own source as text. Trailing blank lines push the last line past
    # the head in case FF turns out to be ignored in standard mode.
    body = (ESC_INIT
            + b"ESC/POS OK\r\n"
            + b"If you can read this, the\r\n"
            + b"printer speaks ESC/POS.\r\n"
            + b"\r\n\r\n\r\n"
            + FORM_FEED)
    _write_raw(device, body)


def lock_path(cfg=None, device=None):
    """Where the print lock for one printer lives.

    `/run/lock` first: it is the standard place, it is world-writable
    (1777) so any identity that can reach the printer can take the lock,
    and it is outside any unit's PrivateTmp. Keyed on the device so two
    printers do not block each other. Falls back to the data directory,
    then to a temp dir, because a printer test has to keep working on a
    box where neither exists."""
    cfg = cfg or {}
    device = device or cfg.get("printer_device") or "default"
    name = "mplabel-" + re.sub(r"[^A-Za-z0-9_.-]", "_", Path(device).name)
    run_lock = Path("/run/lock")
    if run_lock.is_dir() and os.access(run_lock, os.W_OK):
        return run_lock / f"{name}.lock"
    home = cfg.get("home")
    if home:
        return Path(home) / f".{name}.lock"
    return Path(tempfile.gettempdir()) / f"{name}.lock"


@contextmanager
def print_lock(cfg=None, device=None, required=False):
    """Hold the printer for the length of one job.

    More than one thing reaches the printer - the poll loop, the web app,
    and a person running `mplabel reprint` over ssh. `_write_raw` hands
    the whole job over in a single write because this firmware discards
    bytes arriving while the head is moving, so two writers interleaved
    produce one garbage label or a job that silently vanishes. The lock
    spans the settle pause too.

    Blocking, not LOCK_NB: a reprint from her phone should queue behind
    the poller, never fail.

    `required` decides what an unobtainable lock means. For the CLI it is
    a warning: `probe`, `selftest` and `file` deliberately run above
    `connect_db` so a printer test still works when the data directory is
    missing, and a lock we cannot take must not become the thing that
    stops a label. A long-running daemon has no such excuse and should
    pass required=True, so the failure is loud rather than a silent loss
    of interlocking."""
    path = lock_path(cfg, device)
    fh = None
    if fcntl is None:
        # No flock off-Linux, and that is a warning even when `required`,
        # unlike an unobtainable lock. `required` exists so a daemon
        # notices it has lost interlocking against a device it shares;
        # a platform with no flock at all also has no /dev/usb/lp0, so
        # there is nothing to interlock and nothing to lose. Raising here
        # would only stop the daemon being exercised off-target, which is
        # where its tests run.
        log.warning("printing without a lock (%s): no fcntl on this "
                    "platform", path)
        yield
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0o666 at creation: whoever prints first must not lock everyone
        # else out. The file holds nothing - only the flock matters.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass          # not the owner; the existing mode has to do
        fh = os.fdopen(fd, "r+")
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError as exc:
        if fh is not None:
            fh.close()
            fh = None
        if required:
            raise PrinterUnavailable(
                f"cannot take the print lock at {path}: {exc}")
        log.warning("printing without a lock (%s): %s", path, exc)
    try:
        yield
    finally:
        if fh is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def backend_kwargs(cfg, backend=None, code=None):
    """The keyword arguments `send()` needs for one backend, from config.

    Extracted from cli.print_label so that anything else driving a printer
    - a print daemon, a test - builds them the same way rather than
    growing a second, drifting copy."""
    backend = backend or cfg["printer_backend"]
    # `.get` with the module default, not `cfg[...]`: a host that has
    # never had a printer has no reason to carry printer_dpi, and a
    # KeyError here becomes a 503 per request inside printd.
    dpi = int(cfg.get("printer_dpi") or DEFAULT_DPI)
    darkness = int(cfg["printer_darkness"]) if cfg.get("printer_darkness") else None
    head = int(cfg.get("printer_head_dots") or DEFAULT_HEAD_DOTS)
    settle = float(cfg.get("settle_seconds", 2.0))

    if backend == "pi-http":
        return {"url": cfg.get("printd_url"),
                "secret": cfg.get("printd_secret"),
                "timeout": float(cfg.get("printd_timeout", 45)),
                "code": code}
    if backend.startswith("cups"):
        kwargs = {"printer": cfg.get("printer_queue") or None}
        if backend == "cups-raster":
            kwargs["dpi"] = dpi
        return kwargs
    if backend == "tspl":
        return {"device": cfg["printer_device"], "dpi": dpi,
                "darkness": darkness,
                "speed": int(cfg["printer_speed"]) if cfg.get("printer_speed") else None,
                "media": cfg.get("media_tracking", "gap"),
                "gap_in": float(cfg.get("gap_inches", 0.12)),
                "head_dots": head,
                "settle": settle}
    if backend == "escpos":
        # ESC/POS has no gap-distance command - the printer finds the gap
        # itself on a form feed - so gap_inches does not apply here.
        return {"device": cfg["printer_device"], "dpi": dpi,
                "media": cfg.get("media_tracking", "gap"),
                "band_rows": int(cfg.get("escpos_band_rows", 128)),
                "head_dots": head,
                "settle": settle}
    return {"device": cfg["printer_device"], "dpi": dpi,
            "darkness": darkness, "head_dots": head, "settle": settle}


def _write_raw(device, payload, settle=2.0):
    """Write the whole job in one burst, then pause.

    These budget TSPL firmwares silently discard bytes that arrive while
    the head is already moving. A driver that streams at render speed
    loses everything after the first label. So: build the job fully in
    memory, hand it over in a single unbuffered write, and give the
    printer a moment before the next one."""
    dev = Path(device)
    if not dev.exists():
        raise PrinterUnavailable(
            f"{device} not found. Check `ls /dev/usb/` and that the usblp "
            f"module is loaded (`lsmod | grep usblp`). If CUPS has claimed "
            f"the printer it will have unbound usblp - you cannot use a "
            f"CUPS queue and the raw device for the same printer at once.")

    fd = os.open(dev, os.O_WRONLY)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        try:
            os.fsync(fd)
        except OSError:
            # /dev/usb/lp0 is a character device and does not implement
            # fsync - it returns EINVAL. Observed on the G4: the label
            # printed, then fsync raised, and process_message caught that
            # and wrote "print failed" into sales.notes with printed_at
            # left NULL. So a perfectly good label showed up as NOT
            # PRINTED in `mplabel list`. The os.write above has already
            # handed the bytes to the kernel; there is nothing to flush.
            pass
    finally:
        os.close(fd)

    if settle:
        time.sleep(settle)


def detect_language(device="/dev/usb/lp0"):
    """Ask the kernel what the printer said about itself at enumeration.

    Costs nothing and prints nothing. Most TSPL printers self-describe
    with TSPL in the CMD / COMMAND SET field of their IEEE-1284 id."""
    node = Path(device).name
    for candidate in (Path(f"/sys/class/usbmisc/{node}/device/ieee1284_id"),
                      Path(f"/sys/class/usb/{node}/device/ieee1284_id")):
        if candidate.exists():
            ident = candidate.read_text(errors="replace").strip()
            upper = ident.upper()
            for lang in ("TSPL", "ZPL", "EPL", "ESC/POS", "PCL"):
                if lang in upper:
                    return lang, ident
            return None, ident
    return None, None


# Which raw backend serves each language detect_language() can report.
# EPL and PCL deliberately have no entry: probe must say so plainly rather
# than suggest a printer_backend value that send() would reject.

def print_pi_http(pdf_path, url=None, secret=None, timeout=45.0, job=None,
                  code=None):
    """Hand the job to a printd over HTTP.

    A backend like any other, so `cli.print_label` and every caller above
    it - reprint, pending, test-print, the phone app - are unchanged, and
    switching back is one line of config.

    urllib, not requests: the short dependency list is deliberate.

    Retries are *not* done here. A print is not safely retryable from the
    client's side, because a timeout cannot distinguish "never arrived"
    from "printed, and the acknowledgement was lost" - and the second one
    retried is a duplicate label on a parcel. printd keeps a durable
    journal so the question can be *asked* instead; that is what
    GET /printed is for."""
    import urllib.error
    import urllib.request

    if not url:
        raise PrinterUnavailable("printd_url is not set")
    body = Path(pdf_path).read_bytes()
    job = job or f"{code or 'job'}-{os.urandom(8).hex()}"
    req = urllib.request.Request(
        url.rstrip("/") + "/print", data=body, method="POST",
        headers={
            "Content-Type": "application/pdf",
            "X-MPLabel-Protocol": "1",
            "X-MPLabel-Job": job,
            "X-MPLabel-Sig": _sign_job(secret, job, body),
            # How long this caller is willing to wait. printd refuses
            # rather than printing to an empty room after we have given up.
            "X-MPLabel-Deadline": str(timeout),
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read() or b"{}"
        try:
            return json.loads(raw)
        except ValueError as exc:
            # A 200 that is not JSON is a proxy or a captive portal
            # answering instead of printd. Left as a bare ValueError it
            # reaches the phone app as a 400 "bad request", blaming the
            # caller for something upstream.
            raise PrinterUnavailable(
                f"printd at {url} answered 200 but not JSON ({exc}); "
                f"something between here and the printer replied instead. "
                f"The label may or may not have printed - ask it with "
                f"GET /printed before sending this job again.")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        if exc.code == 409:
            # Already printed. Not an error worth failing a batch over.
            log.warning("printd says job %s already printed", job)
            return {"printed": False, "job": job, "duplicate": True}
        raise PrinterUnavailable(f"printd said {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrinterUnavailable(
            f"could not reach printd at {url}: {exc}. The label may or may "
            f"not have printed - ask it with GET /printed before sending "
            f"this job again.")


# ------------------------------------------------------- the label maker
#
# The 48mm tag printer gets its own dispatch rather than an entry in
# BACKENDS, because the two are not the same shape: `send()` takes a file
# path and throws the result away, and a tag is a *spec* whose answer -
# did it stall, what did the device say at each step - is the whole
# point of asking.
#
# What crosses the wire is the spec, never the raster. The label maker
# has more roll-and-hardware facts than the 4x6 printer does, not fewer:
# PRINTABLE_LEFT_DOTS/PRINTABLE_RIGHT_DOTS were measured on one roll and
# are gap_inches wearing a different hat, `density` is burn energy
# against a particular paper, and the label size is the die-cut label
# physically in the machine. Shipping the compressed blob would move all
# four onto whichever host happened to build it - and the blob is
# self-contained precisely *because* they are baked into its buffer
# headers, which is what makes it the wrong thing to send.

TAG_BACKENDS = {}


def tag_geometry(cfg):
    """The roll facts, from the config of the host that has the roll."""
    from . import inventory as inventory_mod

    raw = (cfg.get("supvan_label_mm") or "").strip()
    if raw:
        w, h = parse_label_size(raw)
    else:
        w, h = inventory_mod.DEFAULT_LABEL_MM
    density = cfg.get("supvan_density")
    density = int(density) if density not in (None, "") else None
    if density is None:
        from . import supvan as supvan_mod
        density = supvan_mod.DEFAULT_DENSITY
    return (w, h), density


def parse_label_size(text):
    """`WxH` in millimetres, or with an `in` suffix, in inches.

    Inches are allowed because label stock is sold in them - 4x1in is a
    shelf label - and converting by hand is how a 4in label becomes a
    4mm one."""
    raw = str(text).strip().lower()
    scale = 1.0
    if raw.endswith("in"):
        raw, scale = raw[:-2], 25.4
    try:
        w, h = (float(v) * scale for v in raw.split("x"))
    except ValueError:
        raise ValueError(f"a label size wants WxH, not {text!r}")
    if w <= 0 or h <= 0:
        raise ValueError(f"{text!r} is not a label")
    return w, h


def assemble_tag(spec, cfg):
    """Render a spec and build the job. No device, no network.

    Returns (job, result). `result` is the wire schema minus whatever
    only printing can fill in, so a dry run and a real print answer in
    the same shape and the caller has one thing to render."""
    from . import inventory as inventory_mod
    from . import supvan as supvan_mod

    label_mm, density = tag_geometry(cfg)
    # The spec may override, and an override is a deliberate one-shot -
    # which is why the CLI flags default to None rather than to a value:
    # "the user typed it" has to be distinguishable from "the default
    # fired", or every request silently carries the client's opinion of
    # a roll it cannot see.
    if spec.get("size_mm"):
        label_mm = tuple(spec["size_mm"])
    if spec.get("density") is not None:
        density = int(spec["density"])

    raster, stride, rows, label_mm = inventory_mod.render_tag(spec, label_mm)
    job = supvan_mod.build_job(raster, stride, rows, density=density)

    # Round-trip it whatever happens next: a job that will not come back
    # apart is not going to the printer either, and this is also what
    # makes a preview a picture of the payload rather than of what we
    # meant to send.
    try:
        back, back_stride, cols = supvan_mod.decode_job(job["compressed"])
    except supvan_mod.SupvanError as exc:
        raise ValueError(f"the job does not decode: {exc}")

    ink = sum(bin(b).count("1") for b in raster)
    result = {
        "printed": False,
        "kind": spec.get("kind"),
        "code": spec.get("code"),
        "label": {
            "mm": [label_mm[0], label_mm[1]],
            "sideways": inventory_mod.reads_sideways(label_mm),
            "rows": rows,
            "dots": [stride * 8, rows],
            "feed_mm": round(rows / inventory_mod.DOTS_PER_MM, 1),
            "media_box": list(inventory_mod.media_box(label_mm)),
            "ink_pct": round(100 * ink / (len(raster) * 8), 2),
        },
        # `buffer_count`, never `buffers`. The word that means two things
        # does not appear on the wire at all.
        "payload": {
            "buffer_count": job["buffers"],
            "raw_len": job["raw_len"],
            "compressed_len": len(job["compressed"]),
            "speed": job["speed"],
            "density": density,
            "decoded_columns": cols,
            "decoded_stride": back_stride,
        },
        "trace": [],
    }
    return job, result


def print_tag_local(spec, cfg=None, device=None, dry_run=False, job=None,
                    lock=True):
    """Render and print a tag on a label maker attached to this host.

    `lock=False` for a caller that already holds the device - printd does,
    via its own `_Device`. Taking it twice is not a no-op: `print_lock`
    opens the lock file fresh each call, so the second is a different
    open file description, and `flock` conflicts between descriptions
    **even inside one process**. It deadlocks against itself and blocks
    for ever, with the device held and the tag gate shut.

    This is the same trap `REMOTE_BACKENDS` exists for on the 4x6 path -
    `cli.print_label` skips the flock for `pi-http` because holding it on
    both sides deadlocked on a same-Pi loopback deployment. Same lesson,
    second device."""
    from . import supvan as supvan_mod

    cfg = cfg or {}
    built, result = assemble_tag(spec, cfg)
    result["job"] = job
    result["dry_run"] = bool(dry_run)
    if dry_run:
        import base64
        result["compressed_b64"] = base64.b64encode(
            built["compressed"]).decode()
        return result

    device = device or cfg.get("supvan_device") or supvan_mod.DEFAULT_DEVICE

    def step(label, status, lit):
        result["trace"].append({"step": label, "flags": list(lit)})

    # The tag printer gets its own lock file - `lock_path` is keyed on
    # the device name, so this is `mplabel-hidraw0.lock` and not the
    # 4x6 printer's. Without it a hand-run `inventory-label --print` over
    # ssh and a daemon would collide on the hidraw node with nothing to
    # serialise them.
    import contextlib
    held = (print_lock(cfg, device=device, required=False) if lock
            else contextlib.nullcontext())
    with held:
        final = supvan_mod.print_job(built, path=device, on_step=step)

    result["final"] = _jsonable_status(final)
    result["printed"] = not final.get("stalled")
    result["stalled"] = bool(final.get("stalled"))
    return result


def _jsonable_status(status):
    """A status report that survives `json.dumps`.

    `decode_status` puts the raw report in `status["raw"]` as **bytes**,
    and json refuses them with a TypeError. Inside printd that TypeError
    becomes a 503 - so a label that printed perfectly would be reported
    as a failure and the caller would print it again. Same shape as the
    fsync/EINVAL incident: the print worked, the bookkeeping said
    otherwise."""
    out = {}
    for key, value in (status or {}).items():
        if isinstance(value, (bytes, bytearray)):
            out[key] = bytes(value).hex(" ")
        else:
            out[key] = value
    return out

def print_tag_pi_http(spec, url=None, secret=None, timeout=90.0, job=None,
                      code=None, dry_run=False):
    """Hand a tag spec to a printd that has the label maker.

    The spec is the body, so the HMAC covers it - which is why `kind`
    travels inside it rather than in a header or the path. The signature
    is over the job id and a digest of the body and nothing else, so
    routing on anything unsigned would let a signed body be aimed at a
    printer its signer never chose.

    A longer default timeout than the 4x6 path: the deadline header only
    bounds *getting* the device, and the tag sequence itself polls the
    printer between every step. The socket has to outlast the print."""
    import urllib.error
    import urllib.request

    if not url:
        raise PrinterUnavailable("printd_url is not set")
    payload = dict(spec)
    if dry_run:
        payload["dry_run"] = True
    body = json.dumps(payload, sort_keys=True).encode()
    job = job or f"{code or spec.get('code') or 'tag'}-{os.urandom(8).hex()}"

    req = urllib.request.Request(
        url.rstrip("/") + "/print-tag", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-MPLabel-Protocol": "1",
            "X-MPLabel-Job": job,
            "X-MPLabel-Sig": _sign_job(secret, job, body),
            "X-MPLabel-Deadline": str(timeout),
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read() or b"{}"
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise PrinterUnavailable(
                f"printd at {url} answered 200 but not JSON ({exc}); "
                f"something between here and the printer replied instead.")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        parsed = {}
        try:
            parsed = json.loads(detail)
            detail = parsed.get("error", detail)
        except ValueError:
            pass
        if exc.code == 404:
            raise PrinterUnavailable(
                f"printd at {url} has no /print-tag - it predates the label "
                f"maker being behind the service. Update it.")
        if exc.code == 409:
            log.warning("printd says tag job %s already printed", job)
            return {"printed": False, "job": job, "duplicate": True}
        if parsed.get("stalled"):
            # Paper moved and the job never finished. Carry the trace and
            # the reseat advice - the device leaves the media out of
            # position, which is the seating error that blocks the *next*
            # attempt, so "try again" without reseating fails twice.
            raise PrinterUnavailable(
                f"the label maker stalled: {detail}\nReseat the media before "
                f"the next attempt - the positioning move leaves it out of "
                f"place, which is the seating error that blocks the "
                f"following run.")
        raise PrinterUnavailable(f"printd said {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrinterUnavailable(
            f"could not reach printd at {url}: {exc}. The tag may or may "
            f"not have printed - ask it with GET /printed before sending "
            f"this job again.")


TAG_BACKENDS.update({
    "supvan": print_tag_local,
    "pi-http": print_tag_pi_http,
})


def tag_backend_kwargs(cfg, backend=None, job=None):
    """Arguments for one tag backend, from config.

    The remote branch deliberately carries no geometry: not the label
    size, not the density, not PRINTABLE_*. Those describe the roll in
    the machine, and the machine is at the other end."""
    backend = backend or cfg.get("tag_backend") or "supvan"
    if backend == "pi-http":
        return {"url": cfg.get("printd_url"),
                "secret": cfg.get("printd_secret"),
                "timeout": float(cfg.get("printd_tag_timeout") or 90),
                "job": job}
    return {"cfg": cfg, "device": cfg.get("supvan_device"), "job": job}


def print_tag(spec, backend=None, **kwargs):
    """Print one tag, and **return what happened**.

    Unlike `send`, which is file-shaped and discards its result. A tag's
    answer - did it stall, what did the device report at each step - is
    the reason for asking, so throwing it away would leave a stall
    indistinguishable from a print."""
    backend = backend or "supvan"
    try:
        fn = TAG_BACKENDS[backend]
    except KeyError:
        raise PrinterUnavailable(
            f"Unknown tag backend {backend!r}. "
            f"Choose from: {', '.join(TAG_BACKENDS)}")
    return fn(spec, **kwargs)

def _sign_job(secret, job, body):
    digest = hashlib.sha256(body).hexdigest()
    return hmac.new((secret or "").encode(),
                    f"{job}\n{digest}".encode(), hashlib.sha256).hexdigest()


# Backends that hand the job to something else rather than touching a
# device. The print lock belongs to whoever actually writes to the
# printer: on Phase 3 the client and printd are the *same Pi*, so a
# client that holds the lock while waiting for printd deadlocks against
# printd trying to take it. Verified - it hung until the client timed out.
REMOTE_BACKENDS = {"pi-http"}


def printd_health(cfg, timeout=5.0):
    """Ask a remote printd how it is. Unauthenticated by design - it
    reports configuration and liveness, never anything about an order."""
    import urllib.error
    import urllib.request

    url = cfg.get("printd_url")
    if not url:
        raise PrinterUnavailable("printd_url is not set")
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz",
                                    timeout=timeout) as res:
            return json.loads(res.read() or b"{}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrinterUnavailable(f"could not reach printd at {url}: {exc}")


def printd_printed(cfg, since=None, timeout=10.0):
    """What printd says actually reached the paper."""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = cfg.get("printd_url")
    if not url:
        raise PrinterUnavailable("printd_url is not set")
    q = "?" + urllib.parse.urlencode({"since": since}) if since else ""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/printed" + q,
                                    timeout=timeout) as res:
            return json.loads(res.read() or b"{}").get("printed", [])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrinterUnavailable(f"could not reach printd at {url}: {exc}")


def selftest(cfg):
    """Print the tiny text-only test label, wherever the printer is.

    Dispatches on the backend like send() does. It used to call
    tspl_selftest directly, which meant that with printer_backend set to
    pi-http the one command for "is the printer alive" reached past the
    service to whatever device node happened to exist on *this* host -
    testing the wrong printer, or nothing at all."""
    backend = cfg.get("printer_backend")
    if backend in REMOTE_BACKENDS:
        import urllib.error
        import urllib.request

        url = (cfg.get("printd_url") or "").rstrip("/")
        if not url:
            raise PrinterUnavailable("printd_url is not set")
        job = f"selftest-{os.urandom(8).hex()}"
        req = urllib.request.Request(
            url + "/selftest", data=b"", method="POST",
            headers={"X-MPLabel-Protocol": "1", "X-MPLabel-Job": job,
                     "X-MPLabel-Sig": _sign_job(cfg.get("printd_secret"),
                                                job, b""),
                     "X-MPLabel-Deadline": str(cfg.get("printd_timeout", 45))})
        try:
            with urllib.request.urlopen(
                    req, timeout=float(cfg.get("printd_timeout", 45))) as res:
                json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise PrinterUnavailable(
                f"printd said {exc.code}: "
                f"{exc.read().decode(errors='replace')}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PrinterUnavailable(f"could not reach printd at {url}: {exc}")
        return {"backend": backend, "where": url}

    if backend == "escpos":
        escpos_selftest(cfg["printer_device"])
    else:
        tspl_selftest(cfg["printer_device"], cfg.get("media_tracking", "gap"),
                      float(cfg.get("gap_inches", 0.12)))
    return {"backend": backend, "where": cfg.get("printer_device")}



LANGUAGE_BACKENDS = {
    "TSPL": "tspl",
    "ZPL": "zpl",
    "ESC/POS": "escpos",
}

BACKENDS = {
    "cups-pdf": print_cups_pdf,
    "cups-raster": print_cups_raster,
    "zpl": print_zpl,
    "tspl": print_tspl,
    "escpos": print_escpos,
    "pi-http": print_pi_http,
}


def send(pdf_path, backend="cups-pdf", **kwargs):
    try:
        fn = BACKENDS[backend]
    except KeyError:
        raise SystemExit(f"Unknown backend {backend!r}. "
                         f"Choose from: {', '.join(BACKENDS)}")
    # Return what the backend returned. `pi-http` answers a 409 with
    # {"duplicate": True} rather than raising, and discarding that meant
    # `print_label` came back clean and the caller marked the sale
    # printed - a duplicate recorded as a successful print, which is the
    # one thing the journal exists to make legible.
    return fn(pdf_path, **kwargs)


# -------------------------------------------------------------------- probe

def probe(cups=False):
    """Show what is plugged in and which backend is likely to work."""
    print("=== USB devices ===")
    if shutil.which("lsusb"):
        subprocess.run(["lsusb"])
    else:
        print("lsusb not installed (sudo apt install usbutils)")

    print("\n=== raw USB printer nodes ===")
    nodes = sorted(Path("/dev/usb").glob("lp*")) if Path("/dev/usb").exists() else []
    print("\n".join(str(n) for n in nodes) if nodes
          else "none - usblp may be unloaded, or CUPS has claimed the device")

    print("\n=== CUPS queues ===")
    if shutil.which("lpstat"):
        subprocess.run(["lpstat", "-p", "-d"])
    else:
        print("CUPS not installed (sudo apt install cups)")

    print("\n=== printer self-description (IEEE-1284) ===")
    found = False
    for node in nodes:
        lang, ident = detect_language(str(node))
        found = True
        print(f"{node}: {ident or 'no ieee1284_id exposed'}")
        if lang:
            backend = LANGUAGE_BACKENDS.get(lang)
            if backend:
                print(f"  -> speaks {lang}; set printer_backend = {backend}")
                # The G4 advertises ESC/POS and actually speaks TSPL. These
                # descriptors are often boilerplate the OEM never edited,
                # so say out loud that this is a hint, not a finding.
                print("     (the id can be boilerplate - if that backend "
                      "prints nothing, try tspl)")
            else:
                # Do not name a backend that does not exist: following that
                # advice used to exit with "Unknown backend 'esc/pos'".
                print(f"  -> speaks {lang}, which has no raw backend here; "
                      f"install a CUPS driver and use printer_backend "
                      f"= cups-pdf")
        elif ident:
            print("  -> no known language in the id string; try escpos, "
                  "then tspl")
    if not found:
        print("no raw nodes to query")

    # Behind a flag on purpose. `lpinfo -v` runs CUPS's libusb backend in
    # discovery mode, and that can claim the printer and unbind usblp -
    # which is the very failure this command exists to diagnose. A probe
    # must not be able to break the thing it is probing.
    if cups:
        print("\n=== CUPS-detected devices ===")
        if shutil.which("lpinfo"):
            subprocess.run(["sudo", "lpinfo", "-v"], timeout=CUPS_TIMEOUT)
    else:
        print("\n(skipping `lpinfo -v`: it can claim the printer and unbind "
              "usblp. Pass --cups if you want it.)")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif len(sys.argv) > 1:
        # printers.py label.pdf [backend]
        send(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "cups-pdf")
    else:
        print(__doc__)
