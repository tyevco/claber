# SUPVAN / KATA Symbol T50M Pro — USB HID protocol

Notes for talking to the T50M Pro directly, so a label can be printed
without the vendor's desktop application.

Nothing in mplabel drives this device yet. `mplabel inventory` writes a CSV
for the vendor editor to batch-print. This document exists so that a direct
backend *can* be written later without repeating the analysis.

**Scope:** the 48mm label maker used for inventory tags. It is not, and
cannot be, a shipping-label printer — 48mm is ~384 dots at 203 dpi against
the 812 the 4x6 pipeline emits. Shipping labels stay on the CLABEL G4.

## Provenance and what is deliberately absent

Derived by static analysis of the vendor's Electron desktop application,
which ships JavaScript source maps. Nothing was executed and no traffic was
captured.

**The command and status path has since been confirmed on the device.**
A status poll over `/dev/hidraw0` returned a decodable report, which
settles the 65-byte write with its leading `0x00`, the 8-byte frame
including the big-endian `wValue`, and the byte-0 flag layout. The
bitmap path remains untested and largely unknown.

What follows is a description of **observable device behaviour** — frame
layouts, opcode values, bit meanings, command ordering. It contains no
vendor source code, and none should ever be added: their code is theirs.
Any implementation written from this document must be written from the
facts here, not transcribed from the application.

`.gitignore` excludes `*.asar` and `*.js.map` to keep vendor material out
of this repository by accident.

## Device identification

USB `1820:207f` — vendor 0x1820 (6176), product 0x207f (8319).

It presents as a **USB composite device**: a vendor-defined HID interface
plus a USB mass-storage device exposing a CD-ROM that holds the Windows
installer. There is no USB printer class interface, so on Linux it binds
`hid-generic`, **not** `usblp`, and there is no `/dev/usb/lpN` for it.

The vendor application recognises product ids 8306, 8307, 8309, 8310, 8311,
8314 and 8319 across several models, and maps each to an internal family.
8319 maps to the same family as the T50/T80 series, which is the command
set described here. Other families in that application (SP, TP, G, BP)
use different commands and are out of scope.

## Transport

The HID report descriptor declares usage page 0xFF00 — a vendor-defined
raw byte pipe — with **64-byte input and 64-byte output reports and no
Report ID**.

Everything, commands and bitmap data alike, is sent as a sequence of
64-byte output reports. A payload shorter than 64 bytes is zero-padded; a
longer one is split across consecutive reports.

On Linux `hidraw` a write is therefore **65 bytes**: a leading `0x00`
report-id byte followed by the 64-byte report. Omitting that byte is the
most likely first mistake, and the device simply ignores the write rather
than reporting an error.

The node is root-only by default; see `udev/99-supvan-t50m.rules`.

## Command frame

Commands are 8 bytes, zero-padded to the 64-byte report:

```
offset  value
0       0xC0
1       0x40
2       wValue >> 8      high byte
3       wValue & 0xFF    low byte
4       opcode
5       0x00
6       0x08
7       0x00
```

This is the shape of a USB vendor control-request setup packet
(`bmRequestType` 0xC0, `bRequest` 0x40, wValue, wIndex, wLength 8) carried
inside a HID report — presumably so that Windows binds its built-in HID
driver and the vendor ships no driver at all.

**The byte order is genuinely inconsistent, and that is not a mistake in
this document.** `wValue` here goes out **high byte first**, while the page
counter in the status report is little-endian. A real USB setup packet is
little-endian throughout, so the frame looks wrong next to the structure it
imitates — it was checked twice for exactly that reason and is recorded as
observed. Do not "fix" one to match the other.

A ten-byte variant appends a second 16-bit value, used where a command
needs two parameters:

```
8       wValue2 >> 8
9       wValue2 & 0xFF
```

## Opcodes

| Opcode | Name | Notes |
|---|---|---|
| `0x10` | buffer full | ends a bitmap transfer; carries image length and speed in the ten-byte form |
| `0x11` | inquiry status | poll; reply is the status report below |
| `0x12` | check device | asks whether the device can print; issued once per job |
| `0x13` | start print | sent with wValue 1 |
| `0x14` | stop print | |
| `0x17` | read revision | replies with a length byte and an ASCII string; observed `04 32 2e 34 00` = **"2.4"** |
| `0x30` | return media info | replies with **59 bytes** of high-entropy data — almost certainly the tag content the media carries, given `0x5d` writes label authentication |
| `0x5c` | next frame is bulk | announces an LZMA-compressed bitmap; wValue is its length |
| `0x5d` | set RFID data | label authentication |
| `0xc5` | read firmware revision | replies status-shaped, 8 bytes, with byte 6 set to `0x01` on the observed unit; meaning undetermined |
| `0xc6` | next frame is firmware | firmware update path — **do not send** |

Note the vendor application also uses several large pseudo-opcodes (888,
999, 8888) as internal state markers. Those are not wire values and never
reach the device.

## Status report

The device replies to a status poll with a 64-byte input report.

**Every reply begins with a length byte**, followed by that many bytes of
payload; the rest of the 64-byte report is padding and means nothing.
Confirmed across commands that answer with different lengths — status and
firmware revision give 8, read-revision gives 4, media info gives 59 — so
it is a length, not a marker.

**The device does not clear the report buffer between replies.** Anything a
command does not refresh is left over from the previous one. Two probe runs
made this unambiguous: every reply in the second carried
`bf 83 71 a2 63 36 f6` on the end, byte-for-byte the tail of the *first*
run's media-info reply.

This reaches inside the declared length as well. Read-firmware-revision
sets payload byte 6 to `0x01`, and that byte then persisted into later
status replies that had nothing to do with it. So the rule is stricter than
"trust the length": **interpret only the bytes a command is documented to
refresh.** For status that is six, even though the length byte says eight. The offsets in the table below are relative
to **the payload**, i.e. one byte further into the report than they appear
on the wire.

This was not in the original analysis and cost a false alarm: decoding from
offset 0 made an idle, healthy printer report "media not recognised" while
claiming USB was disconnected on a device that was answering over USB.
Opening the media cover and re-polling settled it — the byte that changed
was the one this offset predicts.

| Byte | Bit | Meaning |
|---|---|---|
| 0 | 0x01 | buffer full |
| 0 | 0x02 | media read/write error |
| 0 | 0x04 | out of media |
| 0 | 0x08 | media not recognised |
| 0 | 0x10 | media seating error |
| 0 | 0x20 | check remaining media |
| 0 | 0x40 | battery low |
| 1 | 0x04 | device busy |
| 1 | 0x08 | print head too hot |
| 2 | 0x08 | media cover open |
| 2 | 0x10 | USB connected |
| 2 | 0x40 | printing in progress |
| 2 | 0x80 | device busy (secondary) |
| 3 | 0x01 | media not installed |
| 3 | 0x80 | charging |

Bytes 4 and 5 are a 16-bit **little-endian** count of pages printed —
byte 5 is the high byte.

Confirmed against the device, media cover closed then open:

```
08 00 00 10 00 00     idle, cover closed  -> usb connected
08 00 00 18 00 00     cover open          -> usb connected + cover open
08 00 04 10 00 00     during check device -> usb connected + busy
```

The third is independent confirmation of the offsets: `0x12` puts the
device briefly busy while it rescans, and the bit that lights is the one
the table calls busy — at that offset and nowhere else.

Both decode cleanly once the leading byte is accounted for, and the cover
bit is the one that moves. Two earlier "anomalies" — an undocumented bit at
byte 3 and a USB flag that would not light — were both this offset, not
separate findings.

This is the most useful part of the protocol to implement first: a status
poll is a safe, read-only round trip that proves the transport works
without moving any paper.

## Printing sequence

The vendor application drives a state machine. Reduced to the wire
exchange, one job is:

1. **check device** (`0x12`), then poll **inquiry status** (`0x11`) until
   the firmware reports the check complete.
2. Abort if the status report shows any error condition, or if printing is
   already in progress.
3. **start print** (`0x13`) with wValue 1.
4. Poll **inquiry status** until the buffer-full flag is clear. Data may
   only be transferred when it is clear — this is the flow-control
   mechanism, and there is no other backpressure signal.
5. **announce bulk** (`0x5c`) with wValue set to the compressed image
   length.
6. Stream the compressed image as consecutive 64-byte reports, with no
   per-chunk header.
7. **buffer full** (`0x10`), ten-byte form, carrying the image length and
   the print speed.
8. For further copies or labels, return to step 4.
9. End the job, or **stop print** (`0x14`) to abort.

Status is polled between every step; the device is treated as busy until a
reply arrives.

## Bitmap data

The image is **LZMA compressed** before transfer — the LZMA1 "alone"
container, an 8KB dictionary, the uncompressed size declared, and **no
end-of-stream marker**.

Python's standard library `lzma` module decodes that but cannot produce
it: it always appends a marker, offers no way to suppress one, and the
marker is entropy-coded so it cannot be trimmed off afterwards. That is
not a detail. The device refuses a stream carrying a marker, and refusing
looks exactly like a media fault — see below.

`src/mplabel/lzma1.py` is a literals-only LZMA1 encoder written for this,
which keeps the Pi dependency list where it is. It compresses worse than
liblzma and that costs nothing at this size, and unlike everything else
in this document it can be **checked without hardware**: liblzma has to
decode its output, with the declared size, back to the original bytes.

The vendor application compresses at level 9, splits large images into
several compressed buffers, and chooses between per-buffer and combined
packaging depending on the total compressed size, with a threshold in the
low thousands of bytes.

## Confirmed from a capture of a working print

A Bluetooth HCI snoop log of the vendor's phone app printing a label
settled the remaining questions. **The opcodes are the same over both
transports** - only the framing differs. Over Bluetooth (RFCOMM) a frame
is:

```
7e 5a <len:LE16> 10 01 aa <opcode> <params>     phone -> printer
7e 5a <len:LE16> 10 03 55 <opcode> <params>     printer -> phone
```

The whole job, with the status polls between each step removed:

```
TX 0x12 check device      10 01 aa 12 01 00 00 01 00 00 00 00
TX 0x13 start print       10 01 aa 13 01 00 00 01 00 00 00 00
TX 0x5c bulk announce     10 01 aa 5c 04 00 00 01 00 02 01 00
TX 0xbb bulk data (512)   10 02 aa bb <2-byte check> 00 01 <lzma>
TX 0x10 buffer full       10 01 aa 10 9a 00 00 01 66 00 33 00
```

Three things that matter:

- **There is no `0x5d`.** Label authentication is not part of a normal
  print, so it is not what blocks one.
- **Bulk data travels under its own opcode, `0xbb`**, with a marker of
  `10 02 aa` rather than `10 01 aa`, and a two-byte field that varies per
  image - a checksum, most likely.
- **`0x5c` announces `00 02` = 512**, the size of the frame that follows,
  not the length of the image.

### The LZMA header, exactly

```
5d 00 20 00 00 00 30 00 00 00 00 00 00
|  |________|  |____________________|
|   dict 8192   uncompressed 12288
properties (lc=3 lp=0 pb=2)
```

An **8KB dictionary**, and the uncompressed size **declared**. Every
earlier attempt from this repo got both wrong - 64MB then 64KB, and
"unknown" for the size - and each wrong guess produced the same symptom:
a job the printer accepted, positioned for, and never completed.

**There is no end-of-stream marker**, and that turned out to be the whole
remaining blocker. Proved in both directions: blank out the declared size
and the captured stream will not decode, so it has no marker; do the same
to a stream from Python's encoder and it decodes, so it has one. The
device takes the first shape and refuses the second.

### The raster

The captured image decompresses to **12288 bytes = 48 bytes per row x 256
rows**, confirming **384 dots** across - 48mm at 203dpi, as arithmetic
suggested. Bit order and polarity are still unconfirmed: rendered either
way the test label is blocks rather than anything recognisable, so it
tells us the geometry but not the sense of a set bit.

## Confirmed from a USB capture of a working print

A USBPcap capture of the vendor application printing over USB. This is
the authoritative reference for the USB path - real frames off the wire.

The job runs on the **interrupt endpoints** (`0x01` out, `0x81` in) in
64-byte reports. The bulk endpoints `0x03`/`0x82` carry only the fake
CD-ROM being probed by Windows - 31-byte CBW, 13-byte CSW, 4096-byte
sector reads - and have nothing to do with printing.

```
OUT c0 40 00 00 12 00 08 00     check device
OUT c0 40 00 00 11 00 08 00     status
OUT c0 40 00 01 13 00 08 00     start print, wValue 1
OUT c0 40 00 00 11 00 08 00     status, until printing is set
OUT c0 40 00 7b 5c 00 08 00     announce: wValue 0x7b = 123 = compressed length
OUT 5d 00 20 00 00 00 30 ...    the LZMA stream, bare, in 64-byte reports
OUT ...                          (123 bytes over two reports)
OUT c0 40 00 7b 10 00 08 00 00 3c   buffer full: same length, second value 0x3c = 60
OUT c0 40 00 00 11 00 08 00     status, until it finishes
```

Three corrections to what came before:

- **The bulk data is bare.** There is no USB equivalent of the `0xbb`
  wrapper seen over Bluetooth; the stream goes straight into 64-byte
  reports after the announce. The transport here was right.
- **The second value in `0x10` is 60** (`0x3c`), not 1. What it means is
  unknown - speed, or something else entirely - but 60 is what a working
  print sends.
- **The image is 12288 bytes**, 48 x 256, matching the media. A shorter
  image looks like a job the printer is still waiting to finish, which is
  what every attempt from here produced.

The LZMA header is `5d 00 20 00 00 00 30 00 00 00 00 00 00`, identical to
the Bluetooth capture: 8KB dictionary, size declared.

### What the USB capture settled

The bulk data is sent as **bare 64-byte reports after the `0x5c`
announce**, with no wrapper of any kind. The Bluetooth capture's `0xbb`
opcode and `10 02 aa` marker are RFCOMM framing and belong to that
transport only. This repo's HID path was already right.

`0x5c` carries the **compressed** length. `0x10` (buffer-full) carries it
again with a second value of 60.

### The replay: a printed label

`mplabel supvan-test-print --replay <captured.lzma>` — the extracted
stream from the capture, pushed through this repo's own code — **printed a
label**. That single result settles a list of things that were open, and
retires two theories outright:

- The transport, the 65-byte hidraw write, the frame layout, the opcode
  sequence, the `0x5c` announce, the `0x10` buffer-full value and the
  status polling are all **correct**.
- **The RFID/label authentication at `0x5d` is not required to print.**
  The replay never sends it.
- **`media_seating_error` does not mean the media.** It is what this
  device reports when it refuses a job, whatever the reason - the replay
  printed on the same roll minutes later. It is reported *after* the head
  positions, which makes it read as a physical fault. It is the same
  answer for every rejection and therefore diagnoses nothing.

So the only thing separating this repo from the vendor application is the
compressed stream itself. `lzma1.py` now produces the vendor's shape,
header for header:

```
5d 00 20 00 00 00 30 00 00 00 00 00 00      the captured print
5d 00 20 00 00 00 30 00 00 00 00 00 00      mplabel supvan-test-print
```

**And a generated pattern is still refused, but the encoder is not the
reason.** `--reencode` put the captured *image* through `lzma1.py` and the
label printed: 419 bytes, 7 reports, a literals-only body with no matches
in it anywhere. So the firmware accepts what this repo produces, and the
transport, the framing, the sequence and the encoder are all settled.

What is left is the picture. Decoding the captured image shows why the
original comparison was never clean - it is **99.87% blank**, 124 set bits
in 98304, 242 of its 256 rows completely empty:

| | ink | stream | result |
|---|---|---|---|
| captured image, vendor encoder | 0.13% | 123 B, 2 reports | prints |
| captured image, `lzma1.py` | 0.13% | 419 B, 7 reports | **prints** |
| generated `blocks` | 7.54% | 724 B, 12 reports | refused |

The middle row is what clears the encoder. The first and third still
differ in two things at once, so `--style scatter` holds one still: one
dot per row at an offset that never repeats, which is **0.26% ink** - the
working end - in **695 bytes over 11 reports** - the failing end.

- Refused -> **size or report count** is the blocker, ink is cleared.
- Prints -> **ink** is the blocker, size is cleared.

Worth noting before that runs: 419 bytes fits inside a 512-byte buffer and
724 does not, and 512 is exactly 8 reports. If size is the answer, that is
the shape to expect, and the fix is to split the stream into buffers the
way the vendor application is described as doing - each announced with
`0x5c` and closed with `0x10` - rather than sending one long run.

### It is the buffer size

`--style scatter` was refused. That is the answer: it carried **0.26% ink**
- the working end - in **695 bytes over 11 reports** - the failing end. Four
measurements, with ink spanning both outcomes and size not:

| stream | reports | ink | result |
|---|---|---|---|
| 123 B | 2 | 0.13% | prints |
| 419 B | 7 | 0.13% | prints |
| 695 B | 11 | 0.26% | **refused** |
| 724 B | 12 | 7.54% | **refused** |

So the device takes a limited amount of compressed data in one buffer,
which is exactly why the vendor application is described as splitting
large images into several. The captured print never needed it: at 123
bytes it fits in one.

The ceiling is somewhere in (419, 695]. It has not been bisected, because
each attempt costs a label and the answer only moves the default. `448` -
seven reports, the largest size seen to print - is what `split_bitmap`
uses. **512 is the tempting guess** and buffers usually are powers of two,
but nothing above 419 bytes has ever printed here, so it stays a guess.

### It is 512 bytes, and the encoder was too weak

`--style sparse` was refused at 551 bytes, and that completes the picture.
Every single-buffer measurement falls on one side of **512**:

| stream | vs 512 | ink | blank rows | result |
|---|---|---|---|---|
| 123 B | under | 0.13% | 242/256 | prints |
| 419 B | under | 0.13% | 242/256 | prints |
| 551 B | over | 0.66% | 183/256 | refused |
| 695 B | over | 0.26% | 0/256 | refused |
| 724 B | over | 7.54% | 0/256 | refused |

Ink and blank rows had looked like candidates. They are not, and the
reason they tracked the outcome so convincingly is worth keeping: with a
**literals-only** encoder, ink and blankness *determine* the compressed
size. All three moved together because two of them caused the third.

So there are two constraints, not one:

1. declared size, no end-of-stream marker - which rules out liblzma;
2. **at most ~512 compressed bytes in a single buffer** - which rules out
   a literals-only encoder, because it cannot get 12288 bytes of bitmap
   under 512 once there is any content on the label.

`lzma1.py` now has match coding - a hash-chain finder, rep distances, the
length and distance coders, and the state machine. The effect:

| | literals only | with matches |
|---|---|---|
| captured image | 419 B | 134 B |
| `blocks` | 724 B | 82 B |
| `sparse` | 551 B | 95 B |
| `scatter` | 695 B | 138 B |

All well inside one buffer, so `split_bitmap` never fires on a normal
label. It is kept for a taller image.

**One bug in that encoder is worth recording**, because its failure mode
is the kind that survives casual testing. A literal following a match is
coded against the byte one match distance back; when a bit disagrees with
that byte the context collapses to the plain literal tree, and the *index*
must lose the match-bit half along with the offset. Adding it
unconditionally corrupts a stream only when a literal follows a match and
the match byte has a set bit after the first disagreement - so every
uniform test bitmap round-tripped perfectly and the real captured image
did not. Caught by testing against the captured image and by fuzzing, not
by the tidy patterns.

### Correction: size was not isolated

`--style scatter` was refused, and that was read as "size is the blocker".
It was not a sound reading. `scatter` held ink still and varied size, but
it also took blank rows from 242 to 0, and blank rows track the outcome
just as perfectly as size does:

| | size | ink | blank rows | longest inked run | result |
|---|---|---|---|---|---|
| their image | 419 B | 0.13% | 242/256 | 11 | prints |
| `blocks` | 724 B | 7.54% | 0/256 | 256 | refused |
| `scatter` | 695 B | 0.26% | 0/256 | 256 | refused |
| `sparse` | 551 B | 0.66% | 183/256 | 64 | untested |

Ink is genuinely ruled out - it spans both outcomes. Size and blankness
are not distinguished by anything measured so far.

Splitting the image into four buffers of 305/248/186/186 bytes, all under
the supposed limit, **did not print** - which is what a wrong premise
looks like from the outside. Every buffer was accepted with no error, so
whatever the device objects to, it is not the size of a single transfer.

`--style sparse --max-buffer 0` separates them in one label: 551 bytes,
over the supposed size bound, with 183 blank rows and a longest inked run
of 64, like the print that works.

- Prints -> blankness or ink distribution matters; the size limit is not
  real and `split_bitmap` is solving the wrong problem.
- Refused -> size stands, and the multi-buffer sequence below is what
  needs work.

### The device can go silent mid-job

After the last buffer of the four-buffer job the device stopped answering
`0x11` altogether - not an error flag, no reply at all. Three readings, in
descending order of how well they fit:

1. The multi-buffer sequence is wrong and the device is waiting for
   something that never came. It accepted all four buffers without
   complaint, so it was not refusing them.
2. It began printing and stopped servicing the interrupt endpoint. Argues
   against itself: nothing came out.
3. `0x13`'s `wValue` of 1 means "one buffer", not "start", and a job of
   four confused it. The capture cannot distinguish the two readings - it
   is a single-buffer job either way.

`experimental_print` now retries a silent poll instead of treating it as
failure, and sends `0x14` on the way out so the device is not left
half-started. The opening poll still fails fast, because silence there
means the device is not there.

### Sending several buffers

`split_bitmap` cuts the image into bands of whole rows, halving the band
height until every band compresses under the limit. Each band is a
**complete** LZMA stream carrying its own 13-byte header with its own
declared length - not a slice of one long stream, which could not be
decoded standing alone.

The sequence puts all the bands inside **one** job:

```
0x12  check device
0x13  start print
      for each band:
0x5c    announce this band's compressed length
        stream it in 64-byte reports
0x10    buffer full: length, speed 60
        poll until buffer_full clears
      poll until printing clears
```

One `0x13` for the job rather than one per band - a job per band would
print each strip on its own label. The `buffer_full` flag is doing real
work here for the first time: with a single buffer it passed straight
through, and between bands it is what says the device is ready for more.

**Tried on hardware and it did not print.** All four buffers were
accepted - no error flag at any point, which is itself a result: the
device does not object to a buffer of this size. It then went silent, as
above. So either the sequence is incomplete, or the premise it was built
on is wrong. See the correction above.

### `pages printed` is not a completion signal

It read **0** immediately after a label came out. The reliable signal is
the status flags clearing: `busy` and `printing` drop and nothing is left
but `usb_connected`.

### Bit polarity, settled without a label

The captured image is **99.87% zero** and the label it printed is
near-blank with a few lines of text. So **a set bit is a black dot** and
zero is bare stock - the same sense as ZPL and ESC/POS, and the opposite
of TSPL on the G4.

That also explains an early experiment: `--invert` on a 7.5% pattern asks
for a **92.5% black** label, and the device pulled the media back rather
than print it. `--invert` is wrong for this device; the default is right.

### Still not determined

- **Row order and origin.** 48 bytes per row and 384 dots across are
  confirmed from the captured image, but which end of the row is dot 0
  and which end of the roll is row 0 are not. `--style sparse` is drawn
  asymmetrically so one printed label answers both.
- **The exact per-buffer ceiling.** Somewhere in (419, 695] bytes. Only
  bisection settles it, at a label per attempt, and it only moves a
  default - so it has been left alone.
- **Whether the multi-buffer sequence is right.** Untested on hardware.

## If implementing

Existing pieces in this repo that carry over:

- `printers.render_bitmap()` already produces packed 1-bit rows from a PDF
  at a given dpi, including the right-edge padding handling that took real
  hardware to get right on the G4.
- `printers._write_raw()`'s discipline — build the whole job in memory,
  write it in one go — applies, though here the unit is a 64-byte report
  rather than one large write.
- `udev/99-supvan-t50m.rules` makes `/dev/hidraw0` writable by the `lp`
  group.

Start with a status poll (`0x11`) and decode the reply against the table
above. It moves no paper, proves the transport end to end, and tells you
immediately whether the leading `0x00` byte is right.
