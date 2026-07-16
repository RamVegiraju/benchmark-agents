"""Locust load test for the MLflow AgentServer.

Two user types, run them separately to benchmark each mode cleanly:

  # non-streaming (total response latency)
  locust -f locustfile.py NonStreamingUser --host http://localhost:8000

  # streaming (TTFT + full-stream duration reported as separate metrics)
  locust -f locustfile.py StreamingUser --host http://localhost:8000

Or run both together and compare in the web UI (http://localhost:8089):
  locust -f locustfile.py --host http://localhost:8000

Each request randomly picks one of three prompt shapes so the load exercises
single-tool and both-tool code paths. The MLflow traces for these requests are
viewable in the workspace experiment for per-request bottleneck analysis.
"""

import json
import random
import time

from locust import HttpUser, between, task

# (prompt, weight) — exercises tool A only, tool B only, and both tools.
PROMPTS = [
    ("What's the weather in Boston?", 3),
    ("What's the stock price of AAPL?", 3),
    ("What's the weather in Boston and the stock price of AAPL?", 2),
]
_choices, _weights = zip(*PROMPTS)


def pick_prompt() -> str:
    return random.choices(_choices, weights=_weights, k=1)[0]


def build_body(prompt: str, stream: bool) -> dict:
    return {"input": [{"role": "user", "content": prompt}], "stream": stream}


class NonStreamingUser(HttpUser):
    """Measures total response latency (request -> full JSON body)."""

    wait_time = between(0.1, 0.5)

    @task
    def invoke(self):
        body = build_body(pick_prompt(), stream=False)
        with self.client.post(
            "/invocations", json=body, name="POST /invocations [non-stream]", catch_response=True
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"status {resp.status_code}: {resp.text[:200]}")
            elif "output" not in resp.json():
                resp.failure("no output field in response")
            else:
                resp.success()


class StreamingUser(HttpUser):
    """Measures TTFT (first token) and full-stream duration as separate metrics."""

    wait_time = between(0.1, 0.5)

    @task
    def invoke_stream(self):
        body = build_body(pick_prompt(), stream=True)
        start = time.perf_counter()
        first_token_at = None
        got_done = False

        # NOTE: with stream=True, locust's auto-recorded time is time-to-first-byte
        # (headers), NOT the full stream. We measure TTFT and total ourselves below
        # with perf_counter and fire them as separate, accurate metrics.
        with self.client.post(
            "/invocations",
            json=body,
            stream=True,
            name="POST /invocations [stream] (conn/TTFB)",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"status {resp.status_code}")
                return
            for raw in resp.iter_lines():
                if not raw or not raw.startswith(b"data: "):
                    continue
                payload = raw[len(b"data: "):]
                if payload == b"[DONE]":
                    got_done = True
                    break
                # TTFT = first *text* token of the answer. Parse the event type so
                # tool-call items (function_call/…) can't false-trigger.
                if first_token_at is None:
                    try:
                        if json.loads(payload).get("type") == "response.output_text.delta":
                            first_token_at = time.perf_counter()
                    except ValueError:
                        pass
            resp.success() if got_done else resp.failure("stream ended without [DONE]")

        total_ms = (time.perf_counter() - start) * 1000
        self._fire("POST /invocations [stream-total]", total_ms)
        if first_token_at is not None:
            self._fire("POST /invocations [stream-TTFT]", (first_token_at - start) * 1000)

    def _fire(self, name: str, response_time_ms: float):
        self.environment.events.request.fire(
            request_type="POST",
            name=name,
            response_time=response_time_ms,
            response_length=0,
            exception=None,
            context={},
        )
