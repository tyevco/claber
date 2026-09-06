# The native iPhone client

A SwiftUI app against the same `/api/v1` that the PWA uses. It exists for
one reason the web app cannot cover: **Safari has no `BarcodeDetector`**,
so a web page cannot read the QR on a printed label, and VisionKit can.

That was settled on paper before any of this was written - a printed
`inventory-label --qr` read first time in the stock Camera app, so the
module size at 5 dots survives thermal bleed. See the QR encoder row in
`CLAUDE.md`.

## Status

**Builds, runs, and talks to the real server.** Signing in, the queue,
pending, the shelf, bins and moving an item between them all work
against her actual database on the Pi, over a cloudflared tunnel.

The models turned out to be right, which was the bet: they were written
against `web.py`'s actual payloads (`_order_row`, `_order_detail`,
`h_inventory`, `h_item`, `h_bins`) rather than against the design, and
the key-contract test in `tests/test_mplabel.py` is what keeps them that
way. Authoring it on a machine that cannot compile Swift cost exactly one
real error - `Lookup`'s Hashable conformance sat in a different file from
the enum, and synthesis only happens in the declaring file.

**Two things are still untested, and they are the interesting two:**

- **The scanner.** The simulator has no camera, so `ScanView` has never
  read a label - and that is the whole reason this target exists rather
  than a web page. Apple's Camera app reading a printed QR proves the
  ink survives thermal; it says nothing about whether
  `DataScannerViewController` is wired up correctly here.
- **Printing.** The print buttons have never been pressed from this app.
  That is the one action that spends physical stock and moves paper, and
  the printer cannot confirm a print, so it wants doing deliberately
  rather than while poking about.

## Opening it

```bash
brew install xcodegen        # once
cd ios && xcodegen generate  # writes MPLabel.xcodeproj
open MPLabel.xcodeproj
```

Then set a team under Signing & Capabilities. A free Apple ID works and
gives a 7-day provisioning profile, which is enough to put it on her
phone for testing; it expires and the app stops launching, which is worth
knowing before it happens on a Saturday.

Without XcodeGen: File > New > Project > iOS App (SwiftUI), then drag
`MPLabel/` in. The only settings that matter are the deployment target
(17.0) and `NSCameraUsageDescription` in Info.plist - without that string
the app is killed the instant the Scan tab opens, with no message.

## What is here

| file | |
|---|---|
| `Models.swift` | Codable mirrors of what `web.py` returns. Almost everything Optional, because on real mail `listing_id` and `order_id` parse as NULL - 0 of 18 |
| `APIClient.swift` | one actor that knows the base URL, the `/api/v1` prefix, the bearer header and the `X-Mplabel` CSRF header |
| `Session.swift` | the token in the Keychain, the server address in UserDefaults, and the state the UI switches on |
| `Views/ScanView.swift` | VisionKit, QR only |
| `Views/QueueView.swift`, `OrderDetailView.swift` | what has to go out, and the two things that happen to it |
| `Views/ShelfView.swift`, `ItemView.swift` | where things are, and moving one |
| `Views/LoginView.swift` | server address, password, settings |

## Re-run xcodegen whenever a file is added

```bash
cd ios && xcodegen generate
```

The file list is written **into** the `.pbxproj` when the project is
generated; Xcode does not rescan the directory on build. So a new file
pulled from git is simply not in the target, and the symptom is a
compile error about whatever it declared:

```
Cannot find 'MP' in scope
```

which reads as a broken reference rather than a missing file. If a pull
brought new Swift and the build suddenly cannot see a type it saw
before, this is why. That is the cost of generating the project instead
of committing it, and it is still the better trade - a `.pbxproj` is a
wall of UUIDs that conflicts on every branch.

## Things that will bite you

**The server address is configuration, not a constant.** It is loopback
in development, a tunnel hostname in the house, and it moves again when
the order side goes to the cluster. First launch asks for it.

**Over plain `http` the token and password cross the network in clear.**
There is no ATS exception in the Info.plist, so a LAN address will simply
fail - which is deliberate. Use the tunnel.

**A 401 clears the token and posts `.mplabelSignedOut`.** The network
layer does not reach into the UI; the UI listens. Do not "fix" this by
making `APIClient` touch `Session` directly - it is an actor and `Session`
is `@MainActor`, and the hop is the bug that arrangement avoids.

**There is no marker decoder here and there should not be one.**
`marker.py` and `marker.js` are only trustworthy because 137 assertions
pin them to each other under node. A third implementation in Swift would
have none of that harness, and the QR made it unnecessary.

**The printer is write-only, so "printed" is not a fact.** The detail
screen says the label is *recorded* as printed and that the paper is the
only proof. Do not tighten that wording - it is the difference between
what the system knows and what it hopes.

**The queue payload carries no address on purpose.** `_order_row` strips
it; only the detail screen asks for one. Do not add `ship_to` to the list
row to save a request.
