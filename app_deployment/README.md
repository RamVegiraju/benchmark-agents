# Part 3 — Deploy the agent as a Databricks App + load-test it from an App

Parts 1 & 2 run everything **locally**: a local AgentServer, a local Locust client.
Part 3 takes it to Databricks:

1. Deploy the agent as a **Databricks App** (the system under test).
2. Run **Locust as its own separate App** (live web UI) — or headless from your
   laptop — pointed at the agent app's URL.

## Why two separate apps (not one)

The load generator must **never share compute with the agent**. If they run in the
same container they fight for the same vCPUs and event loop, and you'd be measuring
that contention instead of the agent's real ceiling. Keeping them separate also lets
you scale each independently (agent workers/compute vs. generator concurrency) and
sends traffic over the real network + OAuth path a production client would use. This
is the [app-templates load-testing skill](https://github.com/databricks/app-templates/blob/main/.claude/skills/load-testing/SKILL.md)
recommendation and the [Databricks docs](https://docs.databricks.com/aws/en/agents/agent-framework/load-test-agent-app?language=LangGraph)
guidance.

```
        ┌─────────────────────────┐         ┌──────────────────────────┐
        │  loadgen_app (Locust)   │  HTTP   │  agent_app (AgentServer) │
        │  web UI :8000, M2M OAuth│ ──────▶ │  /invocations, N workers │
        └─────────────────────────┘  SSE    └──────────────────────────┘
             (or local run_load_test.py CLI)      traces → MLflow experiment
```

## Layout

```
<repo root>/
  agent.py, handlers.py, start_server.py   # the agent (shared with Parts 1 & 2)
  mock_llm.py                              # opt-in MOCK_LLM=1 capacity mode
  app.yaml                                 # agent app config (command + env)
  databricks.yml                           # DAB: BOTH apps + worker/compute targets
  app_deployment/
    README.md                              # this file
    load_test_app/                         # the load generator, deploys as its own App
      app.yaml            # runs the Locust WEB UI on :8000
      locustfile.py       # M2M OAuth, SSE parse, TTFT, StepRampShape
      run_load_test.py    # headless CLI: ramp-to-saturation across app URLs
      dashboard_template.py  # HTML dashboard from a run
      pyproject.toml      # isolated deps (locust<2.40)
      .env.example
  load-test-runs/                          # CLI results + dashboards (git-ignored)
```

## Server config for max traffic (what we benchmarked)

The handlers are **async**, so one uvicorn worker's event loop already interleaves
many in-flight streaming requests. `--workers` (a DAB variable) adds **parallelism
across the container's vCPUs** — set it ~= vCPUs. Local mock benchmark (no FM API):

| Load | workers | completed QPS | p95 | p99 |
|---|---|---|---|---|
| 40 users (below saturation)  | 1 | 54.0 | 500ms | — |
| 40 users                     | 4 | 55.1 | 470ms | — |
| **200 users (CPU-bound)**    | 1 | 392  | 300ms | 370ms |
| **200 users**                | 4 | **476 (+21%)** | **180ms (−40%)** | 200ms |

Takeaway: workers barely move throughput at moderate load (the async loop + downstream
latency govern), but at saturation they lift QPS ~20% and roughly halve tail latency.
So: **async handlers + workers ≈ vCPUs**, and remember the real ceiling is often the
Foundation Model endpoint, not this server — use `MOCK_LLM=1` to measure pure infra.

## Prerequisites

- Databricks CLI authenticated to the workspace (M2M service principal recommended for
  long sweeps — U2M tokens expire mid-run):
  ```bash
  databricks auth login --host <workspace-url> -p <profile>
  ```
- The agent app's service principal needs: access to the FM endpoint in `agent.py`
  (`databricks-claude-opus-4-6`) and `CAN_MANAGE` on the MLflow experiment
  (`MLFLOW_EXPERIMENT_PATH`). For pure-infra tests set `mock_llm: "1"` (no FM needed).

## Deploy

```bash
# 1. Deploy both apps (creates resources + uploads code). Pick a worker/compute target.
databricks bundle deploy -t agent-w4 -p <profile>

# 2. Start the agent app; note the URL it prints.
databricks bundle run agent_app -t agent-w4 -p <profile>
databricks apps get load-test-agent-w4 -p <profile> --output json | jq '{app_status,compute_status,url}'

# 3. Set compute size (not a stable bundle field yet):
databricks apps update load-test-agent-w4 --compute-size LARGE -p <profile>
```

Deploy several variants (`-t agent-w2 / agent-w4 / agent-w8`) to sweep the matrix.

## Load-test it

### Option A — from an App (live UI, scales past a laptop)

```bash
# point the load-gen app at the agent app URL, then start it
databricks bundle deploy -t dev -p <profile> \
  --var="target_host=https://load-test-agent-w4.<region>.databricksapps.com"
databricks bundle run loadgen_app -t dev -p <profile>
databricks apps get load-test-locust -p <profile> --output json | jq '{app_status,url}'
```

Open the `load-test-locust` URL → the **Locust web UI**. With `LOCUST_AUTORAMP=1`
click **Start** and it ramps to saturation; watch live QPS / latency / TTFT / failures.
Grant the load-gen app's service principal permission to invoke the agent app so its
injected OAuth (`DATABRICKS_CLIENT_ID/SECRET`) can authenticate.

### Option B — headless from your laptop (eng default, HTML dashboard)

```bash
cd app_deployment/load_test_app && uv sync
export DATABRICKS_HOST=<workspace-url>
export DATABRICKS_CLIENT_ID=<sp-id> DATABRICKS_CLIENT_SECRET=<sp-secret>

uv run python run_load_test.py \
  --app-url https://load-test-agent-w2.<region>.databricksapps.com --label w2 \
  --app-url https://load-test-agent-w4.<region>.databricksapps.com --label w4 \
  --max-users 300 --step-size 20 --step-duration 30 --run-name sweep1 --dashboard

open ../../load-test-runs/sweep1/dashboard.html
```

## Final report (validated — server-side truth)

`dashboard.html` shows the client-side view; the **validated** report pulls the
server-side MLflow traces (always logged, whether load came from the UI or the CLI) so
reliability and latency come from the source of truth, not just Locust. Run it from the
**repo root** (uses the root venv's mlflow) after any run:

```bash
DATABRICKS_CONFIG_PROFILE=<profile> uv run python app_deployment/load_test_app/report.py \
  --minutes 10 --out app_load_test_report.md
open app_load_test_report.md
```

It reports throughput, latency percentiles, and the **error rate + error-type breakdown**
(e.g. FM 429s) from trace state. Add `--locust-csv load-test-runs/<run>/<label>/results_stats.csv`
to include the client-side column for an explicit client-vs-server cross-check. Experiment
defaults to `$MLFLOW_EXPERIMENT_PATH`; override with `--experiment`.

> **Why validate both sides:** Locust (client) can *miss* server errors that still stream a
> response (FM 429s) and can *count* infra failures that never create a trace (cold-start
> 502s). MLflow trace state is the reliability source of truth; Locust is the wall-clock
> source of truth. Report from both.

## Portability (run in any workspace)

Nothing here is pinned to a specific workspace. `databricks.yml` has no `workspace.host`,
so `-p <profile>` selects the workspace; the experiment path comes from
`$MLFLOW_EXPERIMENT_PATH`; app URLs are passed as `--var target_host=...`. To run elsewhere:
`databricks auth login --host <new-workspace> -p <new-profile>`, then use `-p <new-profile>`
for every command above.

## Interpreting results

- **Peak QPS** — throughput ceiling; the users count where it plateaus is your knee.
- **Failure rate** should be ~0; high means the app is overloaded (add workers/compute).
- If QPS is flat across worker counts, you're not CPU-bound — the FM endpoint or
  per-request stream duration is the limiter (confirm with `MOCK_LLM=1`).

## Troubleshooting

**Deploy fails at "Installing packages… operation timed out" (different package each try).**
The committed `uv.lock` is relocked against *public* PyPI, so the Apps build (which
installs via the workspace's internal PyPI proxy) is forced to fetch those exact
artifacts through the proxy — slow, and it times out. Fix (already applied): `uv.lock`
is in `sync.exclude` in `databricks.yml`, so the app resolves from `pyproject.toml`
against the proxy and uses cached artifacts (installs in seconds). The app-templates
deploy the same way — no lock shipped. Keep the lock locally for Parts 1 & 2.

**`databricks bundle` fails: `unable to verify checksums signature: openpgp: key expired`.**
The CLI's Terraform download fails signature verification. Point it at an
already-downloaded Terraform binary:
`export DATABRICKS_TF_EXEC_PATH=<repo>/.databricks/bundle/dev/bin/terraform`.

**Load-gen app gets 401 from the agent app.** Grant the load-gen app's service
principal `CAN_USE` on the agent app (Apps inject that SP's OAuth into the generator).

## Teardown

```bash
databricks bundle destroy -t dev -p <profile>   # (repeat per target you deployed)
```
