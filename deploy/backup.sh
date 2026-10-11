#!/usr/bin/env bash
# Daily encrypted off-server backup (PRD: server). Install as root cron:
#   10 3 * * * /opt/personal-agent/deploy/backup.sh >> /var/log/pa-backup.log 2>&1
set -euo pipefail

STAMP=$(date +%F)
WORK=$(mktemp -d)
DEST="${BACKUP_DEST:?set BACKUP_DEST, e.g. rsync target or bucket}"
AGE_PUBKEY="${AGE_PUBKEY:?owner's age public key}"
COMPOSE_DIR=$(cd "$(dirname "$0")/.." && pwd)

trap 'rm -rf "$WORK"' EXIT

# 1. Postgres dump
docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T postgres \
  pg_dump -U personal_agent personal_agent > "$WORK/db.sql"

# 2. App data: client folders, run attachments, checkpoints
cp -r "$COMPOSE_DIR/data" "$WORK/data" 2>/dev/null || true

# 3. Encrypt with the owner's key — only the owner can read it
tar -C "$WORK" -cz db.sql data | age -r "$AGE_PUBKEY" -o "$WORK/backup-$STAMP.tar.gz.age"

# 4. Ship it off this server
rsync -a "$WORK/backup-$STAMP.tar.gz.age" "$DEST/"

echo "backup $STAMP shipped"

# Monthly restore drill (not automatic): decrypt into a scratch dir, stand up a
# scratch Postgres, restore, count rows, and record the result in docs/adr/.
