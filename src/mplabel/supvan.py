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


# ---------------------------------------------------------- not built yet

def print_bitmap(*_args, **_kwargs):
    """Deliberately unimplemented - see docs/supvan-t50m-protocol.md.

    The command sequence for a job is known, and the image is LZMA, which
    the stdlib covers. What is not known is what goes *inside* the
    compressed buffer, and all of it has to be settled before bytes are
    sent to a device that then pulls paper through a hot head."""
    raise NotImplementedError(
        "the T50M Pro print path is not implemented: the uncompressed row "
        "format (bytes per row, bit order), the bit polarity (whether a set "
        "bit is a black dot), this model's exact dot width (~384 at "
        "48mm/203dpi, but the real constant is per-model), how media width "
        "and label length are communicated, and the RFID label "
        "authentication exchange (opcode 0x5d) are all undetermined. Use "
        "`mplabel inventory` and the vendor editor until they are, or "
        "`mplabel supvan-test-print` to help settle them.")


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
                        invert=False):
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

    for y in range(min(8, height_dots)):          # top bar
        for x in range(width_dots):
            dot(x, y)
    for y in range(16, min(80, height_dots)):     # left square
        for x in range(0, 64):
            dot(x, y)
    for y in range(height_dots):                  # right-edge rule
        dot(width_dots - 1, y)

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


def compress_bitmap(data, fmt="alone", preset=9,
                    dict_size=LZMA_DICT_SIZE, declare_size=True):
    """LZMA-compress a bitmap the way the device is thought to expect it.

    The document establishes that the payload is LZMA. It does not
    establish the container, and LZMA has several: the 13-byte "alone"
    header, xz, and raw with no header at all.

    The alone container is assembled by hand rather than taken from
    `lzma.compress(format=FORMAT_ALONE)`, for two reasons that both look
    like they matter to an embedded decoder:

    - Python's preset 9 asks for a 64MB dictionary. `dict_size` defaults
      to 64KB, which is what an embedded decoder can actually allocate.
    - Python declares the uncompressed size as *unknown*, eight 0xFF
      bytes. A decoder that sizes its output buffer from that field has
      nothing to work with, so `declare_size` writes the real length.

    `declare_size` is **on**, because a captured print from the vendor's
    own application declares it: the header reads
    `5d 00 20 00 00 00 30 00 00 00 00 00 00` - 8KB dictionary, 12288 bytes
    of image.

    One divergence to know about: liblzma will not read our stream back,
    because Python always appends an end-of-stream marker and liblzma
    rejects a declared size alongside one. The captured stream has no such
    marker and reads back fine. The consumer here is the printer's
    decoder, not liblzma, and embedded decoders stop at the declared size
    and ignore what follows - but it does mean this is checked against the
    device rather than on this machine.

    Both are inferences about this firmware rather than documented facts,
    which is why both are arguments."""
    import lzma
    import struct

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
    length" without saying which, so it is a knob."""
    import time as _time

    say = on_step or (lambda *_a: None)
    compressed = payload["compressed"]
    raw_len = payload["raw_len"]
    announced = len(compressed) if announce == "compressed" else raw_len
    # 0x5c announces the bulk transfer and 0x10 reports the image length.
    # The document names both "length" without saying whether either means
    # the compressed byte count or the uncompressed image, so they are
    # separately settable and default to the same thing.
    if buffer_len is None:
        buffered = announced
    else:
        buffered = len(compressed) if buffer_len == "compressed" else raw_len

    def check(dev, label):
        status = dev.status()
        lit = [n for n, _o, _m in STATUS_FLAGS if status[n]]
        say(label, status, lit)
        if status["errors"]:
            raise SupvanError(
                f"device reports {', '.join(status['errors'])} at step "
                f"'{label}' - stopping rather than sending more")
        return status

    with SupvanDevice(path, timeout) as dev:
        check(dev, "before anything")

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

        # Buffer-full clear is the only backpressure the document names.
        for _ in range(20):
            if not status["buffer_full"]:
                break
            _time.sleep(settle)
            status = check(dev, "waiting for buffer")
        else:
            raise SupvanError("buffer stayed full; the device never became "
                              "ready for data")

        say(f"announcing {announced} bytes (0x5c)", None, [])
        dev.command(OP_NEXT_FRAME_IS_BULK, announced)
        _time.sleep(settle)

        reports = dev.write(compressed)
        say(f"streamed {len(compressed)} bytes in {reports} reports", None, [])
        _time.sleep(settle)

        say(f"buffer full (0x10) len={buffered} speed={speed}", None, [])
        dev.command(OP_BUFFER_FULL, buffered, speed)
        _time.sleep(settle)

        final = check(dev, "after buffer full")

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
