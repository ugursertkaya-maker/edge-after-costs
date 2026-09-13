#!/usr/bin/env bash
# Build and (re)start the EdgeAfterCosts stack with Docker Compose.
#
#   ./scripts/deploy.sh              # build api + web, recreate everything
#   ./scripts/deploy.sh worker       # rebuild and restart one service only
#
# Run it from anywhere; it operates on the repository this script lives in.
# Nothing here is specific to a particular host: it is `docker compose` plus a
# health wait and a status dump.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Where the web container is published on this machine, for the health probe.
# Override if you changed the port mapping in docker-compose.yml.
WEB_URL="${EDGE_AFTER_COSTS_WEB_URL:-http://127.0.0.1:3000}"
BASE_PATH="${BASE_PATH:-/edge-after-costs}"

if [[ ! -f .env ]]; then
  echo "No .env found. Copy the template first:  cp .env.example .env" >&2
  exit 1
fi

TARGETS=("$@")
echo "==> build"
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  docker compose build api web
  docker compose up -d
else
  docker compose build "${TARGETS[@]}" 2>/dev/null || true
  docker compose up -d --no-deps "${TARGETS[@]}"
fi

echo "==> wait for health"
for _ in $(seq 1 40); do
  sleep 5
  if curl -fsS "${WEB_URL}${BASE_PATH}/login" >/dev/null 2>&1; then
    echo "    web is answering"
    break
  fi
done

echo "==> status"
docker compose ps

echo
echo "==> api health"
docker compose exec -T api curl -fsS "http://127.0.0.1:8000${BASE_PATH}/api/health" || true
echo
