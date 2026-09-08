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

The iOS client is a second gate, on a Mac with Xcode:

```bash
xcodebuild test -project ios/MPLabel.xcodeproj -scheme MPLabel \
    -destination 'platform=iOS Simulator,name=iPhone 17 Pro'   # unit tests
./ios/run-ui-tests.sh                      # UI tests; starts a real server
SIMULATOR="iPhone 16 Pro" ./ios/run-ui-tests.sh
python tests/make_ios_fixtures.py          # regenerate ios/MPLabelTests/Fixtures
```

A release to TestFlight is a tag, and needs no Mac at all - it runs on a
GitHub macOS runner:

```bash
git tag ios-v1.2.0 && git push origin ios-v1.2.0
```

The whole suite runs first, then the archive is signed and uploaded to
App Store Connect. `docs/ios-release.md` has the Apple-side setup - the
API key, the app record, the three secrets - none of which can be done
from this repo.

There is no linter, formatter or type checker configured, and that is
still a decision. `pytest` is the gate for the Python side - one test
file, `tests/test_mplabel.py`, ~520 tests. Do not add tooling without
asking; the Pi dependency list is kept short on purpose.

There **is** CI now, and only because there is a release behind a tag.
`.github/workflows/ci.yml` runs `pytest` on Linux and builds the app
plus `MPLabelTests` on a macOS runner, on every push and pull request.
It asks the two questions a tag is about to assume the answer to and
nothing else - it does not lint or format anything. The UI tests are
deliberately not in it: they boot a simulator and start a real server,
which is two minutes on every push for a question that only matters at
release, and they run in full in the release workflow.

**The Swift fixtures are generated, not written.** `make_ios_fixtures.py`
runs a real server against a temporary database and saves what it
actually answers, because a hand-written fixture encodes the same belief
about `web.py` that the Swift models do - and that belief has been wrong
twice. `test_the_ios_fixtures_are_still_what_the_server_sends` fails when
the server's shape drifts, and pytest is where that is caught, not Xcode.

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
| `supvan-test-print --style ruler [--width W] [--height H]` | the calibration target: scales on both axes in dots, the last dot's number at each far end, an inset comb to measure a clipped edge, and a feed arrow. Moves paper |
| `supvan-test-print --style edges` | the edge test: eight bars per side, 8 dots apart, each a different length so it names itself without a number beside it. Reads where each edge starts printing and nothing else. Moves paper |
| `shelf-tag --code XXX [--name N] [--marker\|--qr] [--size WxH[in]] [--preview PNG] [--print]` | a tag for a shelf, bin or area. Code big, name under it, marker beside it. **Three** characters, where an item code is four. No DB |
| `config [--all]` | the resolved config, each key marked `default`/`file`/`env`, secrets redacted, and **which** file was read. No DB |
| `supvan-probe [--device] [--deep]` | status of the 48mm inventory label maker. Reads only - moves no paper. `--deep` also sends the other read-only commands and shows their raw replies |
| `test-print` | reprint the newest label |
| `reprint <ref>` | reprint one |
| `pending [--since] [--all] [--dry-run]` | labels recorded but never printed; today only by default |

| Database only | |
|---|---|
| `list` / `stats` / `ship <ref>` | outstanding orders, analytics, mark shipped |
| `bin new\|ls\|show\|rename\|put\|rm` | the places things live. `new` mints the code and `--print` puts its tag on the shelf; `put` takes the 4-char inventory code off the item's own label |
| `cancel <ref>` | the buyer pulled out; not a sale, and the parcel code is freed |
| `verify` | do archived labels still match their sales |
| `import <path> --format dyi\|csv\|saved [--state ...]` | listings from a file |
| `inventory [-o F] [--state S] [--all]` | CSV of inventory labels for the label maker |
| `sheets [--dry-run]` | push to Google Sheets |

| Servers and recovery | |
|---|---|
| `serve [--bind] [--port]` | the phone app's HTTP server (`web.py`). Binds loopback by default; the intended route in is a Cloudflare tunnel |
| `printd [--bind] [--port]` | the print side of the split (`printd.py`). Owns both printers, knows nothing about orders. Refuses to start without `printd_secret`, exit **78** |
| `passwd` | set the web password (scrypt), written to the config file |
| `status` | ask the G4 how it is. **It does not answer** - kept as the record of that, see the table below |
| `reconcile [--since JOB] [--dry-run]` | reconcile the local DB against printd's journal. The recovery path for an ambiguous print |
| `notify [--dry-run]` | say the three things that earn a notification. `--dry-run` prints the whole decision and records nothing, so it says the same thing twice. Exit **78** if push is not configured |

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

**That route updates the package and nothing else.** systemd units and
udev rules are written by `install_pi.sh`, so a unit added to the repo -
`mplabel-printd.service` was the first - never reaches the Pi through a
pull and a pip install. It presents as `Failed to enable unit:
mplabel-printd.service does not exist`, which reads like a missing file in
the repo rather than a deployment step nobody ran.

Re-run the installer when anything outside `src/` changes:

```bash
cd ~/claber && sudo ./install_pi.sh
```

It is safe to re-run: it does not overwrite `/etc/mplabel.conf`, does not
enable or start anything, and the apt and venv steps are idempotent.

`/etc/mplabel.conf` is never overwritten by the installer, so after an
update check by hand that any new key is set. The file beats the built-in
default.

**The sourcing half needs the installer, not just a pip install.** It
writes photographs to `<data>/photos/`, and that directory is created by
`install_pi.sh` so it is owned by the service user rather than by
whoever happens to upload first. A pull and a `pip install` alone leave
the routes present and the first upload failing on a directory it cannot
create. So:

```bash
cd ~/claber && git pull
sudo ./install_pi.sh                      # photos/ - not optional
/opt/mplabel/venv/bin/pip install --force-reinstall --no-deps ~/claber
sudo systemctl restart mplabel mplabel-web
```

`mplabel-web` is the unit the phone talks to and it is **installed but
not enabled** by the installer, like `printd` - `systemctl enable --now
mplabel-web` is a decision, not a step. Check the deploy landed by
asking the server rather than by trusting the pull:

```bash
curl -fsS -X POST localhost:8080/api/login \
    -H 'Content-Type: application/json' -H 'X-Mplabel: 1' \
    -d '{"password": "..."}'               # -> {"token": ...}
curl -fsS localhost:8080/api/v1/trips -H "Authorization: Bearer $TOKEN" \
    -H 'X-Mplabel: 1'                      # 404 here means old code
ls -ld ~/marketplace/photos                # must exist, owned by the service user
```

A 404 on `/api/v1/trips` is the signature of the package having updated
and the process not having restarted - the same class of failure as the
version in `pyproject.toml` never moving.

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
  web.py         the phone app's server: stdlib http.server, scrypt +
                 signed tokens, the PWA and the /api/v1 surface
  printd.py      the print side of the split: /print, /print-tag,
                 /printed, HMAC, spool, durable journal
  build.py       what code is actually running - install_pi.sh writes
                 _build.py beside it, so a checkout says "checkout"
  static/        the PWA: index.html, app.js, app.css, manifest, icons,
                 and marker.js - the marker decoder, a port of marker.py

tests/test_mplabel.py     the whole Python suite, one file
tests/fixtures/           synthetic stand-ins; make_label.py regenerates the PDF
tests/make_ios_fixtures.py  captures real server payloads for the Swift tests
ios/                      the native client; see ios/README.md and
                          docs/ios-handoff.md for what the Windows box
                          could not verify
  notify.py      push: the three things that earn one, APNs via curl
                 and openssl rather than two large packages
  shopping.py    the aisle: candidates, the receipt read as lines, and a
                 proposal that never writes a cost by itself

docs/                     ios-handoff, ios-release, notifications,
                          split-architecture, supvan-t50m-protocol,
                          phone-access, phase2-hardware-checklist,
                          ui-design-prompt
.github/workflows/        ci.yml (every push), ios-release.yml (a tag)
.github/scripts/          select-xcode, version, check-archive - the
                          parts of the release with reasoning in them
ios/ExportOptions.plist   how -exportArchive turns the archive into an
                          upload
mplabel.conf.example, systemd/{mplabel,mplabel-web,mplabel-printd}.service
udev/99-clabel-g4.rules, udev/99-supvan-t50m.rules
install_pi.sh     Pi bootstrap
```

## Data model

One SQLite file, two schemas declared in two modules: `cli.SCHEMA`
owns `sales`; `listings.SCHEMA` owns `listings`, `mail_events` and
`bins`. Nothing joins `sales` to `listings` at write time -
`listings.link_sales()` reconciles afterwards.

**One foreign key, deliberately, and `PRAGMA foreign_keys=ON` in
`connect_db` so it is a constraint rather than a comment.** The key is
`listings.bin_code -> bins.code`, and it is there because a bin's
identity is *minted by this system*: there is a real key to point at.
`sales -> listings` has no key and gets none - a saved-page import
carries no Facebook listing id at all, so an FK there would mean
inventing a key the source data does not have, which is exactly the
failure `title_key` exists to avoid. Normalise where there is a genuine
key; do not pretend elsewhere.

Note the pragma is per *connection* and off by default. Anything that
opens the database without it - a test fixture, a one-off script - runs
against a schema where `REFERENCES` does nothing, and a dangling-bin bug
would pass there and fail on the Pi. The `db` fixture sets it for that
reason.

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

**An index on a migrated column needs one too.** `executescript(SCHEMA)`
runs *before* the `ALTER TABLE` loop, so a `CREATE INDEX ... ON
listings(bin_code)` sitting in `SCHEMA` fails on any database that predates
the column - and it fails inside `connect_db`, so it takes down every
command rather than just the new feature. `listings.POST_MIGRATION_INDEXES`
runs after the loop. Caught by the migration test, which is why that test
exists.

**A migrated database and a fresh one can disagree about constraints.**
`ALTER TABLE ... ADD COLUMN bin_code TEXT` gets **no foreign key**, while
the identical column in `SCHEMA` gets one - so an upgraded database and a
new one end up with different rules, and every test passes either way
because the `db` fixture builds from SCHEMA. That is what shipped: the
Pi's `bin_code` is unconstrained. The decl in `MIGRATIONS` now carries
the REFERENCES clause and
`test_a_migrated_database_has_the_same_foreign_keys_as_a_fresh_one`
compares the two directly. SQLite cannot add a constraint to an existing
column without rebuilding the table, so a database that already migrated
keeps the loose one.

**`era` is free text, and that is the point.** Not a year and not a
range of years: her titles say "Antique 1900-1915 American Edwardian",
which is a period, a guess and a selling point at once, and an integer
column would force a precision the object does not have. Nothing
computes on it. It is the newest column, so it is also the one that
proves the migration loop still runs.

**Adding a column needs a migration.** `CREATE TABLE IF NOT EXISTS` will
not touch a database that already holds real sales, so `connect_db` carries
a small `PRAGMA table_info` / `ALTER TABLE` loop. Add to that list, not
just to `SCHEMA`, or the column exists only on fresh installs.

**Cost basis enters through the sourcing half, and nowhere else.**
`trips`, `photos` and `listings.paid` had a schema and no API for a
while, which is why `v_listing_perf.margin` and `v_monthly.net` have
always been null: the columns compute correctly and nothing could fill
them. `/api/trips`, `/api/photos` and `POST /api/inventory` are that
route now. Two things in it are load bearing. A hand-added item is keyed
with `listings.title_key(title)`, the same derivation the saved-page
import uses - a local pickup produces no label email, so the sale
arrives later knowing only the title, and a manual item under any other
scheme would sit *beside* its own sale instead of being it. And a trip's
`unassigned` is null rather than zero when `receipt_total` is unknown,
because "nothing left to attribute" and "we never recorded what the till
said" are different answers and triage chases one of them.

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
| Generating the bitmap stream | **Verified end to end, right way round.** `inventory-label --code TEST --qr --print` printed correctly - QR left, code top right, matching the preview - so the print-buffer format, `lzma1.py`, `calc_speed`, the sequence **and the line order** are all settled. The first attempt came out mirrored, which settled the last one: the printhead reads a line's **bytes last-first**, bits inside each byte untouched. Still unknown: the printable inset at each edge, and which end of the label is fed first - both read off `--style ruler`. |
| The T50M Pro's payload protocol | **Verified on the hardware, except the raster's orientation.** `supvan-probe` settled the 65-byte hidraw write with its leading `0x00`, the 8-byte frame with its big-endian `wValue`, and the byte-0 flags. The status reply carries **one leading byte before the flags** (`STATUS_PREFIX_LEN`), which the analysis missed: decoding from offset 0 reported "media not recognised" on a healthy idle printer, and opening the media cover and re-polling showed the byte that moved was the one the offset predicts. Both captures are pinned as tests, as are six command frames from a USBPcap capture. The bulk data is **bare 64-byte reports after the `0x5c` announce** - no wrapper; the Bluetooth capture's `0xbb`/`10 02 aa` framing is RFCOMM's and belongs to that transport only. **The `0x5d` label authentication is not required to print** - the replay never sends it. **Bit polarity is settled without a label**: the captured image is 99.87% zero and printed near-blank, so a set bit is a black dot - the ZPL sense, opposite to TSPL on the G4, and `--invert` is wrong here (it asks for a 92.5% black label, which is what made the media pull back). Still unknown: row order and origin. See `docs/supvan-t50m-protocol.md`. |
| The raw data path works | **Verified on the hardware:** bytes reach `/dev/usb/lp0`, usblp is loaded, the `lp` group permissions are right, paper feeds and marks. If a label comes out wrong from here, suspect the raster or the geometry, not the transport. |
| `fsync` on `/dev/usb/lp0` fails | **Verified on the hardware.** It returns `EINVAL`; the write itself succeeds and the label prints. `_write_raw` treats fsync as best effort — see the note below on why raising there corrupted the printed/not-printed record. |
| `escpos` backend | **UNUSED and unproven.** Written while the id was believed, kept because the job structure is unit-tested and some sibling models really do speak ESC/POS. Nothing it produces has ever printed. Its banding size and trailing form feed are guesses. |
| The QR encoder | **Verified in software and now off thermal paper.** `qr.py` is hand-written to keep the Pi dependency list short. Its codeword stream is identical to `segno`'s for every version, level and mode in range; all 350 symbols in the sweep decoded correctly through `zxing-cpp`; the Reed-Solomon matches the specification's worked example and the format bits match its published table. Neither library is a dependency - they were the oracle, and pinned matrix digests are what is left of them. **A printed `inventory-label --qr` then read first time in the iPhone's own Camera app** - so the module size at 5 dots survives thermal bleed, and Apple's decoder accepts what this encoder emits. That was the open physical question and it is closed. Measured on the loaded roll at the default size and density; a much smaller label or a lighter burn is a new question. |
| The shelf marker | **Round-trips in software, never printed or photographed - and now largely moot.** 6x24 modules - one by four - carrying 4 data bytes and 7 Reed-Solomon parity, so any 3 of the 11 can be wrong. The interior is 4x22 = 88 modules and the codeword is exactly 88 bits, so nothing is spare. Reads back clean at all four rotations, under a 2.5px blur, scaled to 40%, with 4% salt-and-pepper noise, and out of the decoded print-buffer payload of a real label at both sizes. It exists because a QR at this size might not have read off thermal. **It did read**, so the marker's reason for existing is gone: it buys bigger modules at the cost of being readable by nothing but our own decoder. Do not port it to Swift; see the note below. |
| The browser decoder | **Agrees with the Python reference; never run against a real camera.** `static/marker.js` matches `marker.py` byte for byte on clean and damaged codewords under node. What is untested is everything a phone does: exposure, focus, rolling shutter, and whether the aiming reticle is a usable way to hold a box. |
| The inventory label | **Prints, and the printable window is measured.** `--style edges` settled it: the left **40** dots and the right **32** never reach the paper, leaving **312 (39mm), not 384**, and the window is **not centred** - unequal insets mean the media sits off-centre under the head rather than the head being narrow. Top and bottom lose nothing. The layout was a symmetric 12-dot guess before, and it cost a real failure: the QR was drawn from x=22, lost its left finder column, and **did not scan** while looking intact in a photograph. `_geometry` now lays out inside the measured window. Measured on one roll; other stock will differ and the edge test is how to find out. **Not yet reprinted against the new window.** |
| TSPL gap value 0.12in | **Verified on the hardware and on the stock.** A week of production parcels, plus three deliberate labels in a row landing in the same place on their die-cut - no creep, no blank label between them, one job one label. The value was a guess taken from typical 4x6 die-cut; it happens to be right for this roll. A different roll is a different number, and the three-in-a-row print is how to check. |
| Facebook subject patterns | **Partly verified** against a real mailbox survey. Seen and handled: `Shipping label for your Marketplace order`, `New Marketplace order for <item>` (the sale itself, arriving before the label), and messages as `<emoji> <name> sent you a message`. The rest of `EVENT_PATTERNS` (listed / renewed / expired / payout / rating) is still **ASSUMED** - none has been seen. |
| The mailbox mixes buying and selling | **Verified.** `You placed an order: <item>`, `Offer submitted: <item>` and `Confirm if you received your order: <item>` are *her purchases*. They carry the **seller's** listing id, so they are classified `purchase`, kept out of the listings table by `BUYER_KINDS`, and their listing id is dropped at record time. Counting them would invent listings that were never for sale and drag sell-through down. |
| DYI export schema | **ASSUMED.** Undocumented and reshuffled by Meta; importer walks for shape rather than assuming paths. |
| Saved-page JSON shape | **ASSUMED.** Field names from public GraphQL modules; fixture is synthetic. |
| `printd` split (`pi-http`) | **Verified on the hardware, over loopback.** Both printers driven over HTTP: a 4x6 through `/print` and an inventory label through `/print-tag`, each journaled with the right `kind` and `outcome`, and the printed 4x6 indistinguishable from a `tspl` one. So the transport, the HMAC, the spool, the deadline, the journal and both device paths are all real now. **Not yet run off loopback** - that is a bind address and a mesh VPN. |
| The native iOS client | **Builds, runs against the real server, and its eleven UI journeys now execute.** Xcode compiles it, the simulator launches it, and it reads her actual orders, listings and bins off the Pi through a cloudflared tunnel - so the bearer token, the `/api/v1` prefix, every Codable shape and the whole HTTPS path are confirmed on real data rather than a fixture. `./ios/run-ui-tests.sh` is green: 11/11 against a real `mplabel serve`, plus 24 Swift unit tests. Before that the runner had never executed a single assertion - all eight skipped, because `TEST_RUNNER_*` was being passed as a build setting - so everything the UI tests covered was unproven and two of them were in fact wrong. Three things are still **not** verified, all of them needing a real device: the simulator has no camera, so neither `ScanView` - the entire reason this target exists rather than a web page - nor `CaptureView`'s `AVCapturePhotoOutput` has ever seen one; and nothing has been printed from it, which is the one action that spends physical stock. |
| The on-device model | **Verified in the simulator, on real generations.** `FoundationModels` reports `available` and both halves run: the text draft (iOS 26) and the image path (iOS 27), which decoded straight into the `@Generable` type and correctly left `era` and `condition` **empty** on a picture it could not place. So the API, the guided decode and the availability handling are real rather than compiled. **Not** run on the phone, and the model there is the same size but not the same silicon. Nothing about the *quality* of a suggestion is verified - see the two findings below, both of which were measured rather than reasoned. |
| Printer status readback | **Answered on the hardware: it does not.** `mplabel status` got no reply within 0.5s to either query - the G4 is write-only. That is a finding, not a gap, and it is load bearing: **a failed print cannot be detected in software**, so printing is at-least-once and the paper is the only source of truth. `printd` cannot pre-check paper and must not pretend to; a timed-out print stays irreducibly ambiguous. That ambiguity is exactly what the durable journal, `GET /printed` and `mplabel reconcile` exist to convert from "go and look" into a query - which raises their value rather than lowering it. |
| **No email carries the postage charge** | **Verified from the real label email.** It is a *prepaid* label - Facebook pays the carrier and takes it out of the payout - so the one document this system reliably receives says what the parcel weighs and what service it went by, and not what it cost. A test pins that the fixture has no charge in it, because the temptation is to write a parser for a number that is not there. The payout email is the only plausible carrier and **none has ever been seen**, so whether one exists is still open: `mplabel scan` against the real mailbox is what settles it. Until then every figure is typed by a person, and `listings.estimate_postage` derives one only from parcels whose charge she actually confirmed - returning nothing at all when there is no basis, rather than a number that would be indistinguishable from a measured one a week later. |
| Google Sheets sync | **UNTESTED against the API.** Only the dry-run payload path is covered. |
| The release workflow | **UNRUN.** Every piece of it is a command that works on a Mac, and none of it has been executed once - not the runner label, not cloud signing, not the upload. The first tag is the experiment. What is checked in software: `version.sh` against good and bad tags, both workflows' shell blocks parse, and `check-archive.sh` refuses XcodeGen's placeholder version numbers. What cannot be: whether the API key's role is sufficient, whether `macos-26` has an iOS 26 SDK today, and whether App Store Connect accepts a three-part build number of this shape. |

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

**A label longer than the die-cut stock prints across several of them.**
`shelf-tag` defaulted to 4x1in, which reads better across a room and is
101.6mm down the feed; on the 30mm stock in the machine it printed over
three and a bit labels, and the one that came back had the marker on it
and nothing else - which reads as a broken layout and is not. Both label
commands now print the feed length **in millimetres**, because that is
the number that has to match the paper, and the default is the stock that
is actually loaded.

**Taking the print lock twice deadlocks against itself.** `print_lock`
opens the lock file fresh on every call, so a second call is a different
open file description - and `flock` conflicts between descriptions **even
inside one process**. printd held the hidraw node via its own `_Device`,
`print_tag_local` took the same lock again, and the request blocked for
ever with the device held and the tag gate shut: `tag_printing_for: 77.8`
on a printer that answered `supvan-probe` immediately.

The 4x6 path already knew this - `cli.print_label` skips the flock when
the backend is in `REMOTE_BACKENDS`, because holding it on both sides
deadlocked on a same-Pi loopback deployment. The tag path reintroduced
the same bug against the second device. **Exactly one thing takes the
lock per job**, and a test counts it, because there is no `flock` on the
platform the tests run on and the deadlock itself cannot be reproduced
there.

**A bounded wait under a deadline, or the deadline means nothing.**
`printd` bounds acquiring its gate with the caller's
`X-MPLabel-Deadline` and then took `print_lock` underneath it with **no
timeout at all** - so a lock nobody released stalled the request past
that deadline with the device held, which is the same "prints to an
empty room" failure the deadline exists to prevent, one layer down.
`print_lock` takes a `timeout` now (polled `LOCK_EX | LOCK_NB`, because
`SIGALRM` is process-wide and printd is threaded), `_Device` passes what
is *left* of the budget after the gate, and failing inside it is
"printer busy" - the answer the caller already handles - rather than a
socket held open.

The CLI still passes no timeout, deliberately: a person at a terminal
would rather queue behind the poller than be refused. And a *stale* lock
is not a thing that can happen - flock is held by an open file
description, so the kernel drops it when a killed process's descriptors
close. What survives a kill is an empty file that locks nobody out.

**A configuration refusal must not be retried.** `printd` will not start
without `printd_secret` - a print request is a physical action and it
will not accept unsigned jobs - but `Restart=always` turned that into a
unit flapping every ten seconds for ever, with the one line saying what
was wrong buried under systemd noise. Observed at restart counter 11.
Config refusals now exit **78** (`EX_CONFIG`), and the unit carries
`RestartPreventExitStatus=78`, so a permanent error stays dead where it
can be seen. Both halves are needed: the exit code alone does nothing
without the unit honouring it, and a test asserts the unit still does.

**The G4 is write-only, so `printd` must never claim a print it cannot
confirm.** No readback means no paper-out pre-check and no way to tell a
timed-out request from a printed label. Everything downstream follows
from that: `printd` records a job in the journal **after** `_write_raw`
returns and not before, `print_pi_http` deliberately does not retry, and
its unreachable message says "may or may not have printed - ask it with
GET /printed" rather than guessing. `mplabel reconcile` is the recovery
path. Do not add a retry, and do not let a 409 read as a fresh print.

**The phone app's token is a bearer token, and `/api/v1` is the name
that will not move.** `issue_token`/`valid_token` were always stateless
and signed - a cookie was just how a browser carries one - so `authed()`
takes `Authorization: Bearer` as well, and `/api/login` returns the token
in the body beside the `Set-Cookie`. A native client has no cookie jar
worth the name. `/api/v1/...` aliases the whole surface in the
dispatcher, auth and CSRF header rule included; the PWA ships with this
server and can change in the same commit as a route, an app on a phone
cannot.

**The native app reads QR and nothing else.** `marker.py` and
`marker.js` are pinned byte-for-byte by a node test and 137 assertions;
a Swift port would get none of that harness, and it would be a third
implementation of a format only we can read. The question was whether a
QR survives thermal at 5 dots per module, because
`VNDetectBarcodesRequest` reads those with Apple's own decoder for free.
**It does** - a printed label read first time in the stock Camera app.
So the marker does not go to iOS. It stays in the PWA, which already
has a working decoder and costs nothing to leave alone, and
`inventory-label --marker` stays for the same reason; but nothing new
should be built on it, and printing `--qr` is the default worth
preferring on anything a phone is meant to read.

**Both printers are behind `printd`, and the label maker sends a *spec*
not a raster.** `tag_backend` picks `supvan` (the local hidraw node) or
`pi-http` (`POST /print-tag`). What crosses is what to put on the label -
code, title, name, price - and never the roll: `PRINTABLE_LEFT_DOTS`,
`PRINTABLE_RIGHT_DOTS`, `--density` and the label size all describe the
paper in the machine, so they come from the host that can see it.
`--size`/`--density` default to **None** for exactly this reason, so
"the operator typed it" is distinguishable from "the default fired". The
compressed blob is self-contained *because* geometry and density are
baked into its buffer headers - which is what makes it the wrong thing
to send.

**`kind` travels inside the signed body, never a header.** The HMAC
covers the job id and a digest of the body and nothing else - not the
path, not the headers - so routing the choice of *physical device* on
anything unsigned would let a signed body be aimed at a printer its
signer never chose. Cross-posting then fails on body validation alone:
a PDF to `/print-tag` is not JSON, a spec to `/print` fails the `%PDF`
guard.

**A stalled tag is a 503, and it is journaled.** `final["stalled"]`
means paper moved and the job never finished. `200 {"printed": false}`
would be discarded by the client's success path and read as a print.
The journal row carries `outcome`, so a retry of the same id is refused
rather than printing again onto media the last attempt left out of
position - and `kind`, so `reconcile` cannot mark a live parcel printed
because a shelf tag came out with a coincidentally matching code.

**A status report is not JSON-serialisable.** `decode_status` puts the
raw bytes in `status["raw"]`, `json.dumps` raises `TypeError`, and
printd's catch-all turns that into a 503 - so a label that printed
perfectly is reported as a failure and the caller prints it again. Same
shape as the fsync/EINVAL incident: the print worked, the bookkeeping
said otherwise. `printers._jsonable_status` hexes anything bytes-shaped.

**A bin's name is not its code, and only the code is scannable.** The
name is what someone writes on a shelf and reads across a room - `B5`,
`FLOOR`, `LOFT, NORTH WALL` - upper-cased and space-collapsed, because
`b5` and `B5 ` are one shelf in the room and two rows in a `GROUP BY`.
It could never *be* a code: those names are longer than three characters
and contain letters the code alphabet leaves out (I, L, O, U, as
misreadable on thermal). So `bins` carries both, and `find_bin` takes
either, because the phone has scanned one and a person has typed the
other.

An earlier version had a free-text `listings.bin` and no table at all,
on the reasoning that naming a bin is typing it. That was wrong in one
specific way: a typo made a shelf, silently, and nothing could tell you
it had. It is now a real reference with `ON DELETE SET NULL` behind an
enforced pragma - see the Data model section.

The design this came from has **no scanner at all** - the camera is for
photographing items. The code is on the tag anyway because it costs
nothing to print and is what an app would read; whether anything ever
reads it is #16/#17's question, not this table's.

**Three characters means a place, four means a thing.** A location code
is 3 and an inventory code is 4, and that is load bearing: the marker's
payload already carries a format bit distinguishing 3-char codes from
4-char ones, so a scanner knows whether it has a shelf or something to
put on the shelf without a prefix character, a separate symbol, or
anything new on the wire. `inventory.normalise_location_code` refuses a
4-character location rather than drawing it, because such a tag would
scan as an item and bin itself. `cli.CODE_LENGTH` is 3 as well, but a
parcel code is stamped as plain text on a 4x6 shipping label and never
goes into a marker, so the two never meet a scanner together.

**A crop and the drawing that made it must come from one function.** The
decoder is handed a box; a box computed from different arithmetic than
the drawing is a marker that reads as noise however good the decoder is.
`_marker_band`/`marker_box` are that pairing for item labels and
`_tag_carrier_placement`/`shelf_marker_box` for shelf tags. Note
`decode_job` returns only the rows the device is *given*, so its row 0 is
the raster's row `margin_top` - indexing it in raster coordinates reads
eight rows low and looks like a marker that does not match.

**Three codes, three lifetimes - do not merge them.** The **parcel**
code (`sales.code`, 3 chars) is about the boxes waiting to go out, so it
is released for reuse the moment a parcel ships. The **inventory** code
(`listings.inventory_code`, 4 chars) is stuck to a thing on a shelf and is
never reused, including after the item sells - recycling it would leave the
label on a box in the loft naming something else. The **bin** code
(`bins.code`, 3 chars) is on a tag stuck to the shelf itself and follows
the inventory rule: `allocate_bin_code` checks against every bin *ever*,
not the ones in use, because the tag outlives the row. Same alphabet,
three sets of rules; `ensure_inventory_codes` and `allocate_bin_code`
deliberately do not scope their `taken` set by state, where
`allocate_code` deliberately does.

A bin is a **name and a code**, and the split is the design. The name -
`FLOOR`, `ATTIC`, `LOFT, NORTH WALL` - is read across a room and can
change; the code is what the tag carries and a phone reads. `rename_bin`
leaves the code alone, so renaming a shelf does not mean reprinting its
tag, and everything in the bin follows the new name for free because the
listings reference the code. Deleting a bin is `ON DELETE SET NULL`, not
a cascade: the shelf being cleared is the reason the things exist, so
they come back out onto no shelf rather than going with it.

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

**A label on a mark is as losable as the mark.** The ruler's edge comb
pairs a 5x5-dot square with its number, and both vanish together when
that column is lost - which is correct, but leaves nothing to read when
the answer is "most of them are gone", and 0.6mm marks photographed at an
angle are hard to call either way. `--style edges` carries no numbers at
all: eight bars a side, each a different length, so any single bar that
survives identifies itself.

**A gauge has to outrange what it measures.** The edge gauge stopped at
32 dots while the reported loss was around 40, so every mark on that side
was gone and it could only say "more than 32". Nested rectangles were
what limited it - past about 32 they cut through the middle of the label
- so the gauge is now four compact combs, one per edge, which reach any
depth. Same class of mistake as the one below, and the same fix: check
that the instrument covers the case it exists for before printing it.

**The first eight and last eight rows are never sent.**
`split_into_buffers` starts the image at `margin_top` and stops
`margin_bottom` short - the firmware feeds blank for both - so on a
240-row label the outer 8 rows at each end do not reach the device at
all. `render_label` already insets by `feed_margin + 4` for this reason;
the first calibration ruler did not, drew its edge rules and every minor
tick in that band, and read on paper as the printer clipping. **An
instrument must not live in the region it is measuring** - the graduations
sit inboard now, and the edges get a separate gauge of nested rectangles.

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

**Styling must not reach the accessibility tree, and XCTest sorts by
name.** Two separate traps, both of which present as a screen that never
appeared. `MPEyebrow` uppercases its caption for looks, and that
uppercased string was what landed in the accessibility label - so
`app.staticTexts["Ships to"]` matched nothing while the words were
plainly on screen. It carries `.accessibilityLabel(text)` now, which also
stops VoiceOver shouting. Separately, XCUITest runs a class's methods in
**alphabetical order, not source order**: `testShipping...` sorts before
`testTheQueue...`, so the test that ships 7QK ran first and emptied the
queue the later test asserts on, however carefully the file was ordered.
The server is shared across the file, so a test that changes data puts it
back in `tearDownWithError` - `/unship` and `/inventory/<id>/bin` both
exist for that - rather than relying on a name that sorts late.

**The body limit is per route, and the photo route is the only big
one.** `MAX_BODY` is 2MB because reading a hundred megabytes into memory
on a Pi is how the OOM killer stops the label printer; `MAX_PHOTO` is
12MB and applies to `POST /api/photos` alone. That refusal is made on
`Content-Length` **before** a byte is read, so an oversized upload sees
the socket close rather than the JSON error - draining it to be polite
about the connection would be doing the exact thing being refused. What
the test asserts is that nothing was stored.

**A photo upload is idempotent on its digest, and has to be.** She is in
a shop on one bar of signal and the client retries; a second row would
put the same receipt in the triage pile twice. The stored filename *is*
the sha256, and the extension comes from the `Content-Type` rather than
from any name the client sent. There is still no state column and no
`captures` table: "not yet triaged" is `photos.listing_id IS NULL`, and
a flag saying the same thing would be a second place for it to be wrong.

**A receipt is not about one item, and the triage pile has to know
that.** "Triaged" started as `photos.listing_id IS NOT NULL`, following
the schema's note that a capture becomes triaged by turning into an
item. That is true of a photograph of an object and false of the thing
she photographs most: a receipt records a *trip*, several of whose items
it paid for, so a filed receipt sat in the queue for ever - and that
queue is the one number on the capture screen. The pile is now a photo
with **neither** reference, which keeps the property worth keeping
(triaged is an absence, not a flag) and stops the count lying. Money
still needing a home is a different question with its own number:
`trip.unassigned`, which is null rather than zero when nobody wrote the
till total down.

**Five tabs, and Capture took Pending's.** iOS hides the sixth behind
"More", and a tab she cannot see is a feature that does not exist.
Capture is one of the four moments the app is for and is useless two
taps deep while she is holding a cart; Pending is a recovery screen for
a printer that was off all morning. So Pending moved to the queue's "to
print" chip, which is where she looks anyway - and a UI test asserts
that route still exists, because a screen with no way to it is the
quietest kind of deletion.

**Do not tell the on-device model who is selling.** The listing
instructions opened "…second-hand household items sold on Facebook
Marketplace by one person from her home" - true, and helpful-looking
context - and the safety classifier refused the whole request with
`guardrailViolation`, on a milk glass vase with a chip in it. Isolated
one sentence at a time: dropping the negative list did not help,
dropping "Facebook Marketplace" did not help, dropping the description
of *her* did. Describing a private individual is what tripped it. The
instructions now say what to write and nothing about who wants it
written. A refusal still has to be caught and turned into a sentence -
it fires occasionally on wording it cannot place, and it must never
reach her as a raw error or read as though she typed something wrong.

**A gap the model is not told about is a gap it fills in.** Asked to
write a listing for "Cast iron skillet" with nothing said about its
condition, three runs of three invented damage - "light rust on the
bottom edge", "handle is slightly bent", "minor chipping on one side".
Naming the gap (`Condition: not stated`) stopped it three of three. That
is the opposite of the obvious design, which was to omit an empty field
so as not to invite an answer, and it matters more here than most places
a model is wrong: an invented chip is a false statement about an object
a buyer is going to unwrap.

The cost of naming it is that the draft then says "Condition: not
stated", which she would delete every time - and the model **cannot be
told to keep quiet about it**: instructed to write nothing about a
missing field it echoes the line back verbatim. So `OnDevice.tidy`
removes it afterwards, where it is deterministic and unit-tested rather
than a thing we hope the model does. `paid` is not in the generated type
at all, so no guess can ever reach a margin.

**A comment inside a line continuation ends it.** `run-ui-tests.sh`
carries a block of `TEST_RUNNER_*` assignments prefixing `xcodebuild`,
and a comment dropped between two of them terminated the continuation -
so the assignments became their own no-op command and xcodebuild
inherited only the last one. The runner got the screenshot flag and no
server, and skipped saying it had no server. Same class as the original
`TEST_RUNNER_*` bug and the same symptom: a message that is true about a
cause it cannot see.

**A picture of every screen, and why it is not a snapshot test.**
`./ios/screenshots.sh` walks the app against the same seeded server the
UI tests use and writes `ios/screenshots/`. It exists because looking at
the app found two things the assertions did not - a photograph that
failed to load sat on a spinner for ever, and the save button had
drifted underneath two optional panels. Neither is visible to a test
that asks whether a string is on screen. Nothing is compared against a
committed image on purpose: a pixel diff on a design that is still
moving fails whenever a padding changes, and says "something moved"
rather than "this is wrong". The judgement stays a person's; the script
only makes it cheap. It is skipped unless `MPLABEL_SHOTS=1`, so the
ordinary run does not pay two minutes for it. Note a sheet has no back
button and does not reliably close on `swipeDown()` - the walk drags
from the top of the card to the bottom of the screen instead, and
getting that wrong silently took every screen after it.

**Reference goes below the actions, and this has now been got wrong
twice.** The add-item screen put two optional AI panels between the form
and its save button; the order screen then put the label and the
corrections between the order and its print/ship buttons. Both times the
primary action - the thing the screen exists to do - ended up under a
fold, and both times a screenshot showed it in a second where the
assertions had nothing to say. Printing and shipping are what an order
screen is *for*; the label and a correction are repair.

**`date('now')` in SQLite is UTC, and every date in this system is
local.** A ship-by comes off a Facebook email as a local date and the
kitchen table is in Eastern; between 7pm and midnight the two disagree,
so a parcel due tomorrow reads as due now and a label recorded tonight
is not "today". `notify.due_parcels` and `failed_prints` compare against
a date computed in Python for that reason. Found by a test that inserted
with `date('now')` and asserted against `date.today()` - which is the
same mistake, and is why the fixtures now pass the date in.

**A dry run that calls something which commits is not dry.**
`mplabel notify --dry-run` first ran the whole decision and then called
`rollback()`, which did nothing at all: `notify.remember` commits, so the
notices were already written and the dry run reported that they were not.
It takes `remember_sent=False` now. A flag, not a transaction - the
honesty of the thing is the whole feature.

**Push needs two programs, not two packages.** APNs wants HTTP/2 and an
ES256 JWT; the stdlib has neither and `httpx[http2]` plus `cryptography`
is a compiler toolchain and about 40MB on this Pi. `notify.py` shells out
to `curl --http2` and `openssl`, both already on the machine and already
in this project's own instructions - the same rule that keeps
`savedpage.py` on stdlib HTMLParser. The cost is that failures are a
subprocess's, so every call checks its exit status. `openssl` emits DER
where JOSE wants raw `r||s`, and getting that unpack wrong produces
`403 InvalidProviderToken` and no other explanation - hence a test
against a signature openssl actually made.

**A sandbox token is not a bad token.** A build signed with a development
profile gets a sandbox APNs token, and the production host rejects it
with `BadDeviceToken` - which reads as malformed rather than as addressed
to the wrong Apple. The environment is stored with the token and sent by
the app for exactly that reason.

**An estimate must not be able to pass for a fact.** Postage is two
columns - `sales.postage` and `sales.postage_source` - because the
number alone is indistinguishable from a measured one the moment it is
written down, and postage on a heavy item is routinely the difference
between a good margin and none. Typing a figure marks it `confirmed`;
clearing it clears the provenance too, or an orphaned `confirmed` on a
null would make the next estimate look checked. The order screen labels
an unconfirmed figure "Postage (est.)" and says in words where it came
from. There is **no rate card in this repo**: an estimate is derived
from parcels she has actually confirmed, and where there are none the
answer is "nobody knows" rather than a plausible number.

The sheet gets `Postage`, `Postage source` and `You keep` - the source
as its own column rather than a suffix, because a spreadsheet is where a
figure gets summed, sorted and copied into another cell and
"12.40 (est.)" is a string that does none of those. Only *stored*
figures reach it, which means only confirmed ones: the estimate is
computed per request and deliberately not persisted, because an estimate
in a spreadsheet is one that gets copied somewhere else and stops being
one.

**The bundle id is `com.marchvector.Sellomatic`, and three things must
agree on it.** The App ID in the developer account carries the push
capability; the built app's `PRODUCT_BUNDLE_IDENTIFIER` must match it or
a *device* build fails provisioning while the simulator carries on
working; and `apns_topic` on the Pi must be the same string or APNs
refuses with `TopicDisallowed`. It is set explicitly in `project.yml`
rather than derived from `bundleIdPrefix`, which had produced
`com.tyevco.MPLabel` - a plausible identifier that exists nowhere.
Team ID `43FWY7NGK9`.

Changing it leaves a **stale install on the simulator**, and the symptom
is not "wrong app": the runner fails to bootstrap with "Test crashed
with signal term while preparing to run tests". `simctl uninstall` the
old id and clear derived data.

**The scheme is declared in `project.yml`, not left to Xcode.** Every
command here says `-scheme MPLabel`, and that worked only because Xcode
had autocreated one in `xcuserdata` on one machine - which
`xcodegen generate` then wiped, and which a fresh clone never had. The
failure reads `does not contain a scheme named "MPLabel"`, which looks
like a broken project rather than a file nobody generated.
**A caveat that was true when it was written goes on being said.**
Profit and Sold both stated flatly that there was no cost basis in the
database. That was true for months and stopped being true the moment the
sourcing half landed - and the screens went on saying it, which is worse
than saying nothing because it tells her the opposite of the truth.
`v_monthly` has carried `net` and `costed` since the views were written
and `/stats` simply never selected them.

The fix is that the sentence follows the data: `/stats` sends how many
sold items have a cost against them, and the fraction decides which
sentence is honest - `net` over two costed listings out of ninety is not
a month's profit. `kept` is shown only where anything is costed at all,
because a net of 0.00 reads as a month that broke even rather than one
nobody has costed. Every version still says what is **not** in the
figure: postage is per parcel, and Facebook's fee has never been
confirmed against a real payout, so neither is counted.

**`@available` cannot rescue a symbol the SDK does not have.** The
image half of `FoundationModels` is iOS 27, which means a beta Xcode:
code naming `Attachment` does not *compile* against the iOS 26 SDK, and
an availability annotation only guards a runtime call to something the
headers already declare. So `OnDevice.canSeePictures` asks two questions
of different kinds - `#available` about the phone, `#if compiler` about
the build - and the image path throws a sentence saying which is
missing rather than failing to link. The release runner has whatever
Xcode ships, so without this the whole app fails to build there, which
is what happened the day CI arrived and it happened on `main`.

**XcodeGen generates the entitlements file, so a key written into it by
hand does not survive.** `aps-environment` was hand-written into
`MPLabel.entitlements` while `project.yml` declared only its `path` -
and the next `xcodegen generate`, which this repo tells you to run
whenever a file is added, silently deleted it. On the simulator that is
invisible: there is no APNs there and the registration is never
exercised. On a real phone `registerForRemoteNotifications` then fails
with "no valid aps-environment entitlement string found", which reads
as a provisioning problem and is a missing key. The properties live in
`project.yml` now, where the generator can see them.

**A presentation dies when the state under it changes.** The run
chooser was a sheet and then a pushed screen, and both closed themselves:
the runs arrive from the Pi *after* the screen is up, the parent
re-renders when they land, and the presentation goes with it. It is
inline now - no presentation to lose, and it only appears when there is
no run, which is exactly when the question needs answering. Note the
symptom is not an error: the screen simply is not there, and a test
looking for something on it reports that the thing is missing rather
than that the screen went.

**A cancelled load looks exactly like an empty one.** `runs = (try?
await trips()) ?? []` turned a cancelled request - the camera check
flipping the view out from under it - into "no runs yet", which is the
opposite advice. `CancellationError` is now distinguished from a real
failure, and a real failure is shown on the screen it happened on rather
than behind whatever is covering it.

**A price from her own sales is evidence; a price from the model is
not, and they never become one number.** `listings.worth` answers "what
would this sell for" from listings that **actually sold** - an active
listing at $45 is an asking price nobody agreed to - and "what should I
pay" from the margin she has actually been keeping. Both are null unless
her history supports them: no comparables means no range, and no costed
sale means no ceiling, because a ceiling from an assumed margin is a
number this system invented about her business.

The model is asked for a price too, and its answer lives on
`OnDevice.Suggested.estimate` and is shown on its own line saying it has
no market data. It is never added to the comparables, averaged with
them, or shown in the same breath - blending them would launder a guess
into evidence, and she is standing in a shop about to act on it. The
instructions tell it to stay quiet about anything collectable, antique
or unusual, which is exactly where a confident number is worst.

The median, not the mean, for both: one lamp bought for a pound and sold
for eighty drags an average into fantasy. And the ceiling is built on
the median comparable rather than the top of the range - pricing the
next thing off the best day she ever had is how a shelf fills up with
things that do not move.

**A median helper must not round.** `_median` rounded to two places,
which is right for money and wrong for the margin *fraction* it also
serves: 0.625 became 0.62. Rounding belongs where the number is shown,
which knows what kind of number it is.
**A thumbnail must not be the full frame.** The capture strip called
`UIImage(data:)` on each shot's original JPEG *on every redraw* - a
12-megapixel decode per thumbnail per frame, on the main thread, while
she is trying to take the next photograph. It got worse with every shot,
which is what "not very responsive" turned out to mean when the flow was
first used on a real phone. Each shot now keeps one small copy made once
by `preparingThumbnail`, off the main thread, and the full-frame decode
the model needs happens inside its task rather than before it.

Two things about that report are worth keeping. It came from use, not
from a test - nothing in the suite can feel a dropped frame. And the
other half of it, "I couldn't click between the photos", was a missing
affordance rather than a bug: the strip only responded to a failed
upload, so three quick photographs left her able to decide the last one
and with no way back to the first.

**`@UIApplicationDelegateAdaptor` builds its own instance.** Pointing it
at `Push` - which was also the `ObservableObject` the Settings screen
watched via `Push.shared` - produced *two* `Push` objects: Apple's
callbacks went to the adaptor's, the screen observed the singleton, and
the token arrived at an object nobody could see. It presents as
Settings stuck on "Registering…" for ever, with no error anywhere,
because nothing failed. The delegate is its own type now and forwards to
the shared one.

Two things follow from the shape of that failure. A state with no way
out and nothing to say is worse than an error, so `registering` gives up
after twenty seconds and says what it is usually caused by; and the
button is offered whenever push is not actually registered, because a
failure needs a second go more than a fresh install does.
**A shared category is not a comparison.** Nearly everything she sells
is "Home", so scoring a category match as evidence put a tumbler, a
wooden cabinet and a doorway in one another's comparables - "6 like it
sold for $5.00-$235.00", which is worse than saying nothing because it
looks like evidence. A title, where the model offered one, now has to
share a word; category only breaks ties, and only carries the answer on
its own when there is no title at all. A range whose top is more than
three times its bottom is flagged `wide` and the screen says to treat it
as no guide.

Found on the first real trip with the phone, from ten photographs. No
test could have found it: the fixtures have three listings and no two of
them are alike, so category-only matching looked precise.

**There has to be a way to stop.** The only exit from the capture screen
was "Reconcile", which is the kitchen-table job - walking out of a shop
is not the same thing as sitting down with the receipt, and she noticed
because there was no way to do the first. Tapping the run now offers
leaving, switching shop, or carrying on, and says what is in the cart and
what is still undecided before she goes.

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

**A release build's version numbers pass through two indirections, and
neither fails loudly.** `xcodebuild MARKETING_VERSION=...` sets a build
setting; `$(MARKETING_VERSION)` in `project.yml`'s `info:` block expands
it into the plist. Misspell either and the archive builds perfectly,
carrying XcodeGen's own defaults of `1.0` and `1`. App Store Connect
accepts the first such build and rejects every one after it for a
duplicate build number - twenty minutes into a run, after signing. So
`.github/scripts/check-archive.sh` reads the numbers back out of the
built app with PlistBuddy and refuses the placeholders, before the
upload rather than after it. Same shape as asserting on rendered output
where geometry matters: ask the artifact, do not trust the arithmetic.

**A build number must increase for ever, so it is a timestamp.**
`github.run_number` is the obvious source and is wrong in a way that
only bites later: it is per workflow *file*, so renaming the workflow
restarts it at 1 and every build after that is rejected as older than
one from last year. A commit count goes backwards the first time a
branch is rebuilt. `version.sh` emits `YYYY.MMDD.HHMM` in UTC with
leading zeros stripped - three components because each has to stay
inside four digits, and `10#` on the strip because bash reads `0907` as
octal and `09` is not a valid octal digit, so the arithmetic would fail
outright every September.

**`manageAppVersionAndBuildNumber` defaults to true, and that discards
your build number.** Left alone, Xcode reads what App Store Connect
already has on the way up and increments it - so the number the workflow
computed and printed in its log is not the number in TestFlight, and the
build can no longer be traced to the run that made it. It is `false` in
`ios/ExportOptions.plist`. The numbers are ours; Apple is told them.

**`aps-environment` was missing from the entitlements file while a
comment said it was there.** The file was an empty `<dict/>` from the
notifications commit onward. The simulator cannot see that - it has no
APNs and never exercises registration - so it would have presented on a
real handset as `registerForRemoteNotifications` failing with "no valid
aps-environment entitlement string found", which reads as a provisioning
problem. The value is `development`, not `production`, deliberately:
Xcode rewrites it to `production` when it re-signs during
`-exportArchive` for the App Store, so a TestFlight build gets a
production token and a build run from Xcode onto the same phone gets a
sandbox one. Writing `production` here breaks the second without helping
the first. Which means `apns_environment` on the Pi has to say
`production` once she is on a TestFlight build.

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

**Tracked as GitHub issues.** This section is the map, not the backlog -
each line says why the thing matters and points at the issue that holds
the detail. Add the reasoning here when it is the kind that would
otherwise be rediscovered; put the steps in the issue.

### The print path

The G4 has been printing real parcels throughout, so most of what used to
be open here is answered by use: the barcodes scan, one job advances one
die-cut label, and `gap_inches = 0.12` is right for that stock. Two
things came out of settling the rest:

- **The G4 is write-only** (recorded in the table above). `mplabel
  status` got no reply to either query. A failed print cannot be detected
  in software, so printing is at-least-once and the paper is the only
  source of truth. Everything about the journal follows from this.
- **Creep across a batch: none** (#9, closed). Three in a row landed in
  the same place. Note this was already answered by a week of production
  parcels before anyone printed a test - when the hardware has been in
  daily use, ask what that use has already proved before designing an
  experiment for it.

Then the split: #10 loopback is done and #11 - off-loopback over a mesh
VPN - is deployment rather than code. Of the hardening, #12 (signing
`GET /printed`) and #13 (the journal) are done; #14 is answered by
`systemd/mplabel-web.service` existing; #15 (the installer assumes one
host) is open.

#31 (the print lock blocking under a deadline) is done too.

**The journal is the only record there is, so it is written like one.**
The G4 is write-only - a failed print cannot be detected in software -
which makes this file the answer to "did that come out?" with no second
source. So an append is fsynced before the lock is released, the trim
writes a temp file in the *same directory* and `os.replace`s it (atomic;
across filesystems it is not, and /tmp usually is one), `since()` reads
under the lock rather than landing mid-rewrite, and a torn last line
costs that line rather than the file. The in-memory `_done` set is
rebuilt from what survives a trim: it used to outlive the file, so a
trimmed job stayed 409-able until a restart and then silently stopped
being.

### The label maker

It prints correctly. The encoder, the line order, the printable window
and the bit polarity are all settled on hardware - see the table above.
What is left is physical and needs a camera, not a test:

- #16 is **answered: the QR reads off thermal**, first time, in the stock
  iPhone Camera app. That settled the shape of the iPhone app (#20) -
  VisionKit reads QR for free, so no marker decoder goes to Swift. #17
  (does the marker survive a camera) is now a curiosity rather than a
  blocker and can be closed unread.

  What that leaves for #20 is **the same scan through our own code**.
  Apple's Camera app reading the label proves the ink; it does not prove
  `DataScannerViewController` wired up the way `ScanView` wires it, and
  the simulator cannot answer that. One printed label and one device.
- #18 row order and feed origin. Low priority - labels come out right
  today - but it is the difference between knowing and having been lucky.

### What the tags are for

#19, largely landed. `bins` exists, `listings.bin_code` references it,
and `mplabel bin` and `/api/bins` both drive it. What is deliberately
*not* built is move history: this records where a thing **is**, which is
the question being asked. If "where has this been?" turns out to be a
real question, that is a new table beside this one rather than a
different shape of it - and a week of using the printed tags is still
the cheapest way to find out which.

### Moving the order side off the Pi

The destination is k8s (#24). Three prerequisites, each of which is a
silent failure if skipped: #21 label paths are absolute and
`label_belongs_to` is what stands between a reprint and a parcel posted
to a stranger; #22 the Pi's stale copy will reprint shipped parcels with
recycled codes if anyone runs the documented recovery command on it; #23
nothing detects the order side being absent, and a warning inside the
poller cannot detect its own absence.

### The sourcing half

The API is done - trips, photos, attach, create-item and an allow-listed
item `fields` that takes `paid` - and **Capture and Triage are built**
against it in `ios/`. That is the flow the whole phase was for: she
photographs a receipt in a shop, and at the kitchen table attributes
what it cost to the things that came home. Profit stops saying "gross"
as soon as that has been used in anger; the margin views have been
correct and empty this whole time.

**Add an item is built too** - `AddItemView`, off the Shelf tab's plus
menu. Title, paid, asking, era, condition, a bin picker, and the
untriaged captures as selectable thumbnails, which is the only place a
photograph gets attached to a thing: the shutter screen deliberately
asks nothing at the time, because the shop is where the picture has to
be taken and the kitchen table is where it can be said what it was of.
`listings.era` is a new column with a migration behind it - free text,
not a year, because "Antique 1900-1915 American Edwardian" is a range
and a guess at once.

**Both AI halves of that screen are built, on-device.** The design drew
them as an online call with an offline queue behind it;
`FoundationModels` needs no key, no vendor and no network, so the queue
is gone and the "no signal in the store" state with it. `OnDevice.swift`
holds both: a listing draft from the fields she typed (iOS 26, text) and
title/era/condition/category off a photograph (iOS 27, images - the
image half of the framework arrived a version after the text half, so it
carries its own `#available`). Nothing either produces is written
anywhere on its own; a suggestion becomes a value when she taps its
chip. The two measured findings that shaped the wording are in *Things
that will bite you*.

Raising the deployment floor to **iOS 26** is what made this reasonable
rather than a second code path that could not be tested.

**One trip is built** - `TripsView` and `TripView`, reached from a card
on the Shelf that carries the number worth interrupting her for: money
that came out of a till and has not been attached to anything yet. A
trip id is pushed as a `TripRef` rather than an `Int`, because the shelf
already pushes item ids as `Int` and a tap on a run would otherwise open
whatever listing shared its number.

**The order-detail affordances are built** - fix a field, a note, and
the archived label drawn with PDFKit. That closes the last place the PWA
was ahead of the app. The label is fetched rather than linked: it needs
the bearer token and a `Link` cannot carry one, so it would open Safari
to a 401.

**Notifications are built**, and that is the design complete.
`notify.py` decides; the phone only registers. Three things earn one -
a parcel is due, a label never printed, money has no home - and the
scope is the point rather than a starting set. See
`docs/notifications.md` for the Apple-side setup, which cannot be done
from this repo: an APNs key, a Key ID, a Team ID and the capability on
the App ID.

Note the third trigger is **not** "a print failed". The printer is
write-only so nothing can know that; what is reported is "recorded and
never printed", which is the set `mplabel pending` shows.

What is deliberately *not* built: an offline outbox on the phone.
Uploads go straight up and a failed one keeps its bytes on screen to be
retried, which works because the server keys a photo on its sha256 -
pressing retry cannot make a second row. A local queue would be a second
source of truth for the same photographs.

### Older, still true

#25 USPS tracking is probably not available and one lookup settles it.
#26 learn the real Facebook subjects, without which `backfill` finds
almost nothing. #27 the Sheets call itself has never run. #28 the
saved-page parser has only ever seen a synthetic fixture. #29 multi-page
label PDFs, #30 non-USPS carriers - both filed as known limitations
rather than surprises.

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
