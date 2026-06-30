#!/bin/bash
# Scheduled incremental update of the Hemnet search database.
# Run by cron (see README). Safe to run by hand too.
#
# Tunables (env vars):
#   HEMNET_MAX        max listings to fetch per run (default 150)
#   HEMNET_ENRICH_MAX max broker pages for taxeringsvärde per run (default 60)

PROJECT="/Users/lihel5/playground/screening_hemnet"
PY="$PROJECT/.venv/bin/python"
LOG="$PROJECT/data/update.log"
LOCK="$PROJECT/data/.update.lock"

cd "$PROJECT" || exit 1
mkdir -p "$PROJECT/data"

# Every other day: the agent fires daily (when the Mac is awake), but we only do
# real work on even epoch-days. Set HEMNET_EVERY_OTHER_DAY=0 to run every day.
if [ "${HEMNET_EVERY_OTHER_DAY:-1}" = "1" ] && [ $(( $(date +%s) / 86400 % 2 )) -ne 0 ]; then
  echo "$(date '+%F %T') — off day, skipping" >> "$LOG"
  exit 0
fi

# Prevent overlapping runs (mkdir is atomic).
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') — update already running, skipping" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "===== $(date '+%F %T') update start (loc=${HEMNET_LOCATION_ID:-all}) ====="
  LOC=""
  [ -n "${HEMNET_LOCATION_ID:-}" ] && LOC="--location-id ${HEMNET_LOCATION_ID}"
  "$PY" -m hemnet_search.cli fetch --max "${HEMNET_MAX:-150}" $LOC || echo "[fetch failed]"

  # Trails change rarely: reuse them on weekdays, re-download once a week (Sun).
  if [ "$(date +%u)" = "7" ]; then
    "$PY" -m hemnet_search.cli geo || echo "[geo failed]"
  else
    "$PY" -m hemnet_search.cli geo --skip-download || echo "[geo failed]"
  fi

  "$PY" -m hemnet_search.cli embed  || echo "[embed failed]"
  "$PY" -m hemnet_search.cli enrich --max "${HEMNET_ENRICH_MAX:-60}" || echo "[enrich failed]"
  echo "===== $(date '+%F %T') update done ====="
} >> "$LOG" 2>&1
