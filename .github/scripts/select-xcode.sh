#!/usr/bin/env bash
# Point xcode-select at an Xcode new enough to build this app, and fail
# with a sentence rather than a compile error if there is not one.
#
# The deployment target is iOS 26 and the app uses FoundationModels. An
# older Xcode does not refuse the project - it builds it until it reaches
# the first symbol it has never heard of, and reports that. So the check
# is on the SDK version, before anything is compiled.
#
# Runner images move. `macos-latest` follows them, which is exactly the
# wrong property here, so the workflows pin a label and this script is
# what notices when the pin has gone stale.
#
#   XCODE_APP=/Applications/Xcode_26.1.app .github/scripts/select-xcode.sh

set -euo pipefail

MIN_SDK_MAJOR=26

if [ -n "${XCODE_APP:-}" ]; then
    APP="$XCODE_APP"
else
    # Version sort, so 26.10 would come after 26.9 rather than before it.
    APP="$(ls -d /Applications/Xcode*.app 2>/dev/null | sort -V | tail -1)"
fi

if [ -z "$APP" ] || [ ! -d "$APP" ]; then
    echo "::error::No Xcode in /Applications. This runner image cannot build the app." >&2
    exit 1
fi

sudo xcode-select -s "$APP/Contents/Developer"

echo "Xcodes on this runner:"
ls -d /Applications/Xcode*.app | sed 's/^/    /'
echo "Selected: $APP"
xcodebuild -version

SDK="$(xcrun --sdk iphoneos --show-sdk-version)"
echo "iphoneos SDK: $SDK"

if [ "${SDK%%.*}" -lt "$MIN_SDK_MAJOR" ]; then
    cat >&2 <<MSG
::error::This runner's newest Xcode has the iOS $SDK SDK, and the app
needs $MIN_SDK_MAJOR or later - the deployment target is iOS 26.0 and
OnDevice.swift uses FoundationModels.

Left alone this does not fail here. It fails part way through compiling,
on whichever symbol the older SDK reaches first, which reads as broken
Swift rather than as the wrong toolchain.

Fix it by moving the runner label in .github/workflows/*.yml to a newer
image, or by setting XCODE_APP to one that is installed.
MSG
    exit 1
fi
