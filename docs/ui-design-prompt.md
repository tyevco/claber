# UI design prompt — the phone app

`mplabel` is headless today: every interaction is a CLI command over SSH,
which in practice means only one of the two people in the household can
use it. The plan is a PWA served by the Pi and added to the iPhone home
screen, so the person actually running the selling side can work the queue
herself.

This file is the brief to hand to a UI generation tool. It is kept in the
repo rather than pasted ad hoc because the domain detail in it is the part
that matters — the parcel code being a physical object, the not-printed
alarm state, one-handed capture in a thrift-store aisle. A generic "orders
dashboard" prompt produces a generic orders dashboard, which is not what
this is.

Regenerate from this file rather than iterating on a chat thread, so the
constraints stay in one place.

---

```text
Design a mobile web app (iPhone PWA, added to the home screen) for a
two-person household that sells secondhand goods on Facebook Marketplace.
The primary user is one person — she runs the selling side day to day. Her
husband maintains the system. It runs on a Raspberry Pi in their house and
drives a USB thermal label printer.

Design for iPhone portrait, one-handed, thumb-reachable primary actions.
Assume it opens full-screen with no Safari chrome (respect the home
indicator and notch safe areas). Support light and dark; dark matters
because she uses it in the evening and in badly lit thrift stores.

## The four moments this app exists for

1. "What has to go out today?" — standing at the kitchen table with a
   stack of boxes, deciding what to pack and post.
2. "This label didn't print." — a jam, or a print that failed silently.
   Recovering has to be two taps, not a support call.
3. "I'm in a Goodwill holding a receipt." — she's sourcing inventory. One
   hand on the cart, phone in the other. Capture now, tidy up later.
4. "Are we actually making money?" — cost basis vs sale price, per item
   and overall.

## The parcel code is the hero element

Every outgoing order gets a random 3-character code in Crockford base32 —
the digits plus the letters, less I, L, O and U, because each of those is
misread when copied by hand. 32,768 of them. Each one is physically
printed on the shipping label, in the top-right corner of the box. It is
the only thing linking a row on her screen to a specific box in a stack in
the hallway. It must be the visual anchor of every order row — large,
monospaced, instantly scannable while her eyes flick between screen and a
pile of parcels. Codes are recycled once a parcel ships, so they are not
identifiers, they are short-lived physical tags. Treat them like a coat
check number.

## Screens

**1. To Ship (home).** A queue sorted by ship-by deadline, which is a hard
Facebook commitment — missing it hurts her seller rating. Each row: parcel
code, ship-by date with urgency (overdue / today / this week), item title
(these run LONG and share their first 60 characters — "Antique 1900-1915
American Edwardian / Late Victorian..." — so truncate intelligently or the
rows look identical), buyer first name, price. Batch is normal: she once
sold nine items in one afternoon, so the list must stay legible at 15+ rows.

A row has three states, and one of them is an alarm:
  - NOT PRINTED — no label exists yet. This parcel cannot ship. Must be
    visually loud and impossible to scroll past.
  - PRINTED — label is on the box, waiting to be posted.
  - SHIPPED — done, drops off the queue.

**2. Order detail.** Everything known about one sale: item, buyer, price,
ship-by, tracking number (22 digits — never wrap it mid-number), weight,
service class, destination. Actions: mark shipped, reprint the label, add
free-text notes, and correct fields the email parser got wrong (title,
price, buyer). Corrections are common enough to be a first-class action,
not buried in an overflow menu. Show the archived label PDF as a thumbnail
she can tap to view full-size.

**3. Pending labels.** A recovery view listing labels that were recorded
but never physically printed. Batch-select and send to the printer. This
screen is the answer to "the printer was off all morning." Include a
dry-run affordance — she should be able to see what WOULD print before
committing paper, because reprinting a parcel that already went out wastes
stock and puts a second label on a shipped box.

**4. Sourcing capture.** Camera-first. She's in a store, standing up, one
hand free. The default action on opening should be "photograph this
receipt" — big target, immediate, no form to fill first. Captured receipts
land in an untriaged pile she processes later at home: assign a purchase
price per item, a store, a date, photos of the goods themselves. Design
both halves: the 2-second in-store capture, and the calm at-home triage
queue that turns a photo into structured cost data.

There is no OCR. She reads the receipt herself during triage, because
thrift receipts itemize by department ("HOUSEWARES $4.99") rather than by
object — the machine can read the text but still cannot tell you which
$4.99 was the stoneware vase. The design should make that attribution step
feel deliberate and quick rather than like data entry: photo on one side,
the items she's assigning cost to on the other.

**5. Add an item by hand.** Not every sale arrives by email — a local
pickup produces no label email at all, so those sales are invisible to the
system unless she enters them. Also used for listing new inventory:
title, category, asking price, purchase price (cost), photos.

**6. Profit.** Sale price minus purchase price, per item and in aggregate.
Sell-through rate, average days to sell, monthly gross, and which price
bands actually move. She wants to know what to buy more of. Charts should
be glanceable on a phone — no dense dashboards, no tiny axis labels.

## Constraints

- Photos and receipt images are the heaviest thing here. Design for a slow
  home wifi upload: optimistic UI, visible upload state, never block her on
  a spinner while she's still shopping.
- Buyer names and full home addresses are in this data. Nothing about the
  design should encourage screenshotting or over-displaying addresses —
  show them on the detail screen, not in the list.
- Destructive or physical actions (printing, marking shipped) need a
  moment of friction, because both are hard to undo — paper is spent, and
  a wrongly-shipped flag hides a parcel that still needs to go out.
- No external CDNs, fonts, or image hosts: this is served by a small Python
  process on a Raspberry Pi with no internet dependency at render time.
  Everything must be self-contained. System font stack is fine and
  preferred.
- Keep the frontend buildable as plain static assets — HTML, CSS, and
  vanilla JS or a single small bundle. The backend is deliberately
  dependency-light stdlib Python; a heavy framework toolchain would be out
  of place.

Deliver: the To Ship queue, an order detail, the in-store receipt capture,
and the profit summary as the four core screens, plus the component and
color system that ties them together.
```
