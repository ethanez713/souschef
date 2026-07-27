#!/bin/sh
# Nightly SQLite snapshot. Uses the backup API rather than copying the file,
# which would risk a torn read while the app is mid-write.
set -eu
BACKUP_DIR=/backups
KEEP_DAYS=30

apk add --no-cache sqlite >/dev/null 2>&1 || true
mkdir -p "$BACKUP_DIR"

while true; do
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  OUT="$BACKUP_DIR/souschef-$STAMP.db"
  if sqlite3 /data/souschef.db ".backup '$OUT'" 2>/dev/null; then
    gzip -f "$OUT"
    echo "$(date -u) backup ok: $OUT.gz"
  else
    echo "$(date -u) backup FAILED" >&2
  fi
  find "$BACKUP_DIR" -name 'souschef-*.db.gz' -mtime +$KEEP_DAYS -delete 2>/dev/null || true
  sleep 86400
done
