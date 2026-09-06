# Handing the iPhone app over to a session on the Mac

Written on the Windows workstation, which cannot compile Swift. Every
line of `ios/` was authored there and none of it was ever run by the
session that wrote it, so this is as much a list of what is *unverified*
as what is done.

## Where things are

Branch **`bins-and-the-native-app`**, 33 commits, **PR #32, not merged**.
`main` has none of it. The Pi is running this branch, not `main`.

`pytest` is 520 passing, 3 skipped. That is the whole gate on the Python
side and it should stay green.

The plan being worked is
`~/.claude/plans/graceful-frolicking-walrus.md` on the *Windows* box —
it will not be on the Mac. Its shape: phase 0 the scanner bug (done),
1 the design system (done), 2 Sold and Profit (done), 3 the schema for
the sourcing half (done), **4 the API for it (not started)**, 5 the
remaining screens (not started).

## The immediate next thing

```bash
SIMULATOR="iPhone 17 Pro" ./ios/run-ui-tests.sh
```

**No UI test has ever actually executed.** The last run compiled all
three targets and then skipped all eight, because `TEST_RUNNER_*` was
being passed as an xcodebuild argument (a build setting) rather than in
xcodebuild's environment. That is fixed but unproven.

So the next run is the first that exercises anything, and failures
there are expected. The selector queries are the part that was guessed
at hardest — particularly:

- `app.staticTexts["No bin"]` inside a `Button`: SwiftUI often collapses
  a button's children into one accessibility element, so the inner text
  may not be addressable.
- `MPHoldButton` timing in `testShippingNeedsAHoldNotATap`. It holds for
  1.4s against an 0.8s duration, and asserts a *tap* does nothing. If
  the tap half ever fails, that is not a flaky test — it means the
  guard on an irreversible action is gone.

Batch the failures rather than fixing one at a time; they likely share
a cause.

## What this machine's toolchain needed

Each of these cost a round trip. They are in `ios/run-ui-tests.sh` as
preflights now, but worth knowing:

- **Xcode is a beta in `~/Downloads`**, not `/Applications`. `xcode-select`
  had to be pointed at it. Downloads is a directory people empty; if it
  goes, this breaks again.
- **The Mac's system `python3` has none of the dependencies.**
  `mplabel.cli` imports `label`, which imports `pdfplumber` at module
  scope, so every entry point needs the full set. The script prefers
  `$REPO/.venv/bin/python` if it exists.
- **`xcodegen generate` must be re-run whenever a file is added.** The
  file list lives inside the `.pbxproj` and is written at generate time;
  a new file from a pull is simply not in the target, and the symptom is
  a compile error about whatever it declared.

## Things believed and then found wrong

Recorded because each was stated confidently before a real machine
disagreed, and the same reasoning could recur:

- **An XCUITest bundle does not run on the Mac.** It is an iOS process
  on the simulator beside the app, so it has no `Process` and cannot
  start a server. Hence the shell script. What *is* true is that the
  simulator shares the host's network stack, so `127.0.0.1` reaches a
  server on the Mac.
- **`SWIFT_VERSION` is a language mode, not a compiler release.** Valid
  values are 4, 4.2, 5, 6. It is pinned to `5` deliberately: Xcode 27
  ships a Swift 6 compiler, and under mode 6 the concurrency
  diagnostics this code has already hit twice become errors.
- **`INFOPLIST_KEY_*` only handles top-level plist keys.** An attempt to
  set `NSAppTransportSecurity.NSAllowsLocalNetworking` that way was
  silently ignored. It is now a real nested dictionary in the plist,
  which means it applies to **every** configuration, not Debug only.

## Two open items that are not bugs in the app

**The Pi's `bin_code` has no foreign key.** A column added by `ALTER
TABLE ... ADD COLUMN bin_code TEXT` gets no constraint, while the same
column in `SCHEMA` gets one — so a migrated database and a fresh one
disagree. The declaration is fixed for databases that have not migrated
yet, and
`test_a_migrated_database_has_the_same_foreign_keys_as_a_fresh_one`
guards it. SQLite cannot add the constraint to an existing column
without rebuilding the table, so the Pi keeps the unconstrained one
until someone decides that is worth doing. `set_bin` checks the bin
exists regardless, so this is a missing backstop rather than a live
hole.

**A LAN address over plain http now works.** `NSAllowsLocalNetworking`
had to go in for the UI tests and covers the private ranges. So
`http://10.0.2.250:8080` will connect and send the password and bearer
token across the Wi-Fi in clear. Plain http to an *internet* host still
fails, so a tunnel hostname must be https. Earlier advice in this repo
said a LAN address would simply fail; that is no longer true.

## What is still untested on hardware

- **The scanner has never read a label through this app.** The
  simulator has no camera. Apple's Camera app reading a printed QR
  proved the ink survives thermal; it says nothing about whether
  `DataScannerViewController` is wired up the way `ScanView` wires it.
- **Nothing has been printed from the app.** That is the one action
  that spends physical stock, and the printer cannot confirm a print.
