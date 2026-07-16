# Agent Load-Test Report
_Generated 2026-07-16 15:15_

## Test environment

| | |
|---|---|
| Model (LLM) | `databricks-claude-opus-4-6` (Databricks FM API) |
| Agent | LangGraph ReAct · tools: get_weather (fast), get_stock_price (slow) |
| Serving | MLflow AgentServer · uvicorn · 1 worker(s) · async handlers |
| Load | Locust (streaming) · concurrency levels: 8, 16 users · 60s/level |
| Host | Darwin 25.5.0 · arm64 · 14 cores · Python 3.12.13 |

## Load results by concurrency

| Users | TPS (req/s) | req p50 (ms) | req p95 (ms) | TTFT p50 (ms) | output tok/s |
|--:|--:|--:|--:|--:|--:|
| 8 | 1.38 | 5,200 | 6,300 | 4,700 | 272 |
| 16 | 2.70 | 5,400 | 6,600 | 4,800 | 533 |

_TPS, request latency and TTFT are client-measured by Locust (the ground truth). output tok/s = TPS × mean output tokens/request (from MLflow traces). Of in-agent time, ~89% is the LLM and ~11% tools (per-span split; approximate under concurrency)._

## Per-tool latency (bottleneck check)

| Tool | calls | mean (ms) | p95 (ms) | max (ms) |
|---|--:|--:|--:|--:|
| `get_stock_price` | 127 | 799 | 883 | 900 |
| `get_weather` | 105 | 152 | 199 | 210 |

**Bottleneck: `get_stock_price`** — ~5× slower than `get_weather` (799 ms vs 152 ms mean).

