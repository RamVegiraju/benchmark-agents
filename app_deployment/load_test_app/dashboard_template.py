"""Build a self-contained HTML dashboard from a load-test run directory.

Reads each per-label Locust CSV (results_stats_history.csv for the ramp, results_stats.csv
for totals) and renders KPI cards + Chart.js charts. No server needed — open the file.

  uv run dashboard_template.py ../../load-test-runs/<run-name>/
"""

import csv
import json
import sys
from pathlib import Path

# The streaming completed-request metric fired by locustfile.py; it's the true QPS.
_TOTAL_METRIC = "POST /invocations [stream-total]"


def _read_history(label_dir: Path):
    """Return list of {users, rps, p95} sampled over the ramp for the completed-request metric."""
    hist = label_dir / "results_stats_history.csv"
    if not hist.exists():
        return []
    rows = list(csv.DictReader(hist.open()))
    picked = [r for r in rows if r.get("Name") == _TOTAL_METRIC] or \
             [r for r in rows if r.get("Name") == "Aggregated"]
    out = []
    for r in picked:
        try:
            out.append({
                "users": int(float(r.get("User Count") or 0)),
                "rps": float(r.get("Requests/s") or 0),
                "p95": float(r.get("95%") or 0),
            })
        except ValueError:
            continue
    return out


def _summarize(label: str, size: str, samples: list) -> dict:
    if not samples:
        return {"label": label, "size": size, "peak_qps": 0, "users_at_peak": 0,
                "p95_at_peak": 0, "samples": []}
    peak = max(samples, key=lambda s: s["rps"])
    return {
        "label": label, "size": size,
        "peak_qps": round(peak["rps"], 1),
        "users_at_peak": peak["users"],
        "p95_at_peak": round(peak["p95"], 0),
        "samples": samples,
    }


def build(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    cfg = {}
    cfg_file = run_dir / "test_config.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
    labels = cfg.get("labels") or [d.name for d in run_dir.iterdir() if d.is_dir()]
    sizes = cfg.get("compute_sizes") or ["medium"] * len(labels)

    configs = [
        _summarize(label, size, _read_history(run_dir / label))
        for label, size in zip(labels, sizes)
    ]
    best = max(configs, key=lambda c: c["peak_qps"], default={"label": "-", "peak_qps": 0})
    overall_peak = best["peak_qps"]
    lowest_p95 = min((c["p95_at_peak"] for c in configs if c["peak_qps"]), default=0)

    html = _HTML.replace("__DATA__", json.dumps(configs)) \
                .replace("__CFG__", json.dumps(cfg)) \
                .replace("__BEST__", str(best["label"])) \
                .replace("__PEAK__", str(overall_peak)) \
                .replace("__LOWP95__", str(lowest_p95)) \
                .replace("__RUN__", run_dir.name)
    out = run_dir / "dashboard.html"
    out.write_text(html)
    return out


_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Load test — __RUN__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{font-family:system-ui,sans-serif;margin:24px;background:#0f1117;color:#e6e6e6}
 h1{font-size:20px} .cards{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}
 .card{background:#1a1d27;border:1px solid #2a2f3a;border-radius:10px;padding:16px;min-width:170px}
 .card .k{font-size:12px;color:#9aa4b2} .card .v{font-size:24px;font-weight:600;margin-top:6px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px} .panel{background:#1a1d27;border:1px solid #2a2f3a;border-radius:10px;padding:16px}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px} th,td{border-bottom:1px solid #2a2f3a;padding:6px 8px;text-align:right} th:first-child,td:first-child{text-align:left}
</style></head><body>
<h1>Load test — __RUN__</h1>
<div class="cards">
 <div class="card"><div class="k">Best config (peak QPS)</div><div class="v">__BEST__</div></div>
 <div class="card"><div class="k">Peak QPS</div><div class="v">__PEAK__</div></div>
 <div class="card"><div class="k">Lowest p95 @ peak (ms)</div><div class="v">__LOWP95__</div></div>
</div>
<div class="grid">
 <div class="panel"><h3>Peak QPS by config</h3><canvas id="qps"></canvas></div>
 <div class="panel"><h3>p95 latency @ peak (ms)</h3><canvas id="p95"></canvas></div>
 <div class="panel" style="grid-column:1/3"><h3>QPS ramp progression (users → QPS)</h3><canvas id="ramp"></canvas></div>
</div>
<div class="panel" style="margin-top:20px"><h3>Results</h3>
 <table id="tbl"><thead><tr><th>Config</th><th>Compute</th><th>Peak QPS</th><th>Users @ peak</th><th>p95 @ peak (ms)</th></tr></thead><tbody></tbody></table>
</div>
<script>
const DATA=__DATA__, CFG=__CFG__;
const labels=DATA.map(d=>d.label);
new Chart(qps,{type:'bar',data:{labels,datasets:[{label:'Peak QPS',data:DATA.map(d=>d.peak_qps),backgroundColor:'#4c8bf5'}]}});
new Chart(p95,{type:'bar',data:{labels,datasets:[{label:'p95 (ms)',data:DATA.map(d=>d.p95_at_peak),backgroundColor:'#f5a04c'}]}});
new Chart(ramp,{type:'line',data:{datasets:DATA.map((d,i)=>({label:d.label,data:d.samples.map(s=>({x:s.users,y:s.rps})),borderColor:`hsl(${i*67%360} 70% 60%)`,tension:.2}))},
 options:{parsing:false,scales:{x:{type:'linear',title:{display:true,text:'concurrent users'}},y:{title:{display:true,text:'QPS (completed)'}}}}});
const tb=document.querySelector('#tbl tbody');
DATA.forEach(d=>{const tr=document.createElement('tr');
 tr.innerHTML=`<td>${d.label}</td><td>${d.size}</td><td>${d.peak_qps}</td><td>${d.users_at_peak}</td><td>${d.p95_at_peak}</td>`;tb.appendChild(tr);});
</script></body></html>"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: dashboard_template.py <run-dir>")
        raise SystemExit(2)
    print(build(Path(sys.argv[1])))
