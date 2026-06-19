#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
IMAGE_NAME="${NAUTILUS_SPIKE_IMAGE:-nautilus-spike:1.227.0}"
PYTHON_BASE_IMAGE="${NAUTILUS_SPIKE_BASE_IMAGE:-python:3.12-slim}"
UV_VERSION="${NAUTILUS_SPIKE_UV_VERSION:-0.8.15}"
PROJECT_DIR="infra/docker/nautilus"
REPORT_PATH="docs/handoff/window-b/NAUTILUS_COMPATIBILITY_REPORT.md"
OUTPUT_DIR="${REPO_ROOT}/services/nautilus-node/spike/.out"
REQ_LOCK="${PROJECT_DIR}/requirements.spike.lock.txt"
UV_LOCK="${PROJECT_DIR}/uv.lock"

cd "${REPO_ROOT}"
mkdir -p "${OUTPUT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required on the target Linux host. This is expected to fail on pudu-mini." >&2
  exit 127
fi

echo "== Nautilus spike host =="
uname -a
docker --version

echo "== Pulling base image for digest discovery =="
docker pull "${PYTHON_BASE_IMAGE}"
BASE_DIGEST="$(docker image inspect "${PYTHON_BASE_IMAGE}" --format='{{range .RepoDigests}}{{println .}}{{end}}' | head -n 1 || true)"
if [[ -z "${BASE_DIGEST}" ]]; then
  BASE_DIGEST="<unavailable from docker image inspect; run docker buildx imagetools inspect ${PYTHON_BASE_IMAGE}>"
fi
echo "base_image=${PYTHON_BASE_IMAGE}"
echo "base_digest=${BASE_DIGEST}"

echo "== Generating uv.lock and hashed requirements export inside Python 3.12 container =="
docker run --rm \
  -v "${REPO_ROOT}:/workspace" \
  -w /workspace \
  "${PYTHON_BASE_IMAGE}" \
  sh -euxc "
    python -m pip install --no-cache-dir uv==${UV_VERSION}
    if [ ! -s '${UV_LOCK}' ] || ! grep -q '^version = ' '${UV_LOCK}'; then rm -f '${UV_LOCK}'; fi
    uv lock --project '${PROJECT_DIR}'
    uv export --project '${PROJECT_DIR}' --locked --format requirements-txt --no-emit-project --output-file '${REQ_LOCK}'
    grep -n 'nautilus-trader==1.227.0' '${REQ_LOCK}' || grep -n 'nautilus_trader==1.227.0' '${REQ_LOCK}'
  "

echo "== Building spike image =="
docker build -f infra/docker/nautilus/Dockerfile.spike -t "${IMAGE_NAME}" .

ENV_ARGS=()
if [[ -n "${BINANCE_TESTNET_API_KEY:-}" ]]; then
  ENV_ARGS+=("-e" "BINANCE_TESTNET_API_KEY")
fi
if [[ -n "${BINANCE_TESTNET_API_SECRET:-}" ]]; then
  ENV_ARGS+=("-e" "BINANCE_TESTNET_API_SECRET")
fi
if [[ -n "${BINANCE_TESTNET_SYMBOL:-}" ]]; then
  ENV_ARGS+=("-e" "BINANCE_TESTNET_SYMBOL")
fi
if [[ -n "${BINANCE_TESTNET_QTY:-}" ]]; then
  ENV_ARGS+=("-e" "BINANCE_TESTNET_QTY")
fi

run_step() {
  local name="$1"
  shift
  local log_path="${OUTPUT_DIR}/${name}.log"
  echo "== Running ${name} =="
  set +e
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "${REPO_ROOT}:/workspace" \
    -w /workspace \
    "${ENV_ARGS[@]}" \
    "${IMAGE_NAME}" "$@" 2>&1 | tee "${log_path}"
  local status="${PIPESTATUS[0]}"
  set -e
  echo "${status}" > "${OUTPUT_DIR}/${name}.status"
  return "${status}"
}

overall_status=0
run_step check_version python services/nautilus-node/spike/check_version.py || overall_status=$?
run_step capability_matrix python services/nautilus-node/spike/capability_matrix.py || overall_status=$?
run_step testnet_smoke python services/nautilus-node/spike/testnet_smoke.py || overall_status=$?

python3 - <<'PY'
from pathlib import Path

root = Path.cwd()
report = root / "docs/handoff/window-b/NAUTILUS_COMPATIBILITY_REPORT.md"
output_dir = root / "services/nautilus-node/spike/.out"
start = "<!-- SPIKE_RUN_OUTPUT_START -->"
end = "<!-- SPIKE_RUN_OUTPUT_END -->"

sections = ["## Latest Spike Run Output", ""]
for name in ("check_version", "capability_matrix", "testnet_smoke"):
    status_path = output_dir / f"{name}.status"
    log_path = output_dir / f"{name}.log"
    status = status_path.read_text(encoding="utf-8").strip() if status_path.exists() else "missing"
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else "missing log"
    if len(log) > 12000:
        log = log[-12000:]
    sections.append(f"### {name} (exit {status})")
    sections.append("")
    sections.append("```text")
    sections.append(log.rstrip())
    sections.append("```")
    sections.append("")

text = report.read_text(encoding="utf-8")
if start not in text or end not in text:
    raise SystemExit(f"Report markers missing: {start} / {end}")
before = text.split(start, 1)[0]
after = text.split(end, 1)[1]
report.write_text(before + start + "\n" + "\n".join(sections) + end + after, encoding="utf-8")
PY

echo "== Manual backfill items =="
echo "1. Replace Dockerfile.spike base image comment/FROM with digest after review: ${BASE_DIGEST}"
echo "2. Confirm ${UV_LOCK} and ${REQ_LOCK} were generated on the target host and commit them after review."
echo "3. Paste testnet order/cancel evidence into ${REPORT_PATH} once testnet_smoke has a successful order path."

exit "${overall_status}"
