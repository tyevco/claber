"""supvan.py - USB HID transport and status polling for the SUPVAN /
KATA Symbol T50M Pro, the 48mm label maker used for inventory tags.

Written entirely from `docs/supvan-t50m-protocol.md`, which describes the
device's observable behaviour. Nothing here is transcribed from the
vendor's application, and nothing here should ever be: their code is
theirs. Where the document is silent this module says so in a comment
rather than guessing.

**Nothing in this module moves paper.** The transport, the command frame
and the status decode are implemented; the bitmap path is not, because
the document lists the row format, the bit polarity, the dot width and
the label-authentication exchange as undetermined. `print_bitmap` exists
only to raise and name those gaps.

This is not, and cannot be, a shipping-label printer: 48mm is ~384 dots
at 203dpi against the 812 the 4x6 pipeline emits. Shipping labels stay
on the CLABEL G4 at /dev/usb/lp0 - do not confuse the two devices.

Nothing here is platform-specific at import time. The device only exists
on Linux, but the frame builders and the status decoder are pure byte
arithmetic and are tested off-target.
"""

import os

from . import lzma1

try:
    import select
except ImportError:                    # pragma: no cover - select is stdlib
    select = None

DEFAULT_DEVICE = "/dev/hidraw0"

# The report descriptor declares 64-byte input and output reports with no
# Report ID. On Linux hidraw a device without numbered reports still needs
# a leading 0x00 on every *write* - the kernel strips it and sends the
# remaining 64 bytes - so a write is 65 bytes, not 64. Omitting it is the
# most likely first mistake and the device does not complain: it simply
# ignores the write.
REPORT_SIZE = 64
REPORT_ID = 0x00
WRITE_SIZE = REPORT_SIZE + 1

# Reads are the other way round: hidraw prepends the report id only for
# devices that use numbered reports, and this one does not, so an input
# report arrives as a bare 64 bytes. The document states the write rule
# explicitly and is silent on the read; this follows from "no Report ID".
READ_SIZE = REPORT_SIZE

# USB ids, for error messages and for matching against the udev rule.
USB_VENDOR = 0x1820
USB_PRODUCT = 0x207F

DEFAULT_TIMEOUT = 2.0


class SupvanError(Exception):
    """Anything that stops us talking to the label maker."""


# ------------------------------------------------------------- opcodes

# Named from the document's table. The value goes in byte 4 of the frame.
OP_BUFFER_FULL = 0x10           # ends a bitmap transfer; ten-byte form
OP_INQUIRY_STATUS = 0x11        # poll; reply is a status report
OP_CHECK_DEVICE = 0x12          # can the device print? once per job
OP_START_PRINT = 0x13           # sent with wValue 1
OP_STOP_PRINT = 0x14
OP_READ_REVISION = 0x17
OP_RETURN_MEDIA_INFO = 0x30
OP_NEXT_FRAME_IS_BULK = 0x5C    # announces an LZMA-compressed bitmap
OP_SET_RFID_DATA = 0x5D         # label authentication
OP_READ_FIRMWARE_REVISION = 0xC5
OP_NEXT_FRAME_IS_FIRMWARE = 0xC6

OPCODE_NAMES = {
    OP_BUFFER_FULL: "buffer full",
    OP_INQUIRY_STATUS: "inquiry status",
    OP_CHECK_DEVICE: "check device",
    OP_START_PRINT: "start print",
    OP_STOP_PRINT: "stop print",
    OP_READ_REVISION: "read revision",
    OP_RETURN_MEDIA_INFO: "return media info",
    OP_NEXT_FRAME_IS_BULK: "next frame is bulk",
    OP_SET_RFID_DATA: "set RFID data",
    OP_READ_FIRMWARE_REVISION: "read firmware revision",
    OP_NEXT_FRAME_IS_FIRMWARE: "next frame is firmware",
}

# The firmware-update path. The document says do not send it, so this
# module will not build it: a mistyped constant that bricks the device is
# not a bug you get to fix twice.
FORBIDDEN_OPCODES = {OP_NEXT_FRAME_IS_FIRMWARE}

# Opcodes that ask the device a question and do not move paper, in the
# order a deep probe sends them. This list is a safety boundary, not a
# convenience: everything omitted is omitted on purpose.
#
#   0x13 start print, 0x10 buffer full, 0x5c bulk, 0x14 stop  - all part
#        of putting ink on a label, and 0x13 in particular leaves the
#        device waiting for data that a probe will never send.
#   0x5d RFID  - writes label authentication data. Not a question.
#   0xc6       - the firmware path; build_command refuses it outright.
#
# Adding to this list means asserting the device will not print, feed or
# have anything written to it. A test pins the exclusions.
SAFE_PROBE_OPCODES = (
    (OP_INQUIRY_STATUS, "inquiry status"),
    (OP_CHECK_DEVICE, "check device"),
    (OP_READ_REVISION, "read revision"),
    (OP_READ_FIRMWARE_REVISION, "read firmware revision"),
    (OP_RETURN_MEDIA_INFO, "media info"),
)


# -------------------------------------------------------- command frames

def build_command(opcode, value=0, value2=None):
    """Build the 8-byte command frame, or the 10-byte two-value variant.

    The layout is a USB vendor control-request setup packet carried inside
    a HID report - bmRequestType 0xC0, bRequest 0x40, wValue, wIndex,
    wLength 8 - presumably so Windows binds its built-in HID driver and
    the vendor ships none.

    Note that wValue goes out **high byte first** (offset 2 is
    `wValue >> 8`), which is the opposite order to the page counter in the
    status report. That reads like a mistake and is not: it is what the
    document records for each, and they are produced by different firmware
    paths. Do not "fix" one to match the other.

    Returns the bare frame, unpadded. Padding it out to the 64-byte report
    is the transport's job - see `pad_report`."""
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode {opcode!r} is not a byte")
    if opcode in FORBIDDEN_OPCODES:
        raise ValueError(
            f"opcode 0x{opcode:02x} ({OPCODE_NAMES.get(opcode, '?')}) is the "
            f"firmware update path and must not be sent")
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"wValue {value!r} does not fit in 16 bits")

    frame = bytearray(8)
    frame[0] = 0xC0                     # bmRequestType
    frame[1] = 0x40                     # bRequest
    frame[2] = (value >> 8) & 0xFF      # wValue, high byte first
    frame[3] = value & 0xFF
    frame[4] = opcode                   # wIndex, low byte
    frame[5] = 0x00
    frame[6] = 0x08                     # wLength = 8
    frame[7] = 0x00

    if value2 is not None:
        if not 0 <= value2 <= 0xFFFF:
            raise ValueError(f"wValue2 {value2!r} does not fit in 16 bits")
        # The ten-byte variant appends a second 16-bit value in the same
        # high-byte-first order. Bytes 0-7 are unchanged - in particular
        # wLength stays 8, which is what the document shows.
        frame.append((value2 >> 8) & 0xFF)
        frame.append(value2 & 0xFF)

    return bytes(frame)


def pad_report(payload):
    """Zero-pad a payload of at most 64 bytes to one whole report."""
    if len(payload) > REPORT_SIZE:
        raise ValueError(f"{len(payload)} bytes is more than one report")
    return bytes(payload) + b"\x00" * (REPORT_SIZE - len(payload))


def split_reports(payload):
    """Split a payload into whole 64-byte reports, zero-padding the last.

    Everything - commands and bitmap data alike - goes out as a sequence
    of these. An empty payload becomes one empty report rather than
    nothing, so a caller cannot send zero bytes and believe it sent
    something."""
    payload = bytes(payload)
    if not payload:
        return [pad_report(b"")]
    return [pad_report(payload[i:i + REPORT_SIZE])
            for i in range(0, len(payload), REPORT_SIZE)]


def wire_bytes(payload):
    """The exact bytes hidraw wants: each report behind its 0x00 id."""
    return b"".join(bytes([REPORT_ID]) + r for r in split_reports(payload))


# --------------------------------------------------------- status decode

# (name, byte offset, mask), from the document's table. Only the first six
# bytes of the 64-byte input report are meaningful; the rest is padding of
# unknown content and is deliberately not interpreted.
STATUS_FLAGS = (
    ("buffer_full",           0, 0x01),
    ("media_error",           0, 0x02),   # media read/write error
    ("out_of_media",          0, 0x04),
    ("media_not_recognised",  0, 0x08),
    ("media_seating_error",   0, 0x10),
    ("check_remaining_media", 0, 0x20),
    ("battery_low",           0, 0x40),
    ("busy",                  1, 0x04),
    ("head_too_hot",          1, 0x08),
    ("cover_open",            2, 0x08),   # media cover open
    ("usb_connected",         2, 0x10),
    ("printing",              2, 0x40),   # printing in progress
    ("busy_secondary",        2, 0x80),
    ("media_not_installed",   3, 0x01),
    ("charging",              3, 0x80),
)

# Human wording for a probe's output.
FLAG_LABELS = {
    "buffer_full": "buffer full",
    "media_error": "media read/write error",
    "out_of_media": "out of media",
    "media_not_recognised": "media not recognised",
    "media_seating_error": "media seating error",
    "check_remaining_media": "check remaining media",
    "battery_low": "battery low",
    "busy": "device busy",
    "head_too_hot": "print head too hot",
    "cover_open": "media cover open",
    "usb_connected": "USB connected",
    "printing": "printing in progress",
    "busy_secondary": "device busy (secondary)",
    "media_not_installed": "media not installed",
    "charging": "charging",
}

# Which flags mean "do not start a job". The document says to abort on
# "any error condition" without saying which flags those are, so this
# split is ours, not the document's: these are the states where paper
# plainly cannot come out right. battery_low and check_remaining_media are
# warnings and stay out of it, as does busy, which is a wait rather than a
# failure.
ERROR_FLAGS = (
    "media_error",
    "out_of_media",
    "media_not_recognised",
    "media_seating_error",
    "cover_open",
    "head_too_hot",
    "media_not_installed",
)

# The page counter lives in bytes 4 and 5.
PAGES_LOW, PAGES_HIGH = 4, 5

# Every reply begins with a length byte, then that many bytes of payload.
# Confirmed across three commands that answer with different lengths -
# status and firmware revision give 8, read-revision gives 4, media info
# gives 59 - so it is a length and not a constant marker.
#
# This cost a false alarm worth remembering. Decoding the status flags
# from offset 0 made an idle, healthy printer report "media not
# recognised" while claiming USB was disconnected on a device that was
# plainly answering over USB. Settled by opening the media cover and
# polling: the byte that changed was the one this offset predicts, not
# the one the naive reading did. All flag offsets below are relative to
# the payload, not to the report.
STATUS_PREFIX_LEN = 1
STATUS_MIN_LEN = STATUS_PREFIX_LEN + 6


def decode_status(report):
    """Decode a status report into flags plus the page counter.

    Takes the input report as bytes and returns a dict carrying every
    named flag as True or False, plus:

      pages_printed  16-bit count from flag bytes 4 and 5
      errors         the raised flags that mean a job must not start
      prefix         the leading byte the device sends before the flags
      raw            the meaningful bytes as received, for logging

    The report is taken as it comes off the wire, including the device's
    leading byte - see STATUS_PREFIX_LEN, which is why every offset here
    is shifted by one before use.

    A short buffer raises rather than being padded out: a truncated read
    is a transport problem, and quietly decoding it would report a healthy
    device with an empty counter."""
    data = bytes(report)
    if len(data) < STATUS_MIN_LEN:
        raise SupvanError(
            f"status report is {len(data)} bytes; the first "
            f"{STATUS_MIN_LEN} carry the leading byte, the flags and the "
            f"page counter")

    flags = reply_payload(data)
    if len(flags) < 6:
        raise SupvanError(
            f"status payload is {len(flags)} bytes; the flags and page "
            f"counter need 6")
    status = {name: bool(flags[off] & mask)
              for name, off, mask in STATUS_FLAGS}
    # Little-endian: byte 5 is the high byte. Reading it the other way
    # round turns 1 page into 256, which looks plausible for a while.
    status["pages_printed"] = flags[PAGES_LOW] | (flags[PAGES_HIGH] << 8)
    status["errors"] = [n for n in ERROR_FLAGS if status[n]]
    status["prefix"] = data[0]
    status["raw"] = data[:STATUS_MIN_LEN]
    return status


def format_status(status):
    """The status as a few plain lines, for `mplabel supvan-probe`."""
    lines = [f"pages printed: {status['pages_printed']}"]
    raised = [FLAG_LABELS[n] for n, _off, _mask in STATUS_FLAGS if status[n]]
    lines.append("flags set: " + (", ".join(raised) if raised else "none"))
    if status["errors"]:
        lines.append("errors: "
                     + ", ".join(FLAG_LABELS[n] for n in status["errors"]))
    lines.append("raw: " + status["raw"].hex(" "))
    return "\n".join(lines)


# ------------------------------------------------------------- transport

class SupvanDevice:
    """A raw HID pipe to the label maker.

        with SupvanDevice() as dev:
            print(dev.status())

    The path is a parameter throughout rather than a constant baked into
    each call, because /dev/hidraw0 is only the usual number: plug in
    another HID device first and this one becomes hidraw1."""

    def __init__(self, path=DEFAULT_DEVICE, timeout=DEFAULT_TIMEOUT):
        self.path = str(path)
        self.timeout = timeout
        self._fd = None

    # -- open / close

    def open(self):
        if self._fd is not None:
            return self
        # O_BINARY is a no-op everywhere but Windows, where its absence
        # would translate newlines inside a binary report. It only bites
        # when a file stands in for the device, but a transport that
        # corrupts its own test fixture is not worth debugging twice.
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        try:
            self._fd = os.open(self.path, flags)
        except FileNotFoundError:
            raise SupvanError(
                f"{self.path} not found. The T50M Pro is a HID device, not a "
                f"USB printer, so it has no /dev/usb/lpN - look for it with "
                f"`lsusb | grep {USB_VENDOR:04x}:{USB_PRODUCT:04x}` and "
                f"`ls /dev/hidraw*`. Note /dev/usb/lp0 is the G4, not this.")
        except PermissionError:
            raise SupvanError(
                f"{self.path} is not writable. The node is root-only by "
                f"default; install udev/99-supvan-t50m.rules and check the "
                f"user is in the lp group.")
        except OSError as exc:
            raise SupvanError(f"cannot open {self.path}: {exc}")
        return self

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    def _require_fd(self):
        if self._fd is None:
            raise SupvanError("device is not open")
        return self._fd

    # -- reports

    def write(self, payload):
        """Send a payload as one or more 64-byte output reports.

        Returns the number of reports written. Each report goes out in a
        single os.write of 65 bytes: unlike a byte stream a report cannot
        be resumed halfway, because the remainder would be framed as a
        fresh report and the device would act on nonsense. So a short
        write is fatal here rather than something to loop over. (The
        document settles the report size and the leading id byte; treating
        a partial write as fatal is our inference from those.)"""
        fd = self._require_fd()
        reports = split_reports(payload)
        for report in reports:
            buf = bytes([REPORT_ID]) + report
            written = os.write(fd, buf)
            if written != len(buf):
                raise SupvanError(
                    f"short write to {self.path}: {written} of {len(buf)} "
                    f"bytes; a partial HID report cannot be resumed")
        return len(reports)

    def read_report(self, timeout=None):
        """Read one input report and return the bytes the device sent.

        The document gives no timeout, so this one is a guard against
        hanging rather than a protocol value."""
        fd = self._require_fd()
        wait = self.timeout if timeout is None else timeout
        # select.poll is Linux and friends; on Windows select handles only
        # sockets, and there is no device there to read from anyway.
        # Falling back to a blocking read keeps this module importable and
        # testable off-target, where the "device" is a file and always
        # ready.
        if wait and select is not None and hasattr(select, "poll"):
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            if not poller.poll(wait * 1000):
                raise SupvanError(
                    f"no reply from {self.path} within {wait:g}s. A listening "
                    f"device answers a status poll immediately; silence "
                    f"usually means the report id byte or the report size is "
                    f"wrong, and the device is ignoring the write.")
        data = os.read(fd, READ_SIZE)
        if not data:
            raise SupvanError(f"empty read from {self.path}")
        return data

    # -- commands

    def command(self, opcode, value=0, value2=None):
        """Send one command frame, padded to a report. Reads nothing."""
        return self.write(build_command(opcode, value, value2))

    def status(self, timeout=None):
        """Poll the device and decode the reply.

        The safe entry point: it moves no paper, and one round trip proves
        the whole transport - report size, the leading id byte,
        permissions - at once."""
        self.command(OP_INQUIRY_STATUS)
        return decode_status(self.read_report(timeout))


def poll_status(path=DEFAULT_DEVICE, timeout=DEFAULT_TIMEOUT):
    """Open, poll once, close. Raises SupvanError with a plain reason."""
    with SupvanDevice(path, timeout) as dev:
        return dev.status()


def reply_payload(report):
    """Strip the leading length byte and return that many bytes.

    A reply shorter than its own length byte claims is a truncated read,
    not something to decode around."""
    data = bytes(report)
    if not data:
        raise SupvanError("empty reply")
    length = data[0]
    if length > len(data) - 1:
        raise SupvanError(
            f"reply says {length} bytes follow but only {len(data) - 1} "
            f"arrived")
    return data[1:1 + length]


def decode_revision(report):
    """The revision string from a read-revision reply.

    The device answers with a length byte and an ASCII string - observed
    as 4 bytes holding "2.4" and a NUL. Trailing NULs and padding are
    dropped; anything unprintable means this is not a revision reply and
    is worth seeing rather than silently cleaning up."""
    payload = reply_payload(report).split(b"\x00", 1)[0]
    text = payload.decode("ascii", "replace").strip()
    if not text:
        raise SupvanError("revision reply carried no text")
    return text


def probe_deep(path=DEFAULT_DEVICE, timeout=DEFAULT_TIMEOUT):
    """Ask the device every question that does not move paper.

    One status poll proved the transport, but only one opcode and one
    reply shape. This walks SAFE_PROBE_OPCODES and returns
    [(name, opcode, reply_or_None, error_or_None)] so the reply framing
    can be compared across commands - which matters, because the status
    reply turned out to carry a leading byte that the analysis missed, and
    whether the others do the same is unknown.

    Replies to the revision and media commands are returned raw. Their
    formats were never determined, so nothing here pretends to decode
    them; a timeout on one is a finding, not a failure."""
    results = []
    with SupvanDevice(path, timeout) as dev:
        for opcode, name in SAFE_PROBE_OPCODES:
            try:
                dev.command(opcode)
                results.append((name, opcode, dev.read_report(), None))
            except SupvanError as exc:
                # Keep going. A device that will not answer one question
                # may answer the next, and the pattern of which fail is
                # more informative than stopping at the first.
                results.append((name, opcode, None, str(exc)))
    return results


# ----------------------------------------------------- the print buffer
#
# What goes inside the compressed stream, which is what every refused
# label was missing. The device does not take a bare raster: it takes a
# sequence of fixed 4096-byte *print buffers*, each carrying a 14-byte
# header and a checksum, and the whole sequence is compressed as one
# LZMA stream.
#
# This is why the only bitmap that ever printed here was the vendor's
# own. Their capture decompresses to 12288 bytes, which this repo read as
# "48 bytes per row x 256 rows" - a plausible raster, and wrong. 12288 is
# 3 x 4096: three print buffers, headers and checksums included. Replaying
# it printed because it was already three valid buffers; re-encoding it
# printed because the encoder round-trips them unchanged. Everything this
# repo *drew* was a bare raster of some other length, so the firmware
# rejected it - and `media_seating_error` is its only word for "no".
#
# The layout below is transcribed from two independent implementations of
# the vendor's `T50PlusPrint.initLZMAData`, which agree byte for byte:
# heeen/supvan-cups (MIT), both its Rust `supvan-proto` crate and the
# `test_print.py` reference it ships. Neither is a guess about this
# device; both were read off the Android app and one of them drives a
# T50M Pro whose serial prefix (T0117A) is the same as ours.

PRINT_BUF_SIZE = 4096
PRINT_BUF_HEADER = 14

# Image bytes a single buffer will carry. Not 4096-14=4082: the vendor's
# own constant is 4074, and the eight bytes it leaves spare are theirs to
# explain, not ours to reclaim.
MAX_BUF_DATA = 4074

# The firmware re-reads its running checksum every 256 bytes, so the byte
# just before each boundary is folded in a second time. Omitting those
# gives a checksum that looks right and is not.
CHECKSUM_STRIDE = 256

MAX_DENSITY = 15
MARGIN_MAX_DOTS = 900
DEFAULT_MARGIN_DOTS = 8

# Burn energy, 0-15. The vendor's default is 4 for both trims; black
# rides in the header's PAGE_REG_BITS and red in byte 12, and the print
# dialog exposes them separately, so they are two values and not one.
DEFAULT_DENSITY = 4


def build_page_reg_bits(page_st=False, page_end=False, prt_end=False,
                        cut=0, savepaper=False, first_cut=0, nodu=0, mat=1):
    """The two PAGE_REG_BITS bytes at offset 2 of a print buffer.

    `nodu` is the black density; `mat` is the material type, which the
    vendor sends as 1 throughout. The page flags mark where a buffer sits
    in the job: the first buffer of a page carries `page_st`, the last
    carries `page_end`, and the last buffer of the whole job also carries
    `prt_end`."""
    b0 = 0
    if page_st:
        b0 |= 0x02
    if page_end:
        b0 |= 0x04
    if prt_end:
        b0 |= 0x08
    b0 &= 0x0F
    b0 |= (cut & 0x07) << 4
    if savepaper:
        b0 |= 0x80

    b1 = (first_cut & 0x03) | ((nodu & 0x0F) << 2) | ((mat & 0x03) << 6)
    return bytes([b0, b1])


def build_print_buffer(image_data, per_line_byte, cols_in_buf,
                       page_st=False, page_end=False, prt_end=False,
                       margin_top=DEFAULT_MARGIN_DOTS,
                       margin_bottom=DEFAULT_MARGIN_DOTS,
                       density=DEFAULT_DENSITY, red_density=None):
    """One 4096-byte print buffer, header and checksum included.

        [0:2]   checksum, little-endian
        [2:4]   PAGE_REG_BITS (black density lives in here)
        [4:6]   column count, little-endian
        [6]     bytes per printhead line
        [7]     reserved, 0
        [8:10]  margin top, little-endian dots
        [10:12] margin bottom
        [12]    red density
        [13]    0
        [14:]   image data, column-major, each line's bytes last-first

    A "column" is one printhead line - one firing of the 384-dot bar -
    so the column count runs along the feed direction and `per_line_byte`
    runs across the head. That is the opposite of how a raster is usually
    described, and getting it the usual way round is what made the
    captured image look like 256 rows of 48 bytes."""
    if red_density is None:
        red_density = density
    buf = bytearray(PRINT_BUF_SIZE)

    buf[2:4] = build_page_reg_bits(page_st=page_st, page_end=page_end,
                                   prt_end=prt_end, nodu=density, mat=1)
    buf[4:6] = int(cols_in_buf).to_bytes(2, "little")
    buf[6] = per_line_byte & 0xFF

    mt = max(1, min(int(margin_top), MARGIN_MAX_DOTS))
    mb = max(1, min(int(margin_bottom), MARGIN_MAX_DOTS))
    buf[8:10] = mt.to_bytes(2, "little")
    buf[10:12] = mb.to_bytes(2, "little")
    buf[12] = min(red_density, MAX_DENSITY)
    buf[13] = 0

    data_len = min(len(image_data), PRINT_BUF_SIZE - PRINT_BUF_HEADER)
    buf[PRINT_BUF_HEADER:PRINT_BUF_HEADER + data_len] = image_data[:data_len]

    # Checksum over the header, plus the byte before every 256-byte
    # boundary within the declared extent of the image data.
    data_end = cols_in_buf * per_line_byte + PRINT_BUF_HEADER
    chk = sum(buf[2:PRINT_BUF_HEADER])
    for i in range(1, data_end // CHECKSUM_STRIDE + 1):
        idx = i * CHECKSUM_STRIDE - 1
        if idx < len(buf):
            chk += buf[idx]
    buf[0:2] = (chk & 0xFFFF).to_bytes(2, "little")
    return bytes(buf)


def raster_to_column_major(data, per_line_byte):
    """Repack a standard MSB-first raster the way the printhead reads it.

    The device reads a printhead line's **bytes in reverse order**, with
    the bits inside each byte left alone. So this reverses each line's
    bytes and nothing else. No transpose is involved: a raster row and a
    printhead line are already the same run of bytes. Named after the
    vendor's term for the layout rather than after the mechanic, because
    that is what a reader will be looking for.

    This started life as a *bit* reversal within each byte, on the
    reading that the leftmost dot goes in the least significant bit, and
    the first label printed came out cleanly mirrored left to right. That
    observation settles it, because the two possibilities compose:
    writing `T` for the per-byte bit reversal and `R` for the per-line
    byte reversal, a full 384-bit line reversal is `M = R.T`. The mirror
    means the device painted `M(row)` when handed `T(row)`, so its own
    reading is `P(x) = M(T(x)) = R(x)` - and to have `P(E(row)) = row`,
    `E` must be `R`.

    If a print from here comes out *scrambled in 8-dot blocks* rather
    than correct, the bit order is wrong as well and the answer is the
    full `M` - reverse the bytes and the bits. That is the only other
    possibility, and it is one line.

    Reversing a line's bytes is its own inverse, so `decode_job` calls
    this too. That is also why a preview could never have caught the
    mirror: it applies the exact inverse of whatever this does, so it
    renders correctly whether or not this is right. Checking orientation
    needs an assertion on the *absolute* bit position, or paper."""
    if per_line_byte <= 0:
        raise ValueError("per_line_byte must be positive")
    if len(data) % per_line_byte:
        raise ValueError(
            f"{len(data)} bytes is not a whole number of "
            f"{per_line_byte}-byte printhead lines")
    out = bytearray(len(data))
    for start in range(0, len(data), per_line_byte):
        out[start:start + per_line_byte] = data[
            start + per_line_byte - 1:start - 1 if start else None:-1]
    return bytes(out)


def split_into_buffers(image_data, per_line_byte, total_cols,
                       margin_top=DEFAULT_MARGIN_DOTS,
                       margin_bottom=DEFAULT_MARGIN_DOTS,
                       density=DEFAULT_DENSITY, red_density=None):
    """Tile a column-major image into print buffers along the feed axis.

    The margins are declared in the header and their columns are *not*
    sent - the firmware feeds blank for them - so the image data walks
    from `margin_top` and stops `margin_bottom` short of the end.

    This is the split the device actually imposes, and it is on the
    uncompressed side: 4074 image bytes per buffer, so 84 printhead lines
    at 48 bytes each. `split_bitmap` below splits on *compressed* size
    instead, chasing a limit that never existed."""
    if per_line_byte <= 0:
        raise ValueError("per_line_byte must be positive")
    max_cols = MAX_BUF_DATA // per_line_byte
    if max_cols <= 0:
        raise ValueError("a single printhead line does not fit in a buffer")

    cols = total_cols - margin_top - margin_bottom
    if cols <= 0:
        raise ValueError("margins leave no columns to print")

    chunks = []
    start = 0
    while start < cols:
        chunks.append((start, min(max_cols, cols - start)))
        start += chunks[-1][1]

    last = len(chunks) - 1
    buffers = []
    for i, (start_col, cols_in_buf) in enumerate(chunks):
        off = (margin_top + start_col) * per_line_byte
        chunk = image_data[off:off + cols_in_buf * per_line_byte]
        buffers.append(build_print_buffer(
            chunk, per_line_byte, cols_in_buf,
            page_st=(i == 0), page_end=(i == last), prt_end=(i == last),
            margin_top=margin_top, margin_bottom=margin_bottom,
            density=density, red_density=red_density))
    return buffers


# Print speed as a function of how well the image compressed, from the
# vendor's `T50PlusPrint.multiCompression`. Denser data prints slower so
# the head has time to heat, and the argument is the *average* compressed
# bytes per buffer rather than the total.
#
# This settles the one number in the captured print nobody here could
# explain: its BUF_FULL frame carried a second value of 60, which this
# code had been sending as a constant. 123 compressed bytes over three
# buffers averages 41, and 41 falls in the bottom band - so 60 was not a
# constant at all, it was this function's answer for a nearly blank
# label. A real label compresses larger and must be printed slower.
SPEED_BANDS = ((3000, 10), (2800, 15), (2500, 20), (2000, 25),
               (1500, 40), (1000, 45), (500, 55))


def calc_speed(avg_compressed_per_buffer):
    """The speed value for BUF_FULL, from the average compressed size."""
    for threshold, speed in SPEED_BANDS:
        if avg_compressed_per_buffer > threshold:
            return speed
    return 60


def build_job(raster, per_line_byte, total_cols,
              margin_top=DEFAULT_MARGIN_DOTS,
              margin_bottom=DEFAULT_MARGIN_DOTS,
              density=DEFAULT_DENSITY, red_density=None, dict_size=None):
    """Turn a standard MSB-first raster into a job the device will take.

    Returns a dict carrying what `experimental_print` sends: the single
    LZMA stream over every print buffer, the uncompressed length, the
    derived speed, and the buffer count for reporting.

    One stream over all the buffers, not one per buffer: the firmware
    reads a buffer header at each 4096-byte boundary of the *decompressed*
    data, so the buffers are concatenated first and compressed once. The
    captured print is exactly this - 123 compressed bytes covering three
    buffers - which is also why its declared uncompressed size is 12288
    and not the size of any one buffer."""
    kw = {} if dict_size is None else {"dict_size": dict_size}
    buffers = split_into_buffers(
        raster_to_column_major(raster, per_line_byte), per_line_byte, total_cols,
        margin_top=margin_top, margin_bottom=margin_bottom,
        density=density, red_density=red_density)
    blob = b"".join(buffers)
    compressed = compress_bitmap(blob, **kw)
    return {
        "compressed": compressed,
        "raw_len": len(blob),
        "speed": calc_speed(len(compressed) // len(buffers)),
        "buffers": len(buffers),
    }


def decode_job(compressed):
    """Take a job apart again: the picture the device would actually burn.

    The inverse of `build_job`, and deliberately written against the
    *payload* rather than the source raster - it decompresses, splits on
    the 4096-byte boundary, re-checks every checksum and reads the
    geometry out of each header. So what it returns is what the firmware
    would see, not what we meant to send, which is the only version worth
    looking at before spending a label.

    Returns (raster, stride, columns) with the bits back in the
    MSB-first order the rest of this codebase uses.

    Raises SupvanError if a buffer is malformed, because a preview that
    quietly renders a corrupt job is worse than no preview."""
    import lzma
    try:
        blob = lzma.decompress(compressed, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError as exc:
        raise SupvanError(f"job does not decompress: {exc}")
    if len(blob) % PRINT_BUF_SIZE:
        raise SupvanError(
            f"{len(blob)} bytes is not a whole number of "
            f"{PRINT_BUF_SIZE}-byte print buffers")

    data, stride, cols = bytearray(), None, 0
    for i in range(len(blob) // PRINT_BUF_SIZE):
        buf = blob[i * PRINT_BUF_SIZE:(i + 1) * PRINT_BUF_SIZE]
        n = int.from_bytes(buf[4:6], "little")
        per_line = buf[6]
        if per_line == 0:
            raise SupvanError(f"buffer {i} declares no bytes per line")
        if stride is None:
            stride = per_line
        elif per_line != stride:
            raise SupvanError(
                f"buffer {i} changes the stride, {stride} -> {per_line}")

        end = n * per_line + PRINT_BUF_HEADER
        chk = sum(buf[2:PRINT_BUF_HEADER])
        for k in range(1, end // CHECKSUM_STRIDE + 1):
            chk += buf[k * CHECKSUM_STRIDE - 1]
        if int.from_bytes(buf[0:2], "little") != chk & 0xFFFF:
            raise SupvanError(f"buffer {i} checksum does not validate")

        data += buf[PRINT_BUF_HEADER:PRINT_BUF_HEADER + n * per_line]
        cols += n
    return raster_to_column_major(bytes(data), stride), stride, cols


# ------------------------------------------------ not wired in yet

def print_bitmap(raster, per_line_byte, total_cols, path=DEFAULT_DEVICE,
                 timeout=DEFAULT_TIMEOUT, density=DEFAULT_DENSITY,
                 margin_top=DEFAULT_MARGIN_DOTS,
                 margin_bottom=DEFAULT_MARGIN_DOTS, on_step=None):
    """Print one raster on the label maker.

    The format is settled (see the section above) but **no label from
    this function has ever come out of the hardware**, so nothing in
    mplabel calls it automatically: `mplabel inventory` still writes a
    CSV for the vendor editor. `mplabel supvan-test-print` drives this
    same path deliberately, one label at a time, which is how it gets
    promoted from here.

    Deliberately takes a raster rather than a PDF. Rendering belongs to
    `printers.render_bitmap`, and keeping this function to bytes-in is
    what lets the whole job be built and asserted without a device."""
    job = build_job(raster, per_line_byte, total_cols, density=density,
                    margin_top=margin_top, margin_bottom=margin_bottom)
    return experimental_print(job, path=path, timeout=timeout,
                              speed=job["speed"], on_step=on_step)


# ------------------------------------------------------- the experiment
#
# Everything below is a deliberate experiment, not a working print path.
# The command sequence comes from the document; what goes inside the
# compressed buffer does not, so the parameters that are guesses are
# arguments rather than constants, and the failure modes are made as
# legible as possible.
#
# 48mm at 203dpi is 383.5 dots, so 384 - 48 bytes a row - is the obvious
# first guess, and the real constant is per-model.
DEFAULT_WIDTH_DOTS = 384


def render_test_pattern(width_dots=DEFAULT_WIDTH_DOTS, height_dots=120,
                        invert=False, style="blocks", clip=None):
    """A deliberately asymmetric 1-bit pattern, packed MSB-first.

    Asymmetric on purpose: a symmetric pattern comes out looking correct
    under a mirrored row order or a flipped axis, and this is meant to
    tell us which of those is happening.

      - a solid bar across the top, which shows the row stride at a glance
      - a filled square hard against the left edge
      - a one-dot rule down the right edge

    If the bytes-per-row guess is wrong the bar and the rule shear into
    diagonals, and the angle says by how much. If the polarity is wrong
    the label comes out mostly black. If the width is right and the
    polarity is right, it looks like what it is."""
    stride = (width_dots + 7) // 8
    rows = bytearray(stride * height_dots)

    def dot(x, y):
        if 0 <= x < width_dots and 0 <= y < height_dots:
            rows[y * stride + (x >> 3)] |= 0x80 >> (x & 7)

    if style == "scatter":
        # Not a picture - a diagnostic. It separates the two things left
        # standing after the encoder was cleared on hardware.
        #
        #                     ink      stream        result
        #   their image      0.13%   419B, 7 rpts    prints
        #   blocks           7.54%   724B, 12 rpts   refused
        #
        # Both moved together, so either could be the cause. This holds
        # the ink at the working end (0.26%, twice the print that worked,
        # 29x less than the one refused) and pushes the stream to the
        # failing end - 695 bytes in 11 reports - by putting one dot in
        # every row at an offset that never repeats. With no match coder
        # that defeats compression almost completely.
        #
        # Refused  -> size or report count is the blocker; ink is cleared.
        # Prints   -> ink is the blocker; size is cleared.
        for y in range(height_dots):
            dot((y * 137) % width_dots, y)
    elif style == "sparse":
        # The same asymmetry drawn in outline. The captured print that is
        # known to have worked is 0.13% ink; the blocks below are 7.6%,
        # which is 60 times as much and a plausible reason for a refusal
        # on a battery-powered head. This keeps every landmark - stride,
        # origin, both edges - and almost none of the ink, so a failure
        # here cannot be blamed on coverage.
        # Leave as many rows *completely* blank as possible. That is not
        # cosmetic: with no match coder a row containing one dot costs
        # nearly as much as a row of many, so ruling both edges down the
        # full height made the stream larger than the blocks it replaced -
        # which would have confounded ink with size all over again. The
        # captured print that worked leaves 242 of its 256 rows empty.
        for x in range(width_dots):                # one rule across the top
            dot(x, 0)
        for x in range(64):                        # hollow square, left edge
            dot(x, 16)
            dot(x, min(79, height_dots - 1))
        for y in range(17, min(79, height_dots)):
            dot(0, y)
            dot(63, y)
        for y in range(8):                         # a tick on the right, so
            dot(width_dots - 1, 24 + y)            # a mirrored row order or
            dot(width_dots - 1, 200 + y)           # flipped axis still shows
    else:
        for y in range(min(8, height_dots)):      # top bar
            for x in range(width_dots):
                dot(x, y)
        for y in range(16, min(80, height_dots)):  # left square
            for x in range(0, 64):
                dot(x, y)
        for y in range(height_dots):              # right-edge rule
            dot(width_dots - 1, y)

    if clip:
        # Blank every dot outside a box, leaving the image the same size.
        #
        # This exists because of the one pattern that survives every
        # measurement: the *only* bitmap that has ever printed is the
        # vendor's own, and its ink stops at x=351 where everything drawn
        # here runs to x=383. 352 dots is 44 whole bytes, which looks a lot
        # like a printable width narrower than the 384-dot head. Clipping
        # changes only which dots are set, so a run of the same pattern
        # with and without it differs in one thing.
        max_x, max_y = clip
        for y in range(height_dots):
            for xb in range(stride):
                if y >= max_y:
                    rows[y * stride + xb] = 0
                    continue
                keep = 0
                for k in range(8):
                    if xb * 8 + k < max_x:
                        keep |= 0x80 >> k
                rows[y * stride + xb] &= keep

    if invert:
        rows = bytearray(b ^ 0xFF for b in rows)
    return bytes(rows), stride, height_dots


# LZMA1 defaults. lc=3, lp=0, pb=2 encode to the 0x5d properties byte that
# appears at the head of every stream this produces.
LZMA_LC, LZMA_LP, LZMA_PB = 3, 0, 2

# 8KB, taken from a captured print by the vendor's own app. Not a guess:
# the header it sends reads `5d 00 20 00 00` - properties 0x5d, dictionary
# 0x2000. Python's preset 9 asks for 64MB, which a battery-powered label
# printer cannot allocate, and an earlier guess of 64KB was still eight
# times too large.
LZMA_DICT_SIZE = 8192


def compress_bitmap(data, fmt="device", preset=9,
                    dict_size=LZMA_DICT_SIZE, declare_size=True):
    """LZMA-compress a bitmap the way the device expects it.

    The device accepts exactly one shape, established by comparing a
    captured print from the vendor's application against everything this
    could produce: the 13-byte "alone" container, an 8KB dictionary, the
    uncompressed size **declared**, and **no end-of-stream marker**.

    That last one is why `fmt` defaults to `device` and not to `alone`.
    Python's `lzma` always appends a marker and gives no way to suppress
    it, and because the marker is entropy-coded it cannot be trimmed off
    afterwards either. Both directions were checked: the captured stream
    will not decode as unknown-size, so it carries no marker; ours will,
    so it does. The printer refused ours whichever size we declared.

    So `device` uses `lzma1.compress`, a literals-only encoder written
    here. It compresses worse than liblzma - no matches - and that costs
    nothing at this size. What it buys is the one shape the firmware
    takes.

    The other containers are kept because each was a real experiment and
    naming them is how the failures stay legible:

    - `alone`   liblzma's, with a marker. Refused by the device.
    - `raw`     no header at all. Refused.
    - `xz`      the modern container. Refused.

    `declare_size` only applies to `alone`; `device` always declares,
    because a stream without a marker is undecodable without it."""
    import lzma
    import struct

    if fmt == "device":
        return lzma1.compress(data, dict_size=dict_size)
    if fmt == "xz":
        return lzma.compress(data, format=lzma.FORMAT_XZ, preset=preset)

    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                "lc": LZMA_LC, "lp": LZMA_LP, "pb": LZMA_PB}]
    body = lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)
    if fmt == "raw":
        return body
    if fmt != "alone":
        raise ValueError(f"unknown lzma container {fmt!r}")

    props = (LZMA_PB * 5 + LZMA_LP) * 9 + LZMA_LC
    size = len(data) if declare_size else 0xFFFFFFFFFFFFFFFF
    return (bytes([props]) + struct.pack("<I", dict_size)
            + struct.pack("<Q", size) + body)


# The largest stream measured to print is 419 bytes in 7 reports; 695 in
# 11 was refused, as was 724 in 12. Ink was ruled out getting there - the
# 695-byte refusal carried 0.26% ink against the 419-byte success's 0.13%,
# where the earlier 724-byte refusal carried 7.54%. So the device has a
# limit on how much it will take in one buffer, and the honest bound is
# the biggest one seen to work rather than the round number near it.
#
# 448 is 7 reports exactly. 512 would be 8 and is the tempting guess -
# buffers usually are powers of two - but nothing has printed above 419,
# so guessing upwards here costs labels to find out.
MAX_BUFFER_BYTES = 448


def split_bitmap(data, stride, max_bytes=MAX_BUFFER_BYTES, dict_size=None):
    """Compress a bitmap as a list of buffers, each within `max_bytes`.

    Each band is a **complete** LZMA stream - its own 13-byte header
    declaring that band's uncompressed length - not a slice of one long
    stream. A slice would be undecodable on its own, and the vendor
    application is described as splitting large images into several
    compressed buffers, which only makes sense if each stands alone.

    Bands are whole rows, halved until every one fits. Equal row counts
    rather than a greedy fill: a band is a strip of the label, and strips
    of the same height are far easier to reason about when the printed
    result is wrong.

    Returns a list of (compressed, rows_in_band, raw_len)."""
    if stride <= 0:
        raise ValueError("stride must be positive")
    if len(data) % stride:
        raise ValueError("bitmap is not a whole number of rows")
    rows = len(data) // stride
    if not rows:
        raise ValueError("nothing to compress")
    kw = {} if dict_size is None else {"dict_size": dict_size}

    band_rows = rows
    while True:
        bands = []
        for start in range(0, rows, band_rows):
            chunk = data[start * stride:(start + band_rows) * stride]
            bands.append((lzma1.compress(chunk, **kw), len(chunk) // stride,
                          len(chunk)))
        if all(len(c) <= max_bytes for c, _r, _n in bands):
            return bands
        if band_rows == 1:
            raise ValueError(
                "a single row does not compress under "
                + str(max_bytes)
                + " bytes; the limit is too low for this image")
        band_rows = max(1, band_rows // 2)

def print_job(job, path=DEFAULT_DEVICE, timeout=DEFAULT_TIMEOUT,
              on_step=None, settle=0.2):
    """Print what `build_job` built. The one place that unpacks a job.

    `experimental_print` reads `payload["streams"]` as a list of separate
    LZMA streams, while `build_job`'s `"buffers"` is a *count* of
    4096-byte print buffers inside one stream. Two different things under
    one word, and handing a job dict straight over already died once on
    `for c, n in 3` - see the note in `experimental_print`.

    So nothing hands a caller-supplied dict to `experimental_print` any
    more. This takes a job, names the two fields it actually needs, and
    is the only door. It matters more now the spec can arrive from a
    network: a payload that could choose the `streams` branch would let
    the wire pick a code path meant for a local experiment."""
    return experimental_print(
        {"compressed": job["compressed"], "raw_len": job["raw_len"]},
        path=path, timeout=timeout, speed=job["speed"],
        settle=settle, on_step=on_step)

def abort_print(path=DEFAULT_DEVICE, timeout=DEFAULT_TIMEOUT):
    """Send stop-print and report the status afterwards.

    A job that was accepted but never satisfied leaves the device sitting
    in its printing state, and the next attempt then stacks on top of a
    half-started one. This is the way out that is not a power cycle."""
    with SupvanDevice(path, timeout) as dev:
        dev.command(OP_STOP_PRINT)
        try:
            dev.read_report()
        except SupvanError:
            # Some commands answer, some do not. Not answering a stop is
            # not itself a problem; the status poll below is the check.
            pass
        return dev.status()


def experimental_print(payload, path=DEFAULT_DEVICE, timeout=DEFAULT_TIMEOUT,
                       speed=1, announce="compressed", buffer_len=None,
                       settle=0.2, on_step=None):
    """Walk the documented print sequence with a compressed bitmap.

    An experiment. The sequence is from the document; the guesses are the
    arguments. Status is polled between every step and reported through
    `on_step`, so a job that stalls says *where* it stalled - which is the
    point of running it at all.

    Stops at the first error flag rather than pushing on. A device that
    has already refused is not going to be persuaded by more data, and
    leaving it mid-job is how it ends up needing a power cycle.

    `announce` decides what length goes in the 0x5c command: the
    compressed size or the uncompressed one. The document says "its
    length" without saying which, so it is a knob.

    `payload` may carry a single `compressed`/`raw_len` pair or a list of
    them under `buffers`, in which case the 0x5c / data / 0x10 cycle runs
    once per buffer inside a single 0x13 job. That is not a refactor for
    its own sake: the device takes 419 bytes in 7 reports and refuses 695
    in 11, with ink ruled out in between, so anything bigger than a small
    label has to arrive as several buffers."""
    import time as _time

    say = on_step or (lambda *_a: None)
    # "streams", not "buffers". `build_job` returns a dict whose
    # "buffers" is a *count* of 4096-byte print buffers inside a single
    # LZMA stream, and this reads a *list of separate LZMA streams* - two
    # different things under one word. Passing a job straight through
    # crashed on `for c, n in 3`, and only on the one caller that did not
    # unpack the job by hand first.
    if "streams" in payload:
        streams = [(bytes(c), int(n)) for c, n in payload["streams"]]
    else:
        streams = [(payload["compressed"], payload["raw_len"])]

    def lengths(compressed, raw_len):
        announced = len(compressed) if announce == "compressed" else raw_len
        # 0x5c announces the bulk transfer and 0x10 reports the image
        # length. The document names both "length" without saying whether
        # either means the compressed byte count or the uncompressed
        # image, so they are separately settable and default to the same.
        if buffer_len is None:
            return announced, announced
        return announced, (len(compressed) if buffer_len == "compressed"
                           else raw_len)

    def check(dev, label, patience=3):
        # A poll that comes back empty is not the same as a poll that
        # comes back bad. Observed after the last buffer of a four-buffer
        # job: the device simply stopped answering, and treating the first
        # silence as fatal both hid whether it was temporary and left the
        # device sitting in its printing state. So silence is retried, and
        # only then reported as silence - with the stop-print that a
        # half-started job needs.
        for attempt in range(patience):
            # Read the report here rather than calling dev.status(), so
            # silence is detected by *being* empty rather than by matching
            # the text of an exception. There are two such messages in
            # this module and the deployed Pi may carry an older one.
            dev.command(OP_INQUIRY_STATUS)
            report = dev.read_report()
            if report:
                status = decode_status(report)
            else:
                if attempt + 1 < patience:
                    say(f"no answer at '{label}', asking again "
                         f"({attempt + 2} of {patience})", None, [])
                    _time.sleep(settle * 4)
                    continue
                try:
                    dev.command(OP_STOP_PRINT)
                except SupvanError:
                    pass
                raise SupvanError(
                    f"the device stopped answering at step '{label}' after "
                    f"{patience} tries. Sent stop-print (0x14); if the next "
                    f"`mplabel supvan-probe` is also silent it needs a power "
                    f"cycle")
            lit = [n for n, _o, _m in STATUS_FLAGS if status[n]]
            say(label, status, lit)
            if status["errors"]:
                raise SupvanError(
                    f"device reports {', '.join(status['errors'])} at step "
                    f"'{label}' - stopping rather than sending more")
            return status

    with SupvanDevice(path, timeout) as dev:
        # Fail fast on the opening poll only: silence there means the
        # device is not there, and retrying just delays saying so.
        # Once a job has started, silence is worth waiting out.
        check(dev, "before anything", patience=1)

        dev.command(OP_CHECK_DEVICE)
        dev.read_report()
        _time.sleep(settle)
        status_before = check(dev, "after check device")

        if status_before["printing"]:
            raise SupvanError(
                "the device is already in its printing state - a previous "
                "attempt was accepted and never satisfied. Clear it with "
                "`mplabel supvan-test-print --abort`, or power cycle it, "
                "before starting another job")

        dev.command(OP_START_PRINT, 1)
        dev.read_report()
        _time.sleep(settle)
        status = check(dev, "after start print")

        for index, (compressed, raw_len) in enumerate(streams, 1):
            of = f" ({index} of {len(streams)})" if len(streams) > 1 else ""
            announced, buffered = lengths(compressed, raw_len)

            # Buffer-full clear is the only backpressure the document
            # names, and with several buffers it is doing real work rather
            # than passing straight through: the device has to finish with
            # one before it can be handed the next.
            for _ in range(20):
                if not status["buffer_full"]:
                    break
                _time.sleep(settle)
                status = check(dev, f"waiting for buffer{of}")
            else:
                raise SupvanError("buffer stayed full; the device never "
                                  "became ready for data")

            say(f"announcing {announced} bytes (0x5c){of}", None, [])
            dev.command(OP_NEXT_FRAME_IS_BULK, announced)
            _time.sleep(settle)

            reports = dev.write(compressed)
            say(f"streamed {len(compressed)} bytes in {reports} reports{of}",
                None, [])
            _time.sleep(settle)

            say(f"buffer full (0x10) len={buffered} speed={speed}{of}",
                None, [])
            dev.command(OP_BUFFER_FULL, buffered, speed)
            _time.sleep(settle)

            # Patience here, not elsewhere: the last buffer is where
            # the device has the most reason to be busy, and where it
            # was seen to go quiet.
            status = final = check(dev, f"after buffer full{of}",
                                   patience=5)

        # Wait for the job to finish, then clean up after ourselves if it
        # does not. Observed on the hardware: a job the device accepts but
        # cannot satisfy leaves it in printing state *and* leaves the media
        # out of position - the next attempt then reports a seating error
        # before it can start. Sending stop-print costs nothing when the
        # job did finish and saves a reseat when it did not.
        for _ in range(15):
            if not final["printing"]:
                break
            _time.sleep(settle)
            final = check(dev, "waiting for the job to finish")
        else:
            say("still printing - sending stop (0x14) so the device is not "
                "left mid-job", None, [])
            dev.command(OP_STOP_PRINT)
            _time.sleep(settle)
            final = check(dev, "after stop print")
            final["stalled"] = True

        return final
