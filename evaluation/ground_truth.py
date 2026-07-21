"""Ground-truth lookup for evaluating the agent's captured traces.

The canonical question set (structured + open-ended, with expected tools/facts) lives
in the shared root module `questions.py`, which the load test also uses — so the exact
prompts driven under load are the ones we have ground truth for here.

This module maps a captured trace's prompt back to its expectations:
  * structured question -> {"expected_tools": [...], "expected_facts": [...]}
  * open-ended question -> {"expected_tools": None, "expected_facts": None}
                           (scored reference-free: relevance/safety/tool-appropriateness)
  * unknown prompt      -> None (dropped from evaluation)

Run as a script to (re)write eval_dataset.json (a snapshot of the curated set):
    uv run python evaluation/ground_truth.py
"""

import json
import sys
from pathlib import Path

# Shared question set at the repo root (one level up from evaluation/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from questions import QUESTIONS, is_open_ended

DATASET_PATH = Path(__file__).with_name("eval_dataset.json")

# Exact-match lookup: the load test sends these exact strings, so trace prompts match 1:1.
_BY_PROMPT = {q["prompt"]: q for q in QUESTIONS}


def extract_prompt(request) -> str:
    """Pull the last user-message text from a trace's `request` (shape-resilient).

    Direct graph traces use {"messages": [...]}; AgentServer traces may use
    {"input": [...]}. Both carry {"role": "user", "content": ...} items.
    """
    if isinstance(request, str):
        return request
    if isinstance(request, dict):
        msgs = request.get("messages") or request.get("input") or []
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                return str(m.get("content", ""))
        if msgs and isinstance(msgs[-1], dict):
            return str(msgs[-1].get("content", ""))
    return str(request)


def expectations_for(prompt: str) -> dict | None:
    """Return expectations for a known prompt, or None if it isn't in the question set."""
    q = _BY_PROMPT.get((prompt or "").strip())
    if q is None:
        return None
    return {"expected_tools": q["expected_tools"], "expected_facts": q["expected_facts"]}


def has_ground_truth(expectations: dict | None) -> bool:
    """True if this trace has expected facts to check (a structured question)."""
    return bool(expectations) and expectations.get("expected_facts") is not None


def main():
    DATASET_PATH.write_text(json.dumps(QUESTIONS, indent=2) + "\n")
    structured = [q for q in QUESTIONS if not is_open_ended(q)]
    open_ended = [q for q in QUESTIONS if is_open_ended(q)]
    print(f"Wrote {len(QUESTIONS)} curated questions to {DATASET_PATH}")
    print(f"  structured (ground truth): {len(structured)}")
    for q in structured:
        print(f"    - {q['prompt']!r} -> tools={q['expected_tools']}")
    print(f"  open-ended (reference-free): {len(open_ended)}")
    for q in open_ended:
        print(f"    - {q['prompt']!r}")


if __name__ == "__main__":
    main()
