# Agent Load-Test Report
_Generated 2026-07-16 21:36_

## Test environment

| | |
|---|---|
| Model (LLM) | `databricks-claude-opus-4-6` (Databricks FM API) |
| Agent | LangGraph ReAct · tools: get_weather (fast), get_stock_price (slow) |
| Serving | MLflow AgentServer · uvicorn · 1 worker(s) · async handlers |
| Load | Locust (streaming) · concurrency levels: 8, 16 users · 60s/level |
| Host | Darwin 25.5.0 · arm64 · 14 cores · Python 3.12.13 |

## Load results by concurrency

| Users | TPS (req/s) | req p50 (ms) | req p95 (ms) | TTFT p50 (ms) | output tok/s /user |
|--:|--:|--:|--:|--:|--:|
| 8 | 1.51 | 4,700 | 5,800 | 4,300 | 43 |
| 16 | 3.00 | 4,700 | 6,000 | 4,300 | 43 |

_TPS, request latency and TTFT are client-measured by Locust (the ground truth). **output tok/s /user** = output tokens ÷ e2e request latency, per request — a per-user rate that does NOT scale with concurrency (so a drop between levels means each request slowed down). It reads well below raw decode speed because most of the request is TTFT (~4,300 of ~4,700 ms is spent before answer tokens stream, in the decide→tool→second-call path), not answer generation. Across 190 complete traces, in-agent time is ~88% LLM and ~12% tools._

## Per-tool latency (bottleneck check)

| Tool | calls | mean (ms) | p95 (ms) | max (ms) |
|---|--:|--:|--:|--:|
| `get_stock_price` | 118 | 809 | 889 | 895 |
| `get_weather` | 124 | 152 | 196 | 201 |

**Bottleneck: `get_stock_price`** — ~5× slower than `get_weather` (809 ms vs 152 ms mean).

