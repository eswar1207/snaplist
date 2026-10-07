#!/usr/bin/env bash
# Start Redis, the API and workers on this machine (no Docker needed).
#   STANDARD_WORKERS=2 HIGH_WORKERS=1 ./scripts/run.sh
# Stop everything with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")/.."
REDIS_PORT="${REDIS_PORT:-6391}"
API_PORT="${API_PORT:-8300}"
STANDARD_WORKERS="${STANDARD_WORKERS:-2}"
HIGH_WORKERS="${HIGH_WORKERS:-0}"
export SNAPLIST_REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
export OMP_NUM_THREADS=1   # one core per worker process; scale by adding processes

[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt; }
source .venv/bin/activate
python scripts/download_models.py

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if ! redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1; then
  mkdir -p .local/redis
  redis-server --port "$REDIS_PORT" --dir .local/redis --appendonly yes --save "" &
  pids+=($!)
  sleep 0.5
fi

for i in $(seq 1 "$STANDARD_WORKERS"); do
  python -m snaplist.worker --tier standard --worker-id "standard-$i" --metrics-port $((9100 + i)) &
  pids+=($!)
done
for i in $(seq 1 "$HIGH_WORKERS"); do
  python -m snaplist.worker --tier high --worker-id "high-$i" --metrics-port $((9200 + i)) &
  pids+=($!)
done

echo "SnapList on http://127.0.0.1:${API_PORT}  (workers: ${STANDARD_WORKERS} standard, ${HIGH_WORKERS} high)"
uvicorn snaplist.api:create_app --factory --host 0.0.0.0 --port "$API_PORT" --workers 1 &
pids+=($!)
wait
