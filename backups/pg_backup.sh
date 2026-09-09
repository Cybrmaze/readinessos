#!/bin/bash
set -euo pipefail

BACKUP_DIR="/opt/readinessos/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DB_NAME="readinessos"
LOG_FILE="$BACKUP_DIR/backup.log"
TMP_DUMP="/tmp/readinessos_${TIMESTAMP}_backup.dump"
FINAL_DUMP="$BACKUP_DIR/readinessos_${TIMESTAMP}.dump"

fail() {
    echo "$(date -u): Backup FAILED for readinessos_${TIMESTAMP}.dump -- $1" >> "$LOG_FILE"
    rm -f "$TMP_DUMP" "$FINAL_DUMP"
    exit 1
}
trap 'fail "pg_dump or script error, see stderr in log above this line"' ERR

# Dump as the postgres superuser via local peer auth -- same reasoning as
# /opt/clg/backups/pg_backup.sh: pg_dump locks/reads all tables in one
# transaction, so using the app role (whose grants could drift) risks a
# silent partial-permission abort. postgres has unrestricted read access.
sudo -u postgres pg_dump -d "$DB_NAME" -Fc -f "$TMP_DUMP" 2>>"$LOG_FILE"

mv "$TMP_DUMP" "$FINAL_DUMP"

trap - ERR
DUMP_SIZE=$(stat -c%s "$FINAL_DUMP")
if [[ "$DUMP_SIZE" -eq 0 ]]; then
    fail "dump file is 0 bytes"
fi

# Keep only last 7 days of backups
find "$BACKUP_DIR" -name "readinessos_*.dump" -mtime +7 -delete

echo "$(date -u): Backup completed - readinessos_${TIMESTAMP}.dump (${DUMP_SIZE} bytes)" >> "$LOG_FILE"
