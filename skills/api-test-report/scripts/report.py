#!/usr/bin/env python3
"""Generate self-contained HTML report from test-results.json."""
import argparse
import html
import json
import sys
from pathlib import Path
from datetime import datetime


def esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def status_badge(status: str) -> str:
    color = {"passed": "#16a34a", "failed": "#dc2626", "error": "#f59e0b"}.get(status, "#6b7280")
    return f'<span class="badge" style="background:{color}">{esc(status)}</span>'


def failure_badge(cls: str | None) -> str:
    if not cls:
        return ""
    color = {
        "server_error": "#7c2d12",
        "assertion_failed": "#7c2d12",
        "network_error": "#1e40af",
        "config_error": "#6b7280",
    }.get(cls, "#6b7280")
    return f'<span class="badge small" style="background:{color}">{esc(cls)}</span>'


def render(results: dict, baseline: dict | None = None, title: str = "Test Report") -> str:
    s = results["summary"]
    total = s["total"] or 1
    pass_rate = round(s["passed"] / total * 100, 1)

    failed = [r for r in results["results"] if r["status"] != "passed"]
    by_class: dict[str, int] = {}
    for r in failed:
        k = r.get("failureClass") or r["status"]
        by_class[k] = by_class.get(k, 0) + 1

    slowest = sorted(results["results"], key=lambda r: r.get("durationMs") or 0, reverse=True)[:5]

    # Trend section: render only when a baseline was provided. Built from a
    # per-case comparison so flaky tests that randomly pass/fail don't get
    # mischaracterized as regressions.
    trend_html = ""
    if baseline:
        b_results = baseline.get("results", [])
        b_by_id = {r["caseId"]: r for r in b_results}
        regressions, fixes, new_tests = [], [], []
        for r in results["results"]:
            prev = b_by_id.get(r["caseId"])
            if prev is None:
                continue
            if prev["status"] == "passed" and r["status"] == "failed":
                regressions.append((r, prev))
            elif prev["status"] == "failed" and r["status"] == "passed":
                fixes.append(r)
        for r in results["results"]:
            if r["caseId"] not in {p["caseId"] for p in b_results}:
                new_tests.append(r)

        trend_rows = []
        for cur, prev in regressions[:20]:
            trend_rows.append(
                f"<tr><td>🔻 regressed</td><td><code>{esc(cur['caseId'])}</code></td>"
                f"<td>{esc(prev['status'])} → {esc(cur['status'])}</td></tr>"
            )
        for r in fixes[:20]:
            trend_rows.append(
                f"<tr><td>✅ fixed</td><td><code>{esc(r['caseId'])}</code></td>"
                f"<td>failed → passed</td></tr>"
            )
        for r in new_tests[:10]:
            trend_rows.append(
                f"<tr><td>🆕 new</td><td><code>{esc(r['caseId'])}</code></td>"
                f"<td>{esc(r['status'])}</td></tr>"
            )
        trend_table = "".join(trend_rows) or "<tr><td colspan='3'><em>no changes</em></td></tr>"
        bs = baseline.get("summary", {})
        cs = results["summary"]
        trend_summary = (
            f"<tr><td>passed</td><td>{bs.get('passed','-')}</td><td>{cs['passed']}</td>"
            f"<td>{cs['passed']-bs.get('passed',0):+}</td></tr>"
            f"<tr><td>failed</td><td>{bs.get('failed','-')}</td><td>{cs['failed']}</td>"
            f"<td>{cs['failed']-bs.get('failed',0):+}</td></tr>"
            f"<tr><td>errors</td><td>{bs.get('errors','-')}</td><td>{cs['errors']}</td>"
            f"<td>{cs['errors']-bs.get('errors',0):+}</td></tr>"
        )
        trend_html = f"""
<div class="section">
  <h2>Trend vs baseline</h2>
  <table>
    <tr><th>Metric</th><th>Baseline</th><th>Current</th><th>Δ</th></tr>
    {trend_summary}
  </table>
  <table style="margin-top:12px">
    <tr><th>Status</th><th>Case</th><th>Change</th></tr>
    {trend_table}
  </table>
</div>
"""
    trend_badges = ""
    if baseline:
        bs = baseline["summary"]
        cs = s
        trend_badges = (
            f"<div class='card' style='margin-top:12px'>"
            f"<div class='num'>{(cs['passed']-bs.get('passed',0)):+}</div>"
            f"<div class='lbl'>Δ passed vs baseline</div></div>"
            f"<div class='card'>"
            f"<div class='num'>{(cs['failed']-bs.get('failed',0)):+}</div>"
            f"<div class='lbl'>Δ failed vs baseline</div></div>"
        )


    rows = []
    for r in results["results"]:
        # Boundary cases use status_in (accepts 200/201/204/400/422) — surface
        # that explicitly so users don't read a 422 as a real defect.
        category_note = ""
        if r["category"] == "boundary":
            category_note = ' <span class="badge small" style="background:#0891b2">status_in</span>'
        rows.append(f"""<tr>
  <td>{status_badge(r['status'])}</td>
  <td><code>{esc(r['caseId'])}</code></td>
  <td>{esc(r['category'])}{category_note}</td>
  <td>{esc(r.get('httpStatus', '-'))}</td>
  <td>{esc(r.get('durationMs', '-'))}ms</td>
  <td>{failure_badge(r.get('failureClass'))}</td>
  <td>{esc(r.get('request', {}).get('url', ''))}</td>
</tr>""")

    detail_rows = []
    for r in failed:
        body = (r.get("response") or {}).get("body", "") or ""
        body = body[:500] + ("..." if len(body) > 500 else "")
        err = r.get("error") or ""
        boundary_hint = ""
        # Heuristic: if a boundary case ended in 400/422 via the status_in
        # assertion, that may be the intended rejection side of the boundary —
        # not a defect. Surface the hint in the failure detail pane.
        if (r.get("category") == "boundary"
                and r.get("httpStatus") in (400, 422)
                and r.get("failureClass") == "assertion_failed"):
            boundary_hint = ('\n  NOTE: boundary case expects [200/201/204/400/422]; '
                             'an observed 4xx may be the intended rejection. '
                             'Confirm before treating as a defect.\n')
        detail_rows.append(f"""<details>
  <summary><code>{esc(r['caseId'])}</code> — {esc(r.get('failureClass', r['status']))}</summary>
  <pre>URL:    {esc(r.get('request', {}).get('url', ''))}
Status:  {esc(r.get('httpStatus', '-'))}
Failed:  {esc(r.get('failureClass', '-'))}
Error:   {esc(err)}{boundary_hint}
----- Response body -----
{esc(body)}</pre>
</details>""")

    class_cards = "".join(
        f'<div class="card"><div class="num">{n}</div><div class="lbl">{esc(k)}</div></div>'
        for k, n in sorted(by_class.items(), key=lambda x: -x[1])
    )

    # Category × failure-class matrix. Helps catch the case where boundary
    # cases are being misattributed (e.g. all boundary cases hitting
    # authentication means the auth header is broken, not the API).
    categories = sorted({r["category"] for r in results["results"]} | {"positive", "negative", "boundary", "security", "idempotency"})
    failure_classes = ["assertion_failed", "server_error", "network_error", "config_error", "authentication"]
    matrix: dict[tuple[str, str], int] = {}
    for r in results["results"]:
        if r["status"] == "passed":
            continue
        matrix[(r.get("category", ""), r.get("failureClass") or "unknown")] = (
            matrix.get((r.get("category", ""), r.get("failureClass") or "unknown"), 0) + 1)
    matrix_rows = []
    for cat in categories:
        cells = []
        non_empty = False
        for fc in failure_classes:
            n = matrix.get((cat, fc), 0)
            cells.append(f"<td>{n if n else ''}</td>")
            if n:
                non_empty = True
        if non_empty:
            matrix_rows.append(f"<tr><th>{esc(cat)}</th>{''.join(cells)}</tr>")
    matrix_table = ""
    if matrix_rows:
        matrix_table = f"""
<div class="section">
  <h2>Failures by category × failure class</h2>
  <table>
    <tr><th>Category \\ Failure</th>{''.join(f'<th>{esc(fc)}</th>' for fc in failure_classes)}</tr>
    {''.join(matrix_rows)}
  </table>
  <p class="meta">Empty cells = no failures in that bucket. A boundary row full of
  <code>assertion_failed</code> usually means a generator assumption about the API shape; a row of
  <code>authentication</code> usually means header setup is broken.</p>
</div>"""

    slow_rows = "".join(
        f"<tr><td><code>{esc(r['caseId'])}</code></td><td>{r.get('durationMs', 0)}ms</td><td>{esc(r.get('httpStatus', '-'))}</td></tr>"
        for r in slowest
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — {esc(results.get('startedAt', ''))}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 24px; background: #f8fafc; color: #0f172a; }}
  h1 {{ margin: 0 0 8px 0; font-size: 24px; }}
  .meta {{ color: #64748b; margin-bottom: 24px; font-size: 14px; }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }}
  .card .num {{ font-size: 28px; font-weight: 600; }}
  .card .lbl {{ font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card.passed .num {{ color: #16a34a; }}
  .card.failed .num {{ color: #dc2626; }}
  .card.errors .num {{ color: #f59e0b; }}
  .section {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .section h2 {{ margin: 0 0 12px 0; font-size: 16px; text-transform: uppercase; color: #475569; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #e2e8f0; }}
  th {{ background: #f1f5f9; font-weight: 600; color: #475569; }}
  tr:hover {{ background: #f8fafc; }}
  code {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; background: #f1f5f9; padding: 1px 6px; border-radius: 4px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; color: white; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
  .badge.small {{ padding: 1px 6px; font-size: 10px; }}
  details {{ margin: 8px 0; }}
  summary {{ cursor: pointer; padding: 6px 0; }}
  pre {{ background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; }}
</style>
</head>
<body>
<h1>{esc(title)}</h1>
<div class="meta">Started {esc(results.get('startedAt', ''))} • Duration {results.get('durationMs', 0)}ms • Base URL {esc(results.get('baseUrl', ''))}</div>

<div class="summary">
  <div class="card"><div class="num">{s['total']}</div><div class="lbl">Total</div></div>
  <div class="card passed"><div class="num">{s['passed']}</div><div class="lbl">Passed</div></div>
  <div class="card failed"><div class="num">{s['failed']}</div><div class="lbl">Failed</div></div>
  <div class="card errors"><div class="num">{s['errors']}</div><div class="lbl">Errors</div></div>
  <div class="card"><div class="num">{pass_rate}%</div><div class="lbl">Pass rate</div></div>
</div>

{('<div class="section"><h2>Failure breakdown</h2><div class="summary">' + class_cards + trend_badges + '</div></div>') if class_cards or trend_badges else ''}

{matrix_table}

{trend_html}

<div class="section">
  <h2>Slowest tests</h2>
  <table><tr><th>Case</th><th>Duration</th><th>Status</th></tr>{slow_rows}</table>
</div>

<div class="section">
  <h2>All tests</h2>
  <table><tr><th>Status</th><th>Case</th><th>Category</th><th>HTTP</th><th>Duration</th><th>Failure</th><th>URL</th></tr>{''.join(rows)}</table>
</div>

{('<div class="section"><h2>Failure details</h2>' + ''.join(detail_rows) + '</div>') if detail_rows else ''}

</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate HTML report from test-results.json")
    ap.add_argument("input", help="test-results.json")
    ap.add_argument("-o", "--output", default="report.html")
    ap.add_argument("--baseline", help="Previous test-results.json — render a delta table so trends are obvious")
    ap.add_argument("--title", default="Test Report", help="Custom title")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Error: {src} not found")
    data = json.loads(src.read_text(encoding="utf-8"))

    baseline = None
    if args.baseline and Path(args.baseline).exists():
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    html = render(data, baseline=baseline, title=args.title)
    Path(args.output).write_text(html, encoding="utf-8")
    s = data["summary"]
    extra = ""
    if baseline:
        cur = s["passed"]
        prev = baseline.get("summary", {}).get("passed", 0)
        delta = cur - prev
        extra = f"  (Δ passed {delta:+})"
    print(f"OK  {args.output}  ({s['passed']}/{s['total']} passed, {s['failed']} failed, {s['errors']} errors){extra}", file=sys.stderr)


if __name__ == "__main__":
    main()
