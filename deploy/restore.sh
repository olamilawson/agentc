#!/usr/bin/env bash
# Puts the agent back to the state held in one backup: the database and the
# client folders, both from the same archive.
#
#   cd /opt/personal-agent && ./deploy/restore.sh            choose from the backups on this server
#   ./deploy/restore.sh /var/backups/personal-agent/backup-2026-10-12-0215.tar.gz
#   ./deploy/restore.sh --offsite        choose from the encrypted copies in the bucket; it asks
#                                        for your backup key, which is not kept on this server
#
# It asks before it changes anything. First it takes a fresh backup of the state
# you are leaving, so a restore can itself be undone. The site is off for the
# minute or two this takes. Sign-in, keys and other settings are in .env, which
# no backup holds and this command does not touch.
set -euo pipefail

COMPOSE_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$COMPOSE_DIR"

setting() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-; }
DIR=$(setting BACKUP_DIR); DIR=${DIR:-/var/backups/personal-agent}
say() { printf '\n== %s\n' "$1"; }

# 0. From the bucket: fetch an encrypted copy and open it with the owner's secret key
if [ "${1:-}" = "--offsite" ]; then
  BUCKET=$(setting BACKUP_S3_BUCKET)
  if [ -z "$BUCKET" ]; then
    echo "No bucket is set up on this server (./deploy/setup.sh --offsite). Nothing changed."
    exit 1
  fi
  export RCLONE_CONFIG_OFFSITE_TYPE=s3 RCLONE_CONFIG_OFFSITE_PROVIDER=Other
  export RCLONE_CONFIG_OFFSITE_ENDPOINT="$(setting BACKUP_S3_ENDPOINT)"
  export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="$(setting BACKUP_S3_KEY_ID)"
  export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="$(setting BACKUP_S3_SECRET)"
  region=$(setting BACKUP_S3_REGION)
  if [ -n "$region" ]; then export RCLONE_CONFIG_OFFSITE_REGION="$region"; fi
  REMOTE="offsite:$BUCKET/personal-agent"
  mapfile -t stored < <(rclone lsf "$REMOTE" 2>/dev/null | grep '\.tar\.gz\.age$' | sort -r || true)
  if [ "${#stored[@]}" -eq 0 ]; then
    echo "The bucket holds no backup, or could not be reached. Nothing changed."
    exit 1
  fi
  echo "Backups in the bucket, newest first:"
  for i in "${!stored[@]}"; do printf '  %2d  %s\n' "$((i + 1))" "${stored[$i]}"; done
  printf 'Type the number to fetch, or press Enter to leave everything as it is: '
  read -r pick
  if ! [[ "$pick" =~ ^[0-9]+$ ]] || [ "$pick" -lt 1 ] || [ "$pick" -gt "${#stored[@]}" ]; then
    echo "Nothing changed."
    exit 0
  fi
  NAME="${stored[$((pick - 1))]}"
  umask 077
  mkdir -p "$DIR"
  FETCH=$(mktemp -d)
  if ! rclone copyto "$REMOTE/$NAME" "$FETCH/$NAME"; then
    rm -rf "$FETCH"
    echo "The bucket did not give up $NAME. Nothing changed."
    exit 1
  fi
  printf 'Paste your backup key, the line that starts AGE-SECRET-KEY (it does not show): '
  read -rs key; echo
  printf '%s\n' "$key" > "$FETCH/key"
  unset key
  OPENED="$DIR/${NAME%.age}"
  if ! age -d -i "$FETCH/key" -o "$OPENED" "$FETCH/$NAME"; then
    rm -rf "$FETCH" "$OPENED"
    echo "That key did not open $NAME. Nothing changed."
    exit 1
  fi
  rm -rf "$FETCH"
  echo "Fetched and opened $NAME."
  set -- "$OPENED"
fi

# 1. Which backup
FILE="${1:-}"
if [ -z "$FILE" ]; then
  mapfile -t found < <(ls -1t "$DIR"/backup-*.tar.gz 2>/dev/null || true)
  if [ "${#found[@]}" -eq 0 ]; then
    echo "No backup is on this server yet (looked in $DIR). Nothing changed."
    exit 1
  fi
  echo "Backups on this server, newest first:"
  for i in "${!found[@]}"; do
    printf '  %2d  %s  (%s)\n' "$((i + 1))" "$(basename "${found[$i]}")" "$(du -h "${found[$i]}" | cut -f1)"
  done
  printf 'Type the number to restore, or press Enter to leave everything as it is: '
  read -r pick
  if ! [[ "$pick" =~ ^[0-9]+$ ]] || [ "$pick" -lt 1 ] || [ "$pick" -gt "${#found[@]}" ]; then
    echo "Nothing changed."
    exit 0
  fi
  FILE="${found[$((pick - 1))]}"
fi
if [ ! -f "$FILE" ]; then
  echo "There is no file at $FILE. Nothing changed."
  exit 1
fi

# 2. Read the archive before trusting it
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
umask 077
if ! tar -xzf "$FILE" -C "$WORK" db.sql 2>/dev/null \
   || ! grep -q "PostgreSQL database dump complete" "$WORK/db.sql"; then
  echo "$(basename "$FILE") holds no complete database dump, so it was not used. Nothing changed."
  exit 1
fi

# 3. Ask
echo
echo "This replaces the agent's runs, decisions and client files with those in"
echo "  $(basename "$FILE")"
echo "Work done since that backup will no longer show. A backup of the present state is taken first."
printf 'Type restore to go ahead: '
read -r answer
if [ "$answer" != "restore" ]; then
  echo "Nothing changed."
  exit 0
fi

# 4. A backup of the state being left
say "Backing up the present state"
SAFETY=""
if ./deploy/backup.sh; then
  SAFETY=$(ls -1t "$DIR"/backup-*.tar.gz | head -n 1)
else
  printf 'The present state could not be backed up. Type restore anyway to go on without one: '
  read -r answer
  if [ "$answer" != "restore anyway" ]; then
    echo "Nothing changed."
    exit 1
  fi
fi
undo() {
  if [ -n "$SAFETY" ]; then
    echo "To return to the state before this restore:  ./deploy/restore.sh $SAFETY"
  fi
}

# 5. Stop the app, so no run writes while the data changes underneath it
say "Stopping the app"
docker compose stop api

# 6. The database
say "Restoring the database"
if ! docker compose exec -T postgres psql -q -U personal_agent -d postgres -v ON_ERROR_STOP=1 \
      -c "DROP DATABASE IF EXISTS personal_agent WITH (FORCE)" \
      -c "CREATE DATABASE personal_agent OWNER personal_agent" \
   || ! docker compose exec -T postgres psql -q -U personal_agent -d personal_agent -v ON_ERROR_STOP=1 \
      < "$WORK/db.sql" > /dev/null; then
  echo
  echo "The database restore did not finish, and the app is stopped. The client files were not touched."
  undo
  exit 1
fi

# 7. The client folders, from the same archive. The present ones are set aside, not deleted.
say "Restoring the client folders"
STAMP=$(date +%F-%H%M%S)
if [ -d data ]; then
  mv data "data.before-restore-$STAMP"
fi
tar -xzf "$FILE" -C "$COMPOSE_DIR" data 2>/dev/null || mkdir -p data

# 8. Start again and check
say "Starting the app"
docker compose start api
for _ in $(seq 1 30); do
  if docker compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" 2>/dev/null; then
    echo
    echo "Restored $(basename "$FILE"). The site is running."
    [ -d "data.before-restore-$STAMP" ] && echo "The client folders as they were are in $COMPOSE_DIR/data.before-restore-$STAMP; delete that folder once you are sure."
    undo
    exit 0
  fi
  sleep 2
done
echo
echo "The backup was restored, but the app did not answer within a minute. See: docker compose logs api"
undo
exit 1
