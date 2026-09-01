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
| `0x17` | read revision | |
| `0x30` | return media info | |
| `0x5c` | next frame is bulk | announces an LZMA-compressed bitmap; wValue is its length |
| `0x5d` | set RFID data | label authentication |
| `0xc5` | read firmware revision | |
| `0xc6` | next frame is firmware | firmware update path — **do not send** |

Note the vendor application also uses several large pseudo-opcodes (888,
999, 8888) as internal state markers. Those are not wire values and never
reach the device.

## Status report

The device replies to a status poll with a 64-byte input report. The first
six bytes are meaningful:

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

Two corrections from a real reading (`08 00 00 10 00 00`, idle, no
usable media):

- **Byte 3 bit `0x10` is set and is not in the table above.** Meaning
  unknown; it appears on an otherwise idle device.
- **Byte 2 bit `0x10` was clear while the device was plugged in over
  USB and answering**, so reading it as "USB connected" is doubtful.
  It may mean USB power specifically, or something else entirely.
  Treat that one bit as unverified.

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
