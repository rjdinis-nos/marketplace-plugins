#!/usr/bin/env sh
# Dependency-free rotation for the Copilot CLI OTel log file.
#
# Needs only POSIX sh + coreutils + gzip. Uses the copy-truncate
# strategy: gzip the current log to a timestamped sibling, then truncate the
# original IN PLACE. The CLI keeps the file open and only appends, so the file
# must be truncated (not renamed) for its open handle to stay valid.
#
# Usage:
#   rotate_otel_log.sh [LOG_PATH]
# Env (all optional):
#   COPILOT_OTEL_FILE_EXPORTER_PATH  default log path
#   OTEL_LOG_MAX_BYTES   rotate only if the log is at least this big (default 50 MiB)
#   OTEL_LOG_KEEP        number of compressed generations to keep (default 8)
#   OTEL_LOG_FORCE=1     rotate regardless of size
#
# Cron example (hourly):
#   0 * * * * sh "$HOME/<path-to>/rotate_otel_log.sh" >/dev/null 2>&1
set -eu

LOG="${1:-${COPILOT_OTEL_FILE_EXPORTER_PATH:-$HOME/.copilot/logs/otel-signals.jsonl}}"
MAX_BYTES="${OTEL_LOG_MAX_BYTES:-52428800}"
KEEP="${OTEL_LOG_KEEP:-8}"
FORCE="${OTEL_LOG_FORCE:-0}"

[ -f "$LOG" ] || { echo "no log at $LOG; nothing to rotate"; exit 0; }

size=$(wc -c < "$LOG" | tr -d ' ')
[ "$size" -gt 0 ] || exit 0
if [ "$FORCE" != "1" ] && [ "$size" -lt "$MAX_BYTES" ]; then
  exit 0
fi

dest="$LOG.$(date +%Y%m%d-%H%M%S).gz"
gzip -c "$LOG" > "$dest"
: > "$LOG"
echo "rotated $LOG -> $dest"

# Prune oldest, keeping the newest $KEEP compressed generations.
ls -1t "$LOG".*.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
  rm -f "$old"
done
