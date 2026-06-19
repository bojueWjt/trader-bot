#!/usr/bin/env bash
set -euo pipefail

BRANCH="${DEPLOY_BRANCH:-bridge-import}"
WATCHER_CONFIG="${WATCHER_CONFIG:-/srv/trader-secrets/telegram-watcher.config.json}"

if [[ ! -r "${WATCHER_CONFIG}" ]]; then
  echo "missing watcher config: ${WATCHER_CONFIG}" >&2
  exit 1
fi

git fetch origin "${BRANCH}"
git checkout "${BRANCH}" 2>/dev/null || git checkout -B "${BRANCH}" "origin/${BRANCH}"

docker compose build --pull

for container_name in docker-frontend-1 docker-freqtrade-dryrun-1 freqtrade; do
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${container_name}"; then
    docker rm -f "${container_name}"
  else
    echo "old container not present: ${container_name}"
  fi
done

docker compose up -d

# shared signal-data volume must stay writable for the non-root freqtrade user
docker compose exec -T api sh -c 'chmod 777 /data && chmod 666 /data/*.db /data/*.jsonl 2>/dev/null || true'

check_url() {
  local name="$1"
  local url="$2"
  local attempt

  for attempt in $(seq 1 10); do
    if curl -sf "${url}" >/dev/null; then
      echo "health ok: ${name}"
      return 0
    fi
    echo "health retry ${attempt}/10: ${name}"
    sleep 3
  done

  echo "health failed: ${name} (${url})" >&2
  return 1
}

check_url "api" "http://localhost:8000/healthz"
check_url "dashboard" "http://localhost:3000"
check_url "freqtrade-dryrun" "http://localhost:18081/api/v1/ping"
check_url "watcher" "http://localhost:9090/api/status"
