#!/usr/bin/env bash
# Build and (re)start the crypto trader stack on the production host.
#
#   /srv/netedge/scripts/deploy.sh [service ...]
#
# With no arguments it builds api + web and recreates everything. Passing
# service names limits the rebuild, e.g. `deploy.sh worker` after a strategy
# change. Nothing here touches another project, the archived another project stack, or
# any other container on the host.
set -euo pipefail

cd /srv/netedge

if [[ ! -f .env ]]; then
  echo ".env yok — deploy durduruldu." >&2
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
  if curl -fsS http://127.0.0.1:8130/cryptobot/login >/dev/null 2>&1; then
    break
  fi
done

echo "==> status"
docker compose ps
echo
echo "==> api health"
docker compose exec -T api curl -fsS http://127.0.0.1:8000/cryptobot/api/health || true
echo
echo "==> public"
curl -s -o /dev/null -w "https://your-domain.example/cryptobot -> %{http_code}\n" https://your-domain.example/cryptobot/login
