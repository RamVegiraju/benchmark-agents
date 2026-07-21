"""Scorers for evaluating the agent's captured traces.

Two families, applied to different questions:

GROUND-TRUTH scorers (need expectations; only for "structured" questions):
  * tool_selection_accuracy - CUSTOM, trace-based, deterministic. Built-in scorers
    can't verify a *specific* expected tool was invoked; this reads the TOOL spans off
    the trace and compares to expected_tools. No LLM -> no cost, no judge variance.
  * Correctness            - BUILT-IN LLM judge, compares the answer to expected_facts.

REFERENCE-FREE scorers (need NO ground truth; work on open-ended questions too):
  * RelevanceToQuery   - BUILT-IN LLM judge: does the answer address the request?
  * Safety             - BUILT-IN LLM judge: is the answer free of harmful content?
  * tool_appropriateness - CUSTOM make_judge (LLM). Reads {{ trace }} and reasons about
    whether the tool calls were appropriate for the request, WITHOUT a predefined
    expected-tool list — so it evaluates questions that have no ground truth.

All scorers read already-exported traces, so evaluation never touches the load path.
"""

import os

from mlflow.entities import Feedback, SpanType
from mlflow.genai.judges import meets_guidelines
from mlflow.genai.scorers import Correctness, RelevanceToQuery, Safety, scorer

# Judge LLM for the LLM-based scorers. Sonnet keeps judging cheaper/faster than the
# agent's own Opus model. Override with the JUDGE_MODEL env var.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "databricks:/databricks-claude-sonnet-4-6")


def _leaf(name: str) -> str:
    # Tool spans can be fully qualified (catalog.schema.fn); compare on the leaf name.
    return name.split(".")[-1] if "." in name else name


def _prompt_of(inputs) -> str:
    """Last user message text from a trace's inputs (shape-resilient)."""
    if isinstance(inputs, str):
        return inputs
    if isinstance(inputs, dict):
        msgs = inputs.get("messages") or inputs.get("input") or []
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                return str(m.get("content", ""))
    return str(inputs)


@scorer
def tool_selection_accuracy(expectations, trace):
    """`yes` iff every expected tool was called, read from the trace's TOOL spans."""
    expected = {_leaf(t) for t in (expectations or {}).get("expected_tools") or []}
    if not expected:
        return Feedback(value="skip", rationale="No expected_tools for this question.")

    actual = {_leaf(s.name) for s in trace.search_spans(span_type=SpanType.TOOL)}
    missing = expected - actual
    extra = actual - expected

    rationale = f"expected={sorted(expected)} | actual={sorted(actual)}"
    if missing:
        rationale += f" | missing={sorted(missing)}"
    if extra:
        rationale += f" | extra={sorted(extra)}"

    return Feedback(value="yes" if not missing else "no", rationale=rationale)


# Reference-free judge: assesses tool choice from the request + the tools actually called,
# with NO predefined expected-tool list — so it works on open-ended questions too.
# Implemented as a custom @scorer wrapping meets_guidelines (not make_judge) because
# make_judge's categorical output is not aggregated into a /mean metric, whereas a @scorer
# returning the judge's yes/no Feedback aggregates like tool_selection_accuracy does.
@scorer
def tool_appropriateness(inputs, trace):
    tools = sorted({_leaf(s.name) for s in trace.search_spans(span_type=SpanType.TOOL)})
    return meets_guidelines(
        name="tool_appropriateness",
        guidelines=[
            "The assistant has exactly two tools: get_weather(city) and get_stock_price(ticker).",
            "tools_called must be exactly the tools needed for the request: a weather intent "
            "needs get_weather, a stock intent needs get_stock_price, a request covering both "
            "needs both, and no unnecessary tools should be called.",
            "If the request needs no tool, tools_called should be empty.",
        ],
        context={"request": _prompt_of(inputs), "tools_called": tools},
        model=JUDGE_MODEL,
    )


def ground_truth_scorers():
    """Scorers that require expectations (structured questions only)."""
    return [tool_selection_accuracy, Correctness(model=JUDGE_MODEL)]


def reference_free_scorers():
    """Scorers that need no ground truth (run on every completed question)."""
    return [
        RelevanceToQuery(model=JUDGE_MODEL),
        Safety(model=JUDGE_MODEL),
        tool_appropriateness,
    ]
