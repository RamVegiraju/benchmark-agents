"""Locust load test for the agent deployed as a Databricks App (Part 3).

Difference from load_testing/locustfile.py (Parts 1 & 2, which hit a local server
with no auth): a deployed app sits behind Databricks OAuth, so every request needs a
bearer token. This file mints one via the M2M client-credentials grant and refreshes
it before expiry — U2M tokens would expire mid-run and break a long sweep.

Runs two ways, sharing this one file:
  * As a Databricks App  — Locust web UI on :8000 (see app.yaml). Click Start; if
    LOCUST_AUTORAMP=1 it ramps to saturation via StepRampShape.
  * Headless / local      — driven by run_load_test.py across app URLs, with a
    dashboard. Also plain: `locust -f locustfile.py --headless -u 50 -r 20 -t 5m`.

Prompts intentionally mirror the shapes in the repo's questions.py (single-tool,
both-tools, open-ended). They're inlined here so the app is self-contained and
deployable on its own (source_code_path is just this directory).

Env:
  TARGET_HOST / --host        agent app base URL (https://<app>.<region>.databricksapps.com)
  DATABRICKS_HOST             workspace URL for the OAuth token endpoint
  DATABRICKS_CLIENT_ID        service-principal client id  (M2M)
  DATABRICKS_CLIENT_SECRET    service-principal client secret
  MAX_USERS / STEP_SIZE / STEP_DURATION   ramp shape (used when LOCUST_AUTORAMP=1)
  LOCUST_AUTORAMP=1           enable the step-ramp shape
"""

import json
import os
import random
import threading
import time
from typing import Optional

import requests
from locust import HttpUser, LoadTestShape, between, events, task

# --- prompts (mirror questions.py: single-tool, both-tools, open-ended) -----------
_PROMPTS = [
    ("What's the weather in Boston?", 1),
    ("What's the stock price of AAPL?", 1),
    ("What's the weather in Boston and the stock price of AAPL?", 1),
    ("How is Nvidia doing in the market today?", 1),
]
_CHOICES = [p for p, _ in _PROMPTS]
_WEIGHTS = [w for _, w in _PROMPTS]


def pick_prompt() -> str:
    return random.choices(_CHOICES, weights=_WEIGHTS, k=1)[0]


# --- M2M OAuth token, cached + auto-refreshed across all Locust users -------------
class _TokenProvider:
    """Client-credentials token with a background-safe refresh (~60s early)."""

    def __init__(self) -> None:
        # Databricks Apps inject DATABRICKS_HOST as a bare hostname (no scheme); add it
        # so requests doesn't raise MissingSchema on the token endpoint.
        host = (os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
        if host and not host.startswith("http"):
            host = "https://" + host
        self._host = host
        self._cid = os.environ.get("DATABRICKS_CLIENT_ID")
        self._secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._host and self._cid and self._secret)

    def token(self) -> Optional[str]:
        if not self.enabled:
            return None
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            resp = requests.post(
                f"{self._host}/oidc/v1/token",
                auth=(self._cid, self._secret),
                data={"grant_type": "client_credentials", "scope": "all-apis"},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 3600))
            return self._token


_TOKENS = _TokenProvider()


def _auth_headers() -> dict:
    tok = _TOKENS.token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


@events.test_start.add_listener
def _warn_if_no_auth(environment, **_):
    if not _TOKENS.enabled:
        print(
            "[locustfile] No M2M creds (DATABRICKS_CLIENT_ID/SECRET/HOST) — sending "
            "unauthenticated requests. Fine for a local server; a deployed app will 401."
        )


def build_body(prompt: str, stream: bool) -> dict:
    return {"input": [{"role": "user", "content": prompt}], "stream": stream}


class StreamingUser(HttpUser):
    """One streaming request per task, reported as exactly two clean metrics:

      * "POST /invocations (end-to-end)" — the full request lifecycle: send until the
        stream finishes ([DONE]). THIS is the request-latency / throughput number.
      * "POST /invocations (TTFT)"       — time to first answer token (user-perceived
        wait). Fired only when a text token was seen.

    We bypass Locust's HttpSession (its auto-metric for a streamed request is just
    time-to-first-byte, which is confusing) and fire our own timings, so the stats
    table shows only these two rows — no conn/TTFB noise.
    """

    wait_time = between(0.1, 0.5)

    def on_start(self):
        self._session = requests.Session()

    @task
    def invoke(self):
        url = self.host.rstrip("/") + "/invocations"
        body = build_body(pick_prompt(), stream=True)
        start = time.perf_counter()
        first_token_at = None
        error = None
        try:
            with self._session.post(
                url, json=body, headers=_auth_headers(), stream=True, timeout=120
            ) as resp:
                if resp.status_code != 200:
                    error = f"status {resp.status_code}: {resp.text[:200]}"
                else:
                    got_done = False
                    got_text = False
                    for raw in resp.iter_lines():
                        if not raw or not raw.startswith(b"data: "):
                            continue
                        payload = raw[len(b"data: "):]
                        if payload == b"[DONE]":
                            got_done = True
                            break
                        try:
                            evt = json.loads(payload)
                        except ValueError:
                            continue
                        etype = evt.get("type", "")
                        # The server can stream a 200 then emit an error event (e.g. FM
                        # 429). [DONE] alone is NOT success — check for an error event
                        # and that we actually got answer text, or we'd log a false pass.
                        if "error" in etype.lower() or evt.get("error"):
                            error = f"stream error event: {str(evt)[:160]}"
                            break
                        if etype == "response.output_text.delta":
                            got_text = True
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                    if error is None and not got_done:
                        error = "stream ended without [DONE]"
                    elif error is None and not got_text:
                        error = "stream completed but produced no answer text"
        except Exception as e:  # noqa: BLE001 - report any client error as a failure
            error = str(e)

        total_ms = (time.perf_counter() - start) * 1000
        self._fire("POST /invocations (end-to-end)", total_ms, error)
        if error is None and first_token_at is not None:
            self._fire("POST /invocations (TTFT)", (first_token_at - start) * 1000, None)

    def _fire(self, name: str, response_time_ms: float, error):
        self.environment.events.request.fire(
            request_type="POST",
            name=name,
            response_time=response_time_ms,
            response_length=0,
            exception=Exception(error) if error else None,
            context={},
        )


# --- optional step-ramp to saturation ---------------------------------------------
# IMPORTANT: Locust auto-discovers ANY non-abstract LoadTestShape in this module, and
# a shape whose tick() returns None STOPS the run. So we must only *define* the class
# when auto-ramp is requested; otherwise the web UI's manual user box would be killed
# instantly. When LOCUST_AUTORAMP is unset -> no shape -> manual/web-UI control.
if os.environ.get("LOCUST_AUTORAMP", "0").lower() in ("1", "true", "yes"):

    class StepRampShape(LoadTestShape):
        """Adds STEP_SIZE users every STEP_DURATION seconds up to MAX_USERS, then stops."""

        def __init__(self) -> None:
            super().__init__()
            self.max_users = int(os.environ.get("MAX_USERS", "300"))
            self.step_size = int(os.environ.get("STEP_SIZE", "20"))
            self.step_duration = int(os.environ.get("STEP_DURATION", "30"))

        def tick(self):
            step = int(self.get_run_time() // self.step_duration) + 1
            users = step * self.step_size
            if users > self.max_users:
                return None  # sweep complete -> stop
            return (users, self.step_size)
