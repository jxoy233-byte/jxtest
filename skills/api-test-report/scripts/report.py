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


def render(results: dict) -> str:
    s = results["summary"]
    total = s["total"] or 1
    pass_rate = round(s["passed"] / total * 100, 1)

    failed = [r for r in results["results"] if r["status"] != "passed"]
    by_class: dict[str, int] = {}
    for r in failed:
        k = r.get("failureClass") or r["status"]
        by_class[k] = by_class.get(k, 0) + 1

    slowest = sorted(results["results"], key=lambda r: r.get("durationMs") or 0, reverse=True)[:5]

    rows = []
    for r in results["results"]:
        rows.append(f"""<tr>
  <td>{status_badge(r['status'])}</td>
  <td><code>{esc(r['caseId'])}</code></td>
  <td>{esc(r['category'])}</td>
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
        detail_rows.append(f"""<details>
  <summary><code>{esc(r['caseId'])}</code> — {esc(r.get('failureClass', r['status']))}</summary>
  <pre>URL:    {esc(r.get('request', {}).get('url', ''))}
Status:  {esc(r.get('httpStatus', '-'))}
Failed:  {esc(r.get('failureClass', '-'))}
Error:   {esc(err)}
----- Response body -----
{esc(body)}</pre>
</details>""")

    class_cards = "".join(
        f'<div class="card"><div class="num">{n}</div><div class="lbl">{esc(k)}</div></div>'
        for k, n in sorted(by_class.items(), key=lambda x: -x[1])
    )

    slow_rows = "".join(
        f"<tr><td><code>{esc(r['caseId'])}</code></td><td>{r.get('durationMs', 0)}ms</td><td>{esc(r.get('httpStatus', '-'))}</td></tr>"
        for r in slowest
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Report — {esc(results.get('startedAt', ''))}</title>
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
<h1>Test Report</h1>
<div class="meta">Started {esc(results.get('startedAt', ''))} • Duration {results.get('durationMs', 0)}ms • Base URL {esc(results.get('baseUrl', ''))}</div>

<div class="summary">
  <div class="card"><div class="num">{s['total']}</div><div class="lbl">Total</div></div>
  <div class="card passed"><div class="num">{s['passed']}</div><div class="lbl">Passed</div></div>
  <div class="card failed"><div class="num">{s['failed']}</div><div class="lbl">Failed</div></div>
  <div class="card errors"><div class="num">{s['errors']}</div><div class="lbl">Errors</div></div>
  <div class="card"><div class="num">{pass_rate}%</div><div class="lbl">Pass rate</div></div>
</div>

{('<div class="section"><h2>Failure breakdown</h2><div class="summary">' + class_cards + '</div></div>') if class_cards else ''}

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
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Error: {src} not found")
    data = json.loads(src.read_text(encoding="utf-8"))

    html = render(data)
    Path(args.output).write_text(html, encoding="utf-8")
    s = data["summary"]
    print(f"OK  {args.output}  ({s['passed']}/{s['total']} passed, {s['failed']} failed, {s['errors']} errors)", file=sys.stderr)


if __name__ == "__main__":
    main()
