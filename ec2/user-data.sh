#!/bin/bash
set -euo pipefail
exec >/var/log/vmrepro.log 2>&1
trap 'curl -fsS -T /var/log/vmrepro.log "$LOG_URL"; shutdown -h now' EXIT
shutdown -h "+$TIMEOUT"
mkdir -p /opt/vmrepro && cd /opt/vmrepro
curl -fsS "$BUNDLE_URL" | tar -xz
bash host-setup.sh
python3 bench.py "${BENCH_ARGS[@]}" | tee results.md
curl -fsS -T results.md "$RESULTS_URL"
