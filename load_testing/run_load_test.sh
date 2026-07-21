#!/usr/bin/env bash
# Run a streaming step-load across concurrency levels and generate benchmark_report.md.
# The AgentServer must already be running (see run_server.sh).
#
#   ./run_load_test.sh [users...]        e.g. ./run_load_test.sh 8 16 32
set -euo pipefail

# Run from this script's directory so locustfile.py, report.py, and results/ resolve
# regardless of where the script is invoked from.
cd "$(dirname "$0")"

export DATABRICKS_CONFIG_PROFILE="${DATABRICKS_CONFIG_PROFILE:-DEFAULT}"
HOST="${HOST:-http://localhost:8000}"
DURATION="${DURATION:-60s}"
WARMUP="${WARMUP:-15s}"
# --stop-timeout: at the end of each stage, let requests already in flight finish
# (drain) instead of killing them mid-stream. This does NOT extend the load or start
# new requests during the drain — it only lets the in-flight tail complete so its true
# latency is recorded and its MLflow trace is complete (no truncated/cancelled traces).
# Set >= a single request's worst-case latency. Built-in Locust flag (-s/--stop-timeout).
STOP_TIMEOUT="${STOP_TIMEOUT:-30s}"
if [ $# -gt 0 ]; then LEVELS=("$@"); else LEVELS=(8 16); fi
WARM_U="${LEVELS[$((${#LEVELS[@]}-1))]}"   # warm at the highest level

mkdir -p results

# Warmup (discarded): warms the server + FM endpoint so cold starts don't pollute the
# measured tail latencies or the analyzed traces. Runs BEFORE SINCE_MS so its traces are
# excluded from the report, and without --csv so its stats are thrown away.
echo ">>> warmup ${WARMUP} at ${WARM_U} users (discarded)"
uv run locust -f locustfile.py StreamingUser --host "$HOST" \
  --headless -u "$WARM_U" -r "$WARM_U" -t "$WARMUP" --stop-timeout "$STOP_TIMEOUT" \
  --only-summary >/dev/null 2>&1 || true

SINCE_MS=$(python3 -c 'import time; print(int(time.time()*1000))')
echo "$SINCE_MS" > results/since_ms.txt

STAGE_ARGS=()
for U in "${LEVELS[@]}"; do
  echo ">>> streaming load at $U users for $DURATION (drain up to $STOP_TIMEOUT)"
  uv run locust -f locustfile.py StreamingUser --host "$HOST" \
    --headless -u "$U" -r "$U" -t "$DURATION" --stop-timeout "$STOP_TIMEOUT" \
    --csv "results/stream_u${U}" --only-summary
  STAGE_ARGS+=(--stage "$U" "results/stream_u${U}")
done

echo ">>> generating benchmark_report.md"
uv run python report.py "${STAGE_ARGS[@]}" \
  --since-ms "$SINCE_MS" --workers 1 --duration "$DURATION" --out benchmark_report.md
