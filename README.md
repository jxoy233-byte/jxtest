# jxtest

**AI-driven API testing toolkit.** From any OpenAPI/Postman/HAR spec, one CLI (`jxtest <cmd>`) generates 5 categories of tests, runs functional/load/security suites, blocks breaking changes, finds coverage gaps, and self-heals failures. Replaces Postman for automated workflows; adds load testing, OWASP scanning, envelope-aware assertions (HTTP 200 + `body.code` APIs), declarative login auth, and AI-friendly analysis that Postman doesn't have.

> **For AI agents**: read [`SKILL.md`](./SKILL.md) — it's the canonical instruction manual for driving jxtest from an LLM.

## Install

Requirements: **Python 3.10+** and `pyyaml` (one dependency, for parsing YAML OpenAPI specs).

### macOS / Linux

```bash
git clone https://github.com/your-org/jxtest.git
cd jxtest
pip install pyyaml
make install            # symlinks bin/jxtest → ~/.local/bin/
export PATH="$HOME/.local/bin:$PATH"
```

If `make` isn't available (some minimal Linux), install manually:

```bash
pip install pyyaml
ln -sf "$(pwd)/bin/jxtest" ~/.local/bin/jxtest
export PATH="$HOME/.local/bin:$PATH"
```

### Windows

```powershell
git clone https://github.com/your-org/jxtest.git
cd jxtest
pip install pyyaml
```

Then pick one of these to invoke jxtest:

```powershell
# Option A: call python directly (no setup needed)
python bin\jxtest schema openapi.yaml

# Option B: register the CLI in PATH (PowerShell, current user only)
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
# Copy or symlink bin\jxtest into a directory on PATH (e.g. %LOCALAPPDATA%\Programs\Python\Python311\Scripts\)
copy bin\jxtest %LOCALAPPDATA%\Programs\Python\Python311\Scripts\jxtest.cmd
```

`make` isn't natively available on Windows — use the Python invocation above, or install [Chocolatey](https://chocolatey.org/install) / use WSL.

### Verify

```bash
jxtest --help       # shows all 17 commands
jxtest --version    # 1.0
```

## Quick start

```bash
# Plain REST API
jxtest schema examples/petstore/openapi.yaml          # parse spec
jxtest gen api-spec.json -o test-cases.json           # generate tests
jxtest run test-cases.json --base-url https://api.dev.com   # run
jxtest report test-results.json -o report.html        # HTML report

# Enveloped API (HTTP 200 + body.code — FastAPI / Express / Spring common pattern)
jxtest schema openapi.yaml --envelope 'code:0'        # parser prints hint if it looks enveloped
jxtest gen api-spec.json                              # gen adds business_ok assertions
jxtest run test-cases.json --base-url https://api.dev.com   # run with envelope awareness
```

That's it. No virtualenv, no Docker, no Node.js, no GUI.

## Highlights

- **Envelope-aware assertions** — for APIs that wrap everything in HTTP 200 + business codes. `business_ok` / `business_not_ok` catch wrapped 5xx that plain status checks report as passing. One `--envelope 'code:0'` flag enables it end-to-end. The runner auto-detects enveloped APIs and **refuses** to run with an actionable suggestion — silently inverting the pass/fail verdict is the most dangerous bug class.
- **Declarative login auth** — replace `--pre-script` token injection with `auth: {type: login, url, body, tokenPath}` in `test-cases.json`. Tokens are cached, refreshed automatically on 401, and survive long test suites.
- **AI contract workflow** — schema-less `requestBody` no longer means "guess and pray". `gen --contract-gap` surfaces what AI needs to fill in; `gen --contract contract.json` consumes AI-written field contracts; `run --contract` classifies failures into `data_issue` (contract gap) vs `real_defect`; `gen --contract-update` rolls classifications back into the contract.
- **$ref resolution** — OpenAPI `components.schemas` chains resolve recursively (depth 5, cycles preserved as refs). No more "100 endpoints with `{$ref: ...}` body schemas" silently producing empty POSTs.
- **Extract topological parallel** — when a case has `extract`, the runner builds a dependency graph and runs independent cases in parallel within their phase. No more "one case extracts → entire run goes sequential".
- **Dynamic variables** — `{{$timestamp}}`, `{{$uuid}}`, `{{$randomInt}}`, `{{$iso}}` are evaluated fresh per substitution. Override via scope for deterministic snapshots.
- **Isolated endpoints** — `meta.isolated: true` for logout / password-change / account-delete. Runner snapshots auth, gets a fresh token for the case only, then restores. No more "one logout poisons the whole run".
- **Coverage that catches lies** — endpoint coverage alone hides the "we hit it, never saw its 422" gap. Coverage now reports declared-but-unseen response codes and (under envelope) outcome distribution.
- **E2E business scenarios** — `jxtest scenario` chains a real user flow (login → list → create → get → delete) so the suite catches bugs no per-endpoint test can: token expiration, ownership, follow-up consistency.
- **Test-data factory + cleanup** — `jxtest factory` generates parallel-safe unique data per test, then auto-emits a `cleanup-cases.json` that DELETEs what the run created. CI leaves no rows behind.
- **Step-up capacity testing** — `jxtest load --ramp-step N` runs N stages of escalating VUs and prints a one-line-per-stage summary, so capacity planners can pick the row "before the bend".
- **Security findings ship with the fix** — every OWASP probe now reports a paste-ready remediation snippet (parameterized queries, whitelist DTO fields, …). `--rules` adds your own probes.
- **Trend reports** — `jxtest report --baseline prev.json` shows what regressed, what got fixed, and what's new — no manual diffing.
- **Custom assertions** — `--custom-asserts file.py` lets a Python function decide pass/fail for APIs whose quirks the built-in rules can't see.
- **Shell completion** — `eval "$(jxtest completion bash)"` gets you tab-completion across `schema | gen | run | load | security | …`.

## What's in the box

| Skill | What it does |
|-------|--------------|
| `schema` | Parse OpenAPI / Postman / HAR → `api-spec.json` |
| `gen` | Generate 5 categories of test cases from spec |
| `validate` | Validate `test-cases.json` structure |
| `env` | Manage env files + `{{var}}` templating |
| `mock` | Stateful mock server from spec |
| `run` | Functional tests (data-driven / context / OAuth2 / 22+ assertions / JUnit) |
| `load` | Load + SLA + baseline regression + step-up capacity + AI-friendly analysis |
| `security` | OWASP API Top 10 probes (IDOR / auth / SSRF / PII) + fix recipes + `--rules` |
| `diff` | Compare two specs → breaking changes + migration guide |
| `coverage` | Coverage gaps (endpoints / methods / categories / statuses) |
| `heal` | Self-heal failed assertions (heuristic) |
| `report` | Self-contained HTML report + trend delta vs baseline |
| `doc` | Markdown API docs |
| `scenario` | E2E business flow (login → action → verify) |
| `factory` | Per-test unique data + auto cleanup |
| `completion` | bash / zsh / fish shell completion |

Single CLI entry: `jxtest <command>`. JSON in, JSON out. Exit codes 0/1/2 for CI.

## Architecture

```
  OpenAPI / Postman / HAR
           │
           ▼
   ┌──────────────┐
   │   schema     │  parse → api-spec.json
   └──────┬───────┘
          ▼
   ┌──────────────┐
   │     gen      │  generate → test-cases.json
   └──────┬───────┘
          ▼
   ┌──────────────┐         ┌──────────────┐
   │     run      │         │     load     │  ← with AI analysis
   │  functional  │         │  stress/SLA  │
   └──────┬───────┘         └──────┬───────┘
          │                        │
          ▼                        ▼
   test-results.json       test-load-results.json
          │                        │
          └──────────┬─────────────┘
                     ▼
          ┌────────────────────┐
          │  coverage/heal/    │
          │   report/doc       │
          └────────────────────┘
```

## Why jxtest

| Postman | k6 | jxtest |
|---------|----|--------|
| Mouse-click UI | Pure load | **Single CLI for everything** |
| Manual test writing | Manual scripts | **Auto-generate 5 categories** |
| Static assertions | None | **16+ assertion types** |
| Read error trace | Numbers only | **AI-friendly analysis with recommendations** |
| No security scans | No security scans | **OWASP Top 10 built-in** |
| No contract diffing | No contract diffing | **`jxtest diff` blocks breaking changes** |
| No coverage | No coverage | **`jxtest coverage` shows gaps** |
| $$ Pro for team | OSS but limited | **100% open, stdlib-only Python** |

## Design principles

1. **Stdlib-only** — `urllib`, `http.server`, `json`, `threading`. No `requests`, no `aiohttp`, no Node.js. One dependency: `pyyaml`.
2. **AI-first** — every command takes JSON in, returns JSON out. AI agents can drive end-to-end without screen scraping.
3. **CI-native** — exit codes 0/1/2, JUnit XML, structured logs. Any CI (GitHub / GitLab / Jenkins / CircleCI / Azure) consumes the output with `cat test-results.xml`.
4. **Token-efficient** — one master `SKILL.md` (~600 lines), per-skill `SKILL.md` (~50-100 lines each). No bloat.

## For AI agents

**Read [`SKILL.md`](./SKILL.md)** — it has the 12-command map, decision matrix (which skill to invoke when), use case patterns, and a TL;DR.

If you need details for a specific skill, read `skills/<skill-name>/SKILL.md` (each is < 100 lines).

## Requirements

- **Python 3.10+** (preinstalled on macOS / most Linux)
- **`pyyaml`** (only for YAML OpenAPI specs)

No virtualenv, no Docker, no Node, no browsers.

## Layout

```
jxtest/
├── SKILL.md            ← AI's instruction manual (read this if you're an LLM)
├── README.md           ← this file (human-facing intro)
├── guideline.md        ← development roadmap
├── Makefile            ← convenience targets (Unix)
├── bin/jxtest          ← unified CLI (~90 lines, forwards to skills/)
├── examples/petstore/  ← sample OpenAPI spec
└── skills/
    ├── api-test-schema/   each skill has its own SKILL.md + scripts/
    ├── api-test-env/
    ├── api-test-mock/
    ├── api-test-gen/
    ├── api-test-run/
    ├── api-test-load/
    ├── api-test-security/
    ├── api-test-diff/
    ├── api-test-coverage/
    ├── api-test-heal/
    ├── api-test-report/
    └── api-test-doc/
```

## License

TBD