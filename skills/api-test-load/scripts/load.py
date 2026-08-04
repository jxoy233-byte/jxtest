#!/usr/bin/env python3
"""Load test runner: drive test-cases.json under sustained load."""
import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _common import build_url, execute, resolve_auth, load_env, apply_defaults


def parse_duration(s: str) -> float:
    """Parse '30s' / '2m' / '1h' to seconds."""
    s = s.strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    return float(s)


def percentile(sorted_data: list, p: float) -> float:
    if not sorted_data:
        return 0
    idx = int(len(sorted_data) * p / 100)
    return sorted_data[min(idx, len(sorted_data) - 1)]


def worker_loop(vu_id: int, cases: list, base_url: str, auth_headers: dict, defaults: dict,
                end_time: float, latencies: list, by_endpoint: dict, errors_count: list,
                all_requests: list, lock: threading.Lock, sample_rate: float = 1.0):
    rng = random.Random(vu_id)
    while time.perf_counter() < end_time:
        case = apply_defaults(rng.choice(cases), defaults)
        url = build_url(base_url, case["path"], case.get("query"))
        result = execute(url, case["method"], {**case.get("headers", {}), **auth_headers}, case.get("body"), timeout=10)
        latency = result.get("durationMs", 0)
        status = result.get("status", 0)
        is_error = status >= 500 or status == 0 or (status >= 400 and status not in (404, 429))
        # Categorize error for AI breakdown
        if status == 0:
            err_kind = "network"
        elif 400 <= status < 500:
            err_kind = "client"
        elif 500 <= status < 600:
            err_kind = "server"
        else:
            err_kind = None
        # Fall back to case id when endpointId is missing — `case["endpointId"]`
        # used to raise KeyError on hand-written cases, silently killing the
        # worker after one iteration (issue: load 30 VUs × 15s ran 30 reqs in
        # 0.12s — bug report 2026-08-03).
        ep_id = case.get("endpointId") or case.get("id") or f"<missing-vu{vu_id}>"
        with lock:
            latencies.append(latency)
            errors_count[0] += 1 if is_error else 0
            ep = by_endpoint.setdefault(ep_id, {"count": 0, "errors": 0, "latencies": []})
            ep["count"] += 1
            if is_error:
                ep["errors"] += 1
            ep["latencies"].append(latency)
            # Sample individual requests for slow-request tracking (avoid memory blowup)
            if rng.random() < sample_rate:
                all_requests.append({
                    "vu": vu_id,
                    "endpointId": ep_id,
                    "status": status,
                    "latency_ms": latency,
                    "err_kind": err_kind,
                    "is_error": is_error,
                })


def run_scenario(cases: list, base_url: str, auth_headers: dict, defaults: dict,
                 vus: int, duration_s: float, ramp_up_s: float, name: str) -> dict:
    """Execute one scenario with given VUs and duration."""
    print(f"  ▶ {name}: {vus} VUs, {duration_s}s", file=sys.stderr)
    latencies: list = []
    by_endpoint: dict = {}
    errors_count = [0]
    all_requests: list = []
    lock = threading.Lock()
    start = time.perf_counter()
    end_time = start + duration_s

    with ThreadPoolExecutor(max_workers=vus) as ex:
        def _safe_worker(*args, **kwargs):
            # Worker exceptions used to be swallowed silently — the whole run
            # would look healthy for 30 VUs × 15s but actually exit after one
            # iteration. Surface the trace so the next bug is debuggable.
            try:
                worker_loop(*args, **kwargs)
            except Exception as e:
                print(f"  [vu {args[0]}] worker crashed: {type(e).__name__}: {e}",
                      file=sys.stderr)
        for i in range(vus):
            delay = (ramp_up_s / vus) * i if i > 0 and ramp_up_s > 0 else 0
            time.sleep(delay)
            if time.perf_counter() >= end_time:
                break
            # Sample-rate cap: keep all_requests ≤ ~5000 entries regardless of load
            sample_rate = min(1.0, 5000 / max(vus * duration_s * 10, 1))
            ex.submit(_safe_worker, i, cases, base_url, auth_headers, defaults,
                      end_time, latencies, by_endpoint, errors_count,
                      all_requests, lock, sample_rate)

    actual_duration = time.perf_counter() - start
    total = len(latencies)
    sorted_lat = sorted(latencies)
    summary = {
        "name": name,
        "vus": vus,
        "duration_s": round(actual_duration, 2),
        "total_requests": total,
        "throughput_rps": round(total / actual_duration, 2) if actual_duration > 0 else 0,
        "latency_ms": {
            "avg": round(sum(sorted_lat) / total, 2) if total else 0,
            "p50": percentile(sorted_lat, 50),
            "p90": percentile(sorted_lat, 90),
            "p95": percentile(sorted_lat, 95),
            "p99": percentile(sorted_lat, 99),
            "max": round(max(sorted_lat), 2) if sorted_lat else 0,
        },
        "error_rate": round(errors_count[0] / total, 4) if total else 0,
        "by_endpoint": [
            {
                "endpointId": ep_id,
                "requests": stats["count"],
                "errors": stats["errors"],
                "p50_ms": percentile(sorted(stats["latencies"]), 50),
                "p95_ms": percentile(sorted(stats["latencies"]), 95),
                "p99_ms": percentile(sorted(stats["latencies"]), 99),
            }
            for ep_id, stats in sorted(by_endpoint.items(), key=lambda x: -x[1]["count"])
        ],
    }
    return summary, all_requests


def analyze_results(summary: dict, all_requests: list, sla_violations: list[dict]) -> dict:
    """AI-friendly analysis: bottlenecks, slow requests, error breakdown, recommendations."""
    by_ep = {e["endpointId"]: e for e in summary["by_endpoint"]}

    # 1. Bottlenecks: endpoints with tail latency skew / high variance / high error rate
    bottlenecks = []
    for ep in summary["by_endpoint"]:
        ep_lat = sorted([r["latency_ms"] for r in all_requests if r["endpointId"] == ep["endpointId"]])
        if not ep_lat:
            continue
        p50 = percentile(ep_lat, 50)
        p95 = percentile(ep_lat, 95)
        p99 = percentile(ep_lat, 99)
        max_lat = max(ep_lat)
        # Skew: how heavy is the tail?
        skew_ratio = round((p99 - p50) / max(p50, 1), 2)
        # Bimodality heuristic: if p99 >> p95 with sparse outliers above, suggests cache miss path
        bimodal = skew_ratio > 10
        verdict_parts = []
        if ep["errors"] / max(ep["requests"], 1) > 0.05:
            verdict_parts.append(f"high error rate ({ep['errors']}/{ep['requests']})")
        if p95 > 1000:
            verdict_parts.append(f"p95={p95}ms exceeds 1s")
        if bimodal:
            verdict_parts.append("bimodal latency (likely cache miss path)")
        if skew_ratio > 5 and not bimodal:
            verdict_parts.append(f"heavy tail (p99/p50={skew_ratio}x)")
        bottlenecks.append({
            "endpointId": ep["endpointId"],
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1),
            "max_ms": round(max_lat, 1),
            "skew_ratio": skew_ratio,
            "bimodal": bimodal,
            "verdict": "; ".join(verdict_parts) if verdict_parts else "healthy",
        })

    # 2. Slowest individual requests (top 10)
    slowest = sorted(all_requests, key=lambda r: -r["latency_ms"])[:10]
    slowest_requests = [
        {"endpointId": r["endpointId"], "latency_ms": round(r["latency_ms"], 1),
         "status": r["status"], "vu": r["vu"]}
        for r in slowest
    ]

    # 3. Error breakdown by category
    error_breakdown = {"total": 0, "client_4xx": 0, "server_5xx": 0, "timeout": 0, "network": 0}
    for r in all_requests:
        if not r["is_error"]:
            continue
        error_breakdown["total"] += 1
        if r["status"] == 0 and r["err_kind"] == "network":
            error_breakdown["network"] += 1
        elif 400 <= r["status"] < 500:
            error_breakdown["client_4xx"] += 1
        elif 500 <= r["status"] < 600:
            error_breakdown["server_5xx"] += 1
        else:
            error_breakdown["timeout"] += 1

    # 4. Recommendations (heuristic-based, AI-readable)
    recommendations = []
    # Sort bottlenecks by severity
    flagged = sorted(bottlenecks, key=lambda b: -b["skew_ratio"])
    for b in flagged[:3]:  # top 3 problematic endpoints
        if b["bimodal"]:
            recommendations.append(
                f"{b['endpointId']}: bimodal latency (p50={b['p50_ms']}ms, p99={b['p99_ms']}ms) "
                f"— likely cache miss path; check cache hit ratio and consider prewarming"
            )
        elif "p95=" in b["verdict"] and "exceeds 1s" in b["verdict"]:
            recommendations.append(
                f"{b['endpointId']}: p95={b['p95_ms']}ms exceeds 1s — investigate slow queries / "
                f"missing indexes / downstream calls"
            )
        elif "heavy tail" in b["verdict"]:
            recommendations.append(
                f"{b['endpointId']}: heavy tail latency (p99/p50={b['skew_ratio']}x) — "
                f"check for GC pauses, lock contention, or sporadic slow requests"
            )
        elif "high error rate" in b["verdict"]:
            recommendations.append(
                f"{b['endpointId']}: high error rate — check backend health and downstream services"
            )

    # Error-rate based recommendations
    total = error_breakdown["total"]
    if total > 0:
        if error_breakdown["server_5xx"] / max(error_breakdown["total"], 1) > 0.5:
            recommendations.append(
                f"Backend errors dominate ({error_breakdown['server_5xx']}/{total} errors are 5xx) — "
                f"check application logs and recent deployments"
            )
        if error_breakdown["network"] > 10:
            recommendations.append(
                f"{error_breakdown['network']} network errors — check DNS, firewall, or connection pool exhaustion"
            )

    # SLA breaches
    if sla_violations:
        for v in sla_violations:
            recommendations.insert(0, f"SLA breach: {v['metric']}{v['op']}{v['value']} "
                                        f"actual={v['actual']:.1f} — block deploy until fixed")

    if not recommendations:
        recommendations.append("No issues detected — all endpoints within healthy thresholds")

    return {
        "bottlenecks": bottlenecks,
        "slowest_requests": slowest_requests,
        "error_breakdown": error_breakdown,
        "recommendations": recommendations,
    }


def parse_sla(spec: str) -> list[dict]:
    """Parse 'p95<500,errors<1%' → [{metric:'p95', op:'lt', value:500}, ...]"""
    rules = []
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        # Find operator
        for op in ("<=", ">=", "<", ">"):
            if op in clause:
                metric, raw = clause.split(op, 1)
                metric = metric.strip()
                val = raw.strip().rstrip("%")
                is_pct = raw.strip().endswith("%")
                rules.append({"metric": metric, "op": op, "value": float(val), "isPercent": is_pct})
                break
        else:
            print(f"  SLA: skipping unparseable clause '{clause}'", file=sys.stderr)
    return rules


def eval_sla(metrics: dict, rules: list[dict]) -> tuple[bool, list[dict]]:
    """Evaluate SLA rules against metrics. Returns (passed, violations)."""
    violations = []
    for r in rules:
        # Look up metric: top-level (error_rate) or under latency_ms (p95)
        v = metrics.get(r["metric"])
        if v is None and "latency_ms" in metrics:
            v = metrics["latency_ms"].get(r["metric"])
        if v is None:
            continue
        actual = v / 100 if r["isPercent"] else v
        op = r["op"]
        ok = {"<": actual < r["value"], "<=": actual <= r["value"],
              ">": actual > r["value"], ">=": actual >= r["value"]}[op]
        if not ok:
            violations.append({**r, "actual": actual})
    return (not violations), violations


def compare_baseline(current_endpoints: list[dict], baseline: dict, regression_pct: float) -> list[dict]:
    """Compare current per-endpoint latency against baseline. Returns list of regressions."""
    base_by_ep = {e["endpointId"]: e for e in baseline.get("scenarios", [{}])[0].get("by_endpoint", [])}
    regressions = []
    for cur in current_endpoints:
        ep_id = cur["endpointId"]
        base = base_by_ep.get(ep_id)
        if not base or cur["p95_ms"] == 0:
            continue
        delta_pct = (cur["p95_ms"] - base["p95_ms"]) / base["p95_ms"] * 100
        if delta_pct > regression_pct:
            regressions.append({
                "endpointId": ep_id,
                "current_p95_ms": cur["p95_ms"],
                "baseline_p95_ms": base["p95_ms"],
                "delta_pct": round(delta_pct, 1),
            })
    return regressions


def main() -> None:
    ap = argparse.ArgumentParser(description="Load test runner")
    ap.add_argument("cases", help="test-cases.json")
    ap.add_argument("--config", help="load.config.json with scenarios")
    ap.add_argument("-o", "--output", default="test-load-results.json")
    ap.add_argument("--base-url", default=os.environ.get("API_BASE_URL", ""))
    ap.add_argument("--env", help="Environment name")
    ap.add_argument("--vus", type=int, default=10, help="Virtual users (default scenario)")
    ap.add_argument("--duration", default="30s", help="Duration (default scenario)")
    ap.add_argument("--ramp-up", default="0s", help="Ramp-up time (default scenario)")
    ap.add_argument("--sla", help="SLA rules: 'p95<500,p99<1000,errors<1%%'")
    ap.add_argument("--baseline", help="Previous test-load-results.json to compare against")
    ap.add_argument("--regression-pct", type=float, default=20.0,
                    help="Flag endpoint if p95 grows by more than this %% vs baseline")
    ap.add_argument("--ramp-step", type=int, default=0,
                    help="Step-up load: split into N stages of equal VU increments (e.g. 4 = 25/50/75/100%% of --vus). Each stage runs for --duration seconds.")
    args = ap.parse_args()

    cases_path = Path(args.cases)
    if not cases_path.exists():
        sys.exit(f"Error: {cases_path} not found")
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if not cases:
        sys.exit("Error: no cases in test-cases.json")

    base_url = args.base_url or data.get("baseUrl", "")
    if not base_url:
        sys.exit("Error: base URL not set")

    scopes = load_env(args.env)
    auth_headers = resolve_auth(data.get("auth"), scopes, base_url).headers()
    if auth_headers.get("error"):
        sys.exit(f"Error: auth failed: {auth_headers['error']}")
    defaults = data.get("defaults", {})

    # Scenarios
    if args.config and Path(args.config).exists():
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        scenarios = cfg.get("scenarios", [])
    else:
        scenarios = [{
            "name": "default",
            "vus": args.vus,
            "duration": args.duration,
            "rampUp": args.ramp_up,
        }]

    # Step-up: a single scenario expands into N sub-scenarios that escalate VUs.
    # Capacity planners use this to find the inflection point where p95 starts
    # to bend — much cheaper than running several full --vus tests by hand.
    if args.ramp_step and args.ramp_step > 1 and not (args.config and Path(args.config).exists()):
        n = args.ramp_step
        base_scenario = scenarios[0]
        expanded = []
        for stage in range(1, n + 1):
            vu = max(1, round(args.vus * stage / n))
            expanded.append({
                **base_scenario,
                "name": f"step-{stage}/{n} ({vu} VUs)",
                "vus": vu,
                "duration": args.duration,
                "rampUp": args.ramp_up,
            })
        scenarios = expanded

    results = []
    for sc in scenarios:
        vus = int(sc.get("vus", 10))
        duration_s = parse_duration(sc.get("duration", "30s"))
        ramp_up_s = parse_duration(sc.get("rampUp", "0s"))
        name = sc.get("name", f"scenario-{len(results)}")
        result, all_requests = run_scenario(cases, base_url, auth_headers, defaults, vus, duration_s, ramp_up_s, name)

        # SLA check
        sla_violations = []
        if args.sla:
            rules = parse_sla(args.sla)
            ok, violations = eval_sla({"error_rate": result["error_rate"], **result["latency_ms"]}, rules)
            result["sla"] = {"rules": rules, "passed": ok, "violations": violations}
            sla_violations = violations
            status = "✓" if ok else "✗"
            print(f"    {status} SLA: {result['latency_ms']['p95']}ms p95, {result['error_rate']*100:.1f}% errors", file=sys.stderr)
            for v in violations:
                print(f"        VIOLATED {v['metric']}{v['op']}{v['value']}{'%' if v['isPercent'] else ''} (actual={v['actual']})", file=sys.stderr)

        # Baseline regression check
        if args.baseline and Path(args.baseline).exists():
            base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            regressions = compare_baseline(result["by_endpoint"], base, args.regression_pct)
            result["regressions"] = regressions
            for r in regressions:
                print(f"        REGRESSION: {r['endpointId']} p95 {r['baseline_p95_ms']}→{r['current_p95_ms']}ms ({r['delta_pct']:+.0f}%)", file=sys.stderr)

        # AI-friendly analysis (bottlenecks, slow requests, error breakdown, recommendations)
        result["analysis"] = analyze_results(result, all_requests, sla_violations)
        rec_count = len([r for r in result["analysis"]["recommendations"] if "No issues" not in r])
        print(f"    → {rec_count} recommendation(s) in analysis", file=sys.stderr)

        results.append(result)
        print(f"    ✓ {result['total_requests']} reqs, {result['throughput_rps']} RPS, p95={result['latency_ms']['p95']}ms, errors={result['error_rate']*100:.1f}%", file=sys.stderr)

    out = {
        "version": "1.0",
        "baseUrl": base_url,
        "env": args.env,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenarios": results,
        "summary": {
            "total_requests": sum(r["total_requests"] for r in results),
            "total_duration_s": sum(r["duration_s"] for r in results),
            "avg_rps": round(sum(r["throughput_rps"] for r in results) / len(results), 2) if results else 0,
        },
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK  {args.output}", file=sys.stderr)

    # Step-up capacity table: when --ramp-step was used, print a one-line p95
    # summary per stage so capacity planners can pick the "before the bend" row
    # without opening the JSON.
    if args.ramp_step and args.ramp_step > 1 and len(results) > 1:
        print(f"    capacity table (vu → p95 / errors):", file=sys.stderr)
        for r in results:
            err_pct = round(r["error_rate"] * 100, 1)
            print(f"      {r['vus']:>3} VUs → p95={r['latency_ms']['p95']}ms, "
                  f"rps={r['throughput_rps']}, errors={err_pct}%", file=sys.stderr)

    # Exit non-zero on SLA violations or regressions (CI-friendly)
    any_sla_fail = any(r.get("sla") and not r["sla"]["passed"] for r in results)
    any_regress = any(r.get("regressions") for r in results)
    if any_sla_fail or any_regress:
        sys.exit(1)


if __name__ == "__main__":
    main()