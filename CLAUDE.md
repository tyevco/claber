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
pytest                                     # 40 tests, all should pass
pytest tests/test_mplabel.py::test_output_is_exactly_4x6   # one test
pytest -k tspl                             # printer-language regressions
python -m mplabel --help
python -m mplabel file tests/fixtures/label_sample.pdf   # no config needed
```

Config resolution order: `MPLABEL_<KEY>` env var, then
`/etc/mplabel.conf`, then `~/.config/mplabel.conf`, then `DEFAULTS` in
`cli.py`.

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
by `listing_id` afterwards.

`listings.refresh()` is the single rebuild entry point: schema ->
link_sales -> apply_events -> build_views. Analytics are views, not
tables: `v_listing_perf` derives days_to_sell / days_listed /
price_band, and `v_price_band`, `v_monthly` and `v_aging` are built on
top of it. `sheets.TABS` selects from those views by column name, so
renaming a view column breaks the sheet with no test failure.

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
| Ship-by year inference | **Verified** by unit test incl. New Year rollover. |
| G4 speaks TSPL | **ASSUMED.** Inferred from the OEM family and from Clabel's docs quoting density 1-15 (the TSPL range). Never sent to hardware. |
| TSPL gap value 0.12in | **ASSUMED.** Typical for 4x6 die-cut; not measured on their stock. |
| Facebook subject patterns | **ASSUMED** except `shipping_label`. Only one real email exists. |
| DYI export schema | **ASSUMED.** Undocumented and reshuffled by Meta; importer walks for shape rather than assuming paths. |
| Saved-page JSON shape | **ASSUMED.** Field names from public GraphQL modules; fixture is synthetic. |
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

**TSPL and ZPL have opposite bit polarity.** TSPL prints on a *clear*
bit, ZPL on a *set* bit. `render_bitmap(invert=True)` for TSPL, and the
padding bits past the right edge must be set to white or you get a black
stripe. See `test_tspl_and_zpl_bit_polarity_are_opposite`.

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

**Sell-through is meaningless without prices on unsold listings.** Prices
only reach the DB if the listing email or a saved-page/DYI import carried
one. If `v_aging` shows blank prices, the percentages are lying.

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
  list short. Do not add it for convenience.
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

1. **Confirm the printer language.** `python -m mplabel probe` reads the
   IEEE-1284 id; `selftest` sends a few dozen bytes of text-only TSPL.
   If it is not TSPL, `printers.BACKENDS` already has ZPL and two CUPS
   paths.
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
7. No retry queue for print failures; they are recorded in `sales.notes`
   with `printed_at` NULL, and `mplabel list` shows them as NOT PRINTED.

## Style

Plain Python, no framework. Comments explain *why*, especially where
something non-obvious was learned the hard way — those comments are load
bearing, keep them. Docstrings on modules and non-trivial functions.
Tests are regression tests: each one exists because something was
actually wrong. If you fix a bug, add the test first.
