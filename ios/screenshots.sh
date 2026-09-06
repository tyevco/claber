#!/usr/bin/env bash
# A picture of every screen, into ios/screenshots/ (gitignored).
#
#   ./ios/screenshots.sh
#   SIMULATOR="iPhone 16 Pro" ./ios/screenshots.sh
#   OUT=/tmp/shots ./ios/screenshots.sh
#
# Same seeded server the UI tests use - the pictures are of the app
# talking to a real `mplabel serve`, not to a stub, for the same reason
# the tests are.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$REPO/ios/screenshots}"
BUNDLE="$(mktemp -d)/shots.xcresult"

# The heavy lifting - seeding, the server, the simulator check - is all
# in run-ui-tests.sh already. Reuse it rather than growing a second copy
# that drifts.
export MPLABEL_SHOTS=1
export MPLABEL_ONLY_TESTING="MPLabelUITests/ScreenshotTests"
export MPLABEL_RESULT_BUNDLE="$BUNDLE"
"$REPO/ios/run-ui-tests.sh"

rm -rf "$OUT"
mkdir -p "$OUT"
xcrun xcresulttool export attachments --path "$BUNDLE" --output-path "$OUT" \
    >/dev/null

# Attachments come out named by UUID; the manifest is what knows which
# screen each one is. Rename them so the directory is readable.
python3 - "$OUT" <<'PY'
import json, os, pathlib, sys

out = pathlib.Path(sys.argv[1])
manifest = out / "manifest.json"
if not manifest.exists():
    sys.exit("no manifest - did any screenshot get taken?")
for entry in json.loads(manifest.read_text()):
    for att in entry.get("attachments", []):
        src = out / att["exportedFileName"]
        # "05-triage_0_<uuid>.png" -> "05-triage.png"
        name = att.get("suggestedHumanReadableName", src.name).split("_")[0]
        if src.exists():
            src.rename(out / f"{name}.png")
manifest.unlink()
PY

echo "==> $(ls "$OUT" | wc -l | tr -d ' ') screens in $OUT"
ls "$OUT"
