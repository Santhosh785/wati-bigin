#!/bin/bash
# Wrapper for the 3-hourly cron run of bigin_sync.py.
#
# cron gives a job almost no environment (no PATH beyond /usr/bin:/bin, no
# shell profile), so everything here is absolute and self-contained.
#
# Install:  crontab -e   ->   0 */3 * * * /path/to/deploy/bigin_sync_cron.sh

set -uo pipefail

# Resolve the repo root from this script's own location, so moving the folder
# does not silently break the cron entry.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
LOG_DIR="$HERE/logs"
LOG="$LOG_DIR/bigin_sync.log"
LOCK="$LOG_DIR/.bigin_sync.lock"
MAX_LOG_BYTES=$((5 * 1024 * 1024))   # rotate at 5 MB so cron can't fill the disk

mkdir -p "$LOG_DIR"

# Rotate before writing, keeping one previous file.
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_BYTES" ]; then
    mv -f "$LOG" "$LOG.1"
fi

# flock stops a slow full sync from overlapping the next 3-hourly run.
# -n = give up immediately rather than queue behind the running one.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date -Is)] previous sync still running — skipping this tick" >> "$LOG"
    exit 0
fi

cd "$HERE" || exit 1
echo "[$(date -Is)] --- cron sync start ---" >> "$LOG"
"$PYTHON" "$HERE/bigin_sync.py" >> "$LOG" 2>&1
STATUS=$?
echo "[$(date -Is)] --- cron sync end (exit $STATUS) ---" >> "$LOG"
exit $STATUS
