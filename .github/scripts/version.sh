#!/usr/bin/env bash
# Decide the two numbers that go on the build, and write them as GitHub
# Actions outputs.
#
#   EVENT=push TAG=ios-v1.2.0 .github/scripts/version.sh
#   EVENT=workflow_dispatch INPUT_VERSION=1.2.0 [INPUT_BUILD=...] ...
#
# Marketing version comes from the tag or from the person who pressed
# the button. Build number is a UTC timestamp, and the reasoning for
# that is below - it is the part worth reading.

set -euo pipefail

EVENT="${EVENT:-workflow_dispatch}"

if [ "$EVENT" = "push" ]; then
    # Two steps, because `${TAG#...}` on an unset TAG is an unbound
    # variable under `set -u` and kills the script before it can say so.
    VERSION="${TAG:-}"
    VERSION="${VERSION#ios-v}"
    if [ "$VERSION" = "${TAG:-}" ]; then
        echo "::error::Tag '${TAG:-}' does not start with ios-v, so there is no version in it." >&2
        exit 1
    fi
else
    VERSION="${INPUT_VERSION:-}"
fi

if [ -z "$VERSION" ]; then
    echo "::error::No marketing version. Tag as ios-v1.2.0, or type one into the workflow." >&2
    exit 1
fi

# One to three integers separated by dots, which is what App Store
# Connect accepts. Checked here rather than discovered at the end of a
# twenty-minute archive, and a version that has already been used is the
# other way this goes wrong - Apple rejects that one and only that one
# can be found out by asking Apple.
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+(\.[0-9]+){0,2}$'; then
    echo "::error::'$VERSION' is not a version App Store Connect will take. One to three integers, e.g. 1.2.0." >&2
    exit 1
fi

if [ -n "${INPUT_BUILD:-}" ]; then
    BUILD="$INPUT_BUILD"
else
    # A UTC timestamp, in three parts, and every bit of that shape is
    # load bearing.
    #
    # It has to increase for ever: App Store Connect refuses a build
    # number it has already seen, and it refuses it *after* the upload,
    # so a repeat costs the whole run. `github.run_number` looks like the
    # obvious source and is not - it is per workflow *file*, so renaming
    # this one restarts it at 1 and every subsequent build is rejected
    # for being older than one from last year. A commit count is not it
    # either: it goes backwards the first time a branch is rebuilt.
    #
    # Three components rather than one long integer, because each one
    # has to stay inside four digits. Leading zeros are stripped with
    # 10# - not decoration: bash reads 0907 as octal, and 09 is not a
    # valid octal number, so the arithmetic would fail outright every
    # September.
    #
    # It reads as year.monthday.hourminute, so a build in TestFlight
    # says when it was cut without anyone looking it up.
    NOW="$(date -u +%Y%m%d%H%M)"
    BUILD="${NOW:0:4}.$((10#${NOW:4:4})).$((10#${NOW:8:4}))"
fi

if ! printf '%s' "$BUILD" | grep -Eq '^[0-9]+(\.[0-9]+){0,2}$'; then
    echo "::error::'$BUILD' is not a build number App Store Connect will take." >&2
    exit 1
fi

echo "marketing_version=$VERSION"
echo "build_number=$BUILD"
