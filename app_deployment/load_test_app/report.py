"""Part 3 validated final report: the server-side truth for an app load test.

Unlike a raw Locust summary, this reads the MLflow traces the agent app emits for
the run window (always logged, whether load came from the hosted Locust UI or the
run_load_test.py CLI) and reports latency + reliability from the source of truth.
Reliability is taken from trace state (OK/ERROR) because Locust's client view can
both miss server errors (FM 429s that still stream) and count infra failures that
never create a trace — see the two-systems note in app_deployment/README.md.

Run from the REPO ROOT (needs mlflow + pandas from the root venv, not the load-gen
deps), with your Databricks profile:

  # last 10 minutes of traffic
  DATABRICKS_CONFIG_PROFILE=<profile> uv run python app_deployment/load_test_app/report.py --minutes 10
  # an exact window (epoch ms) + custom output
  ... report.py --start-ms 1785111777654 --end-ms 1785111863095 --out app_load_test_report.md
  # include Locust client-side numbers for a client-vs-server cross-check
  ... report.py --minutes 10 --locust-csv load-test-runs/<run>/<label>/results_stats.csv
"""

import argparse
import collections
import os
import time
from pathlib import Path

import mlflow
import pandas as pd

# Default to the same experiment the agent traces to (set per workspace via env).
EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT_PATH", "/Shared/load-test-agents")


def _err_type(spans) -> str:
    for s in (spans or []):
        st = str(s.get("status", "")) if isinstance(s, dict) else ""
        if "429" in st or "RateLimit" in st:
            return "FM 429 (output-token/min rate limit)"
    return "other server error"


def main() -> int:
    p = argparse.ArgumentParser(description="Validated server-side load-test report.")
    p.add_argument("--experiment", default=EXPERIMENT)
    p.add_argument("--minutes", type=float, default=10, help="Report the last N minutes.")
    p.add_argument("--start-ms", type=int)
    p.add_argument("--end-ms", type=int)
    p.add_argument("--locust-csv", help="Locust results_stats.csv for a client-vs-server cross-check.")
    p.add_argument("--out", default="report.md")
    args = p.parse_args()

    now_ms = int(time.time() * 1000)
    end_ms = args.end_ms or now_ms
    start_ms = args.start_ms or (end_ms - int(args.minutes * 60_000))

    mlflow.set_tracking_uri("databricks")
    exp = mlflow.get_experiment_by_name(args.experiment)
    if not exp:
        raise SystemExit(f"experiment not found: {args.experiment}")
    # `locations` is the current param (experiment_ids is deprecated in mlflow 3.x).
    df = mlflow.search_traces(locations=[exp.experiment_id], max_results=5000,
                              order_by=["timestamp DESC"])
    ms = pd.to_numeric(df["request_time"], errors="coerce")  # epoch ms
    win = df[(ms >= start_ms) & (ms <= end_ms)].copy()
    n = len(win)
    if n == 0:
        raise SystemExit("no traces in the window — widen --minutes or check the window.")

    err = win[win["state"].astype(str).str.contains("ERROR")]
    ok = win[~win["state"].astype(str).str.contains("ERROR")]
    # Latency over SUCCESSFUL traces only — mixing in fast-failing 429s skews the median.
    dur = pd.to_numeric(ok["execution_duration"], errors="coerce").dropna()
    err_rate = 100 * len(err) / n
    window_s = (end_ms - start_ms) / 1000
    tps = n / window_s
    types = collections.Counter(_err_type(r.get("spans")) for _, r in err.iterrows())

    lines = []
    lines.append("# App load-test report (validated, server-side)\n")
    lines.append(f"- Experiment: `{args.experiment}`")
    lines.append(f"- Window: {window_s:.0f}s  (epoch ms {start_ms}..{end_ms})")
    lines.append(f"- Requests (traces): **{n}**  ·  throughput ≈ **{tps:.1f} req/s**\n")

    lines.append(f"## Latency — successful requests only ({len(dur)} OK traces)\n")
    if len(dur):
        lines.append("| median | p95 | p99 | max |")
        lines.append("|---|---|---|---|")
        lines.append(f"| {dur.median():.0f}ms | {dur.quantile(.95):.0f}ms | "
                     f"{dur.quantile(.99):.0f}ms | {dur.max():.0f}ms |\n")
    else:
        lines.append("_No successful traces in this window (all errored) — no latency to report._\n")

    lines.append("## Reliability (source of truth: trace state)\n")
    lines.append(f"- OK: **{n - len(err)}**  ·  ERROR: **{len(err)}**  ·  "
                 f"error rate: **{err_rate:.1f}%**")
    for t, c in types.items():
        lines.append(f"  - {c} × {t}")
    if err_rate == 0:
        lines.append("- No server-side errors in this window.")
    lines.append("")

    if args.locust_csv and Path(args.locust_csv).exists():
        import csv
        rows = list(csv.DictReader(open(args.locust_csv)))

        def pick(substr):
            return next((r for r in rows if substr in r.get("Name", "")), None)

        e2e, ttft = pick("(end-to-end)"), pick("(TTFT)")
        # ONE unified client-side row: end-to-end and TTFT side by side as columns
        # (the clean view stock Locust can't give — no double-counted Aggregated).
        lines.append("## Client-side latency (Locust) — end-to-end + TTFT in one row\n")
        lines.append("| requests | fails | e2e p50 | e2e p95 | e2e p99 | TTFT p50 | TTFT p95 |")
        lines.append("|---|---|---|---|---|---|---|")
        def cell(r, col):
            return f"{r.get(col)}ms" if r else "—"
        lines.append(
            f"| {e2e.get('Request Count') if e2e else '—'} "
            f"| {e2e.get('Failure Count') if e2e else '—'} "
            f"| {cell(e2e,'50%')} | {cell(e2e,'95%')} | {cell(e2e,'99%')} "
            f"| {cell(ttft,'50%')} | {cell(ttft,'95%')} |\n")

        lines.append("## Client vs server cross-check\n")
        if e2e:
            lines.append(f"- Locust client end-to-end median: {e2e.get('50%')}ms "
                         f"(reqs {e2e.get('Request Count')}, fails {e2e.get('Failure Count')})")
        lines.append(f"- MLflow server median: {dur.median():.0f}ms (reqs {n}, errors {len(err)})")
        lines.append("- Client failures with no matching trace = infra (cold-start/unavailable); "
                     "server errors Locust may miss = FM 429s. Trust both.\n")

    lines.append("## Notes\n")
    lines.append("- Reliability here reflects the FM output-token/min (OTPM) quota, not the "
                 "serving stack. For the pure serving ceiling, run against the `MOCK_LLM=1` "
                 "agent variant (no FM calls).")

    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}  ({n} traces, {err_rate:.1f}% errors, {tps:.1f} req/s, "
          f"median {dur.median():.0f}ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
