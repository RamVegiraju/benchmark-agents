#!/usr/bin/env bash
# Evaluate the traces captured during the last load test, then synthesize the final report.
# Runs AFTER a load test (re-invokes nothing). The load window is read from
# load_testing/results/since_ms.txt automatically.
#
#   ./run_eval.sh [--judge-sample N]
set -euo pipefail

cd "$(dirname "$0")"

export DATABRICKS_CONFIG_PROFILE="${DATABRICKS_CONFIG_PROFILE:-DEFAULT}"
export MLFLOW_EXPERIMENT_PATH="${MLFLOW_EXPERIMENT_PATH:-/Shared/load-test-agents}"

# MLflow exports/reads traces via the OTLP exporter; clear any inherited OTEL_* vars
# so it talks to the workspace experiment (harmless when unset).
unset OTEL_TRACES_EXPORTER OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_TRACES_ENDPOINT \
      OTEL_EXPORTER_OTLP_HEADERS OTEL_EXPORTER_OTLP_TRACES_HEADERS OTEL_EXPORTER_OTLP_PROTOCOL \
      OTEL_METRICS_EXPORTER OTEL_LOGS_EXPORTER 2>/dev/null || true

echo ">>> curating ground-truth eval dataset"
uv run python ground_truth.py

echo ">>> evaluating captured traces"
uv run python run_eval.py "$@"

echo ">>> synthesizing final_report.md"
uv run python final_report.py
