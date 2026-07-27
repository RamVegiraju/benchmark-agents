"""Headless load-test orchestrator (eng's local/CI path; the app is the UI path).

Ramps each app URL to saturation with the shared locustfile.py (StepRampShape via
LOCUST_AUTORAMP) and writes per-config Locust CSVs, then optionally an HTML dashboard.
Use this to sweep the agent-app worker/compute matrix and find each variant's peak QPS.

  uv run run_load_test.py \
      --app-url https://agent-w2.<region>.databricksapps.com --label w2 \
      --app-url https://agent-w4.<region>.databricksapps.com --label w4 \
      --client-id $DATABRICKS_CLIENT_ID --client-secret $DATABRICKS_CLIENT_SECRET \
      --max-users 300 --step-size 20 --step-duration 30 --dashboard --run-name sweep1

Auth: pass --client-id/--client-secret (or set DATABRICKS_CLIENT_ID/SECRET +
DATABRICKS_HOST). Long sweeps must use M2M OAuth — U2M tokens expire mid-run.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE.parent.parent / "load-test-runs"


def _label_from_url(url: str) -> str:
    host = urlparse(url).netloc.split(".")[0]
    return host or "app"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ramp-to-saturation load test for agent apps.")
    p.add_argument("--app-url", action="append", required=True, help="Agent app URL (repeatable).")
    p.add_argument("--label", action="append", default=[], help="Label per app (repeatable).")
    p.add_argument("--compute-size", action="append", default=[], help="Compute tag per app (repeatable).")
    p.add_argument("--client-id", default=os.environ.get("DATABRICKS_CLIENT_ID"))
    p.add_argument("--client-secret", default=os.environ.get("DATABRICKS_CLIENT_SECRET"))
    p.add_argument("--databricks-host", default=os.environ.get("DATABRICKS_HOST"))
    p.add_argument("--max-users", type=int, default=int(os.environ.get("MAX_USERS", "300")))
    p.add_argument("--step-size", type=int, default=int(os.environ.get("STEP_SIZE", "20")))
    p.add_argument("--step-duration", type=int, default=int(os.environ.get("STEP_DURATION", "30")))
    p.add_argument("--spawn-rate", type=int, default=20)
    p.add_argument("--warmup", default="10s", help="Warmup duration (discarded).")
    p.add_argument("--run-name", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    p.add_argument("--dashboard", action="store_true", help="Build HTML dashboard after the run.")
    return p.parse_args()


def base_env(args: argparse.Namespace) -> dict:
    env = os.environ.copy()
    env["LOCUST_AUTORAMP"] = "1"
    env["MAX_USERS"] = str(args.max_users)
    env["STEP_SIZE"] = str(args.step_size)
    env["STEP_DURATION"] = str(args.step_duration)
    if args.databricks_host:
        env["DATABRICKS_HOST"] = args.databricks_host
    if args.client_id:
        env["DATABRICKS_CLIENT_ID"] = args.client_id
    if args.client_secret:
        env["DATABRICKS_CLIENT_SECRET"] = args.client_secret
    return env


def healthcheck(url: str, env: dict) -> bool:
    """One streaming request to confirm the app answers and streams to [DONE]."""
    code = subprocess.call(
        ["locust", "-f", "locustfile.py", "StreamingUser", "--host", url,
         "--headless", "-u", "1", "-r", "1", "-t", "5s", "--only-summary"],
        cwd=str(HERE), env={**env, "LOCUST_AUTORAMP": "0"},
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    return code == 0


def run_one(url: str, label: str, out_dir: Path, args: argparse.Namespace, env: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # duration = (max/step) steps * step_duration, + a little slack for drain.
    steps = max(1, args.max_users // args.step_size)
    total = steps * args.step_duration + args.step_duration
    print(f">>> {label}: warmup {args.warmup}, then ramp to {args.max_users} users "
          f"(+{args.step_size}/{args.step_duration}s) ~{total}s")

    # Warmup (discarded, no --csv).
    subprocess.call(
        ["locust", "-f", "locustfile.py", "StreamingUser", "--host", url,
         "--headless", "-u", str(args.step_size), "-r", str(args.spawn_rate),
         "-t", args.warmup, "--only-summary"],
        cwd=str(HERE), env={**env, "LOCUST_AUTORAMP": "0"},
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )

    # Measured ramp (StepRampShape drives users; -t is an upper bound).
    log = (out_dir / "locust_output.log").open("w")
    subprocess.call(
        ["locust", "-f", "locustfile.py", "StreamingUser", "--host", url,
         "--headless", "-t", f"{total}s", "--stop-timeout", str(args.step_duration),
         "--csv", str(out_dir / "results"), "--csv-full-history", "--only-summary"],
        cwd=str(HERE), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    log.close()


def main() -> int:
    args = parse_args()
    labels = list(args.label) + [_label_from_url(u) for u in args.app_url[len(args.label):]]
    sizes = list(args.compute_size) + ["medium"] * (len(args.app_url) - len(args.compute_size))

    run_dir = RUNS_DIR / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test_config.json").write_text(json.dumps({
        "run_name": args.run_name,
        "app_urls": args.app_url, "labels": labels, "compute_sizes": sizes,
        "max_users": args.max_users, "step_size": args.step_size,
        "step_duration": args.step_duration, "spawn_rate": args.spawn_rate,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    env = base_env(args)
    if not (args.client_id and args.client_secret):
        print("[run] WARNING: no M2M creds — requests will be unauthenticated (fine only "
              "for a local server; a deployed app returns 401).")

    for url, label, size in zip(args.app_url, labels, sizes):
        if args.client_id and not healthcheck(url, env):
            print(f"[run] healthcheck FAILED for {label} ({url}); skipping. "
                  f"See that the app is ACTIVE and creds can invoke it.")
            continue
        run_one(url, label, run_dir / label, args, env)

    print(f">>> results in {run_dir}")
    if args.dashboard:
        import dashboard_template
        out = dashboard_template.build(run_dir)
        print(f">>> dashboard: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
