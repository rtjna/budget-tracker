#!/usr/bin/env bash
# Timestamped backup of the SQLite database. Uses `sqlite3 .backup`, which
# takes a consistent copy even while the app is running (a plain `cp` can
# capture a half-written file). Keeps the last KEEP backups.
#
# Usage:  ./scripts/backup.sh
# Cron:   0 3 * * *  /path/to/scripts/backup.sh   (nightly at 03:00)
set -euo pipefail

DATA_DIR="${DATA_DIR:-$HOME/FinanceData}"
DB="$DATA_DIR/budget.sqlite3"
BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
KEEP="${KEEP:-14}"

if [ ! -f "$DB" ]; then
  echo "No database at $DB (set DATA_DIR if it lives elsewhere)." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/budget-$STAMP.sqlite3"

sqlite3 "$DB" ".backup '$DEST'"
gzip -f "$DEST"
echo "Backed up to $DEST.gz"

# Prune all but the most recent KEEP backups.
ls -1t "$BACKUP_DIR"/budget-*.sqlite3.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
  echo "Pruned $old"
done
