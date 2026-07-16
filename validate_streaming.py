"""Validate that the /invocations streaming endpoint delivers tokens incrementally.

Timestamps every SSE event as it arrives on the client. Real streaming => text
deltas trickle in over time with gaps between them; buffered/fake streaming =>
all deltas land in one clump at the end (TTFT ≈ total).

Run (server must be up):
  uv run python validate_streaming.py --prompt "weather in Boston?"
"""

import argparse
import json
import statistics
import time

import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:8000")
    ap.add_argument("--prompt", default="What is the weather in Boston and the stock price of AAPL?")
    args = ap.parse_args()

    body = {"input": [{"role": "user", "content": args.prompt}], "stream": True}
    start = time.perf_counter()
    events = []  # (t_ms, type, size)
    delta_times = []  # arrival ms of each text delta

    r = requests.post(f"{args.host}/invocations", json=body, stream=True)
    for raw in r.iter_lines():
        if not raw or not raw.startswith(b"data: "):
            continue
        now = (time.perf_counter() - start) * 1000
        payload = raw[len(b"data: "):]
        if payload == b"[DONE]":
            events.append((now, "[DONE]", 0))
            break
        try:
            evt = json.loads(payload)
            etype = evt.get("type", "?")
        except ValueError:
            etype = "?"
            evt = {}
        events.append((now, etype, len(payload)))
        if etype == "response.output_text.delta":
            delta_times.append(now)

    total = (time.perf_counter() - start) * 1000

    print(f"\nEvents received: {len(events)}  |  text deltas: {len(delta_times)}")
    print("\nFirst 8 events (arrival ms → type):")
    for t, etype, _ in events[:8]:
        print(f"  {t:8.1f} ms  {etype}")

    if len(delta_times) >= 2:
        gaps = [b - a for a, b in zip(delta_times, delta_times[1:])]
        span = delta_times[-1] - delta_times[0]
        ttft = delta_times[0]
        print("\n--- text-delta timing ---")
        print(f"  TTFT (first delta):     {ttft:8.1f} ms")
        print(f"  last delta:             {delta_times[-1]:8.1f} ms")
        print(f"  delta span (last-first):{span:8.1f} ms  over {len(delta_times)} deltas")
        print(f"  inter-delta gap median: {statistics.median(gaps):8.1f} ms  (max {max(gaps):.1f})")
        print(f"  total (to [DONE]):      {total:8.1f} ms")
        # Real streaming => deltas trickle over time with real gaps. Buffered =>
        # all deltas share ~one timestamp (span ≈ 0). Judge the trickle itself, NOT
        # span/total: in a ReAct agent most of `total` is the pre-answer tool phase.
        median_gap = statistics.median(gaps)
        if span > 30 and len(delta_times) > 3 and median_gap > 1:
            print(f"\n  VERDICT: REAL incremental streaming — {len(delta_times)} deltas "
                  f"trickle over {span:.0f} ms (median {median_gap:.0f} ms apart), not one flush.")
        else:
            print(f"\n  VERDICT: SUSPECT — deltas clustered in {span:.0f} ms; may be buffered.")
        print("  (High TTFT is expected for tool-using prompts: the answer only streams "
              "after the ReAct decide→tool→answer steps.)")
    else:
        print("\n  Only one/zero deltas — cannot confirm incremental streaming.")


if __name__ == "__main__":
    main()
