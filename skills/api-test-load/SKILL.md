---
name: api-test-load
description: Run load/stress tests against an API using `test-cases.json`. Reports throughput, latency percentiles, error rate. Use this skill when the user wants to "load test", "stress test", "find the breaking point", "measure p95 latency".
---

# api-test-load

Drive `test-cases.json` under sustained load. Reports throughput, latency distribution (p50/p90/p95/p99), and error rate, both overall and per-endpoint.

## When to invoke

- User says "load test", "stress test", "how much can this API handle", "measure p95 under load".
- Before deploying a new version, run a baseline.
- After tuning, compare two runs.

## Input

- `test-cases.json` (mandatory)
- `--config load.config.json` (optional, defines scenarios)
- `--env <name>` (optional)
- `--vus`, `--duration`, `--ramp-up` (overrides if not in config)

## Output

- `test-load-results.json` — full metrics
- `test-load-summary.json` — compact summary

## Config file (`load.config.json`)

```json
{
  "scenarios": [
    {"name": "smoke", "vus": 5, "duration": "30s"},
    {"name": "load",  "vus": 50, "duration": "2m", "rampUp": "30s"},
    {"name": "spike", "vus": 200, "duration": "10s", "rampUp": "5s"}
  ]
}
```

Each scenario runs sequentially. Total time = sum of durations.

## Metrics reported

| Metric | Meaning |
|--------|---------|
| `total_requests` | Total HTTP calls made |
| `throughput_rps` | Average requests per second |
| `latency.avg` | Mean latency (ms) |
| `latency.p50` | Median |
| `latency.p90` | 90th percentile |
| `latency.p95` | 95th percentile |
| `latency.p99` | 99th percentile |
| `latency.max` | Worst case |
| `error_rate` | Fraction of non-2xx / network errors |
| `by_endpoint` | Same metrics per endpointId |

## AI-friendly analysis (NEW)

Each scenario includes an `analysis` block designed for AI consumption:

```json
{
  "analysis": {
    "bottlenecks": [
      {
        "endpointId": "GET_/posts/1",
        "p50_ms": 840,
        "p95_ms": 3954,
        "p99_ms": 3954,
        "skew_ratio": 3.71,
        "bimodal": false,
        "verdict": "p95=3954ms exceeds 1s"
      }
    ],
    "slowest_requests": [
      {"endpointId": "GET_/posts/1", "latency_ms": 3954, "status": 200, "vu": 3}
    ],
    "error_breakdown": {
      "total": 0, "client_4xx": 0, "server_5xx": 0, "timeout": 0, "network": 0
    },
    "recommendations": [
      "GET_/posts/1: p95=3954ms exceeds 1s — investigate slow queries / missing indexes / downstream calls"
    ]
  }
}
```

AI can read this directly and write a performance report without re-computing.

### Verdict heuristics

- `skew_ratio = (p99 - p50) / p50` — high values mean heavy tail latency (GC pauses, lock contention)
- `bimodal = (skew_ratio > 10)` — suggests cache miss path; requests split between fast and slow clusters
- Recommendations are auto-generated from bottleneck verdicts + error breakdown + SLA violations

### Example AI prompt using analysis

```
Read test-load-results.json. The `analysis` block contains:
- bottlenecks[] — sorted by severity
- slowest_requests[] — top 10 individual slow requests
- error_breakdown{} — error categorization
- recommendations[] — pre-computed suggestions

Write a performance report identifying the top 3 issues with concrete remediation steps.
```

## Steps

1. **Quick run** (one scenario):
   ```bash
   python skills/api-test-load/scripts/load.py test-cases.json \
     --base-url https://api.example.com \
     --vus 50 --duration 30s
   ```

2. **Multi-scenario** (recommended):
   ```bash
   python skills/api-test-load/scripts/load.py test-cases.json \
     --config load.config.json
   ```

3. **Read results**:
   ```bash
   jq '.summary' test-load-results.json
   jq '.scenarios[].by_endpoint[0]' test-load-results.json
   ```

4. **Compare runs** (manual): diff two `test-load-results.json` files.

## Rules

- **Stdlib only**: threading + urllib. No external load testing libs.
- **Reasonable upper bound**: 100-200 VUs comfortably. For 1000+ VUs, recommend k6.
- **Auth applied**: same env vars / OAuth2 as `api-test-run`.
- **No assertions**: load test only measures; doesn't validate responses.
- **Random case selection**: each VU picks a random case per loop to simulate real traffic.

## When to stop

Press `Ctrl+C` for graceful shutdown. Partial results are saved.

## Next step

After load test, run `api-test-report` or compare runs with `diff` / `jq`.
