# The iPhone app: where it stands

Started as a handover from the Windows workstation, which cannot compile
Swift — every line of `ios/` was authored there and none of it had ever
been run. Most of what that document listed as unverified has since been
run on a Mac, so this is now a statement of where the app actually is,
keeping the parts of the handover that are still load bearing.

## What is proven

Three suites, all green, and each answers a different question:

```bash
pytest                                     # 538 - the server the app talks to
xcodebuild test -project ios/MPLabel.xcodeproj -scheme MPLabel \
    -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
    -only-testing:MPLabelTests             # 24 - do the models match the payloads
./ios/run-ui-tests.sh                      # 13 - does the app work against a real server
```

`run-ui-tests.sh` starts a real `mplabel serve` against a temporary
seeded database and hands the runner its address. Deliberately the real
server rather than a mock: a stub would answer what we *believe*
`web.py` answers, and that belief has been wrong twice.

The app has also been run against the Pi through a cloudflared tunnel,
so the bearer token, the `/api/v1` prefix and every Codable shape are
confirmed on her real orders rather than on a fixture.

## What is still unverified, and needs a real device

- **The scanner has never read a label through this app.** The simulator
  has no camera. Apple's Camera app reading a printed QR proved the ink
  survives thermal; it says nothing about whether
  `DataScannerViewController` is wired up the way `ScanView` wires it.
- **`CaptureView`'s still camera has never run.** `AVCapturePhotoOutput`,
  the shutter, and the upload-with-retry path are all unexercised for the
  same reason.
- **Nothing has been printed from the app.** That is the one action that
  spends physical stock, and the printer cannot confirm a print.

## What the Mac's toolchain needed

Each of these cost a round trip. They are preflights in
`ios/run-ui-tests.sh` now, but worth knowing:

- **Xcode is a beta in `~/Downloads`**, not `/Applications`.
  `xcode-select` had to be pointed at it. Downloads is a directory people
  empty; if it goes, this breaks again.
- **The Mac's system `python3` has none of the dependencies.**
  `mplabel.cli` imports `label`, which imports `pdfplumber` at module
  scope, so every entry point needs the full set. The script prefers
  `$REPO/.venv/bin/python` if it exists.
- **`xcodegen generate` must be re-run whenever a file is added.** The
  file list lives inside the `.pbxproj` and is written at generate time;
  a new file from a pull is simply not in the target, and the symptom is
  a compile error about whatever it declared.
- **`git` and `gh` can disagree about who you are.** `gh auth status`
  said the right account while git's `osxkeychain` helper still held
  another one with no write access — which presents as a 403 on push
  and nothing else.

## Things believed and then found wrong

Recorded because each was stated confidently before a real machine
disagreed, and the same reasoning could recur.

From the Windows box:

- **An XCUITest bundle does not run on the Mac.** It is an iOS process on
  the simulator beside the app, so it has no `Process` and cannot start a
  server. Hence the shell script. What *is* true is that the simulator
  shares the host's network stack, so `127.0.0.1` reaches a server on the
  Mac.
- **`SWIFT_VERSION` is a language mode, not a compiler release.** Valid
  values are 4, 4.2, 5, 6. It is pinned to `5` deliberately: Xcode 27
  ships a Swift 6 compiler, and under mode 6 the concurrency diagnostics
  this code has already hit twice become errors.
- **`INFOPLIST_KEY_*` only handles top-level plist keys.** An attempt to
  set `NSAppTransportSecurity.NSAllowsLocalNetworking` that way was
  silently ignored. It is now a real nested dictionary in the plist,
  which means it applies to **every** configuration, not Debug only.
- **`TEST_RUNNER_*` must be in xcodebuild's environment, not among its
  arguments.** A trailing `KEY=value` is a build setting override, and
  build settings do not reach the runner process. This is why no UI test
  had ever executed: the first real run skipped all eight, saying it had
  no server, which was true for a reason the message could not guess at.

Once the tests actually ran:

- **XCUITest sorts a class's methods alphabetically, not by source
  order.** `testShipping…` runs before `testTheQueue…`, so the test that
  ships a parcel emptied the queue a later test asserts on — and the file
  had a comment saying the shipping test went last. Tests that change
  data restore it in `tearDownWithError` instead.
- **Styling must not reach the accessibility tree.** `MPEyebrow`
  uppercases its caption, and the uppercased string was the accessibility
  label, so `staticTexts["Ships to"]` matched nothing while the words
  were plainly on screen. It carries `.accessibilityLabel(text)` now,
  which also stops VoiceOver shouting.
- **A `TextField(axis: .vertical)` is a textView, not a textField.**
  Query the form by `accessibilityIdentifier` rather than by shape or
  placeholder — a placeholder is copy, and copy gets reworded by people
  who do not know a test is reading it.
- **A screen with no route to it is the quietest kind of deletion.**
  Capture took Pending's tab (iOS hides the sixth behind "More"), so
  Pending moved to the queue's "to print" chip and a UI test pins that
  the route exists.

## The floor is iOS 26

Raised from 17 deliberately. Nothing needed more than 17 until
`FoundationModels` - the on-device model, which is how a suggestion gets
made on the phone rather than by sending her photographs to somebody's
API. Supporting 17 as well would put an `#available` on every one of
those call sites and a second path that cannot be tested on the only
handset there is.

The image half of that framework is **27**, not 26, so passing a
photograph to the model still needs its own `#available` until the floor
moves again. Text in and structured output back (`@Generable`) is
available at 26.

## Two open items that are not bugs in the app

**The Pi's `bin_code` has no foreign key.** A column added by `ALTER
TABLE ... ADD COLUMN bin_code TEXT` gets no constraint, while the same
column in `SCHEMA` gets one — so a migrated database and a fresh one
disagree. The declaration is fixed for databases that have not migrated
yet, and
`test_a_migrated_database_has_the_same_foreign_keys_as_a_fresh_one`
guards it. SQLite cannot add the constraint to an existing column
without rebuilding the table, so the Pi keeps the unconstrained one until
someone decides that is worth doing. `set_bin` checks the bin exists
regardless, so this is a missing backstop rather than a live hole.

**A LAN address over plain http now works.** `NSAllowsLocalNetworking`
had to go in for the UI tests and covers the private ranges. So
`http://10.0.2.250:8080` will connect and send the password and bearer
token across the Wi-Fi in clear. Plain http to an *internet* host still
fails, so a tunnel hostname must be https. Earlier advice in this repo
said a LAN address would simply fail; that is no longer true.

## Where the work is

Branch **`bins-and-the-native-app`**, **PR #32, not merged**. `main` has
none of it, and the Pi is running this branch.

The screens the design has and the app does not are listed in CLAUDE.md
under *Open work → The sourcing half*. The API behind all of them
exists; what is left is client work.
