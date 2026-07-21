"""Shared question set for BOTH the load test and the evaluation (imported by
load_testing/locustfile.py and evaluation/ground_truth.py).

Each entry has a load-test `weight` plus optional ground truth:
  * `expected_tools` + `expected_facts` set -> a "structured" question we can check
    against ground truth (tool-call correctness + factual correctness).
  * both None -> an "open-ended" question with no predefined answer. It is still
    evaluated, but only with reference-free scorers (relevance, safety, tool
    appropriateness) that need no ground truth.

Ground-truth facts mirror the deterministic, argument-agnostic mock tools in agent.py:
    get_weather(city)    -> "The weather in {city} is 72F and sunny."
    get_stock_price(tkr) -> "{TKR} is trading at $187.34, up 1.2% today."
so the expected facts below are exactly what a correct answer must convey.
"""


def _weather_facts(city: str) -> list[str]:
    return [f"The weather in {city} is 72F and sunny"]


def _stock_facts(ticker: str) -> list[str]:
    return [f"{ticker} is trading at $187.34", f"{ticker} is up 1.2% today"]


QUESTIONS = [
    # --- structured: have ground truth (expected tools + facts) ---
    {"prompt": "What's the weather in Boston?", "weight": 3,
     "expected_tools": ["get_weather"], "expected_facts": _weather_facts("Boston")},
    {"prompt": "What's the weather in Seattle?", "weight": 1,
     "expected_tools": ["get_weather"], "expected_facts": _weather_facts("Seattle")},
    {"prompt": "What's the stock price of AAPL?", "weight": 3,
     "expected_tools": ["get_stock_price"], "expected_facts": _stock_facts("AAPL")},
    {"prompt": "What's the stock price of TSLA?", "weight": 1,
     "expected_tools": ["get_stock_price"], "expected_facts": _stock_facts("TSLA")},
    {"prompt": "What's the weather in Boston and the stock price of AAPL?", "weight": 2,
     "expected_tools": ["get_weather", "get_stock_price"],
     "expected_facts": _weather_facts("Boston") + _stock_facts("AAPL")},

    # --- open-ended: no predefined answer -> reference-free scorers only ---
    {"prompt": "I'm heading to Chicago this weekend — what should I expect outside?",
     "weight": 1, "expected_tools": None, "expected_facts": None},
    {"prompt": "How is Nvidia doing in the market today?",
     "weight": 1, "expected_tools": None, "expected_facts": None},
    {"prompt": "Give me a quick morning briefing: New York weather and the MSFT stock price.",
     "weight": 1, "expected_tools": None, "expected_facts": None},
]


def is_open_ended(q: dict) -> bool:
    return q.get("expected_tools") is None and q.get("expected_facts") is None


def weighted_prompts() -> tuple[list[str], list[int]]:
    """(prompts, weights) for Locust's random.choices — includes open-ended questions."""
    return [q["prompt"] for q in QUESTIONS], [q["weight"] for q in QUESTIONS]
