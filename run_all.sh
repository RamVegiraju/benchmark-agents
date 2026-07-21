#!/usr/bin/env bash
# End-to-end: load test the running AgentServer, then evaluate the captured traces,
# then synthesize final_report.md. The AgentServer must already be running
# (start it in another terminal with ./run_server.sh).
#
#   ./run_all.sh [users...]        e.g. ./run_all.sh 8 16 32   (defaults to "8 16")
set -euo pipefail

cd "$(dirname "$0")"

export DATABRICKS_CONFIG_PROFILE="${DATABRICKS_CONFIG_PROFILE:-DEFAULT}"
export MLFLOW_EXPERIMENT_PATH="${MLFLOW_EXPERIMENT_PATH:-/Shared/load-test-agents}"

echo "=== 1/2 load test ==="
./load_testing/run_load_test.sh "$@"

echo "=== 2/2 evaluation + final report ==="
./evaluation/run_eval.sh

echo ">>> done — see final_report.md"
