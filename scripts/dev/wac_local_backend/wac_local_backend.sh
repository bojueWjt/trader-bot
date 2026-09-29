#!/usr/bin/env bash
# wac-100 local test backend: watcher + control-plane watcher gateway on 127.0.0.1.
# Usage: wac_local_backend.sh {up|down|status|verify|token} [--port 18731] [...]
# The manager itself only needs a stdlib python3 (>= 3.9); the gateway python is
# autodetected separately (see backend.py --python).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manager_python="${WAC_MANAGER_PYTHON:-}"
if [[ -z "${manager_python}" ]]; then
  for candidate in python3 /usr/bin/python3; do
    if command -v "${candidate}" >/dev/null 2>&1 \
      && "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      manager_python="$(command -v "${candidate}")"
      break
    fi
  done
fi
if [[ -z "${manager_python}" ]]; then
  echo "error: python3 >= 3.9 not found (set WAC_MANAGER_PYTHON)" >&2
  exit 2
fi
exec "${manager_python}" "${here}/backend.py" "$@"
