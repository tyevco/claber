#!/usr/bin/env bash
# Run the UI tests against a real mplabel server.
#
# The server cannot be started from inside the test bundle: an XCUITest
# runner is an iOS process on the simulator, so it has no Process and no
# way to spawn anything. It is started here, on the Mac, and its address
# is handed to the runner through TEST_RUNNER_* variables - xcodebuild
# sets those on the runner with the prefix stripped.
#
# The simulator shares the host's network stack, so 127.0.0.1 in the app
# reaches this server.
#
# Deliberately the real server rather than a mock: a stub would answer
# what we believe web.py answers, and that belief has been wrong twice.
#
#   ./ios/run-ui-tests.sh                     # default simulator
#   SIMULATOR="iPhone 16 Pro" ./ios/run-ui-tests.sh
#   MPLABEL_PYTHON=/usr/bin/python3 ./ios/run-ui-tests.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# A virtualenv in the repo wins over the system python, because the
# system one on a Mac almost certainly does not have Pillow and
# pdfplumber - and `mplabel.cli` imports `label`, which imports
# pdfplumber at module scope, so *every* entry point needs them.
if [ -n "${MPLABEL_PYTHON:-}" ]; then
    PYTHON="$MPLABEL_PYTHON"
elif [ -x "$REPO/.venv/bin/python" ]; then
    PYTHON="$REPO/.venv/bin/python"
else
    PYTHON="python3"
fi

SIMULATOR="${SIMULATOR:-iPhone 16}"
PASSWORD="uitest-password"
# Not a fixed port: a server left running from an interrupted run would
# be picked up silently, and the tests would pass against stale data.
PORT="${PORT:-$((49200 + RANDOM % 700))}"

# Exported before the check, not after: the rest of the script runs with
# this set, so a preflight without it could reject a python that would
# actually have worked.
export PYTHONPATH="$REPO/src"

# Check before doing anything, and say what to run. Without this the
# first failure is a ModuleNotFoundError from inside a heredoc, which
# reads as a broken script rather than an environment that has never had
# the dependencies installed.
if ! "$PYTHON" -c "import mplabel.cli" >/dev/null 2>&1; then
    cat >&2 <<EOF
$PYTHON cannot import mplabel.

Most likely it has none of the dependencies: mplabel.cli imports label,
which imports pdfplumber at module scope, so every entry point needs
them. Set one up once:

    cd "$REPO"
    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"

This script picks up .venv/bin/python automatically after that. Or point
it somewhere else with MPLABEL_PYTHON=/path/to/python.

The underlying error:
EOF
    "$PYTHON" -c "import mplabel.cli" >&2 || true
    exit 1
fi

# Before the server, not after: failing here having already started one
# leaves a killed background job and a stray temp directory, and the
# real error scrolls past above the noise.
if ! xcodebuild -version >/dev/null 2>&1; then
    cat >&2 <<EOF
xcodebuild is not usable.

Usually xcode-select is pointing at the Command Line Tools rather than
at Xcode itself, which is enough to compile but not to drive a
simulator:

    sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
    xcodebuild -version

The underlying error:
EOF
    xcodebuild -version >&2 || true
    exit 1
fi

HOME_DIR="$(mktemp -d)"
mkdir -p "$HOME_DIR/labels"
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        # `wait` rather than letting bash announce "Terminated: 15" on
        # its own terms - that line looks like a second failure stacked
        # on the first, and it is only the teardown working.
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$HOME_DIR"
}
trap cleanup EXIT

export MPLABEL_HOME="$HOME_DIR"

echo "==> seeding a database in $HOME_DIR"
# The same seed the model fixtures use. One set of awkward rows, not two
# that drift apart.
"$PYTHON" - <<PY
import pathlib, sys
sys.path.insert(0, r"$REPO/tests")
from mplabel import cli
import make_ios_fixtures as gen
conn = cli.connect_db(pathlib.Path(r"$HOME_DIR"))
gen.seed(conn)
conn.close()
PY

HASH="$("$PYTHON" -c "from mplabel import web; print(web.hash_password('$PASSWORD'))")"

echo "==> starting mplabel serve on 127.0.0.1:$PORT"
MPLABEL_WEB_PASSWORD_HASH="$HASH" \
MPLABEL_WEB_BIND=127.0.0.1 \
MPLABEL_WEB_PORT="$PORT" \
    "$PYTHON" -m mplabel serve --bind 127.0.0.1 --port "$PORT" \
    >"$HOME_DIR/server.log" 2>&1 &
SERVER_PID=$!

BASE="http://127.0.0.1:$PORT"
for _ in $(seq 1 60); do
    if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then break; fi
    sleep 0.25
done
if ! curl -fsS "$BASE/healthz" >/dev/null 2>&1; then
    echo "the server never answered; its log:" >&2
    cat "$HOME_DIR/server.log" >&2
    exit 1
fi

# A token, so the tests that are not about signing in do not have to.
TOKEN="$(curl -fsS -X POST "$BASE/api/login" \
    -H 'Content-Type: application/json' -H 'X-Mplabel: 1' \
    -d "{\"password\": \"$PASSWORD\"}" \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

echo "==> running the UI tests against $BASE"
xcodebuild test \
    -project "$REPO/ios/MPLabel.xcodeproj" \
    -scheme MPLabel \
    -destination "platform=iOS Simulator,name=$SIMULATOR" \
    -only-testing:MPLabelUITests \
    TEST_RUNNER_MPLABEL_UITEST_SERVER="$BASE" \
    TEST_RUNNER_MPLABEL_UITEST_TOKEN="$TOKEN" \
    TEST_RUNNER_MPLABEL_UITEST_PASSWORD="$PASSWORD"
