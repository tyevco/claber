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
(26.0) and `NSCameraUsageDescription` in Info.plist - without that string
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

## Tests

⌘U in Xcode, or:

```bash
xcodebuild test -project ios/MPLabel.xcodeproj -scheme MPLabel \
  -destination 'platform=iOS Simulator,name=iPhone 15'
```

Two bundles, and they answer different questions.

**`MPLabelTests`** decodes committed JSON fixtures into the models. The
fixtures are **generated, never hand-written** — `python
tests/make_ios_fixtures.py` starts a real server against a real
temporary database and writes what comes back. That property is the
whole point: a JSON file typed out by hand would carry the same belief
as the models it checks, and that belief has been wrong twice. A pytest
guard fails if the server's shape drifts from the committed copies.

**`MPLabelUITests`** drives the real app against **the real server**, not
a mock. Run them with the script, not ⌘U:

```bash
./ios/run-ui-tests.sh
SIMULATOR="iPhone 16 Pro" ./ios/run-ui-tests.sh
MPLABEL_PYTHON=/usr/bin/python3 ./ios/run-ui-tests.sh
```

The script starts `mplabel serve` against a temporary seeded database,
passes its address to the runner, and tears it down afterwards.

**The server cannot be started from inside the test bundle**, which is
worth knowing before trying: an XCUITest runner is an iOS process
running on the simulator beside the app, so `Process` is not in scope
there at all. Only the *host* half of the toolchain can spawn anything,
which is why this is a shell script. What is true is that the simulator
shares the host's network stack, so a `127.0.0.1` URL in the app reaches
a server on the Mac.

Still the real server rather than a stub: a stub would answer what we
*believe* `web.py` answers, so it would have agreed with the models on
both occasions it mattered and caught neither.

⌘U on the UI bundle skips with a sentence saying to run the script,
rather than failing every test with a connection error.

`TestHooks` is how a test points the app at that server and skips the
login screen. It is `#if DEBUG` throughout, so on a release build there
is no path from a launch argument to the app's credentials.

**ATS:** one exception, `NSAllowsLocalNetworking`, and it is the narrow
one — http to unqualified names, `.local` and the private IP ranges.
Not `NSAllowsArbitraryLoads`, which would permit the whole internet.

It applies to every configuration rather than Debug only. That is a
correction, not a choice: the Debug-only version set
`INFOPLIST_KEY_NSAppTransportSecurity_NSAllowsLocalNetworking`, and no
such build setting exists — `INFOPLIST_KEY_*` handles top-level plist
keys and `NSAppTransportSecurity` is a dictionary, so it would have been
ignored in silence. What still holds is the property that mattered: the
app cannot talk to a plain-http host on the internet, so a tunnel
hostname over http fails on her phone exactly as before.

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
A LAN address now *works* - `NSAllowsLocalNetworking` had to go in for
the UI tests, and it covers the private IP ranges as well as loopback.
So the thing that used to stop this is gone.

What still fails is plain http to an internet host, so a tunnel hostname
must be https. But `http://10.0.2.250:8080` will now connect happily and
send her password and bearer token across the Wi-Fi in clear. Fine for
poking at it on a bench; not the way to leave it configured.

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

## Releasing it

```bash
git tag ios-v1.2.0 && git push origin ios-v1.2.0
```

That builds, signs and uploads to App Store Connect on a GitHub macOS
runner - no Mac needed, and the whole suite runs first. The Apple-side
setup is in `docs/ios-release.md`: an App Store Connect API key with the
**Admin or App Manager** role (a Developer key can upload but cannot mint
a certificate, and the failure says neither of those things), the app
record, and three repository secrets.

The version comes off the tag; the build number is a UTC timestamp,
because App Store Connect refuses a number it has already seen and
refuses it *after* the upload. Both are read back out of the built app
before anything is sent - see the note in `CLAUDE.md` about the two
indirections they pass through.

`./ios/run-ui-tests.sh` takes `MPLABEL_ONLY_TESTING=all` now, which drops
the `-only-testing` flag so both bundles run in one pass against one
server and one simulator boot. That is what CI uses. Bare, the script
still runs the UI tests and nothing else.

## Pictures of it

```bash
./ios/screenshots.sh            # every screen into ios/screenshots/
OUT=/tmp/shots ./ios/screenshots.sh
```

Same seeded server the UI tests use. Not a snapshot test - nothing is
compared against a committed image, because a pixel diff on a design
that is still moving fails whenever a padding changes. It is there to
make looking cheap, which is how the spinner-that-never-stopped and the
buried save button were both found.
