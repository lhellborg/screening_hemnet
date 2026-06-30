#!/bin/bash
# One-time backfill: fetch the four counties ONE AT A TIME with gaps between,
# then geo/embed/enrich once. Each county runs as its own process with a hard
# timeout so a wedged browser can't stall the whole thing. Self-removes its
# LaunchAgent after running (one-shot). Logs to data/backfill.log.

PROJECT="/Users/lihel5/playground/screening_hemnet"
PY="$PROJECT/.venv/bin/python"
LOG="$PROJECT/data/backfill.log"
PLIST="$HOME/Library/LaunchAgents/com.hemnet-search.backfill.plist"
GAP="${HEMNET_GAP:-1200}"          # seconds between counties (default 20 min)
TIMEOUT="${HEMNET_TIMEOUT:-2400}"  # hard cap per county (default 40 min)
MAX="${HEMNET_MAX:-120}"

cd "$PROJECT" || exit 1
mkdir -p "$PROJECT/data"

# Share the per-run lock with update.sh so a weekly county job can't overlap us.
LOCK="$PROJECT/data/.update.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') — backfill: another update is running, skipping" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Run a command but kill it (and its browser) if it exceeds $1 seconds.
run_with_timeout() {
  local secs=$1; shift
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
  local watcher=$!
  wait "$pid" 2>/dev/null
  kill "$watcher" 2>/dev/null
}
cleanup_browser() {
  pkill -9 -f "ms-playwright" 2>/dev/null
  pkill -9 -f "headless_shell" 2>/dev/null
}

{
  echo "===== $(date '+%F %T') backfill start ====="
  first=1
  for entry in 17759:Dalarna 17760:Gavleborg 17761:Vasternorrland 17762:Jamtland; do
    id="${entry%%:*}"; name="${entry##*:}"
    if [ "$first" -eq 0 ]; then
      echo "$(date '+%T') — pausing ${GAP}s before $name"
      sleep "$GAP"
    fi
    first=0
    echo "$(date '+%T') — fetching $name ($id), max $MAX"
    run_with_timeout "$TIMEOUT" "$PY" -m hemnet_search.cli fetch --location-id "$id" --max "$MAX"
    cleanup_browser
  done

  echo "$(date '+%T') — geo + embed + enrich"
  "$PY" -m hemnet_search.cli geo --skip-download || echo "[geo failed]"
  "$PY" -m hemnet_search.cli embed || echo "[embed failed]"
  "$PY" -m hemnet_search.cli enrich --max 80 || echo "[enrich failed]"
  cleanup_browser

  echo "$(date '+%T') — totals:"
  "$PY" - <<'PYEOF'
from hemnet_search.config import Config
from hemnet_search.db import Database
c = Database(Config.load().db_path).conn
print("  total objects:", c.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"])
for r in c.execute("SELECT county, COUNT(*) n FROM listings GROUP BY county ORDER BY n DESC"):
    print("   ", r["county"], r["n"])
PYEOF
  echo "===== $(date '+%F %T') backfill done ====="
} >> "$LOG" 2>&1

# One-shot: unload + remove self so it does not repeat.
launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
