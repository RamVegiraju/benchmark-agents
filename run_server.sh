#!/usr/bin/env bash
# Start the MLflow AgentServer for the LangGraph agent.
#
#   ./run_server.sh [--port 8000] [--workers 4] [--reload]
#
# Uses the Databricks profile below for both the FM API (LLM) and MLflow tracing.
set -euo pipefail

export DATABRICKS_CONFIG_PROFILE="${DATABRICKS_CONFIG_PROFILE:-adb-984752964297111}"

# MLflow 3 exports traces to Databricks via the OTLP exporter. If the shell has
# generic OTEL_* exporter vars set (some tooling injects them), they can redirect
# MLflow's trace export elsewhere — clear them so traces land in the workspace
# experiment. Harmless when these vars are not set.
unset OTEL_TRACES_EXPORTER OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_TRACES_ENDPOINT \
      OTEL_EXPORTER_OTLP_HEADERS OTEL_EXPORTER_OTLP_TRACES_HEADERS OTEL_EXPORTER_OTLP_PROTOCOL \
      OTEL_METRICS_EXPORTER OTEL_LOGS_EXPORTER 2>/dev/null || true

exec uv run python start_server.py "$@"
