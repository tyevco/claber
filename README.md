# mplabel

Prints Facebook Marketplace shipping labels on a USB thermal printer from
a Raspberry Pi, tracks every sale in SQLite, and mirrors it all to Google
Sheets with sell-through analytics.

```bash
pip install -e ".[dev,sheets]"
pytest                    # 40 tests
python -m mplabel --help
```

Deploying to a Pi: `sudo bash install_pi.sh`. Working on the code:
read **CLAUDE.md** first — it lists what is verified against real data
versus what is still inference, which is the difference between a safe
change and a parcel that does not ship.

## Files

```
src/mplabel/       the package (see CLAUDE.md for module map)
tests/             pytest suite + synthetic fixtures
install_pi.sh      Raspberry Pi OS installer
systemd/           service unit
udev/              raw USB printer node rule
mplabel.conf.example
```

## Where the data actually comes from

You were right that most of it is in the email. Split:

| Field | Source |
|---|---|
| buyer name | email body |
| item title | email body |
| price | email body |
| ship-by deadline | email body |
| listing id | `See order details` link, and the attachment filename |
| order id | `order_id=` in the same link |
| sale timestamp | email `Date` header |
| **tracking number** | **label PDF only** |
| **buyer's postal address** | **label PDF only** |
| **parcel weight** | **label PDF only** |
| **carrier + service class** | **label PDF only** |

The email genuinely does not carry tracking or the destination address, so
the label still gets parsed — but only for those four fields, and it never
overwrites anything the email already gave us. Everything else comes from
the body, which is far more stable than PDF text extraction.

Verified against your Andy Lines order, the parser returns:

```
buyer       Andy Lines
item        The Gleaners by Jean-François Millet print
price       15.0
ship_by     2026-09-04          <- resolved from "Fri, Sep 4"
listing_id  2379911152536775
tracking    9334610579700002137836
ship_to     ANDY LINES, 707 BROOKS DR, NORTH AUGUSTA SC 29841-3221
weight      1 lb 15 oz
service     USPS Ground Advantage
```

Note `ship_by`: Facebook writes "Fri, Sep 4" with no year. The parser
anchors it to the email's received date and rolls into next year when the
deadline crosses New Year, so a late-December sale files correctly.

## Install on the Pi

```bash
scp -r dist/ pi@raspberrypi.local:~/mplabel
ssh pi@raspberrypi.local
cd ~/mplabel && sudo bash install_pi.sh
```

Then log out and back in (for the `lp` group), edit `/etc/mplabel.conf`,
and:

```bash
/opt/mplabel/venv/bin/python /opt/mplabel/mplabel probe       # find printer
/opt/mplabel/venv/bin/python /opt/mplabel/mplabel check       # dry run
/opt/mplabel/venv/bin/python /opt/mplabel/mplabel test-print  # print one
sudo systemctl enable --now mplabel
journalctl -u mplabel -f
```

All four Python deps (pypdf, pdfplumber, pypdfium2, Pillow) ship aarch64
wheels, so nothing compiles on the Pi. Install takes about a minute.

## Gmail

IMAP needs an **App Password**, not the account password — Google rejects
the latter outright. Generate at Google Account → Security → 2-Step
Verification → App passwords. 2FA must be on first.

Prefer to keep it off disk? Put it in `/etc/mplabel.env`:

```
MPLABEL_IMAP_PASSWORD=xxxxxxxxxxxxxxxx
```

`chmod 600` it. The systemd unit reads it, and environment variables
override the config file.

## Your printer: CLABEL G4

Confirmed specs: **203 dpi** (8 dots/mm), 152 mm/s, USB + Bluetooth + COM,
made by Shenzhen Dudian. At 203 dpi a 4 x 6 label is exactly **812 x 1218
dots**, which is what the converter now emits.

Clabel's driver portal (ga.ctaiot.com) ships **Windows and Mac only** —
there is no vendor Linux driver, and no CUPS PPD. So the default backend
talks to the printer directly in **TSPL** over the raw USB node. That is
the same language the cheap 4x6 clones share (Munbyn, iDPRT, Beeprt,
Xprinter, JADENS), and Clabel's own docs give print density as 1-15, which
is the TSPL `DENSITY` range.

I could not verify the G4's TSPL support from a datasheet — it is inferred
from the OEM family and the density range. **Confirm it in two steps before
trusting the pipeline:**

```bash
mplabel probe      # reads the printer's IEEE-1284 id, prints nothing
mplabel selftest   # a few dozen bytes of text-only TSPL
```

`probe` asks the kernel what the printer said about itself at enumeration —
most TSPL units self-describe with `TSPL` in the `CMD:` / `COMMAND SET:`
field. `selftest` prints a small text label using the printer's built-in
fonts. If `selftest` works but a real label does not, the problem is data
transfer, not the language.

If it turns out **not** to speak TSPL, switch `printer_backend`:

| Backend | When |
|---|---|
| `tspl` | Default. Raw TSPL to `/dev/usb/lp0`. |
| `zpl` | If probe reports ZPL. |
| `cups-pdf` | If you install a CUPS driver and want CUPS to own the printer. |
| `cups-raster` | As above, but the PDF path prints scaled or blank. |

There is also a community CUPS driver for this whole printer family with
prebuilt arm64/armhf packages, if you would rather have a normal CUPS queue
(and AirPrint from her phone as a bonus):
`github.com/RunTheWall/tspl-cups-driver`. The G4 is not on its tested list
either, but it is the same command set. Use `cups-pdf` as the backend if
you go that route.

### Two things I got wrong first time

**GAP.** My initial TSPL header had `GAP 0,0`, which means *continuous*
stock. On die-cut 4x6 labels the printer then never finds the label edge,
so prints creep further down the roll with each label until they straddle
the gap. Now `GAP 0.12,0` by default, configurable via `media_tracking`
and `gap_inches`. If you are on fanfold with a black registration mark,
set `media_tracking = blackmark`.

**Streaming.** These budget firmwares silently discard bytes that arrive
while the print head is already moving — the documented failure is that
multi-label jobs print the first label and drop the rest. The backend now
builds the entire job in memory and hands it over in one unbuffered
`os.write`, then pauses `settle_seconds` (default 2) before the next.
Don't set that to 0 if labels ever go out in batches.

### If the raw device is missing

CUPS grabs USB printers and unbinds `usblp`. If `/dev/usb/lp0` does not
exist:

```bash
lsmod | grep usblp
sudo modprobe usblp
```

You cannot use a CUPS queue and the raw device for the same printer at
once — pick one. The installer drops a udev rule making the node writable
by the `lp` group so the service does not need root.

### Tuning the print

- Barcodes scanning faintly → raise `printer_darkness` (0-15).
- Solid blacks bleeding or smearing → lower it.
- Fine detail blurry → lower `printer_speed` (in/sec).

## Listings and analytics

### The constraint

There is no Facebook Marketplace API. Meta has never published one for
individual sellers — Marketplace is a closed consumer product, and the
Commerce Platform API is a restricted alpha for approved business
partners managing their own catalogues. This is not a permissions problem
you can apply your way out of.

That leaves scraping. Meta's robots.txt disallows it, and an
unauthenticated crawler cannot see her history anyway; doing it from her
logged-in session is what gets accounts flagged. If her account is
restricted she loses the selling channel, the buyer conversations and the
order history at once — a steep price for a sell-through chart. Saving
the page by hand and parsing it offline (below) gets the same data with
none of that exposure.

So the listing catalogue is assembled from data she already owns.

### 1. Her mailbox (the main source)

Facebook emails her on listing, sale, inquiry, renewal, expiry and
payout. That is a full listing lifecycle, going back as far as her mail
does.

```bash
mplabel scan       # survey only, changes nothing
mplabel backfill   # parse into the listings table
```

**Run `scan` first.** It prints a histogram of her Facebook subject lines
split into recognised and unrecognised. I had to guess at the subject
wording for everything except the shipping-label email — that is the only
one I have a real sample of — and Facebook's wording varies by locale and
drifts over time. `scan` shows you what is actually in her inbox so you
can add the real patterns to `EVENT_PATTERNS` in `listings.py`. Each is
just a name and a regex.

If `scan` finds nothing, her Facebook mail is probably filtered to a
label rather than the inbox; point `imap_folder` at it.

`backfill` is resumable — it records which messages it has already seen,
so re-running only picks up new ones. Add `--restart` to redo everything.

### 1b. A saved copy of her selling page (best single source)

Scraping the profile URL directly does not work and is not worth
attempting. `facebook.com/robots.txt` disallows automated access, and an
unauthenticated fetch of a seller profile returns a login wall — the
listing history, sale dates and prices only exist inside her logged-in
session. Driving that session with automation is what trips Meta's bot
detection, and a false positive costs her the account, the buyer threads
and the order history together.

None of that applies to opening a page in her own browser and saving it.
One human page load, no automated requests, and the file contains the
same JSON the page used to render itself. It is a one-time job:

1. Open **facebook.com/marketplace/you/selling**
2. **Scroll to the bottom** until everything has loaded — the list is
   lazy-loaded, so anything never scrolled into view is not in the file
3. Ctrl-S / Cmd-S → **"Web page, HTML only"**
4. `mplabel import --format saved ~/Downloads/selling.html`

Step 2 is the one that matters. Save it after a couple of scrolls and
you get a couple of screens' worth of listings and no warning that the
rest are missing.

Facebook's markup is machine-generated — class names are rotating hashes
— so the parser ignores the DOM entirely and reads the embedded
hydration JSON, matching on any of several known field spellings rather
than one fixed path. Verified against the real listing id from the
shipping-label email (`2379911152536775`), which it recovers correctly.

**Alternative:** `python -m mplabel.savedpage --snippet` prints a DevTools
console snippet that does the same thing and downloads clean JSON instead
of HTML. Same principle — reads only what the page has already loaded,
issues no requests.

If it reports zero listings, the causes in order of likelihood are: saved
before scrolling; saved as "complete" rather than "HTML only"; or
Facebook renamed a field. It also reports how many script blocks failed
to parse, so "3 listings, 40 unparseable" tells you the save went wrong
rather than that she has three listings.

### 2. Download Your Information

Facebook's official self-service export. Settings → Your information →
Download your information, tick Marketplace, choose JSON.

```bash
mplabel import ~/Downloads/facebook-export.zip
```

Meta reshuffles this export regularly and does not document its schema,
so the importer walks the JSON looking for listing-shaped objects rather
than assuming a fixed layout. It reports how many marketplace files it
examined, so "0 listings from 0 files" tells you the export did not
include the Marketplace section — re-request it with that box ticked —
while "0 from 3" means the shape changed and the importer needs a look.

### 3. Manual CSV

For anything the first two miss:

```bash
mplabel import --format csv listings.csv
```

Columns, any subset: `listing_id,title,price,category,condition,listed_at,sold_at,state`

### What you get

```bash
mplabel stats
```

```
Sell-through by price band
  $10-25     1/2 sold  50.0%   avg 12.0 days
  $25-50     1/1 sold  100.0%  avg 21.0 days
  $50-100    1/2 sold  50.0%   avg  3.0 days

Sitting longest (active)
  Wool rug          $ 95.00  169d  1 inquiries
```

SQL views do the work, so you can query them directly too:
`v_listing_perf` (per listing, with days-to-sell and days-listed),
`v_price_band` (sell-through by price bracket), `v_monthly`,
`v_aging` (active listings by age).

**A caveat on sell-through.** It is only meaningful if unsold listings
have prices, and a price only reaches the database if the listing email
carries one. If the "sitting longest" table shows blank prices, that is
why — fill them from a DYI import or a CSV rather than trusting the
percentages.

## Google Sheets

Five tabs, rebuilt from SQLite on every sync: **Sales**, **Listings**,
**By price band**, **Monthly**, **Aging**.

```bash
mplabel sheets --dry-run   # print what would be written
mplabel sheets             # write it
```

It also runs automatically after any poll that found a new order, unless
you set `sheets_after_poll = no`. That call is wrapped in its own
try/except — a Google outage can slow the sheet down but can never stop a
label printing.

### Setup

Auth is a **service account**, not OAuth. A headless Pi cannot complete a
browser consent flow, and OAuth refresh tokens expire; a service account
key is a file that keeps working.

1. console.cloud.google.com → new project
2. APIs & Services → Enable APIs → enable **Google Sheets API**
3. Credentials → Create credentials → **Service account** → create,
   then Keys → Add key → **JSON**
4. Save it to the Pi as `/etc/mplabel-sheets.json`, `chmod 600`
5. Open the JSON, copy the `client_email` value
6. Create the Google Sheet, click **Share**, paste that address, **Editor**

**Step 6 is the one everyone misses.** The service account is a separate
identity with its own address. Until the sheet is shared with it, every
write returns 403 no matter how correct the key is.

Then set `sheets_id` in the config to the long string in the sheet's URL
between `/d/` and `/edit`. Using the id rather than the name means
renaming the sheet will not break the sync.

Each tab is written as one batched call and replaces the tab's contents
outright, so the sheet is a true mirror — re-running never doubles rows,
and a correction in SQLite propagates. Values go up as `RAW` so Sheets
does not reinterpret a 22-digit tracking number as a float and mangle it.

## Day to day

```bash
mplabel list                    # what still needs shipping
mplabel reprint 2379911152536775   # label jammed, print it again
mplabel ship 2379911152536775      # mark done
```

Queries against `~/marketplace/sales.db`:

```sql
SELECT item, buyer, price, ship_by FROM sales WHERE status != 'shipped';
SELECT strftime('%Y-%m', received_at) AS month,
       COUNT(*) AS orders, ROUND(SUM(price),2) AS gross
  FROM sales GROUP BY month ORDER BY month DESC;
```

## Safety rails

- `message_id` is UNIQUE and `listing_id` has a UNIQUE index, so a
  re-poll or a Facebook resend cannot double-print or double-count.
- Non-label mail from Facebook is left unread and untouched.
- A message that throws is marked unread again so it retries next pass,
  rather than being silently dropped.
- Print failures are recorded in `notes` and leave `printed_at` NULL, so
  `mplabel list` shows them as NOT PRINTED.
- The poll loop backs off exponentially to 30 min on network errors, which
  a Pi on wifi will hit.

## Files

```
mplabel         daemon + CLI
mailparse.py       email parsing (stdlib HTML parser, no BeautifulSoup)
listings.py        listing schema, event classification, analytics views
backfill.py        mailbox survey + historical import
sheets.py          Google Sheets sync (service account)
savedpage.py       parse a saved selling page for listing history
label.py           PDF crop/rotate to exact 4x6, label-only field extraction
printers.py        raw TSPL/ZPL and CUPS backends, probe, selftest
install_pi.sh      Raspberry Pi OS installer
mplabel.service    systemd unit
99-clabel-g4.rules udev rule for the raw USB node
```
