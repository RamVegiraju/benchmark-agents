"""Evaluate the agent's captured load-test traces with MLflow judges (post-hoc).

Runs AFTER the load test and re-invokes the agent zero times: it pulls the traces
produced during the load window and scores them in place
(mlflow.genai.evaluate with a trace DataFrame, no predict_fn), so it adds no latency
to the load test and cannot change its measured numbers.

Flow:
  1. Fetch the load-window traces and match each to the shared question set.
  2. Classify each as COMPLETED or CANCELLED. Cancelled = an in-flight streaming request
     that was torn down at a load stage's end (no tool spans and no captured output).
     These are reported as a completion rate, NOT scored (you can't judge a non-answer).
  3. Score EVERY completed trace (no sampling):
       - reference-free scorers (relevance, safety, tool-appropriateness) on ALL completed
       - ground-truth scorers (tool_selection, correctness) on the STRUCTURED subset
  Results land as evaluation runs in the same experiment as the traces.

  DATABRICKS_CONFIG_PROFILE=<p> uv run python evaluation/run_eval.py --out eval_results.json
"""

import argparse
import json
import os
from pathlib import Path

import mlflow

from ground_truth import expectations_for, extract_prompt, has_ground_truth
from scorers import JUDGE_MODEL, ground_truth_scorers, reference_free_scorers

EXPERIMENT_PATH = os.environ.get("MLFLOW_EXPERIMENT_PATH", "/Shared/load-test-agents")
REPO_ROOT = Path(__file__).resolve().parent.parent
SINCE_FILE = REPO_ROOT / "load_testing" / "results" / "since_ms.txt"


def default_since_ms() -> int | None:
    try:
        return int(SINCE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _n_tool_spans(spans) -> int:
    """Count TOOL spans directly from the search-DataFrame `spans` column (no fetch)."""
    n = 0
    for s in spans or []:
        attrs = s.get("attributes", {}) if isinstance(s, dict) else {}
        stype = attrs.get("mlflow.spanType") if isinstance(attrs, dict) else None
        if stype and "TOOL" in str(stype):
            n += 1
    return n


def _has_output(meta) -> bool:
    out = (meta or {}).get("mlflow.traceOutputs") if isinstance(meta, dict) else None
    return bool(out) and str(out).strip() not in ("", "null", "{}", "[]")


def is_completed(row) -> bool:
    """A request completed if it called a tool or produced an answer; cancelled if neither.

    Cancelled streaming requests torn down at stage end have no tool spans and no output.
    """
    return _n_tool_spans(row["spans"]) > 0 or _has_output(row["trace_metadata"])


def fetch_traces(experiment_id, since_ms, max_results):
    filt = "attributes.status = 'OK'"
    if since_ms:
        filt += f" AND attributes.timestamp_ms > {since_ms}"
    return mlflow.search_traces(
        locations=[experiment_id], filter_string=filt, max_results=max_results,
        order_by=["attributes.timestamp_ms DESC"],
    )


def pass_rate(metrics: dict, name: str):
    if f"{name}/mean" in metrics:
        return float(metrics[f"{name}/mean"])
    yes = metrics.get(f"{name}/yes/count", 0)
    no = metrics.get(f"{name}/no/count", 0)
    return float(yes / (yes + no)) if (yes + no) else None


def workspace_host() -> str:
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if host:
        return host
    try:
        from databricks.sdk import WorkspaceClient

        return (WorkspaceClient().config.host or "").rstrip("/")
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-ms", type=int, default=default_since_ms(),
                    help="only score traces after this epoch-ms (defaults to the load window)")
    ap.add_argument("--max-traces", type=int, default=2000)
    ap.add_argument("--experiment", default=EXPERIMENT_PATH)
    ap.add_argument("--out", default=str(Path(__file__).with_name("eval_results.json")))
    args = ap.parse_args()

    mlflow.set_tracking_uri("databricks")
    exp = mlflow.set_experiment(args.experiment)

    df = fetch_traces(exp.experiment_id, args.since_ms, args.max_traces)
    df["expectations"] = [expectations_for(extract_prompt(r)) for r in df["request"]]
    matched = df[df["expectations"].notna()].reset_index(drop=True)
    unknown = len(df) - len(matched)

    matched["completed"] = matched.apply(is_completed, axis=1)
    completed = matched[matched["completed"]].reset_index(drop=True)
    cancelled = int((~matched["completed"]).sum())
    total = len(matched)
    print(f"Matched {total} traces ({unknown} unknown prompts dropped). "
          f"Completed={len(completed)}, cancelled/truncated={cancelled}.")
    if completed.empty:
        raise SystemExit("No completed traces to evaluate. Run the load test first.")

    structured = completed[completed["expectations"].apply(has_ground_truth)].reset_index(drop=True)
    open_ended = len(completed) - len(structured)
    print(f"  structured (ground truth)={len(structured)}, open-ended={open_ended}")

    # Reference-free scorers on EVERY completed trace. Drop `expectations` here: open-ended
    # rows carry None values, and MLflow would try to log them as Expectation assessments
    # (which rejects null values). These scorers need no ground truth anyway. Also drop the
    # helper `completed` column so only trace data is passed.
    rf_data = completed.drop(columns=["expectations", "completed"], errors="ignore")
    with mlflow.start_run(run_name="eval-reference-free-all"):
        rf = mlflow.genai.evaluate(data=rf_data, scorers=reference_free_scorers())
    print(f"[reference-free] run_id={rf.run_id} metrics={rf.metrics}")

    # Ground-truth scorers on the structured subset only (expectations are real lists here).
    gt = None
    if not structured.empty:
        gt_data = structured.drop(columns=["completed"], errors="ignore")
        with mlflow.start_run(run_name="eval-ground-truth"):
            gt = mlflow.genai.evaluate(data=gt_data, scorers=ground_truth_scorers())
        print(f"[ground-truth] run_id={gt.run_id} metrics={gt.metrics}")

    ws = workspace_host()
    scores = {
        "relevance_to_query": pass_rate(rf.metrics, "relevance_to_query"),
        "safety": pass_rate(rf.metrics, "safety"),
        "tool_appropriateness": pass_rate(rf.metrics, "tool_appropriateness"),
    }
    if gt is not None:
        scores["tool_selection_accuracy"] = pass_rate(gt.metrics, "tool_selection_accuracy")
        scores["correctness"] = pass_rate(gt.metrics, "correctness")

    results = {
        "experiment_path": args.experiment,
        "experiment_id": exp.experiment_id,
        "experiment_url": f"{ws}/ml/experiments/{exp.experiment_id}" if ws else None,
        "judge_model": JUDGE_MODEL,
        "since_ms": args.since_ms,
        "traces": {
            "matched": total,
            "completed": len(completed),
            "cancelled": cancelled,
            "completion_rate": round(len(completed) / total, 4) if total else None,
            "structured": len(structured),
            "open_ended": open_ended,
        },
        "runs": {
            "reference_free": rf.run_id,
            "ground_truth": gt.run_id if gt is not None else None,
        },
        "scores": scores,
        "raw_metrics": {
            "reference_free": rf.metrics,
            "ground_truth": gt.metrics if gt is not None else {},
        },
    }
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
    print(f"\n[written to {args.out}]")
    print("Scores:", json.dumps(scores, indent=2))


if __name__ == "__main__":
    main()
