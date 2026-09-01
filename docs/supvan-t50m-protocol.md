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

The image is **LZMA compressed** before transfer. Python's standard library
`lzma` module covers this, so no new dependency is required — which matters
here, where the Pi dependency list is deliberately short.

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

One divergence to know about: Python always appends an end-of-stream
marker, and liblzma refuses to read back a stream that declares a size
and carries one. The captured stream has no marker. The consumer is the
printer's decoder, which stops at the declared size, so a stream built
here cannot be verified locally - only on the device.

### The raster

The captured image decompresses to **12288 bytes = 48 bytes per row x 256
rows**, confirming **384 dots** across - 48mm at 203dpi, as arithmetic
suggested. Bit order and polarity are still unconfirmed: rendered either
way the test label is blocks rather than anything recognisable, so it
tells us the geometry but not the sense of a set bit.

### What is still missing, after the capture

With the dictionary at 8192 and the size declared - both taken from the
captured print, both verified in the stream we send - the USB attempt
fails exactly as it did before: accepted, positioned, then
`media_seating_error` after about a second. **So the LZMA header was not
the blocker.** That is worth knowing: it removes the whole compression
question from the list.

The remaining difference from the working print is the **framing of the
bulk data**. Over RFCOMM it travels under its own opcode `0xbb`, with a
marker of `10 02 aa` instead of `10 01 aa` and a two-byte per-image
field; over HID this repo sends bare 64-byte chunks after the `0x5c`
announce. Whether the HID path needs an equivalent wrapper cannot be
inferred from a Bluetooth capture - the two transports frame everything
differently, and only the opcodes are shared.

**That question needs a USB capture** of the vendor application printing:
Wireshark with USBPcap on an x64 Windows machine, filtered to this device
alone. The thing to read is narrow - the reports sent between `0x5c` and
`0x10`. If they carry a header, ours needs the same; if they are bare
LZMA, the transport was right and something else is wrong.

Until then, further attempts cost labels for an ambiguous signal. The
supported route is `mplabel inventory` and the vendor editor.

### What the USB experiment established



`mplabel supvan-test-print` got as far as the device accepting a job and
acting on it, but never to a printed label. What was learned:

- **`0x5c`'s length is the number of bytes about to be streamed**, not the
  uncompressed image size. Announcing 5760 and sending 67 made the device
  stop answering entirely and require a power cycle — it blocks waiting
  for the count it was promised.
- **The job is genuinely accepted.** `0x13` sets `busy` and `printing`,
  and the head performs a positioning move: on one attempt the media
  audibly pulled back.
- **It then ends in `media_seating_error`**, with `printing` clearing
  after roughly a second. That is the device abandoning the job, not
  hanging on the data — and it happened identically across every LZMA
  container and dictionary size tried.
- A stalled attempt leaves the media out of position, so the *next*
  attempt reports a seating error before it can start. Reseat between
  runs.

That the failure is a **media** error, is reached after positioning, and
does not change with the payload, points away from the bitmap encoding
and towards the one step the experiment skips entirely: the label
authentication at `0x5d`. The media itself is readable — `0x30` returns
59 bytes of tag data and no error flags are set at rest — so this looks
like a validation step the firmware requires before committing a job,
rather than unreadable media.

**Before more of this is guessed at**, print one label from the vendor's
own phone application. Nothing here has ever confirmed that this printer
and this roll can produce a label at all, which makes every negative
result ambiguous: a protocol gap and a media problem look identical from
this side.

**Not yet determined**, and needed before anything can print:

- the uncompressed row format — bytes per row, bit order, and whether a set
  bit means a black dot or a white one
- the exact dot width for this model (~384 at 48mm and 203 dpi, but the
  application carries a real constant per model)
- how media width and label length are communicated
- the RFID/label authentication exchange (`0x5d`), and whether printing is
  refused without it

Those live in the vendor's image-encoding module and the media handling,
and are the remaining work for anyone implementing this.

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
