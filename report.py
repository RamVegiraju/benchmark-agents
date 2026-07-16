"""Build a concise benchmark report from Locust CSVs + MLflow trace data.

Three tables, each with a distinct job:
  1. Test environment (what we ran on).
  2. Load results by concurrency — TPS, request latency, TTFT, output throughput.
  3. Per-tool latency — surfaces the bottleneck tool.

Locust supplies rates/latency (client-side); MLflow traces supply token counts and
per-tool timing (read post-hoc from async-exported traces — no latency impact).

Example:
  DATABRICKS_CONFIG_PROFILE=<p> uv run python report.py \
    --stage 8 results/stream_u8 --stage 16 results/stream_u16 \
    --since-ms <ms> --workers 1 --duration 60s --out benchmark_report.md
"""

import argparse
import datetime as dt
import os
import platform

import mlflow
import pandas as pd

EXPERIMENT_PATH = "/Users/ram.vegiraju@databricks.com/load-test-agents"
MODEL = "databricks-claude-opus-4-6"


def load_stats(prefix: str):
    try:
        return pd.read_csv(f"{prefix}_stats.csv")
    except FileNotFoundError:
        return None


def row_for(df, name_contains: str):
    if df is None:
        return None
    m = df[df["Name"].str.contains(name_contains, regex=False, na=False)]
    return m.iloc[0] if len(m) else None


def ms(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def trace_breakdown(since_ms, max_results):
    mlflow.set_tracking_uri("databricks")
    exp = mlflow.set_experiment(EXPERIMENT_PATH)
    filter_string = f"timestamp_ms > {since_ms}" if since_ms else None
    df = mlflow.search_traces(
        locations=[exp.experiment_id], max_results=max_results, filter_string=filter_string
    )
    rows, tool_rows = [], []
    for tid in df["trace_id"]:
        t = mlflow.get_trace(tid)
        total = t.info.execution_time_ms or 0
        llm = tool = 0.0
        for s in t.data.spans:
            dur = (s.end_time_ns - s.start_time_ns) / 1e6
            st = str(s.span_type)
            if st in ("CHAT_MODEL", "LLM"):
                llm += dur
            elif st == "TOOL":
                tool += dur
                tool_rows.append({"tool": s.name, "duration_ms": dur})
        u = t.info.token_usage or {}
        rows.append({"total_ms": total, "llm_ms": llm, "tool_ms": tool,
                     "overhead_ms": max(total - llm - tool, 0.0),
                     "output_tokens": u.get("output_tokens", 0)})
    return pd.DataFrame(rows), pd.DataFrame(tool_rows)


def section_env(args, stages) -> list[str]:
    levels = ", ".join(str(l) for l, _ in stages) or "-"
    host = f"{platform.system()} {platform.release()} · {platform.machine()} · {os.cpu_count()} cores · Python {platform.python_version()}"
    return [
        "## Test environment", "", "| | |", "|---|---|",
        f"| Model (LLM) | `{MODEL}` (Databricks FM API) |",
        f"| Agent | LangGraph ReAct · tools: get_weather (fast), get_stock_price (slow) |",
        f"| Serving | MLflow AgentServer · uvicorn · {args.workers} worker(s) · async handlers |",
        f"| Load | Locust (streaming) · concurrency levels: {levels} users · {args.duration}/level |",
        f"| Host | {host} |", "",
    ]


def section_results(stages, tb) -> list[str]:
    out_pr = tb["output_tokens"].mean() if not tb.empty else 0
    lines = ["## Load results by concurrency", "",
             "| Users | TPS (req/s) | req p50 (ms) | req p95 (ms) | TTFT p50 (ms) | output tok/s |",
             "|--:|--:|--:|--:|--:|--:|"]
    for label, prefix in stages:
        df = load_stats(prefix)
        tot = row_for(df, "[stream-total]")
        ttft = row_for(df, "[stream-TTFT]")
        if tot is None:
            continue
        rps = float(tot["Requests/s"])
        lines.append(
            f"| {label} | {rps:.2f} | {ms(tot['50%'])} | {ms(tot['95%'])} | "
            f"{ms(ttft['50%']) if ttft is not None else '-'} | {rps * out_pr:,.0f} |"
        )
    lines.append("")
    if not tb.empty:
        # LLM-vs-tool split of in-agent (leaf-span) time. We deliberately do NOT divide by
        # the trace root duration: under high async concurrency the root span closes before
        # its LangChain-autolog child spans (their on_*_end callbacks fire late on a saturated
        # event loop), so root time under-reports. Leaf spans stay reliable (per-tool times
        # below match the simulated tool delays), so their ratio is the trustworthy signal.
        llm, tool = tb["llm_ms"].mean(), tb["tool_ms"].mean()
        span_sum = llm + tool or 1
        lines.append(
            f"_TPS, request latency and TTFT are client-measured by Locust (the ground truth). "
            f"output tok/s = TPS × mean output tokens/request (from MLflow traces). Of in-agent time, "
            f"~{llm / span_sum * 100:.0f}% is the LLM and ~{tool / span_sum * 100:.0f}% tools "
            f"(per-span split; approximate under concurrency)._"
        )
        lines.append("")
    return lines


def section_tools(tools) -> list[str]:
    lines = ["## Per-tool latency (bottleneck check)", ""]
    if tools.empty:
        lines.append("_No tool spans in window._\n")
        return lines
    g = tools.groupby("tool")["duration_ms"].agg(
        calls="count", mean="mean", p95=lambda x: x.quantile(0.95), max="max").reset_index()
    g = g.sort_values("mean", ascending=False)
    lines.append("| Tool | calls | mean (ms) | p95 (ms) | max (ms) |")
    lines.append("|---|--:|--:|--:|--:|")
    for _, r in g.iterrows():
        lines.append(f"| `{r['tool']}` | {int(r['calls'])} | {r['mean']:,.0f} | {r['p95']:,.0f} | {r['max']:,.0f} |")
    lines.append("")
    slow = g.iloc[0]
    fast = g.iloc[-1]
    if len(g) > 1 and fast["mean"] > 0:
        lines.append(
            f"**Bottleneck: `{slow['tool']}`** — ~{slow['mean'] / fast['mean']:.0f}× slower than "
            f"`{fast['tool']}` ({slow['mean']:,.0f} ms vs {fast['mean']:,.0f} ms mean)."
        )
        lines.append("")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", nargs=2, action="append", metavar=("USERS", "PREFIX"), default=[],
                    help="concurrency level + Locust csv prefix, e.g. --stage 8 results/stream_u8")
    ap.add_argument("--since-ms", type=int, default=None)
    ap.add_argument("--max-traces", type=int, default=200)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--duration", default="60s")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    tb, tools = trace_breakdown(args.since_ms, args.max_traces)
    out = ["# Agent Load-Test Report", f"_Generated {dt.datetime.now():%Y-%m-%d %H:%M}_", ""]
    out += section_env(args, args.stage)
    out += section_results(args.stage, tb)
    out += section_tools(tools)

    report = "\n".join(out)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report + "\n")
        print(f"\n[written to {args.out}]")


if __name__ == "__main__":
    main()
