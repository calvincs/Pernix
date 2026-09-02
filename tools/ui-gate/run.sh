#!/usr/bin/env bash
# run.sh <tag> [level] — the mobile/tablet acceptance gate.
#
# Boots an isolated Pernix from $REPO on $PORT against a throwaway data
# directory, seeds it with realistic transcript shapes, drives seven viewports
# with Playwright, and stops the server again. Nothing it does touches your own
# data/ directory or your running instance.
#
#   LEVEL=m1|m2   which checks to assert (default m2 — everything)
#   PORT=8790     the port to boot on (default 8790)
#   REPO=...      the checkout to test (default: the one this script is in)
#
# Everything it writes lands in tools/ui-gate/out/, which is git-ignored:
# out/app-$PORT (the throwaway instance), out/shots (screenshots),
# out/check-<tag>.json (the machine-readable result) and out/server-<tag>.log.
#
# See README.md. Needs the repo's .venv with Playwright and Chromium installed.
set -u

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=${REPO:-$(cd -- "$HERE/../.." && pwd)}
TAG=${1:-run}
LEVEL=${2:-${LEVEL:-m2}}
PORT=${PORT:-8790}

PY="$REPO/.venv/bin/python3.12"
OUT="$HERE/out"
APP="$OUT/app-$PORT"
SHOTS="$OUT/shots"
PIDFILE="$OUT/server-$PORT.pid"

if [ ! -x "$PY" ]; then
    echo "no .venv at $PY — see tools/ui-gate/README.md"
    exit 2
fi

mkdir -p "$APP/data" "$SHOTS"
ln -sfn "$REPO/static" "$APP/static"
# The agent directory is read for SOUL/RULES and the skill list; a copy keeps
# the gate's instance from writing to yours.
[ -d "$APP/data/agent" ] || cp -r "$REPO/data/agent" "$APP/data/agent"

cd "$APP" || exit 2
rm -f data/sessions.db data/sessions.db-wal data/sessions.db-shm

( "$PY" "$REPO/run.py" --host 127.0.0.1 --port "$PORT" > "$OUT/server-$TAG.log" 2>&1 & echo $! > "$PIDFILE" )
for _ in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
    sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "server did not come up"
    tail -30 "$OUT/server-$TAG.log"
    kill "$(cat "$PIDFILE")" 2>/dev/null
    exit 2
fi

SIDS=$("$PY" "$HERE/seed.py" "$REPO" 2>/dev/null | tail -1)
MAIN=$(echo "$SIDS" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["main"])')
PARENT=$(echo "$SIDS" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["parent"])')

"$PY" "$HERE/check.py" "http://127.0.0.1:$PORT" "$SHOTS" "$TAG" "$MAIN" "$PARENT" "$LEVEL"
RC=$?

kill "$(cat "$PIDFILE")" 2>/dev/null
sleep 0.5
lsof -ti:"$PORT" | xargs -r kill -9 2>/dev/null
exit $RC
