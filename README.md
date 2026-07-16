# Agent load-testing sample

A LangGraph ReAct agent (two mock tools of different speeds) served via the
**MLflow AgentServer**, load-tested with **Locust**, with per-request observability
from **MLflow Tracing**. It measures throughput (TPS), request latency, time-to-first-token
(TTFT), output-token throughput, and per-tool latency — and pinpoints the slow tool.

## Repo layout

| File | Purpose |
|---|---|
| `agent.py` | LangGraph agent + `ChatDatabricks` + two async tools: `get_weather` (~0.15s), `get_stock_price` (~0.8s, the bottleneck). |
| `handlers.py` | Async `@invoke` / `@stream` handlers; MLflow tracing config (toggle `MLFLOW_TRACING_ENABLED`). |
| `start_server.py`, `run_server.sh` | Start the AgentServer. |
| `locustfile.py` | Locust users (streaming + non-streaming); streaming reports TTFT and full-stream duration. |
| `report.py`, `run_load_test.sh` | Run the step-load and build `benchmark_report.md`. |
| `validate_streaming.py` | Confirms tokens stream incrementally (not buffered). |
| `bench_tracing.py` | Side check that tracing adds no latency. |

## Configuration (what we tested on)

| Setting | Value |
|---|---|
| LLM | `databricks-claude-opus-4-6` (Databricks FM API) |
| Databricks profile | your profile via `DATABRICKS_CONFIG_PROFILE` (defaults to `DEFAULT`) |
| MLflow experiment | your path via `MLFLOW_EXPERIMENT_PATH` (defaults to `/Shared/load-test-agents`) |
| Serving | MLflow AgentServer · uvicorn · **async** handlers · 1 worker |
| Load | Locust, streaming, concurrency **8 and 16** users, **60s** per level |
| Tools | `get_weather` ~0.10–0.20s · `get_stock_price` ~0.70–0.90s |

Configure via env vars — nothing workspace-specific is hardcoded:
`DATABRICKS_CONFIG_PROFILE` (auth for FM API + tracing), `MLFLOW_EXPERIMENT_PATH`
(where traces land), model in `agent.py` (`LLM_ENDPOINT`), concurrency as args to
`run_load_test.sh`.

## How to run

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and a Databricks profile with FM API access.

```bash
# 0. authenticate (once) and point the tools at your workspace
databricks auth login --host <your-workspace-url> -p <your-profile>
export DATABRICKS_CONFIG_PROFILE=<your-profile>
export MLFLOW_EXPERIMENT_PATH=/Shared/load-test-agents   # or /Users/<you>/load-test-agents

# 1. start the AgentServer (terminal 1). Add --workers N for multi-core scaling.
./run_server.sh --port 8000 --workers 1

# 2. run the step-load and generate the report (terminal 2)
./run_load_test.sh 8 16            # concurrency levels; defaults to "8 16"

# 3. read the report
open benchmark_report.md
```

`run_load_test.sh` writes Locust CSVs to `results/` and regenerates `benchmark_report.md`.

Run pieces manually if you prefer:

```bash
# single streaming level
uv run locust -f locustfile.py StreamingUser --host http://localhost:8000 \
    --headless -u 16 -r 16 -t 60s --csv results/stream_u16

# report from existing CSVs (--since-ms scopes which traces to analyze)
uv run python report.py \
  --stage 8 results/stream_u8 --stage 16 results/stream_u16 \
  --since-ms <run-start-epoch-ms> --out benchmark_report.md

# validate streaming is incremental
uv run python validate_streaming.py --prompt "weather in Boston?"
```

## Concurrency model

Handlers are **async** (`graph.ainvoke` / `graph.astream`), so one uvicorn worker's event
loop interleaves many in-flight (I/O-bound) LLM requests instead of serializing them —
throughput scales with concurrency on a single worker. Add `--workers N` for multi-core.

## Metric sources

- **TPS, request latency, TTFT** — Locust (client-side). TTFT is the time to the first
  streamed answer token, read off the SSE stream. These are the ground-truth latency numbers.
- **Output tokens, per-tool latency, LLM-vs-tool split** — MLflow traces, read post-hoc from
  the async-exported data, so they never touch the request path.

Note on trace timing under async concurrency: LangChain autolog records span times via
callbacks whose context propagation is imperfect under `asyncio` (see MLflow's
`langchain_tracer.py`), so the trace **root** span can be mis-timed on a small fraction of
traces. The report never uses the root for latency (that comes from Locust) and computes the
LLM-vs-tool split from leaf spans only. For strictly-nested async traces, enable
`mlflow.langchain.autolog(run_tracer_inline=True)` — at the cost of running callbacks on the
request path.

## Tracing adds no latency

Trace export runs on a background thread. Confirmed with `bench_tracing.py` (tracing ON vs
OFF): p50 delta ≈ 0 ms, while the async export flush happens off the request path.

```bash
uv run python bench_tracing.py --n 15
```

## References & credits

- **Locust** — load generation. https://github.com/locustio/locust · docs: https://docs.locust.io
- **MLflow AgentServer** — agent serving. https://mlflow.org/docs/latest/genai/serving/agent-server/
- **MLflow** — tracing & observability. https://github.com/mlflow/mlflow
- **Databricks app-templates** — async agent-serving patterns. https://github.com/databricks/app-templates
