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

import os
import shutil
import subprocess
import time
import sys
from pathlib import Path

DEFAULT_DPI = 203          # 203 dpi is standard; some printers are 300
LABEL_W_IN = 4.0
LABEL_H_IN = 6.0


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


def render_bitmap(pdf_path, dpi=DEFAULT_DPI, invert=False):
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
    subprocess.run(cmd, check=True)


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
    subprocess.run(cmd, check=True)


def print_zpl(pdf_path, device="/dev/usb/lp0", dpi=DEFAULT_DPI, darkness=None):
    data, width_px, width_bytes, height = render_bitmap(pdf_path, dpi, invert=False)
    total = width_bytes * height

    head = ["^XA", f"^PW{width_px}", f"^LL{height}", "^LH0,0"]
    if darkness is not None:
        head.append(f"~SD{int(darkness):02d}")   # 0-30
    zpl = ("\n".join(head)
           + f"\n^FO0,0^GFA,{total},{total},{width_bytes},"
           + data.hex().upper()
           + "^FS\n^XZ\n")
    _write_raw(device, zpl.encode("ascii"))


def build_tspl(pdf_path, dpi=DEFAULT_DPI, darkness=None, speed=None,
               media="gap", gap_in=0.12, copies=1):
    """Assemble a complete TSPL job as bytes.

    media: 'gap' for die-cut labels (the normal case for 4x6 shipping
    stock), 'blackmark' for black-mark stock, 'continuous' for gapless.

    Getting this wrong is the classic failure. GAP 0,0 means continuous:
    on die-cut labels the printer never finds the label edge, so prints
    drift down the roll a little further each time. Conversely a gap
    command on continuous stock makes the printer hunt for a gap it will
    never find and throw a media error."""
    # TSPL bitmaps are inverted relative to ZPL: a clear bit prints.
    data, _width_px, width_bytes, height = render_bitmap(pdf_path, dpi,
                                                         invert=True)
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
               settle=2.0):
    payload = build_tspl(pdf_path, dpi, darkness, speed, media, gap_in)
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


def _write_raw(device, payload, settle=2.0):
    """Write the whole job in one burst, then pause.

    These budget TSPL firmwares silently discard bytes that arrive while
    the head is already moving. A driver that streams at render speed
    loses everything after the first label. So: build the job fully in
    memory, hand it over in a single unbuffered write, and give the
    printer a moment before the next one."""
    dev = Path(device)
    if not dev.exists():
        raise SystemExit(
            f"{device} not found. Check `ls /dev/usb/` and that the usblp "
            f"module is loaded (`lsmod | grep usblp`). If CUPS has claimed "
            f"the printer it will have unbound usblp - you cannot use a "
            f"CUPS queue and the raw device for the same printer at once.")

    fd = os.open(dev, os.O_WRONLY)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
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


BACKENDS = {
    "cups-pdf": print_cups_pdf,
    "cups-raster": print_cups_raster,
    "zpl": print_zpl,
    "tspl": print_tspl,
}


def send(pdf_path, backend="cups-pdf", **kwargs):
    try:
        fn = BACKENDS[backend]
    except KeyError:
        raise SystemExit(f"Unknown backend {backend!r}. "
                         f"Choose from: {', '.join(BACKENDS)}")
    fn(pdf_path, **kwargs)


# -------------------------------------------------------------------- probe

def probe():
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
            print(f"  -> speaks {lang}; set printer_backend = {lang.lower()}")
        elif ident:
            print("  -> no known language in the id string; try tspl anyway")
    if not found:
        print("no raw nodes to query")

    print("\n=== CUPS-detected devices ===")
    if shutil.which("lpinfo"):
        subprocess.run(["sudo", "lpinfo", "-v"])


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif len(sys.argv) > 1:
        # printers.py label.pdf [backend]
        send(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "cups-pdf")
    else:
        print(__doc__)
