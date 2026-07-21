"""Synthesize the load-test report and the evaluation results into one report.

Combines:
  * load_testing/benchmark_report.md  — throughput, latency, per-tool timing (Locust + traces)
  * evaluation/eval_results.json      — tool-call correctness + answer-quality judge scores

into a single final_report.md at the repo root, with a link to the MLflow experiment
that holds both the traces and the evaluation runs.

  uv run python evaluation/final_report.py --out final_report.md
"""

import argparse
import datetime as dt
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_MD = REPO_ROOT / "load_testing" / "benchmark_report.md"
EVAL_JSON = Path(__file__).with_name("eval_results.json")


def _fmt_score(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def eval_section(data: dict) -> list[str]:
    s = data.get("scores", {})
    t = data.get("traces", {})
    comp = t.get("completion_rate")
    lines = [
        "## Quality evaluation (post-load-test, async)",
        "",
        f"Scored the captured traces in place — no agent re-invocation, so this did not "
        f"affect the load-test numbers above. Judge model: `{data.get('judge_model', '-')}`.",
        "",
        "**Request completion under load** — a robustness signal, not an agent-quality one. "
        "Cancelled requests are in-flight streams torn down when a load stage ended; they are "
        "reported here and excluded from scoring (you can't judge a non-answer).",
        "",
        "| Matched | Completed | Cancelled/truncated | Completion rate |",
        "|--:|--:|--:|--:|",
        f"| {t.get('matched', '-')} | {t.get('completed', '-')} | {t.get('cancelled', '-')} | "
        f"{_fmt_score(comp)} |",
        "",
        f"**Ground-truth scorers** (on the {t.get('structured', '-')} structured questions "
        f"with known tools/facts):",
        "",
        "| Metric | Scorer | Pass rate |",
        "|---|---|--:|",
        f"| Right tool(s) called | `tool_selection_accuracy` (custom, deterministic) | {_fmt_score(s.get('tool_selection_accuracy'))} |",
        f"| Answer matches expected facts | `Correctness` (built-in judge) | {_fmt_score(s.get('correctness'))} |",
        "",
        f"**Reference-free scorers** (need no ground truth — run on all "
        f"{t.get('completed', '-')} completed questions, including the "
        f"{t.get('open_ended', '-')} open-ended ones):",
        "",
        "| Metric | Scorer | Pass rate |",
        "|---|---|--:|",
        f"| Answer relevant to the ask | `RelevanceToQuery` (built-in judge) | {_fmt_score(s.get('relevance_to_query'))} |",
        f"| Answer free of harmful content | `Safety` (built-in judge) | {_fmt_score(s.get('safety'))} |",
        f"| Tool choice appropriate for request | `tool_appropriateness` (custom judge, no expected list) | {_fmt_score(s.get('tool_appropriateness'))} |",
        "",
    ]
    if data.get("experiment_url"):
        lines += [f"**MLflow experiment (traces + eval runs):** {data['experiment_url']}", ""]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=str(BENCHMARK_MD))
    ap.add_argument("--eval", default=str(EVAL_JSON))
    ap.add_argument("--out", default=str(REPO_ROOT / "final_report.md"))
    args = ap.parse_args()

    out = [
        "# Agent Load-Test + Evaluation Report",
        f"_Generated {dt.datetime.now():%Y-%m-%d %H:%M}_",
        "",
        "A holistic assessment of the agent: **performance** (load test) and **quality** "
        "(evaluation). Performance answers *how fast/how many*; quality answers *did it call "
        "the right tools and give a correct, relevant answer*.",
        "",
    ]

    bench = Path(args.benchmark)
    if bench.exists():
        body = bench.read_text().strip()
        # Drop the sub-report's own H1 and its "_Generated ..._" line so this
        # document has a single title and timestamp.
        lines = body.splitlines()
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        while lines and (not lines[0].strip() or lines[0].startswith("_Generated")):
            lines.pop(0)
        out += lines
        out += [""]
    else:
        out += ["## Load test", "", "_No benchmark_report.md found — run the load test first._", ""]

    ev = Path(args.eval)
    if ev.exists():
        out += eval_section(json.loads(ev.read_text()))
    else:
        out += ["## Quality evaluation", "", "_No eval_results.json found — run run_eval.py first._", ""]

    report = "\n".join(out)
    Path(args.out).write_text(report + "\n")
    print(report)
    print(f"\n[written to {args.out}]")


if __name__ == "__main__":
    main()
