#!/usr/bin/env bash
# Read the version numbers back out of the built app, and refuse to
# upload placeholders.
#
#   .github/scripts/check-archive.sh path/to/MPLabel.xcarchive
#
# The numbers reach the app through two indirections - an xcodebuild
# command-line override sets a build setting, and `$(MARKETING_VERSION)`
# in project.yml's `info:` block expands it into the plist - and neither
# fails loudly when it does not happen. A misspelled setting name, or an
# Info.plist key someone replaced with a literal, produces a perfectly
# successful archive carrying XcodeGen's defaults of "1.0" and "1".
#
# App Store Connect is otherwise where that gets found out: the first
# such build is accepted, and every one after it is rejected for a
# duplicate build number, twenty minutes into a run. So the archive is
# asked what it actually says.

set -euo pipefail

ARCHIVE="${1:?usage: check-archive.sh <path to .xcarchive>}"
PLIST="$ARCHIVE/Products/Applications/MPLabel.app/Info.plist"

if [ ! -f "$PLIST" ]; then
    echo "::error::No Info.plist at $PLIST - the archive is not shaped as expected." >&2
    ls -R "$ARCHIVE/Products" >&2 || true
    exit 1
fi

read_key() {
    /usr/libexec/PlistBuddy -c "Print :$1" "$PLIST" 2>/dev/null || echo ""
}

SHORT="$(read_key CFBundleShortVersionString)"
BUILD="$(read_key CFBundleVersion)"
BUNDLE="$(read_key CFBundleIdentifier)"
REVISION="$(read_key MPLabelSourceRevision)"
# Via a real file, not a pipe: PlistBuddy seeks, so /dev/stdin is not a
# thing it can read, and the failure is an empty string that reads as a
# missing entitlement.
ENT="$(mktemp)"
trap 'rm -f "$ENT"' EXIT
codesign -d --entitlements :- --xml \
    "$ARCHIVE/Products/Applications/MPLabel.app" >"$ENT" 2>/dev/null || true
APS="$(/usr/libexec/PlistBuddy -c 'Print :aps-environment' "$ENT" 2>/dev/null || echo "")"

echo "Version   $SHORT"
echo "Build     $BUILD"
echo "Bundle    $BUNDLE"
echo "Revision  $REVISION"
echo "APNs      ${APS:-<none>}"

fail=0

# The literals from project.yml's defaults. Seeing either here means the
# override did not arrive, not that somebody is genuinely shipping 1.0.
# A real 1.0 release is `ios-v1.0.0`, which is three components and does
# not collide with this.
if [ "$SHORT" = "1.0" ] || [ -z "$SHORT" ]; then
    echo "::error::CFBundleShortVersionString is '$SHORT' - the MARKETING_VERSION override did not reach the plist." >&2
    fail=1
fi
if [ "$BUILD" = "1" ] || [ -z "$BUILD" ]; then
    echo "::error::CFBundleVersion is '$BUILD' - the CURRENT_PROJECT_VERSION override did not reach the plist." >&2
    fail=1
fi
if [ "$BUNDLE" != "com.marchvector.Sellomatic" ]; then
    echo "::error::Bundle id is '$BUNDLE', not com.marchvector.Sellomatic. Push and provisioning are both registered against that string." >&2
    fail=1
fi
if [ "$REVISION" = "checkout" ] || [ -z "$REVISION" ]; then
    echo "::error::MPLabelSourceRevision is '$REVISION' - this build cannot say which commit made it." >&2
    fail=1
fi

# A warning rather than an error. Xcode rewrites aps-environment to
# `production` when it re-signs during -exportArchive, which has not
# happened yet at this point - so `development` here is expected and
# `<none>` is the thing worth noticing, because it means Push.swift will
# never get a token on a real handset.
if [ -z "$APS" ]; then
    echo "::warning::No aps-environment entitlement in the archive. Notifications will not register on a device." >&2
fi

exit "$fail"
