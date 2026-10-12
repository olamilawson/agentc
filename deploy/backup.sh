#!/usr/bin/env bash
# Daily backup: the database and the client folders, in one archive.
#
# With no setup it keeps the archives on this server, readable by root alone,
# for 14 days. That covers a bad deploy or a mistaken deletion. It does not
# cover losing the server: for that, add two lines to .env and each archive is
# also encrypted with your key and copied off the server:
#
#   BACKUP_DEST=user@host:/path      an rsync target somewhere else
#   AGE_PUBKEY=age1...               your age public key; you alone can open the copy
#
# deploy/setup.sh schedules this through deploy/jobs.sh. To run it by hand:
#   /opt/personal-agent/deploy/jobs.sh backup
set -euo pipefail

COMPOSE_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$COMPOSE_DIR"

setting() {  # a value from .env, read as text and never executed
  { grep -E "^$1=" .env 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-
}
DIR=$(setting BACKUP_DIR); DIR=${DIR:-/var/backups/personal-agent}
KEEP=$(setting BACKUP_KEEP_DAYS); KEEP=${KEEP:-14}
DEST=$(setting BACKUP_DEST)
AGE_PUBKEY=$(setting AGE_PUBKEY)

STAMP=$(date +%F-%H%M%S)  # to the second: a restore's own backup never lands on the archive it restores
FILE="$DIR/backup-$STAMP.tar.gz"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
umask 077
mkdir -p "$DIR"
chmod 700 "$DIR"

# 1. The database: the app's tables and the paused runs' checkpoints
docker compose exec -T postgres pg_dump -U personal_agent personal_agent > "$WORK/db.sql" || true
if ! grep -q "PostgreSQL database dump complete" "$WORK/db.sql"; then
  echo "backup failed: the database dump did not finish, so no archive was kept"
  exit 1
fi

# 2. One archive: the dump and the client folders
mkdir -p data
tar -czf "$FILE" -C "$WORK" db.sql -C "$COMPOSE_DIR" data

# 3. Read it back before it counts
if ! tar -tzf "$FILE" | grep -qx "db.sql"; then
  rm -f "$FILE"
  echo "backup failed: the archive could not be read back, so it was removed"
  exit 1
fi

# 4. Drop archives past the keeping period
find "$DIR" -name 'backup-*.tar.gz' -mtime +"$KEEP" -delete
count=$(find "$DIR" -name 'backup-*.tar.gz' | wc -l)
size=$(du -h "$FILE" | cut -f1)

# 5. A copy off this server, when a destination and a key are set
if [ -n "$DEST" ] && [ -n "$AGE_PUBKEY" ]; then
  age -r "$AGE_PUBKEY" -o "$WORK/backup-$STAMP.tar.gz.age" "$FILE"
  rsync -a "$WORK/backup-$STAMP.tar.gz.age" "$DEST/"
  echo "backup $STAMP: $size, $count kept on this server, encrypted copy sent off the server"
else
  echo "backup $STAMP: $size, $count kept on this server; no copy off the server yet"
fi
