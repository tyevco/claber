# CLAUDE.md

Context for working on this repo. Read this before changing anything.

## What this is

A Raspberry Pi service for a two-person household selling on Facebook
Marketplace. It watches a Gmail inbox for Marketplace shipping-label
emails, records each sale in SQLite, converts the letter-size label PDF
to exactly 4x6in, prints it on a USB thermal printer, and mirrors
everything into a Google Sheet with sell-through analytics.

The user is technically capable and running this on real orders. Broken
output means a parcel does not ship on time, so correctness beats
cleverness.

## Run it

```bash
pip install -e ".[dev,sheets]"
pytest                                     # the whole suite should pass
pytest tests/test_mplabel.py::test_output_is_exactly_4x6   # one test
pytest -k tspl                             # printer-language regressions
python -m mplabel --help
python -m mplabel file tests/fixtures/label_sample.pdf   # no config needed
```

There is no linter, formatter or type checker configured, and no CI.
`pytest` is the entire gate - one test file, `tests/test_mplabel.py`.
Do not add tooling without asking; the Pi dependency list is kept short
on purpose.

Config resolution order: `MPLABEL_<KEY>` env var, then
`/etc/mplabel.conf`, then `~/.config/mplabel.conf`, then `DEFAULTS` in
`cli.py`. Two traps in that chain: it stops at the **first** config file
that exists, so `~/.config/mplabel.conf` is never read while
`/etc/mplabel.conf` is there - values do not merge; and the file needs its
`[mplabel]` section header, without which the parser finds nothing and
silently falls back to `DEFAULTS`, which looks exactly like an empty
config.

## The subcommands

All are `python -m mplabel <cmd>` (installed as `mplabel`). Grouped by
what they touch, because that is what decides whether they are safe to
run against a real database.

| Reads mail | |
|---|---|
| `check` | poll once, record, do **not** print |
| `run [--loop]` | poll and print; `--loop` is what systemd runs |
| `scan [--limit N]` | survey Facebook subjects, change nothing. Feeds open work #2 |
| `backfill [--limit N] [--restart]` | classify old mail into `mail_events` |

| Prints | |
|---|---|
| `file <pdf> [-o] [--rotate] [--print] [--code NNN]` | convert one PDF. Needs no config and no DB |
| `probe` | printers, USB devices, IEEE-1284 id |
| `selftest` | tiny text-only TSPL label |
| `inventory-label --code X [--qr\|--marker] [--size WxH[in]] [--preview PNG]` | draw one inventory label and show what the label maker would burn. `--size 4x1in` for a shelf label. No DB |
| `supvan-probe [--device] [--deep]` | status of the 48mm inventory label maker. Reads only - moves no paper. `--deep` also sends the other read-only commands and shows their raw replies |
| `test-print` | reprint the newest label |
| `reprint <ref>` | reprint one |
| `pending [--since] [--all] [--dry-run]` | labels recorded but never printed; today only by default |

| Database only | |
|---|---|
| `list` / `stats` / `ship <ref>` | outstanding orders, analytics, mark shipped |
| `cancel <ref>` | the buyer pulled out; not a sale, and the parcel code is freed |
| `verify` | do archived labels still match their sales |
| `import <path> --format dyi\|csv\|saved [--state ...]` | listings from a file |
| `inventory [-o F] [--state S] [--all]` | CSV of inventory labels for the label maker |
| `sheets [--dry-run]` | push to Google Sheets |

`probe`, `selftest`, `supvan-probe` and `file` run above `connect_db` in
`main()` - see the note below on why.

## Deploying to the Pi

`install_pi.sh` copies `src/` into `/opt/mplabel` and does a
**non-editable** install, so the running code is in the venv's
site-packages, not in the git checkout. Pulling in `~/claber` changes
nothing on its own:

```bash
cd ~/claber && git pull
/opt/mplabel/venv/bin/pip install --force-reinstall --no-deps ~/claber
sudo systemctl restart mplabel
```

`--force-reinstall` is not optional. The version in `pyproject.toml` never
moves, so pip sees `mplabel 0.1.0` already installed and skips - which
means a re-run of `install_pi.sh` used to leave the old code in place while
looking like it had worked. `--no-deps` stops it re-downloading Pillow and
friends.

`/etc/mplabel.conf` is never overwritten by the installer, so after an
update check by hand that any new key is set. The file beats the built-in
default.

## Layout

```
src/mplabel/
  cli.py         argparse entrypoint, config, SQLite schema, poll loop
  mailparse.py   Marketplace email -> dict. stdlib HTMLParser, no bs4
  label.py       letter-size PDF -> exact 4x6, + label-only field extraction
  printers.py    TSPL/ZPL raw backends, CUPS backends, rasteriser, probe
  listings.py    listings schema, subject classification, analytics views
  backfill.py    one-off mailbox survey and historical import
  rs.py          Reed-Solomon over GF(256), encode and decode
  qr.py          a QR encoder, stdlib only, versions 1-10
  marker.py      the shelf marker: a 6x24 band for our own 3-4 char codes
  inventory.py   draws the 48mm inventory label; QR or shelf marker
  static/marker.js  the marker decoder in the browser, a port of marker.py
  savedpage.py   parse a saved Marketplace selling page
  sheets.py      Google Sheets sync via service account
  supvan.py      T50M Pro label maker: HID transport, frames, status
  lzma1.py       LZMA1 encoder, match coded, no end-of-stream marker

tests/fixtures/   synthetic stand-ins; make_label.py regenerates the PDF
mplabel.conf.example, systemd/mplabel.service, udev/99-clabel-g4.rules
install_pi.sh     Pi bootstrap
```

## Data model

One SQLite file, two schemas declared in two modules: `cli.SCHEMA`
owns `sales`; `listings.SCHEMA` owns `listings` and `mail_events`.
Nothing joins them at write time - `listings.link_sales()` reconciles
afterwards.

**Reconciliation is by title as often as by id.** A saved-page import has
no Facebook listing id to work with (the cards do not carry one), and
plenty of label emails carry none either. So a listing with no id is keyed
by `listings.title_key(title)` - `saved:<slug>-<sha1[:8]>` - and
`link_sales` falls back to matching a sale against a listing by normalised
title, creating the row under the same scheme when nothing matches. One
function owns that derivation for both sides; if they ever drift, sales
stop finding their listings and duplicates appear silently beside them.

The digest is not decoration: her titles run long and share their first
sixty characters ("Antique 1900-1915 American Edwardian / Late
Victorian..."), and a plain truncated slug merged two real listings into
one, quietly shrinking the denominator sell-through is measured against.

**Adding a column needs a migration.** `CREATE TABLE IF NOT EXISTS` will
not touch a database that already holds real sales, so `connect_db` carries
a small `PRAGMA table_info` / `ALTER TABLE` loop. Add to that list, not
just to `SCHEMA`, or the column exists only on fresh installs.

`listings.refresh()` is the single rebuild entry point: schema ->
link_sales -> apply_events -> build_views. Analytics are views, not
tables: `v_listing_perf` derives days_to_sell / days_listed /
price_band, and `v_price_band`, `v_monthly` and `v_aging` are built on
top of it. `sheets.TABS` selects from those views by column name, so
renaming a view column breaks the sheet with no test failure.

## Getting her listings out of Marketplace

This is the part that took the most attempts, so the reasoning is worth
keeping.

`savedpage.extract()` reads the JSON Facebook embeds in `<script>` tags.
On a real selling page **that JSON is not there any more** - the cards are
rendered from data that never lands in a parseable script tag, so a save of
the page yields nothing. The working route is `CONSOLE_SNIPPET`
(`python -m mplabel.savedpage --snippet`), pasted into DevTools on
`facebook.com/marketplace/you/selling` after scrolling to the bottom. It
reads the rendered page and downloads clean JSON, which `extract()` also
imports - a file that is itself JSON is parsed directly.

Three things the snippet learned the hard way, each from a real run:

- **Anchor on the price, not on links.** Her own listings do not link to
  `/marketplace/item/<id>` - they open an edit panel - so an href-based
  scan found zero.
- **Climb until the block holds a real title.** Reduced listings show two
  prices; stopping at the first two-line block picked up the struck-through
  original price *as the title*, and rows imported titled `$325.00`.
- **Field labels are chrome.** `Category: Women's clothing & shoes` became a
  title on cards where the real one sat outside the price's block.

`mplabel import --format saved <file> --state sold` forces the state for a
capture taken from one tab. The snippet reads "sold" from a badge inside
each card, but on the Sold tab the tab itself carries that meaning and the
cards may not repeat it - without the override every sold listing imports
as active, which inverts sell-through: the numerator empties while the
denominator grows.

Neither capture carried dates, so `listed_at` is empty and `v_aging` is
empty with it. The DYI export is the only route to those.

## Verified vs assumed

This matters more than anything else in this file. Some of this was
tested against real data; some is inference that has never touched
hardware or a real Facebook account.

| Area | Status |
|---|---|
| Label crop/rotate geometry | **Verified.** Real label: letter page, ink at (90,450)-(522,738) = 432x288pt, text matrix `(0, .76, -.76, 0)` so rotate 90 CW. |
| Dot counts 812x1218 @203dpi | **Verified** by rendering. |
| Label field extraction | **Verified** against the real PDF (tracking, weight, service, recipient address). |
| Email field extraction | **Verified** against one real email, reproduced as a fixture. |
| Marketplace sender address | **Verified.** Real mail comes from `Facebook Marketplace <noreply@marketplace.facebook.com>`. `SENDER_DOMAINS` also allows `facebookmail.com`, which is **ASSUMED** for the non-label notifications. |
| Ship-by year inference | **Verified** by unit test incl. New Year rollover and leap day. Parse with an explicit year; year-less `strptime` defaults to 1900 and loses Feb 29. |
| G4 speaks TSPL | **Verified on the hardware.** `tspl_selftest` rendered correctly — `TSPL OK` in large type, the following lines smaller and each on its own line, i.e. `TEXT 40,80,"4",0,2,2` executed rather than echoed — and a real label then printed through the `tspl` backend. |
| **The IEEE-1284 id lies on this unit** | **Verified.** `probe` reads `MANUFACTURER:Clabel-;COMMAND SET:ESC/POS;MODEL:G4;COMMENT:Impact Printer;ACTIVE COMMAND:ESC/POS;` — every word of which points at ESC/POS, and it is wrong. The same string calls this thermal printer an "Impact Printer", so the descriptor is boilerplate the OEM never edited. An ESC/POS text selftest printed *nothing*, which is what a TSPL parser does with commands it does not recognise. Do not switch backends on the strength of the id; print something first. USB `28e9:02ad`, CUPS sees `usb://Clabel-/G4`. |
| Parcel code placement | **Verified.** The 3-character code renders upright in the header strip above the label's border, top right, clear of the postage indicia, the addresses and the tracking barcode. Checked by rendering for the widest code the alphabet allows (`WWW`) as well as an all-digit one, and both right-align on the same margin. Confirmed on the label output; **not yet** confirmed on a thermal print, where edge margins are tighter, nor scanner-tested. |
| The T50M Pro is a HID device, not a printer | **Verified on the hardware.** USB `1820:207f`, enumerating as a vendor-defined HID pipe (usage page 0xFF00) *plus* a fake CD-ROM holding the Windows installer. No usblp binding, so it has **no /dev/usb/lpN** - writes go to `/dev/hidraw0`. Its report descriptor declares 64-byte input and output reports with **no Report ID**, so a hidraw write is 65 bytes: a leading `0x00` then the 64-byte payload. Bidirectional, so it can be asked for status before anything is printed. `udev/99-supvan-t50m.rules` makes the node group-writable; without it the node is root-only. **`/dev/usb/lp0` is the G4** - do not confuse them. |
| The T50M Pro's command sequence | **Verified on the hardware: a replayed stream printed a label.** `mplabel supvan-test-print --replay <file>` sent 123 bytes captured from the vendor app and the printer produced the label. So the transport, the frames, the `0x5c` announce carrying the compressed length, the `0x10` buffer-full with its second value of 60, and the status polling are all correct - this repo can drive the device. Six frames are pinned byte-for-byte against a USBPcap capture. |
| Generating the bitmap stream | **A label printed.** `inventory-label --code TEST --qr --print` put the QR, the code and the rule on real stock - the first thing this repo has *drawn* that reached paper, after months where only the vendor's own captured bitmap ever would. The print-buffer format (3 x 4096, 14-byte header, checksum, one LZMA stream) is therefore **verified**, as is `lzma1.py`, `calc_speed` and the whole sequence. It came out **mirrored left to right**, which settled the last unknown rather than raising a new one: the printhead reads a line's **bytes last-first**, bits inside each byte untouched. Fixed in `raster_to_column_major`; **the corrected orientation has not itself printed yet**. Still unknown: the feed-axis origin, which a correctly-mirrored print will show at a glance. |
| The T50M Pro's payload protocol | **Verified on the hardware, except the raster's orientation.** `supvan-probe` settled the 65-byte hidraw write with its leading `0x00`, the 8-byte frame with its big-endian `wValue`, and the byte-0 flags. The status reply carries **one leading byte before the flags** (`STATUS_PREFIX_LEN`), which the analysis missed: decoding from offset 0 reported "media not recognised" on a healthy idle printer, and opening the media cover and re-polling showed the byte that moved was the one the offset predicts. Both captures are pinned as tests, as are six command frames from a USBPcap capture. The bulk data is **bare 64-byte reports after the `0x5c` announce** - no wrapper; the Bluetooth capture's `0xbb`/`10 02 aa` framing is RFCOMM's and belongs to that transport only. **The `0x5d` label authentication is not required to print** - the replay never sends it. **Bit polarity is settled without a label**: the captured image is 99.87% zero and printed near-blank, so a set bit is a black dot - the ZPL sense, opposite to TSPL on the G4, and `--invert` is wrong here (it asks for a 92.5% black label, which is what made the media pull back). Still unknown: row order and origin. See `docs/supvan-t50m-protocol.md`. |
| The raw data path works | **Verified on the hardware:** bytes reach `/dev/usb/lp0`, usblp is loaded, the `lp` group permissions are right, paper feeds and marks. If a label comes out wrong from here, suspect the raster or the geometry, not the transport. |
| `fsync` on `/dev/usb/lp0` fails | **Verified on the hardware.** It returns `EINVAL`; the write itself succeeds and the label prints. `_write_raw` treats fsync as best effort — see the note below on why raising there corrupted the printed/not-printed record. |
| `escpos` backend | **UNUSED and unproven.** Written while the id was believed, kept because the job structure is unit-tested and some sibling models really do speak ESC/POS. Nothing it produces has ever printed. Its banding size and trailing form feed are guesses. |
| The QR encoder | **Verified against two independent oracles, never printed.** `qr.py` is hand-written to keep the Pi dependency list short. Its codeword stream is identical to `segno`'s for every version, level and mode in range; all 350 symbols in the sweep decoded correctly through `zxing-cpp`; the Reed-Solomon matches the specification's worked example and the format bits match its published table. Neither library is a dependency - they were the oracle, and pinned matrix digests are what is left of them. **Never read off thermal paper**, where the module size and the burn darkness both matter. |
| The shelf marker | **Round-trips in software, never printed or photographed.** 6x24 modules - one by four - carrying 4 data bytes and 7 Reed-Solomon parity, so any 3 of the 11 can be wrong. The interior is 4x22 = 88 modules and the codeword is exactly 88 bits, so nothing is spare. Reads back clean at all four rotations, under a 2.5px blur, scaled to 40%, with 4% salt-and-pepper noise, and out of the decoded print-buffer payload of a real label at both sizes. **Never read off thermal paper by a real camera**, which is the only test that counts - bleed closes modules up and a phone adds glare, motion and a lens. |
| The browser decoder | **Agrees with the Python reference; never run against a real camera.** `static/marker.js` matches `marker.py` byte for byte on clean and damaged codewords under node. What is untested is everything a phone does: exposure, focus, rolling shutter, and whether the aiming reticle is a usable way to hold a box. |
| The inventory label | **Assembles and round-trips; never printed.** `inventory-label --preview` renders it, builds the real print buffers, decodes them back with every checksum checked, and both the QR and the marker still read out of that payload. Two sizes covered: 48x30mm, and 4x1in - which prints sideways, ten buffers, and is the first label to exercise the tiling properly. What is untested is everything physical: whether 5 dots per QR module survives thermal bleed, whether the text is legible, and **whether the media is 48mm at all** - a 4x1in label assumes stock this printer may not take. |
| TSPL gap value 0.12in | **ASSUMED.** Typical for 4x6 die-cut; not measured on their stock. |
| Facebook subject patterns | **Partly verified** against a real mailbox survey. Seen and handled: `Shipping label for your Marketplace order`, `New Marketplace order for <item>` (the sale itself, arriving before the label), and messages as `<emoji> <name> sent you a message`. The rest of `EVENT_PATTERNS` (listed / renewed / expired / payout / rating) is still **ASSUMED** - none has been seen. |
| The mailbox mixes buying and selling | **Verified.** `You placed an order: <item>`, `Offer submitted: <item>` and `Confirm if you received your order: <item>` are *her purchases*. They carry the **seller's** listing id, so they are classified `purchase`, kept out of the listings table by `BUYER_KINDS`, and their listing id is dropped at record time. Counting them would invent listings that were never for sale and drag sell-through down. |
| DYI export schema | **ASSUMED.** Undocumented and reshuffled by Meta; importer walks for shape rather than assuming paths. |
| Saved-page JSON shape | **ASSUMED.** Field names from public GraphQL modules; fixture is synthetic. |
| `printd` split (`pi-http`) | **Works, but only against a fake device.** A signed job crosses as a ~2KB PDF and is rendered to ~124KB of TSPL on the printd side. Covered: dedup, deadline-refusal, bad signature, surviving a SystemExit from the backend, `/healthz` answering while a print is wedged, `selftest` and `probe --remote` following the backend, Settings reading the printer from printd rather than from a stale local copy, and `reconcile` recovering a print whose acknowledgement was lost. **Never run against the real printer**, and deploying it is gated on the label geometry below. |
| Printer status readback | **UNKNOWN, and it is the experiment worth running.** `mplabel status` asks; nobody has ever tried reading from this unit. If it answers, a failed print becomes visible instead of silent. |
| Google Sheets sync | **UNTESTED against the API.** Only the dry-run payload path is covered. |

When the user reports real-world results, move rows up this table and
tighten the code around what they saw. Do not quietly delete an
"ASSUMED" row because a test passes — the tests use synthetic fixtures
built from the same assumptions.

## Things that will bite you

**Output must be exactly 4.00x6.00in.** Not 4.06. An earlier version
cropped to ink-plus-2pt margin, which is fine through CUPS but 824 dots
wide at 203dpi — wider than the 812-dot print head. The overflow rows
eject a second, near-blank label. `label._snap()` centres the ink in a
nominal-size window instead. `test_output_is_exactly_4x6` guards this.

**Parse the label after rotation, not before.** `extract_text()` on the
source PDF returns every line mirrored (`sIPA` for `USPS APIs`) because
the text is drawn rotated. `cli.process_message` deliberately calls
`extract_label_fields(out_pdf)`.

**Five backends, two ways in.** `printers.BACKENDS` is
`cups-pdf`, `cups-raster`, `zpl`, `tspl`, `escpos` - the last three write
raw bytes to `printer_device`, the first two go through a CUPS
`printer_queue`. Separately, `LANGUAGE_BACKENDS` maps what `probe`
detected to a `printer_backend` value, and it deliberately has no entry
for EPL or PCL: `probe` must name the language plainly rather than
suggest a value `send()` would reject with `Unknown backend`.

**TSPL has the opposite bit polarity to ZPL and ESC/POS.** TSPL prints on
a *clear* bit; ZPL and ESC/POS print on a *set* bit. So
`render_bitmap(invert=True)` for TSPL only, and for TSPL the padding bits
past the right edge must be set to white or you get a black stripe — 812
dots is not a byte boundary, so there are always 4 spare bits per row. See
`test_tspl_and_zpl_bit_polarity_are_opposite`,
`test_escpos_prints_on_a_set_bit_like_zpl` and
`test_escpos_right_edge_padding_is_white`.

**`GAP 0,0` means continuous stock.** On die-cut labels the printer never
finds the label edge and prints creep down the roll. Default is
`GAP 0.12,0`. Configurable via `media_tracking` / `gap_inches`.

**Budget TSPL firmware drops streamed bytes.** Data arriving while the
head is moving is silently discarded, so multi-label jobs lose everything
after the first. `_write_raw` builds the whole job in memory and writes
it with a single unbuffered `os.write`, then sleeps `settle_seconds`.
Do not "optimise" this into a streaming writer.

**`amount_with_offset` is in cents.** Facebook gives price several ways
in the same object. Reading the offset field raw turns $15 into $1500 and
silently corrupts every average in the sheet.
`test_price_offset_not_read_as_dollars` guards it.

**The recipient is the *second* address block on the label.** Picking the
first ships every parcel back to the seller.

**Sheets writes use `value_input_option="RAW"`.** Otherwise Sheets
reinterprets a 22-digit tracking number as a float and mangles it.

**Set `sheets_id`, not `sheets_name`.** `SCOPES` asks only for
`spreadsheets`, but opening a sheet *by name* makes gspread search Drive,
which needs the Drive scope - so `sheets_name` fails with a permission
error even when the key and the sharing are both correct. The id is the
long string in the sheet's URL between `/d/` and `/edit`. And the sheet
must be shared with the service account's `client_email` as Editor: it is
a separate identity, and until it is shared every write is a 403 no matter
how good the key is.

**A printer test must not need the database.** `probe`, `selftest`,
`supvan-probe` and
`file` run above `connect_db` in `main()`, because an unwritable home
directory once stopped a printer test dead - which is the one thing you
want working when nothing else is. `cmd_file` takes no `conn` at all.

**Sell-through is meaningless without prices on unsold listings.** Prices
only reach the DB if the listing email or a saved-page/DYI import carried
one. If `v_aging` shows blank prices, the percentages are lying.

**One label file per email, and never named after the listing.** On real mail `listing_id` and `order_id` parse as **NULL** - 0 of 18 - so the archive name fell back to a timestamp at second resolution, and a batch of labels put three pairs in the same second. Each pair shared one file, so three sales pointed at another buyer's label and one of those printed. The name now prefers the id in Facebook's own attachment name (`label_<id>.pdf`) and always carries a digest of the Message-ID, so it is unique per email and searchable by the id on the PDF.

**The unit of a sale is the order, not the listing.** `already_seen` keys on message_id and order_id; `sales.listing_id` is a plain index, not UNIQUE. A buyer cancels, someone else buys the same item, and Facebook sends a second label email with the same listing_id - which the old unique index and the old listing_id check both silently rejected. `mplabel cancel` closes the dead order without counting it as revenue.

**Check a label still matches its sale before printing it.** `label_belongs_to` re-reads the recipient off the PDF and compares it with the `ship_to` recorded from that same page when the sale was filed; `reprint` refuses on a mismatch, and `mplabel verify` sweeps the archive. This is the backstop for anything that leaves a row pointing at the wrong file - the failure is silent and the consequence is a parcel posted to a stranger.

**Two codes, two lifetimes - do not merge them.** The **parcel** code
(`sales.code`, 3 chars) is about the boxes waiting to go out, so it is
released for reuse the moment a parcel ships. The **inventory** code
(`listings.inventory_code`, 4 chars) is stuck to a thing on a shelf and is
never reused, including after the item sells - recycling it would leave the
label on a box in the loft naming something else. Same alphabet, different
rules; `ensure_inventory_codes` deliberately does not scope its `taken` set
by state, where `allocate_code` deliberately does.

**The device can stop answering entirely, mid-job.** After the last
buffer of a four-buffer job it went silent - no error flag, no reply at
all. `experimental_print` now retries a silent poll rather than treating
it as failure, and sends stop-print on the way out if it stays silent, so
the device is not left half-started. The opening poll still fails fast:
silence there means the device is not there. If `supvan-probe` is silent
too, it needs a power cycle.

**`pages printed` reads 0 on a label that printed.** The counter at `0x30` did not move on a confirmed successful print, so it cannot be used to decide whether a job worked. The reliable signal is the status flags clearing: `busy`/`printing` go away and nothing is left but `usb_connected`.

**`media_seating_error` is this device's only way of saying no.**
It is reported *after* the head has positioned, which reads as a physical
problem, and it is not one: a replayed stream printed on the same roll
minutes later. So it means "job refused" and nothing narrower. Several
labels went on reseating media that was never the problem. Do not read it
as a diagnosis - it is the same answer for every rejection, which is
exactly why isolating one variable per label is the only way through.

**Check every property that moved, not just the one you meant to move.**
`--style scatter` was built to hold ink still and vary stream size, and it
did - but it also took blank rows from 242 to 0, which went unnoticed, and
the conclusion "size is the blocker" was drawn from a comparison that had
two free variables in it. Splitting into four sub-limit buffers then did
not print, which is what a wrong conclusion looks like from the outside.
Before spending a label, tabulate size, ink, blank rows and longest inked
run for the new image against the one known to print, and confirm exactly
one differs.

**The device takes print buffers, not a raster.** A job is a run of
fixed 4096-byte buffers - 14-byte header, checksum, then column-major
LSB-first image data - concatenated and compressed as one LZMA stream.
`supvan.build_job` owns that; `build_print_buffer` and `split_into_buffers`
are the pieces. Send a bare raster and the device answers
`media_seating_error`, which is its only word for "no" and says nothing
about what was wrong. This cost about a week of labels: every hypothesis
before it measured the compressed stream, and the firmware objects to what
is inside it.

**The checksum folds in every 256th byte.** `sum(buf[2:14])` plus the byte
before each 256-byte boundary within the declared data extent. A checksum
over the header alone is a plausible-looking number that the firmware
rejects, and the rejection is the same generic one as everything else.

**Three sizes were blamed and all three were wrong.** 448, then 512, then
report count - each looked settled because size correlated with everything
else that varied, and each was retracted. `split_bitmap`, `MAX_BUFFER_BYTES`
and `--max-buffer` are what is left of them: kept because their mechanics
are tested and the failures stay legible, but they split on *compressed*
size, which is not a thing the device measures. The real split is by
printhead line into 4096-byte buffers and it is automatic. Do not reach
for them.

**The printhead reads the left dot from the low bit.** Everything else in
this codebase packs MSB-first, `printers.render_bitmap` included. A
printhead line and a raster row are the same run of bytes, so the fix is a
bit reversal per byte and no transpose - `raster_to_column_major`. Get it
wrong and the label is mirrored across the head, not refused, so no error
says so.

**Print speed is derived from the compressed size.** `calc_speed(average
compressed bytes per buffer)`, from the vendor's `multiCompression`. The
captured print's `BUF_FULL` carried 60 and this repo sent 60 as a
constant for months; 60 is simply that function's answer for a nearly
blank label. A real label compresses larger and has to print *slower* so
the head has time to heat, and the constant would have been wrong for
every label that mattered.

**The only bitmap that had ever printed was the vendor's own,** and that
sentence turned out to be the whole answer rather than a mystery: theirs
was three valid print buffers and nothing else ever was. Kept here because
the shape of the reasoning is worth remembering - the blunt pattern that
survives every measurement was pointing straight at the cause, while each
number that moved was a proxy for it.

**One word, two meanings, and only one caller noticed.** `build_job`
returns `"buffers"` as a *count* of 4096-byte print buffers inside a
single LZMA stream; `experimental_print` read `"buffers"` as a *list of
separate LZMA streams*. Passing a job straight through died on
`for c, n in 3`. It only bit `inventory-label --print`, because
`supvan-test-print` happened to unpack the job by hand first - so the
suite was green and the failure waited for the hardware. The list is
`"streams"` now, and a test sends a `build_job` dict unmodified.

**`import fcntl` must stay guarded, in every module.** This is the second
time an unguarded one has taken the whole suite down on Windows, which is
where these tests are written. `printers.py` and `cli.py` both carry the
try/except now, `printers.print_lock` warns rather than raises when there
is no flock (a platform without it has no `/dev/usb/lp0` to interlock
against either), and `needs_flock` skips the one test that genuinely
needs it.

**A symmetric round-trip cannot see an orientation error.**
`inventory-label --preview` decodes the real print buffers back and draws
them, which is the right instinct and caught nothing: `decode_job` inverts
with the *same* `raster_to_column_major` the encoder used, and reversing a
line is its own inverse. So the preview rendered perfectly while the paper
came out mirrored. Orientation has to be asserted on an **absolute** bit
position - x=0 goes out in the *last* byte of the line - or read off
paper. The same trap is waiting for the feed-axis origin.

**A silent encoder regression would look exactly like a device fault.**
An encoder that stopped emitting matches would still round-trip through
liblzma perfectly and simply not print, which is a day of chasing the
printer. `test_lzma1_still_beats_a_literal_only_encoding` asserts the size
directly for that reason.

**One variable per label, and say which one before printing.** This
printer has cost more labels to guessing than to testing. The pattern
that keeps repeating: a change is made, it fails, and the failure cannot
be attributed because the change moved several things at once. Generating
a bitmap instead of replaying one changed the encoder *and* the picture,
and the picture differed in ink and in stream size as well. Hence
`--replay` (their bytes), `--reencode` (their image, our encoder) and
`--style sparse` (our encoder, less ink and a smaller stream) - each one
exists to hold something still.

**The label maker is driven directly, but is not yet a printer backend.**
The KATA/SUPVAN T50M Pro is a 48mm consumer label maker that ships with
SUPVAN's own editor. `supvan.py` speaks its protocol - status, the print
sequence, `lzma1.py` for the compressed stream and `build_job` for the
print buffers inside it - and `mplabel supvan-test-print` sends a test
pattern. A replayed vendor stream printed, and so did a captured image
**re-encoded by `lzma1.py`**, so the encoder is settled on hardware. What
was refusing a *generated* image was the payload: a bare raster where the
firmware wanted print buffers. That is fixed and fully unit-tested, but
**no label built by `build_job` has come out of the device yet**, so
nothing renders an inventory label automatically and `print_bitmap` is
called only by `supvan-test-print`, deliberately, one label at a time.

`mplabel inventory` still writes a CSV for the vendor editor and that
route is unaffected. The CSV is **utf-8-sig**, because her titles carry
accents and curly quotes and Excel on Windows reads a plain utf-8 CSV as
mojibake - and whatever the editor shows is what gets printed. It cannot
print 4x6 shipping labels either: 48mm is 384 dots against the 812 the
pipeline emits, so that stays with the G4.

**The all-zero codeword is a valid codeword, and it used to read as
"000".** Reed-Solomon is happy with all zeros - every syndrome is zero -
and `crc8(b"\0\0\0")` is also zero, so a blank picture satisfied every
check in `marker.py` and returned a real code, confidently, from a
photograph of nothing. Marker formats are therefore numbered from **1**,
which makes format 0 unreachable, and `read_grid` refuses anything whose
finder scores under `MIN_FINDER_SCORE`. Blank and noise both score about
22 of 44. Do not renumber the formats back to zero to "tidy" them.

**Reed-Solomon corrects; it does not certify.** A clean return from
`rs.decode` means *some* valid codeword was reached, not the right one.
On the marker's dimensions it refused on all 4000 over-capacity trials
rather than mis-correcting, so this is a guard against something not yet
seen here - but the consequence is a label naming the wrong object, which
is silent, so the payload carries its own CRC and it is re-checked after
correction.

**`marker.read_image` needs a crop, not a label.** The grid is located
from the bounding box of the ink, so a whole label - code, title, price -
stretches that box across everything and samples the marker at the wrong
pitch. `inventory.marker_box()` is where the rectangle comes from, and
the phone app's aiming reticle is how it happens there. The reticle in
`app.css` and `SCAN_BOX` in `app.js` are the same fraction on purpose:
the person lines up the square that `scanTick` actually crops.

**One speck decides the bounding box.** It is a min and a max over every
dark pixel, so a single dust mote in a corner stretches the grid and
every module after it is sampled in the wrong place - 44/44 to 11/44 with
the picture otherwise perfect. `_despeckle` clears dark pixels with fewer
than two dark neighbours, and `_ink_bounds` needs two dark pixels in a
line before it counts it.

**`marker.js` is a port and has to stay one.** The printer writes these
and the phone reads them; a drift between the two ends is a code that
prints and cannot be scanned. `test_marker_js_port_agrees_with_python`
runs the browser file under node against vectors generated from the
Python side, damaged codewords included, because error correction is
exactly where a port diverges quietly. It has already caught one: the
format renumbering above landed in Python and not in JavaScript.

**A served asset missing from `asset_stamp` never reaches the phone.**
It lists the files whose mtime busts the cache. `marker.js` is on that
list; anything else added to `static/` must be too, or the phone goes on
running the copy it has.

**The marker is one by four, and that is a layout decision as much as a
format one.** A square marker took a bite out of the middle of a label
that is mostly words and pushed the title into three cramped lines. A
6x24 band goes *under* the text, which keeps the full width for the code
and the title above it. `_marker_band` places it; it is sized by height
first, because filling the width would make the band a third of a 48mm
label's height and leave the text it captions nowhere to go.

**A rectangle rules out half the orientations before decoding starts.**
A 6x24 grid photographed at 90 degrees is not 6x24, so `read_image`
settles the quarter turns from the ink's own aspect - sampling
transposed when the box is taller than it is wide - and `read_grid` is
left with only the two ways up. Do not put quarter turns back into
`read_grid`; the shape already carries that information.

**Raster order matters more on a strip than it did on a square.** Along
the rows a byte is eight neighbouring modules, so a scratch down the
length damages three of eleven bytes - just inside what the parity
carries. Down the columns it would be one bit from each of eleven,
which is the same damage spread so thin that nothing is recoverable.

**The code font has to be sized on height, not just width.** It was
fitted to the available width while the marker band was taking two
fifths of the height, so the title underneath was pushed *into* the
band - and the marker still read, because the parity absorbed it, which
is exactly how that would have reached paper unnoticed. The title loop
also used to force a line with `max(1, ...)` where there was room for
none. Both are pinned by tests that read the raster rather than trust
the arithmetic.

**The printhead does not turn, so the label size chooses its own
orientation.** The bar is 384 dots - 48mm - and that is the *only* axis
a label can be wide on. A 4x1in shelf label therefore prints with its
1in across the head and its 4in down the feed, which means the drawing
is laid out in reading orientation and rotated a quarter turn at the
end. `inventory.reads_sideways()` says whether that happened; a size
that fits neither way round is refused rather than silently cropped to
its own middle.

**After that rotation the feed axis is the reading orientation's
*width*.** So the feed margin - whose columns the firmware never sends -
has to be inset on left and right rather than top and bottom. Inset the
wrong pair and the ink lands in the dead band: dropped, not printed
small, and nothing reports it. `_geometry` owns which pair, and a test
pins that both ends of the raster stay empty at both sizes.

**The raster is head-width; the label usually is not.** Every printhead
line is 384 dots because the bar is, but a 1in label covers 203 of them
and the rest is bar hanging off the media. `media_box()` is that band,
and the preview crops to it - a preview of the whole raster shows broad
empty margins that read as a badly laid out label and are nothing of the
sort. The media is *centred* under the bar, which is why the band is
centred rather than flush left.

**A 4x1in label is the first one that really exercises buffer tiling.**
813 printhead lines at 84 lines a buffer is ten of them; the 48x30mm
label fits in three and never tests the tiling past the first split.

**The label preview is decoded from the payload, not from the drawing.**
`inventory-label --preview` assembles the real job, then takes it apart
again - decompressing, checking every buffer checksum, reading the
geometry out of the headers - and draws what comes back. Previewing the
source raster instead would show a perfect label for a job the device
was about to refuse, which is the whole failure this project has been
chasing on this printer.

**Ink in the feed margin is dropped, not printed small.** The margin
columns are declared in the print-buffer header and never sent, so
anything drawn there vanishes. The price sat in that dead band and came
out with its bottom sheared off, which reads as a font problem and is
not. `inventory.render_label` insets by `supvan.DEFAULT_MARGIN_DOTS` for
exactly this reason, and a test pins that the margin stays empty.

**The device's buffer checksum is weak, and that is its design.** It
covers the 12 header bytes and then only the byte before each 256-byte
boundary - so most of the image is not covered at all. A valid checksum
says the header is intact and says very little about the picture.

**The QR and the printed characters must not be able to disagree.**
`render_label` derives both from the same `code` argument and there is
no parameter to pass them separately. A label whose two identifiers name
different objects is worse than one with no QR on it, and this is the
kind of thing that only goes wrong once a caller gets convenient.

**The parcel code is a handle, not just a marking.** `reprint` and `ship`
both accept it (`cli.find_sale`), case-insensitively, alongside listing id
/ order id / tracking - it is the only one of those printed on the box, and
`mplabel list` shows it and none of the others. Three characters from
digits and capitals minus **I L O U**, which get misread as 1, 1, 0 and V
on thermal stock; 32^3 is 32768 codes, so collisions among open parcels
never bite. Helvetica letters are not one width - W is nearly twice I - so
`label._text_width` measures the white patch from real advance widths
rather than assuming the digit width, or a code like WWW spills off its own
background.

**A successful write does not mean a label came out.** `_write_raw`
pushes the whole job into the printer's buffer and never reads back, so
out of paper, head open, a jam and a wrong `gap_inches` all look like a
clean print: `mark_printed` sets `printed_at` and the row *leaves* the
Pending query. The one physical failure that loses a parcel is the one
that hides it from the recovery path - and the phone app removes the
last defence, which was a person standing near the printer. `mplabel
status` is the experiment: it asks the printer how it is and reports
whether this unit answers at all. **Whether it does is UNKNOWN** - no
bidirectional read has ever been tried on this hardware. Run it, and
record the answer either way.

**The print lock belongs to whoever writes to the device.** With
`printer_backend = pi-http` the client must *not* hold it: printd runs
on the same Pi over loopback and resolves the same lock file, so a
client holding it while waiting deadlocks against printd trying to take
it - two file descriptions, one flock. Verified: it hung until the
client timed out. `printers.REMOTE_BACKENDS` is what `print_label`
checks, and two tests pin both halves.

**The parcel code is stamped on a copy, never the archive.** `labels/<ref>_4x6.pdf` stays as Facebook sent it; `print_label` stamps a throwaway file on its way to the printer. That is what makes a reprint safe - there is no way to double-stamp, and no stamped/not-stamped flag to keep straight. It also means the ~15 orders already recorded pick up a code the moment they print.

**The page is not stored upright.** `to_4x6` leaves a landscape mediabox with `/Rotate 90`, and the mediabox origin is not (0,0) - it is the crop window on the letter page, e.g. `[90 450 522 738]`. So the printed top-right corner is the page's top-*left*, and text there needs a +90 (CCW) matrix, the same convention the label's own text uses. `label._code_placement` owns that; `test_code_lands_in_the_printed_top_right` settles it by rendering the page and looking, rather than by trusting the arithmetic.

**Catalogued is not printed.** `backfill` records every classified Facebook message in `mail_events`, `shipping_label` included - so a label email is almost always in `mail_events` whether or not it ever reached a printer. `already_recorded` therefore checks `sales` for a label email and `mail_events` for everything else. Conflating them made `poll_once` skip 18 label emails on sight, 15 of which had never printed. `peek_headers` pulls From and Subject alongside Message-ID precisely so this can be decided before the body is downloaded.

**Read state must never gate printing.** Gmail marks *every* message in a conversation read when one is opened. She sold nine items at once, Gmail threaded the nine label emails, and one glance hid eight of them from `(UNSEEN FROM "facebook")` - so eight parcels had no label. `candidate_ids` now searches everything from Facebook within `lookback_days`, and `already_recorded` (message_id, against both `sales` and `mail_events`) is what prevents repeats. Note it does **not** filter on the processed Gmail label either: Gmail's search is thread-aware in places, and labelling one message must not be able to hide its eight siblings.

**The IMAP search is not a sender check.** `poll_once` searches `(UNSEEN FROM "facebook")`, which matches the From header as *text* - a display name is enough to get a message fetched, and anyone can set one. `mailparse.is_from_facebook` is the real gate, and it matches the address domain with a boundary: `noreply@marketplace.facebook.com.example.net` contains `marketplace.facebook.com` but is not Facebook. Every path that turns mail into data - `is_label_email`, `cli.record_event`, `backfill.run` - must call it, because a subject reading "New Marketplace order for <item>" now creates a sold listing.

**A sale is not the same thing as a label.** `is_label_email` keeps only subjects with "label" or "shipping", but the sale itself arrives as `New Marketplace order for <item>`, and a **local pickup sale produces no label email at all**. The poller therefore records a `mail_events` row for any classified non-label Facebook mail before putting it back, and `apply_events` reconciles it by the item name in the subject. Without that the database only ever knew about items that shipped, which is how 25 sold listings sat next to a 3-row `sales` table.

**Unrecognised mail is put back.** `poll_once` searches
`(UNSEEN FROM "facebook")`, and anything failing `is_label_email` - or
raising during processing - is re-marked `-FLAGS \Seen`. Dropping that
silently eats a customer's label email on the next parser bug.

**Email fields outrank label fields.** `process_message` merges
`extract_label_fields()` with `rec.setdefault()`, so the label only
fills blanks. Switching to `update()` lets label text overwrite
known-good values from the email.

## Data handling

Fixtures are synthetic on purpose. Real labels carry a buyer's home
address and the real DB carries customer names and purchases;
`.gitignore` excludes `labels/`, `*.db`, `*_4x6.pdf`, `selling*.html`
and the credential files. `tests/fixtures/make_label.py` regenerates a
geometrically identical label with invented names.

**Never commit a real label, database, saved page, or service-account
key.** If the user pastes one into an issue, work from it but do not add
it to the repo.

## Design decisions worth preserving

- **No scraping.** `facebook.com/robots.txt` disallows it and an
  unauthenticated fetch hits a login wall, so a crawler cannot see her
  history anyway; driving her logged-in session risks the account that
  the whole business runs on. `savedpage.py` gets the same data from a
  manually saved page with zero automated requests. If asked to add a
  scraper, raise this before building it.
- **stdlib HTML parsing.** No BeautifulSoup, to keep the Pi dependency
  list short. Do not add it for convenience. Same rule elsewhere: the
  parcel-code overlay is hand-written PDF bytes using base-14 Helvetica
  rather than promoting **reportlab** from a test-only dependency, and any
  HTTP client should be `urllib` rather than `requests`.
- **Shape-tolerant importers.** DYI and saved-page parsers walk nested
  JSON looking for listing-shaped objects rather than following fixed
  paths, because both formats are undocumented and change without
  notice.
- **Sheets sync is wrapped in try/except in the poll loop.** A Google
  outage must never stop a label printing.
- **Whole-tab replacement, not append.** Sheet is a mirror; re-running is
  idempotent.
- **Idempotency everywhere.** `sales.message_id` is UNIQUE and
  `listing_id` has a unique index, so a re-poll cannot double-print or
  double-count.

## Open work

Roughly in priority order.

0. **USPS tracking is probably not available, and that is a finding, not a
   gap.** The idea was to look up each tracking number and mark the parcel
   shipped on its first scan. USPS tied tracking access to the **Mailer ID
   that bought the postage** on 1 April 2026, and on a Marketplace label
   that MID belongs to Facebook's label provider, not to her - parties
   without it need a signed IP agreement and a monthly fee. The Web Tools
   XML API that used to answer on a bare tracking number was shut down on
   25 January 2026, so there is no legacy fallback. Settle it with one
   OAuth token and one lookup (`apis.usps.com/oauth2/v3/token`, then
   `/tracking/v3/tracking/<number>`) before writing a client; a 403 means
   fall back to deriving status from her mailbox, where every other fact in
   this system already comes from.

1. **Check the printed label against the stock.** TSPL prints, so what is
   left is geometry, not language: does one job advance exactly one
   die-cut label, is the ink centred rather than creeping down the roll
   (that would mean `gap_inches` is wrong for their stock), and do the
   barcodes scan? `printer_darkness` 0-15 and `printer_speed` are the
   knobs. This is the last thing between the pipeline and real parcels.
1b. **Spend one label on the print buffers.** The T50M Pro's payload
   format is settled and unit-tested but has never printed:
   `mplabel supvan-test-print` now builds real print buffers instead of a
   bare raster, and `mplabel inventory-label --code X --qr` draws a real
   label through the same path. Run one. If a label comes out, move
   "Generating the bitmap stream" up the table and the inventory path can
   stop going through the vendor editor. If it does not, `--bare-raster`
   reproduces the old refused shape for comparison and `--style sparse`
   is drawn asymmetrically to settle which end of the roll column 0 is.
   One variable per label, as ever.

   Three things to check on the paper that no test here can reach:
   whether the QR scans off thermal stock at 5 dots per module (it scans
   out of the wire payload, which is not the same thing - bleed closes
   up the modules), whether the **shelf marker band** scans at the 7-9
   dots per module it gets, and whether the wrapped title is legible. All are `--density` knobs
   before they are layout changes. `inventory-label --marker` and
   `--qr` draw the two candidates on the same label size, so one print
   run settles which carrier to keep.

2. **Learn the real email subjects.** `python -m mplabel scan` prints
   unrecognised Facebook subject lines with counts. Add them to
   `listings.EVENT_PATTERNS` — a name and a regex each. Until this is
   done, backfill will find almost nothing beyond shipping labels.
3. **Test the Sheets path against the live API.** Everything up to the
   `gspread` call is covered; the call itself is not.
4. **Validate the saved-page parser on a real save.** The fixture is
   synthetic. Run `python -m mplabel.savedpage <file>` to see raw
   extraction and block counts before trusting it.
5. Multi-page label PDFs are not handled — page 0 only.
6. Only USPS labels are parsed. UPS/FedEx tracking formats differ.
7. `mplabel pending` is the way back for a label that was recorded but
   never printed - a `check` run, or a print that failed at the
   printer. `run` cannot do it: once a message is in `sales` the
   poller skips it on sight, which is what stops a re-poll reprinting
   everything. It defaults to **today only**, because the poll window
   is days wide and older labels may already have been printed and
   posted by hand.

## Style

Plain Python, no framework. Comments explain *why*, especially where
something non-obvious was learned the hard way — those comments are load
bearing, keep them. Docstrings on modules and non-trivial functions.
Tests are regression tests: each one exists because something was
actually wrong. If you fix a bug, add the test first.

Two habits worth keeping in the tests themselves:

- **The `db` fixture builds the real schemas**, `cli.SCHEMA` and
  `listings.SCHEMA`. It used to hand-roll a trimmed `sales` table, which
  drifted until it had no `message_id` - so tests passed against a table
  the code would never meet.
- **Assert on rendered output where geometry matters.** The parcel code's
  placement is checked by rasterising the page and asking which corner
  gained ink, not by trusting the rotation arithmetic. The page is stored
  landscape with `/Rotate 90` and a non-zero mediabox origin, and reasoning
  about that is exactly where a plausible-looking mistake hides.
