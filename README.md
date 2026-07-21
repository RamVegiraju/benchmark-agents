# Agent load-testing + evaluation sample

A **holistic assessment** of a LangGraph ReAct agent (two mock tools of different
speeds), served via the **MLflow AgentServer**:

- **Performance** — load-tested with **Locust**, with per-request observability from
  **MLflow Tracing**. Measures throughput (TPS), request latency, time-to-first-token
  (TTFT), output-token throughput, and per-tool latency; pinpoints the slow tool.
- **Quality** — evaluated with **MLflow judges** *after* the load test (re-invoking
  nothing, so it never affects the perf numbers). Checks that the agent called the
  **right tools** and produced a **correct, relevant** final answer.

Both land in one **MLflow experiment** (traces + evaluation runs) and are synthesized
into a single **`final_report.md`**.

## Repo layout

Shared agent + serving code lives at the repo root; the two assessments live in their
own directories.

| Path | Purpose |
|---|---|
| `agent.py` | LangGraph agent + `ChatDatabricks` + two async tools: `get_weather` (~0.15s), `get_stock_price` (~0.8s, the bottleneck). **Shared.** |
| `questions.py` | **Shared** question set (structured + open-ended) used by BOTH the load test and the eval, so they stay in lockstep. Add your own questions here. |
| `handlers.py` | Async `@invoke` / `@stream` handlers; MLflow tracing config (toggle `MLFLOW_TRACING_ENABLED`). |
| `start_server.py`, `run_server.sh` | Start the MLflow AgentServer. |
| `run_all.sh` | End-to-end: load test → evaluate → `final_report.md`. |
| **`load_testing/`** | The **load-test sample** (see the [video](#video-walkthrough)). |
| `load_testing/locustfile.py` | Locust users (streaming + non-streaming); streaming reports TTFT and full-stream duration; prompts come from `questions.py`. |
| `load_testing/report.py`, `run_load_test.sh` | Run the step-load and build `load_testing/benchmark_report.md`. |
| `load_testing/validate_streaming.py` | Confirms tokens stream incrementally (not buffered). |
| `load_testing/bench_tracing.py` | Side check that tracing adds no latency. |
| **`evaluation/`** | The **quality evaluation** (MLflow scorers). |
| `evaluation/ground_truth.py` | Maps each captured trace's prompt to its expectations (from `questions.py`). |
| `evaluation/scorers.py` | Ground-truth scorers (custom tool-call + `Correctness`) and reference-free scorers (`RelevanceToQuery`, `Safety`, custom tool-appropriateness judge). |
| `evaluation/run_eval.py`, `run_eval.sh` | Score **all** captured traces post-hoc; write `eval_results.json`. |
| `evaluation/final_report.py` | Merge load-test + eval into `final_report.md`. |

## Video walkthrough

The **load-testing** portion of this repo (`load_testing/` + the agent/serving code)
is walked through here: **https://youtu.be/GaNRwzE6oaE**

The `evaluation/` directory extends the sample beyond the video.

**Coming from the first video?** The load-test workflow is unchanged — the same files
moved into `load_testing/` (git tracks them as renames), and the agent/serving code stayed
at the repo root. The only difference: prefix the load-test script with its directory.

```bash
./run_server.sh --port 8000 --workers 1      # unchanged (root)
./load_testing/run_load_test.sh 8 16          # was ./run_load_test.sh
open load_testing/benchmark_report.md
```

## Configuration (what we tested on)

| Setting | Value |
|---|---|
| LLM (agent) | `databricks-claude-opus-4-6` (Databricks FM API) |
| Judge LLM (eval) | `databricks-claude-sonnet-4-6` (override with `JUDGE_MODEL`) |
| Databricks profile | your profile via `DATABRICKS_CONFIG_PROFILE` (defaults to `DEFAULT`) |
| MLflow experiment | your path via `MLFLOW_EXPERIMENT_PATH` (defaults to `/Shared/load-test-agents`) |
| Serving | MLflow AgentServer · uvicorn · **async** handlers · 1 worker |
| Load | Locust, streaming; concurrency + duration are args (default **8 and 16** users, **60s**/level); in-flight requests drain at stage end (`--stop-timeout`) |
| Tools | `get_weather` ~0.10–0.20s · `get_stock_price` ~0.70–0.90s (the bottleneck) |

Nothing workspace-specific is hardcoded — configure via env vars:
`DATABRICKS_CONFIG_PROFILE` (auth for FM API + tracing + judges), `MLFLOW_EXPERIMENT_PATH`
(where traces/eval land), `JUDGE_MODEL` (eval judge), model in `agent.py` (`LLM_ENDPOINT`).

## How to run

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and a Databricks profile with FM API access.

```bash
# 0. authenticate (once) and point the tools at your workspace
databricks auth login --host <your-workspace-url> -p <your-profile>
export DATABRICKS_CONFIG_PROFILE=<your-profile>
export MLFLOW_EXPERIMENT_PATH=/Shared/load-test-agents   # or /Users/<you>/load-test-agents

# 1. start the AgentServer (terminal 1). Add --workers N for multi-core scaling.
./run_server.sh --port 8000 --workers 1

# 2. everything else (terminal 2): load test -> evaluate -> final_report.md
./run_all.sh 8 16                  # concurrency levels; defaults to "8 16"
open final_report.md
```

Or run the two parts separately (the server must be running for Part 1):

```bash
# --- Part 1: load testing (the video) ---
# Drives the questions in questions.py, writes load_testing/results/ + benchmark_report.md.
# Uses Locust --stop-timeout so in-flight streaming requests drain cleanly at stage end.
./load_testing/run_load_test.sh 8 16          # concurrency levels; defaults to "8 16"
open load_testing/benchmark_report.md

# --- Part 2: evaluation (runs against the traces Part 1 captured) ---
# Scores ALL completed traces (no sampling) and synthesizes final_report.md.
./evaluation/run_eval.sh
open final_report.md
```

Tuning knobs (env vars): `DURATION` (per-level load time, default `60s`), `STOP_TIMEOUT`
(drain window, default `30s`), `JUDGE_MODEL` (eval judge endpoint).

### How long it takes

For the default `./run_all.sh 8 16` (two 60s levels, ~280 captured traces):

| Phase | Rough time | What's happening |
|---|---|---|
| Server startup | ~1 min (first run only) | `uv` resolves deps, AgentServer boots, creates the experiment |
| Load test | ~3–5 min | 15s warmup + 2 levels × (60s load + up to 30s drain) + reading traces back to build `benchmark_report.md` |
| Evaluation | ~15–25 min | LLM-judge scoring of **every** captured trace across 5 scorers — the long pole |
| **Total** | **~20–30 min** | |

**The evaluation dominates and scales with trace count** — it runs the judges (`Correctness`,
`RelevanceToQuery`, `Safety`, `tool_appropriateness`) over all completed traces, so more users
or a longer `DURATION` means more traces and a proportionally longer judge pass. At `8 16` you
get ~280 traces; the judge pass over that took ~17 min in our run. A quick smoke run (e.g.
`./run_all.sh 4` with `DURATION=20s`) finishes in a few minutes.

**Speeding it up:** the load test itself is only a few minutes — if you just want perf numbers,
run only Part 1 (`./load_testing/run_load_test.sh 8 16`) and skip the eval. If you see repeated
`databricks.sdk: Failed to ... host metadata ... Timed out after 0:05:00` warnings during the
eval, those 5-minute fallbacks inflate the wall-clock (they're non-fatal — the SDK falls back to
your explicit profile config).

Run individual pieces manually if you prefer:

```bash
# single streaming load level
cd load_testing && uv run locust -f locustfile.py StreamingUser --host http://localhost:8000 \
    --headless -u 16 -r 16 -t 60s --stop-timeout 30s --csv results/stream_u16

# validate streaming is incremental
uv run python load_testing/validate_streaming.py --prompt "weather in Boston?"

# score the traces from the last load window (reads load_testing/results/since_ms.txt)
uv run python evaluation/run_eval.py
```

## Concurrency model

Handlers are **async** (`graph.ainvoke` / `graph.astream`), so one uvicorn worker's event
loop interleaves many in-flight (I/O-bound) LLM requests instead of serializing them —
throughput scales with concurrency on a single worker. Add `--workers N` for multi-core.

## Performance metrics explained

| Metric | What it measures | How it's computed | Source |
|---|---|---|---|
| **Users (concurrency)** | Requests in flight at once — the load knob. Each Locust user sends a request, waits for the full response, then sends another. | Set per run (`-u N`). | Locust |
| **TPS (req/s)** | Request throughput — how many full requests complete per second. Rises with concurrency until the server (or FM endpoint) saturates. | completed requests ÷ elapsed time | Locust |
| **req p50 / p95 (ms)** | End-to-end request latency: send → full response received. p50 = median, p95 = slow tail. | client stopwatch per request | Locust |
| **TTFT p50 (ms)** | Time to first token: send → first *answer* token streamed. In a ReAct agent this includes the decide→tool→second-call path, so it's the bulk of the request. | stopwatch to first `output_text.delta` on the SSE stream | Locust |
| **output tok/s /user** | Per-user output rate: output tokens per second a single request gets, averaged over its whole life. Per-user, so it does **not** scale with concurrency — a drop between levels means each request slowed. | output tokens ÷ e2e request latency | tokens: MLflow · latency: Locust |
| **Per-tool mean/p95 (ms)** | How long each tool takes (its span from call to return) — surfaces the bottleneck tool. | tool span duration, grouped by tool | MLflow traces |
| **% LLM / % tools** | Of time spent inside the agent, model vs tools. | summed LLM spans vs tool spans (leaf spans) | MLflow traces |

**Two measurement systems:** Locust is an external client with a stopwatch — its numbers
(TPS, latency, TTFT) are ground truth for wall-clock. MLflow traces are read *after* the run
from async-exported data (never on the request path) and supply token counts and per-tool
timing.

**Why `output tok/s /user` reads in the tens, not hundreds:** its denominator is the *whole*
request, but only a fraction of that (~0.5s) is spent streaming the answer — most is TTFT
(deciding, running the tool, starting the second call). It's an *effective* per-user rate, not
raw model decode speed.

**Scope / honest caveats:**
- We benchmark *through a hosted FM endpoint*, so a throughput ceiling may reflect the
  endpoint's provisioned rate, not just this app/server.
- Two concurrency levels (8, 16) is a starting point, not a full saturation sweep — to locate
  the knee, sweep more levels (`./load_testing/run_load_test.sh 4 8 16 32 64`) until latency
  climbs or failures appear.
- Input-token counts are omitted (the streaming integration double-counts them across SSE
  chunks); `output_tokens` and `total_tokens` are reliable.

**Trace timing under async concurrency:** LangChain autolog records span times via callbacks
whose context propagation is imperfect under `asyncio` (see MLflow's `langchain_tracer.py`),
so the trace **root** span is mis-timed on a small fraction of traces. The report never uses
the root for latency (that's Locust) and computes the LLM-vs-tool split from leaf spans only.
For strictly-nested async traces, enable `mlflow.langchain.autolog(run_tracer_inline=True)` —
at the cost of running callbacks on the request path.

## Quality evaluation explained

Evaluation runs **after** the load test and scores the traces it already captured, in place
(`mlflow.genai.evaluate(data=trace_df, scorers=[...])` with **no `predict_fn`**). Nothing is
re-invoked, so it adds no latency to the load test and cannot change its numbers. Each captured
trace is matched by its prompt to the shared `questions.py` set to recover its ground truth (if
any). **All** completed traces are scored — no sampling.

**Completion under load (a robustness signal, not agent quality).** A streaming request still
in flight when a load stage ends gets its connection torn down (`CancelledError`) → a truncated
trace with no tool spans and no answer. `run_load_test.sh` uses Locust's `--stop-timeout` to let
in-flight requests *drain* (finish) at stage end, which eliminates these. Any that remain are
reported as a **completion rate** and excluded from scoring — you can't judge a non-answer.
(Note: Locust itself reports these as neither success nor failure; it counts **0 failures**.)

**The scorers — what each checks, and built-in vs custom:**

| Scorer | Built-in / Custom | LLM judge? | What it checks | Needs ground truth |
|---|---|---|---|---|
| `tool_selection_accuracy` | **Custom** (`@scorer`, trace-based) | No — deterministic | All `expected_tools` were called (reads `TOOL` spans off the trace). No LLM → no cost/variance. | Yes (`expected_tools`) |
| `Correctness` | **Built-in** (`mlflow.genai.scorers`) | Yes | The final answer matches `expected_facts`. | Yes (`expected_facts`) |
| `RelevanceToQuery` | **Built-in** | Yes | The answer addresses the user's request (on-topic). | No |
| `Safety` | **Built-in** | Yes | The answer is free of harmful/toxic content. | No |
| `tool_appropriateness` | **Custom** (`@scorer` wrapping the `meets_guidelines` judge) | Yes | The tools actually called suit the request — **no** predefined expected-tool list, so it works on open-ended questions. | No |

> Implementation note: `tool_appropriateness` wraps `meets_guidelines` inside a `@scorer`
> (not `make_judge`) because a `@scorer` returning the judge's yes/no `Feedback` aggregates
> into a `/mean` pass rate, whereas `make_judge`'s categorical output does not.

Ground-truth scorers run on the **structured** questions (explicit "weather in X" / "stock price
of Y", where tools + facts are known). Reference-free scorers run on **every** completed
question — including **open-ended** ones like *"How is Nvidia doing in the market today?"* that
have no predefined answer. That's how you evaluate questions without expectations: relevance,
safety, and tool-appropriateness need only the request + trace.

Results are logged as two evaluation runs (`eval-reference-free-all`, `eval-ground-truth`) in
the same experiment as the traces, and rolled into `final_report.md`.

**Adding your own questions:** edit `questions.py`. Set `expected_tools` + `expected_facts` for
a structured question (gets all scorers), or leave both `None` for an open-ended one (gets the
reference-free scorers). The load test and eval both read this file, so they stay in sync.

### Experiment design: one experiment, not two

There is **no separate eval experiment**. Everything lands in the single experiment at
`MLFLOW_EXPERIMENT_PATH` (default `/Shared/load-test-agents`): the server traces to it, and
`run_eval.py` calls `mlflow.set_experiment()` on the *same* path. In the workspace UI you get,
all in one place:

- **Traces** (Observability → Traces) — the load-test requests + tokens, latency, tool spans
- **Assessments** on those traces — the scorer pass/fail columns (`Correctness`, `Relevance`,
  `Safety`, `tool_selection_accuracy`, `tool_appropriateness`)
- **Expectations** on those traces — the `expected_tools` / `expected_facts` ground truth
- **Evaluation runs** (Evaluation → Evaluation runs) — `eval-reference-free-all` and
  `eval-ground-truth`

## Tracing adds no latency

Trace export runs on a background thread. Confirmed with `load_testing/bench_tracing.py`
(tracing ON vs OFF): p50 delta ≈ 0 ms, while the async export flush happens off the request path.

```bash
uv run python load_testing/bench_tracing.py --n 15
```

## References & credits

- **Video walkthrough** — https://youtu.be/GaNRwzE6oaE
- **Locust** — load generation. https://github.com/locustio/locust · docs: https://docs.locust.io
- **MLflow AgentServer** — agent serving. https://mlflow.org/docs/latest/genai/serving/agent-server/
- **MLflow GenAI evaluation** — scorers & judges. https://docs.databricks.com/aws/en/mlflow3/genai/eval-monitor/concepts/scorers
- **MLflow** — tracing & observability. https://github.com/mlflow/mlflow
- **Databricks app-templates** — async agent-serving patterns. https://github.com/databricks/app-templates
