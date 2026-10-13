#!/usr/bin/env bash
# Runs one scheduled job and records how it went, so the Settings and cost
# screen can show the last run of each. deploy/setup.sh schedules the three:
#
#   backup      daily   the database and client folders (deploy/backup.sh)
#   retention   daily   removes files and run content past each client's period
#   learning    weekly  reviews the week's decisions and proposes rule changes
#   dispatch    each minute  sends an approved agency brief once its recall window ends
#
# By hand:  /opt/personal-agent/deploy/jobs.sh backup
set -uo pipefail

JOB="${1:-}"
COMPOSE_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$COMPOSE_DIR"

case "$JOB" in
  backup)    run() { ./deploy/backup.sh; } ;;
  retention) run() { docker compose exec -T api python -m personal_agent.retention; } ;;
  learning)  run() { docker compose exec -T api python -m personal_agent.learning; } ;;
  dispatch)  run() { docker compose exec -T api python -m personal_agent.engine.dispatch; } ;;
  *) echo "Use: jobs.sh backup | retention | learning | dispatch"; exit 2 ;;
esac

LOG_DIR=/var/log/personal-agent
mkdir -p "$LOG_DIR" data/jobs

out=$(run 2>&1)
code=$?

# The each-minute job logs only when it did something.
if [ "$JOB" != "dispatch" ] || [ "$out" != "no dispatch is due" ]; then
  { echo "== $JOB $(date -u +%FT%TZ) exit $code"; echo "$out"; } >> "$LOG_DIR/jobs.log"
fi

# One small text file per job; the app reads it and never runs it.
last=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-300)
printf 'time=%s\nok=%s\ndetail=%s\n' "$(date -u +%FT%TZ)" "$([ $code -eq 0 ] && echo yes || echo no)" "$last" \
  > "data/jobs/$JOB.status"

echo "$out"
exit $code
