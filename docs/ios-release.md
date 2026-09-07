# Getting the app into TestFlight

`.github/workflows/ios-release.yml` builds the app on a GitHub macOS
runner, signs it, and uploads it to App Store Connect. It cannot be run
anywhere else in this repo: there is no Mac here.

```bash
git tag ios-v1.2.0
git push origin ios-v1.2.0
```

That is the whole release. The rest of this page is the Apple-side setup
that has to happen once, none of which is in the repo, and the failures
that follow from getting a piece of it wrong.

The `ios-` prefix is deliberate. The Pi half of this repo will want
release tags of its own one day, and `v1.2.0` on the Python side must not
spend an App Store review slot.

## What you have to do once

### 1. A paid membership, and the app record

TestFlight needs the **Apple Developer Program**, not a free Apple ID.
The free tier gives a 7-day provisioning profile, which is enough to put
a build on her phone from Xcode and is not enough to upload anything.

Then **App Store Connect → Apps → +**, with the bundle id
`com.marchvector.Sellomatic`. The record has to exist before the first
upload: without it the upload fails at the end of a twenty-minute run
with a message about the bundle id not being found, which reads as a
signing problem.

### 2. An App Store Connect API key

**App Store Connect → Users and Access → Integrations → App Store
Connect API → +.**

Give it the **Admin** or **App Manager** role. Not Developer, and this
is the one that costs an afternoon: a Developer key can upload a build
but cannot mint a distribution certificate, so `-allowProvisioningUpdates`
fails part way through the archive with an authorisation error that says
nothing about roles.

It downloads once, as `AuthKey_XXXXXXXXXX.p8`. There is no second copy —
Apple does not keep one. `.gitignore` covers `*.p8`; keep the original
somewhere that is not this repo.

Note this is **not** the APNs key from `docs/notifications.md`. Same file
extension, same account, different key, different purpose: that one signs
push notifications from the Pi, this one signs uploads. Mixing them up
produces an authentication failure on both sides at once.

### 3. Three secrets on the repository

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | What |
|---|---|
| `ASC_KEY_ID` | The `XXXXXXXXXX` from the filename. Also shown in the key list |
| `ASC_ISSUER_ID` | A UUID, at the top of the same App Store Connect page. One per account, not per key |
| `ASC_KEY_P8` | The key file, base64'd |

```bash
base64 -i AuthKey_XXXXXXXXXX.p8 | pbcopy
```

Base64, not the file's text, because a secret is a single value and a PEM
key is several lines. The workflow decodes it and checks the result
starts with `-----BEGIN` before it does anything with it, so a key pasted
in raw fails immediately with a sentence rather than at signing with an
authentication error.

### 4. Push, if it is wanted on a TestFlight build

The **Push Notifications capability on the App ID**, which
`docs/notifications.md` already asks for. `MPLabel.entitlements` carries
`aps-environment: development`, and Xcode rewrites that to `production`
when it re-signs during export — so a TestFlight build gets a production
token and a build run from Xcode gets a sandbox one.

Which means `apns_environment` in `/etc/mplabel.conf` has to say
`production` for a phone running a TestFlight build. The app sends the
environment alongside the token for exactly this reason; a sandbox token
offered to the production host comes back `BadDeviceToken`, which reads
as malformed rather than as addressed to the wrong Apple.

## What the workflow does

**Suite**, then **Archive and upload**, as two jobs, so the second cannot
start on a red suite.

The suite is the whole thing: `pytest` — including
`test_the_ios_fixtures_are_still_what_the_server_sends`, which is where a
drift between `web.py` and the Swift models is caught — and then both
Swift bundles in one pass, the eleven UI journeys against a real
`mplabel serve` on a seeded temporary database.

Signing is Apple's **cloud signing**: `-allowProvisioningUpdates` with
the API key, so the distribution certificate and the App Store profile
are fetched from Apple on each run. There is no `.p12` in the secrets to
expire in a year and no `.mobileprovision` to re-export. The cost is the
role requirement above.

The upload is `-exportArchive` with `destination: upload` in
`ios/ExportOptions.plist` — Xcode's own route, the same one Organizer
uses. `altool --upload-app` is the other one and it is the deprecated
one.

## The two numbers

**Marketing version** comes from the tag: `ios-v1.2.0` → `1.2.0`. One to
three integers, checked before the archive rather than discovered by
Apple after it.

**Build number** is a UTC timestamp in three parts —
`2026.907.1528`, being year, month-day, hour-minute. Every bit of that
shape is load bearing:

- It has to increase **for ever**. App Store Connect refuses a build
  number it has seen, and refuses it *after* the upload, so a repeat
  costs the whole run.
- `github.run_number` is the obvious source and is wrong: it is per
  workflow *file*, so renaming the workflow restarts it at 1 and every
  build after that is rejected for being older than one from last year.
  A commit count is wrong too — it goes backwards the first time a branch
  is rebuilt.
- Three components rather than one long integer, because each has to stay
  inside four digits.
- It reads as a date, so a build in TestFlight says when it was cut
  without anyone looking it up.

Both are printed in the run's summary, and **read back out of the built
app** before the upload. That check is not ceremony: the numbers reach
the app through two indirections — a command-line build setting, then
`$(MARKETING_VERSION)` expanding inside the Info.plist — and neither
fails loudly. A misspelling produces a perfectly successful archive
carrying XcodeGen's defaults of `1.0` and `1`. The first such build
uploads fine and every one after it is rejected for a duplicate.

The build also carries `MPLabelSourceRevision`, the commit it was made
from — the same question `build.py` answers on the Pi, and a checkout
says `checkout` for the same reason. A TestFlight build outlives the
branch it came from and there is no other source for "which commit was
that".

## When it goes wrong

| What you see | What it is |
|---|---|
| Authorisation error during the archive | The API key is Developer role. It needs Admin or App Manager |
| `No suitable application records were found` | The app record does not exist in App Store Connect, or the bundle id differs |
| `ASC_KEY_P8 decoded to something that is not a PEM private key` | The secret holds the file's text rather than its base64 |
| The runner's Xcode is too old | The `macos-26` label has gone stale. `select-xcode.sh` says so before anything compiles; move the label in **both** workflows |
| Build stuck on "Missing Compliance" | Should not happen — `ITSAppUsesNonExemptEncryption` is `false` in `project.yml`. If it does, that key did not reach the plist |
| Uploaded, but not in TestFlight | Apple processes first. Usually minutes, occasionally an hour |
| Nothing happened when the tag was pushed | The tag has to start `ios-v`. `v1.2.0` matches nothing |

## Running it by hand

Everything the workflow does is a command, on a Mac with the key:

```bash
cd ios && xcodegen generate && cd ..

xcodebuild archive \
    -project ios/MPLabel.xcodeproj -scheme MPLabel \
    -configuration Release -destination 'generic/platform=iOS' \
    -archivePath build/MPLabel.xcarchive \
    -allowProvisioningUpdates \
    -authenticationKeyPath "$PWD/AuthKey_XXXXXXXXXX.p8" \
    -authenticationKeyID XXXXXXXXXX \
    -authenticationKeyIssuerID xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
    MARKETING_VERSION=1.2.0 \
    CURRENT_PROJECT_VERSION="$(date -u +%Y).$(date -u +%m%d | sed 's/^0*//').$(date -u +%H%M | sed 's/^0*//')" \
    MPLABEL_SOURCE_REVISION="$(git rev-parse --short=12 HEAD)"

.github/scripts/check-archive.sh build/MPLabel.xcarchive

xcodebuild -exportArchive \
    -archivePath build/MPLabel.xcarchive \
    -exportPath build/export \
    -exportOptionsPlist ios/ExportOptions.plist \
    -allowProvisioningUpdates \
    -authenticationKeyPath "$PWD/AuthKey_XXXXXXXXXX.p8" \
    -authenticationKeyID XXXXXXXXXX \
    -authenticationKeyIssuerID xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Archiving from Xcode's own Organizer works too and is the thing to reach
for when the workflow is failing for a reason that is not the build — it
uses the same `ExportOptions` choices from the Distribute sheet, and it
will happily pick its own build number, which is the one thing to watch.
