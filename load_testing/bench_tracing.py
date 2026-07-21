"""Empirically validate that MLflow trace export adds no latency to the served call.

MLflow exports traces on a background thread (async export queue), so the span's
network upload happens *after* the traced function returns. This script measures
the same agent call with tracing ON vs OFF and compares per-call wall time. If the
claim holds, the two distributions overlap within noise.

Run:
  DATABRICKS_CONFIG_PROFILE=<profile> uv run python bench_tracing.py --n 20
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

import mlflow

# Shared agent code lives at the repo root (one level up from load_testing/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import build_messages, graph

EXPERIMENT_PATH = os.environ.get("MLFLOW_EXPERIMENT_PATH", "/Shared/load-test-agents")

PROMPT = "What's the weather in Boston and the stock price of AAPL?"  # exercises both tools


async def timed_invoke() -> float:
    # Tools are async, so drive the graph with ainvoke (sync invoke raises on async tools).
    start = time.perf_counter()
    await graph.ainvoke(build_messages(PROMPT))
    return (time.perf_counter() - start) * 1000  # ms


async def run(n: int) -> list[float]:
    # warm-up (JIT/connection pool) so we don't bias the first sample
    await timed_invoke()
    return [await timed_invoke() for _ in range(n)]


def summarize(label: str, xs: list[float]) -> None:
    xs_sorted = sorted(xs)
    p50 = statistics.median(xs_sorted)
    p95 = xs_sorted[max(0, round(0.95 * len(xs_sorted)) - 1)]
    print(f"{label:16s} n={len(xs)}  mean={statistics.mean(xs):7.1f}ms  "
          f"p50={p50:7.1f}ms  p95={p95:7.1f}ms  min={min(xs):7.1f}ms")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="calls per condition")
    args = ap.parse_args()

    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(EXPERIMENT_PATH)

    # --- Tracing OFF ---
    mlflow.langchain.autolog(disable=True)
    mlflow.tracing.disable()
    off = await run(args.n)

    # --- Tracing ON (async export to workspace experiment) ---
    mlflow.tracing.enable()
    mlflow.langchain.autolog()
    on = await run(args.n)

    # Ensure any queued exports finish before we exit (does not count toward call latency).
    export_start = time.perf_counter()
    mlflow.flush_trace_async_logging()
    flush_ms = (time.perf_counter() - export_start) * 1000

    print("\n=== Agent call latency: tracing ON vs OFF ===")
    summarize("tracing OFF", off)
    summarize("tracing ON", on)
    delta = statistics.median(on) - statistics.median(off)
    print(f"\np50 delta (ON - OFF): {delta:+.1f} ms")
    print(f"Post-call async export flush took {flush_ms:.0f} ms "
          f"(happens off the request path, NOT included in call latency above).")


if __name__ == "__main__":
    asyncio.run(main())
