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
| `test-print` | reprint the newest label |
| `reprint <ref>` | reprint one |
| `pending [--since] [--all] [--dry-run]` | labels recorded but never printed; today only by default |

| Database only | |
|---|---|
| `list` / `stats` / `ship <ref>` | outstanding orders, analytics, mark shipped |
| `import <path> --format dyi\|csv\|saved [--state ...]` | listings from a file |
| `sheets [--dry-run]` | push to Google Sheets |

`probe`, `selftest` and `file` run above `connect_db` in `main()` - see
the note below on why.

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
  savedpage.py   parse a saved Marketplace selling page
  sheets.py      Google Sheets sync via service account

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
| The raw data path works | **Verified on the hardware:** bytes reach `/dev/usb/lp0`, usblp is loaded, the `lp` group permissions are right, paper feeds and marks. If a label comes out wrong from here, suspect the raster or the geometry, not the transport. |
| `fsync` on `/dev/usb/lp0` fails | **Verified on the hardware.** It returns `EINVAL`; the write itself succeeds and the label prints. `_write_raw` treats fsync as best effort — see the note below on why raising there corrupted the printed/not-printed record. |
| `escpos` backend | **UNUSED and unproven.** Written while the id was believed, kept because the job structure is unit-tested and some sibling models really do speak ESC/POS. Nothing it produces has ever printed. Its banding size and trailing form feed are guesses. |
| TSPL gap value 0.12in | **ASSUMED.** Typical for 4x6 die-cut; not measured on their stock. |
| Facebook subject patterns | **Partly verified** against a real mailbox survey. Seen and handled: `Shipping label for your Marketplace order`, `New Marketplace order for <item>` (the sale itself, arriving before the label), and messages as `<emoji> <name> sent you a message`. The rest of `EVENT_PATTERNS` (listed / renewed / expired / payout / rating) is still **ASSUMED** - none has been seen. |
| The mailbox mixes buying and selling | **Verified.** `You placed an order: <item>`, `Offer submitted: <item>` and `Confirm if you received your order: <item>` are *her purchases*. They carry the **seller's** listing id, so they are classified `purchase`, kept out of the listings table by `BUYER_KINDS`, and their listing id is dropped at record time. Counting them would invent listings that were never for sale and drag sell-through down. |
| DYI export schema | **ASSUMED.** Undocumented and reshuffled by Meta; importer walks for shape rather than assuming paths. |
| Saved-page JSON shape | **ASSUMED.** Field names from public GraphQL modules; fixture is synthetic. |
| `printd` split (`pi-http`) | **Works, but only against a fake device.** A signed job crosses as a ~2KB PDF and is rendered to ~124KB of TSPL on the printd side; dedup, deadline-refusal, bad-signature and SystemExit-survival all covered. **Never run against the real printer**, and deploying it is gated on the label geometry below. |
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

**A printer test must not need the database.** `probe`, `selftest` and
`file` run above `connect_db` in `main()`, because an unwritable home
directory once stopped a printer test dead - which is the one thing you
want working when nothing else is. `cmd_file` takes no `conn` at all.

**Sell-through is meaningless without prices on unsold listings.** Prices
only reach the DB if the listing email or a saved-page/DYI import carried
one. If `v_aging` shows blank prices, the percentages are lying.

**One label file per email, and never named after the listing.** On real mail `listing_id` and `order_id` parse as **NULL** - 0 of 18 - so the archive name fell back to a timestamp at second resolution, and a batch of labels put three pairs in the same second. Each pair shared one file, so three sales pointed at another buyer's label and one of those printed. The name now prefers the id in Facebook's own attachment name (`label_<id>.pdf`) and always carries a digest of the Message-ID, so it is unique per email and searchable by the id on the PDF.

**The unit of a sale is the order, not the listing.** `already_seen` keys on message_id and order_id; `sales.listing_id` is a plain index, not UNIQUE. A buyer cancels, someone else buys the same item, and Facebook sends a second label email with the same listing_id - which the old unique index and the old listing_id check both silently rejected. `mplabel cancel` closes the dead order without counting it as revenue.

**Check a label still matches its sale before printing it.** `label_belongs_to` re-reads the recipient off the PDF and compares it with the `ship_to` recorded from that same page when the sale was filed; `reprint` refuses on a mismatch, and `mplabel verify` sweeps the archive. This is the backstop for anything that leaves a row pointing at the wrong file - the failure is silent and the consequence is a parcel posted to a stranger.

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
