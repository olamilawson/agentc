#!/usr/bin/env bash
# Pull, build the api image, prove the Postgres checkpointer on it, then switch over.
# Run on the VPS:
#   /opt/personal-agent/deploy/deploy.sh
# The running api container is replaced only after the test passes.
set -euo pipefail

COMPOSE_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$COMPOSE_DIR"

# 0. Bring the checkout up to date (fast-forward only: local commits or a
#    diverged branch stop the deploy), then start again from the pulled copy of
#    this script, since bash reads a script as it runs it.
if [ "${1:-}" != "--pulled" ]; then
  git pull --ff-only
  exec "$COMPOSE_DIR/deploy/deploy.sh" --pulled
fi

LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

# 1. Build the new image; nothing running changes yet
docker compose build api

# 2. Pause-and-resume on Postgres, in a throwaway container of the new image.
#    The test works in a scratch database on the compose Postgres and drops it;
#    pytest is installed into the container only, not the image.
docker compose run --rm -T -v "$COMPOSE_DIR/tests:/app/tests:ro" api sh -c \
  "uv pip install --system pytest httpx && python -m pytest tests/test_postgres_checkpointer.py -q -rs -p no:cacheprovider" \
  | tee "$LOG"

# pytest also exits 0 when the test is skipped; a skip proves nothing
if ! grep -q "1 passed" "$LOG" || grep -q "skipped" "$LOG"; then
  echo "deploy stopped: the Postgres checkpointer test did not run and pass" >&2
  exit 1
fi

# 3. Switch over
docker compose up -d

# 4. Wait for the new api to answer (it is not published; ask from inside)
for _ in $(seq 1 30); do
  if docker compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" 2>/dev/null; then
    echo "deployed $(git rev-parse --short HEAD 2>/dev/null || echo '(no git)'): api is healthy"
    exit 0
  fi
  sleep 2
done

echo "deploy failed: api did not answer /health within a minute; see 'docker compose logs api'" >&2
exit 1
