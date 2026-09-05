# The native iPhone client

A SwiftUI app against the same `/api/v1` that the PWA uses. It exists for
one reason the web app cannot cover: **Safari has no `BarcodeDetector`**,
so a web page cannot read the QR on a printed label, and VisionKit can.

That was settled on paper before any of this was written - a printed
`inventory-label --qr` read first time in the stock Camera app, so the
module size at 5 dots survives thermal bleed. See the QR encoder row in
`CLAUDE.md`.

## Status

**Written, never compiled.** Every line here was authored on a Windows
workstation, which cannot run Xcode. Treat the first build as a code
review with a compiler doing the reading: expect a handful of signature
and availability fixes, and do not expect it to be wrong about *shapes* -
the models were written against `web.py`'s actual payloads
(`_order_row`, `_order_detail`, `h_inventory`, `h_item`, `h_bins`)
rather than against the design.

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
