#!/usr/bin/env bash
# Daily backup: the database and the client folders, in one archive.
#
# With no setup it keeps the archives on this server, readable by root alone,
# for 14 days. That covers a bad deploy or a mistaken deletion. It does not
# cover losing the server. For that, run once:
#
#   ./deploy/setup.sh --offsite
#
# It makes a key pair, shows you the secret key once to keep somewhere safe,
# and asks for a storage bucket. From then on each archive is also encrypted
# to your key, copied to the bucket, read back, and kept there for 30 days.
# (An rsync target also works: set BACKUP_DEST and AGE_PUBKEY in .env.)
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

# 5. A copy off this server, when a key and a destination are set. The archive is
#    encrypted to your public key first: the store it goes to cannot read it, and
#    neither can this server once it has gone. Only your secret key opens it.
S3_BUCKET=$(setting BACKUP_S3_BUCKET)
if [ -z "$AGE_PUBKEY" ] || { [ -z "$DEST" ] && [ -z "$S3_BUCKET" ]; }; then
  echo "backup $STAMP: $size, $count kept on this server; no copy off the server yet"
  exit 0
fi

kept="backup $STAMP: $size, $count kept on this server"
ENC="$WORK/backup-$STAMP.tar.gz.age"
if ! age -r "$AGE_PUBKEY" -o "$ENC" "$FILE"; then
  echo "$kept; the copy off the server FAILED: the archive could not be encrypted"
  exit 1
fi

if [ -n "$S3_BUCKET" ]; then
  # An S3-compatible bucket, reached with rclone. Its settings go to rclone in the
  # environment of this one script and are written to no file.
  export RCLONE_CONFIG_OFFSITE_TYPE=s3 RCLONE_CONFIG_OFFSITE_PROVIDER=Other
  export RCLONE_CONFIG_OFFSITE_ENDPOINT="$(setting BACKUP_S3_ENDPOINT)"
  export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="$(setting BACKUP_S3_KEY_ID)"
  export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="$(setting BACKUP_S3_SECRET)"
  region=$(setting BACKUP_S3_REGION)
  if [ -n "$region" ]; then export RCLONE_CONFIG_OFFSITE_REGION="$region"; fi
  REMOTE="offsite:$S3_BUCKET/personal-agent"
  NAME=$(basename "$ENC")
  if ! rclone copyto "$ENC" "$REMOTE/$NAME"; then
    echo "$kept; the copy off the server FAILED: the store did not take the file"
    exit 1
  fi
  # Read it back: the store must list the file at the size that was sent.
  sent=$(wc -c < "$ENC" | tr -d '[:space:]')
  there=$(rclone lsf --format s "$REMOTE/$NAME" 2>/dev/null | tr -d '[:space:]' || true)
  if [ "$sent" != "$there" ]; then
    echo "$kept; the copy off the server FAILED: the store holds ${there:-no} bytes of the $sent sent"
    exit 1
  fi
  OFFSITE_KEEP=$(setting BACKUP_OFFSITE_KEEP_DAYS); OFFSITE_KEEP=${OFFSITE_KEEP:-30}
  rclone delete --min-age "${OFFSITE_KEEP}d" "$REMOTE" || true
  echo "$kept; encrypted copy sent off the server and read back ($sent bytes)"
else
  if ! rsync -a "$ENC" "$DEST/"; then
    echo "$kept; the copy off the server FAILED: rsync could not reach $DEST"
    exit 1
  fi
  echo "$kept; encrypted copy sent off the server"
fi
