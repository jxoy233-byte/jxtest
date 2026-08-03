"""Contract schema + AI-facing helpers for schema-less endpoints.

When an endpoint declares a requestBody but the spec gives no schema and no
example, gen.py can't build a happy-path body — it falls back to a single
"empty body must be rejected" case. That's safe but useless: AI has no way to
fill in a real body.

`contract.json` is the AI's escape hatch: a per-endpoint field contract keyed
by endpointId. gen consumes it to synthesise a body, run classifies failures
into `real_defect` vs `data_issue`, and `--contract-update` rolls the
classifications back into the contract.

Format (version 1.0):
    {
      "version": "1.0",
      "contracts": {
        "POST_/api/v1/users": {
          "fields": {
            "username": {"type": "string", "required": true, "example": "alice", "unique": true},
            "email":    {"type": "string", "format": "email", "required": true, "example": "a@b.com"}
          },
          "preconditions": ["auth required"],
          "notes": "username must be unique within org"
        }
      }
    }
"""
import json
import re
import sys
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "1.0"
SUPPORTED_VERSIONS = {"1.0"}


# Field-name heuristics for `--contract-gap`. When the spec gives nothing else
# to anchor on, this is the only signal we have. Be conservative: emit
# placeholders, not actual values, so AI has to fill them in.
_NAME_HINTS = {
    "id": "integer",
    "uuid": "string",
    "user_id": "integer",
    "userid": "integer",
    "username": "string",
    "name": "string",
    "email": "string",
    "phone": "string",
    "status": "string",
    "type": "string",
    "category": "string",
    "is_active": "boolean",
    "active": "boolean",
    "enabled": "boolean",
    "count": "integer",
    "amount": "number",
    "price": "number",
    "balance": "number",
    "tags": "array",
}


def load_contract(path: str | None) -> dict:
    """Read contract.json. Returns empty contract on missing file."""
    if not path:
        return {"version": CONTRACT_VERSION, "contracts": {}}
    p = Path(path)
    if not p.exists():
        return {"version": CONTRACT_VERSION, "contracts": {}}
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("version") not in SUPPORTED_VERSIONS:
        print(f"[contract] unknown version {doc.get('version')!r}, treating as v1.0", file=sys.stderr)
    return doc or {"version": CONTRACT_VERSION, "contracts": {}}


def build_body_from_contract(fields: dict) -> dict:
    """Synthesize a body object from a contract.fields map.

    Required fields get their `example` (or `default`, or a type-appropriate
    placeholder). Optional fields are dropped — leaving them out keeps cases
    close to "what the real client would send" rather than padding with noise.
    """
    body: dict[str, Any] = {}
    for name, spec in (fields or {}).items():
        if not isinstance(spec, dict):
            continue
        if not spec.get("required", False):
            continue
        body[name] = spec.get("example", spec.get("default", _placeholder_for(spec)))
    return body


def _placeholder_for(spec: dict) -> Any:
    """Last-resort value when a required field has no example/default."""
    t = spec.get("type")
    if t == "string":
        if spec.get("format") == "email":
            return "test@example.com"
        if spec.get("format") == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        return "string"
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return {}
    return None


def gen_contract_gap(spec: dict) -> dict:
    """Produce a structured JSON report of schema-less endpoints.

    The caller (gen.py) saves this to `--contract-gap-output`. AI reads it,
    fills in field contracts, writes them back, and re-runs.
    """
    gaps: list[dict] = []
    for ep in spec.get("endpoints", []):
        body = ep.get("requestBody")
        if not body:
            continue
        # Skip endpoints with usable body schemas — those don't need a contract
        if body.get("schema") or body.get("example"):
            continue

        # Suggest fields by parsing operationId / summary / tags
        suggested = _suggest_fields(ep)
        gaps.append({
            "endpointId": ep["id"],
            "method": ep["method"],
            "path": ep["path"],
            "operationId": ep.get("operationId"),
            "reason": "requestBody has no schema and no example",
            "suggestedFields": suggested,
        })

    return {
        "version": CONTRACT_VERSION,
        "specTitle": spec.get("title"),
        "summary": {"gaps": len(gaps)},
        "gaps": gaps,
    }


def _suggest_fields(ep: dict) -> list[dict]:
    """Heuristic field hints from operationId/summary/tags.

    The goal is to give AI *something* to anchor on — a list of plausible
    field names with type guesses. Heuristics can be wrong; AI is expected
    to refine.
    """
    hints: list[dict] = []
    seen: set[str] = set()

    # Pull words out of operationId like `createUser` → [create, user]
    op_id = ep.get("operationId") or ""
    for match in re.finditer(r"[A-Z][a-z]+|[a-z]+", op_id):
        word = match.group(0).lower()
        if word in ("create", "update", "delete", "add", "get", "list", "fetch", "remove", "set"):
            continue
        if word in seen:
            continue
        seen.add(word)
        hints.append({
            "name": word,
            "hint": f"{_NAME_HINTS.get(word, 'string')} (heuristic from operationId)",
        })

    # Pull camelCase from summary / description if present
    summary = ep.get("summary") or ep.get("description") or ""
    for match in re.finditer(r"\b([a-z][a-zA-Z]*(?:Id|Name|Code|Type))\b", summary):
        word = match.group(1)
        if word.lower() in seen:
            continue
        seen.add(word.lower())
        hints.append({
            "name": word,
            "hint": f"{_NAME_HINTS.get(word.lower(), 'string')} (heuristic from summary)",
        })

    return hints[:6]  # cap to keep the file readable


def classify_failures(results: list[dict], contract: dict, envelope: dict | None) -> list[dict]:
    """For each failed case, decide: data_issue (contract gap) or real_defect.

    `data_issue` is "the contract was wrong" (missing field, type mismatch,
    uniqueness violation). `real_defect` is everything else (gen produced a
    reasonable body but the API still failed). The classification drives what
    `--contract-update` will roll back into contract.json.
    """
    feedback: list[dict] = []
    for r in results:
        if r.get("status") != "failed":
            continue
        cat = r.get("category") or "positive"
        if cat not in ("positive", "negative", "idempotency"):
            continue

        ep_id = r.get("endpointId", "")
        ep_contract = (contract.get("contracts") or {}).get(ep_id)
        issue = _classify_one(r, ep_contract, envelope)
        if issue is None:
            # No signal either way → treat as real defect (don't pollute contract)
            feedback.append({
                "endpointId": ep_id,
                "caseId": r.get("caseId"),
                "classification": "real_defect",
                "issue": {"kind": "unclassified",
                          "note": "no contract signal — investigate manually"},
            })
        else:
            feedback.append({
                "endpointId": ep_id,
                "caseId": r.get("caseId"),
                "classification": "data_issue" if issue["kind"] != "real_defect" else "real_defect",
                "issue": issue,
            })
    return feedback


def _classify_one(result: dict, ep_contract: dict | None, envelope: dict | None) -> dict | None:
    """Classify a single failure. Returns None to defer to unclassified."""
    outcome = result.get("outcome", "")
    code = result.get("businessCode") or result.get("httpStatus")
    fields = (ep_contract or {}).get("fields", {}) or {}

    if outcome == "server_error":
        # 5xx / envelope 5xx → the handler crashed on this input. Likely a real
        # defect (missing validation), not a contract gap.
        return {"kind": "real_defect",
                "note": f"server_error (code={code}) — handler crashed, not a contract gap"}

    if outcome == "rejected":
        # 4xx-shaped rejection → was a request field wrong?
        # If the failing case was supposed to be valid (positive/idempotency)
        # and we have a contract for this endpoint, check what fields are
        # missing from the body that was sent — both ones the contract marked
        # required AND ones it marked optional (server might require more than
        # the contract claimed).
        cat = result.get("category") or "positive"
        if cat in ("positive", "idempotency") and ep_contract:
            sent = result.get("request", {}) or {}
            issue = _detect_missing_required(sent.get("body"), fields)
            if issue:
                return {"kind": "missing_required", "field": issue}
            # Try to glean the missing field name from the response body
            issue = _parse_error_for_field(result, fields)
            if issue:
                return {"kind": "missing_required", "field": issue}
            # Type mismatch: harder without a real body to inspect. We can
            # at least flag cases where the contract says a field has
            # format=email but the failure class looks like validation.
            issue = _detect_format_or_type(result, fields)
            if issue:
                return issue
        # Uniqueness: 409 / envelope code in conflict range
        # Only count positive cases — idempotency tests reusing the same body
        # are *expected* to fail when unique fields collide, that's the test
        # doing its job, not a contract gap.
        if cat == "positive":
            try:
                if isinstance(code, int) and 400 <= code < 500 and code != 401 and code != 403:
                    unique_fields = [n for n, s in fields.items() if s.get("unique")]
                    if unique_fields:
                        return {"kind": "uniqueness_violation", "field": unique_fields[0]}
            except TypeError:
                pass
        return {"kind": "real_defect", "note": f"rejected (code={code}) — no contract gap detected"}

    return None


def _detect_missing_required(sent_body: Any, fields: dict) -> str | None:
    """If the sent body is missing a `required: true` field, name it."""
    if not isinstance(sent_body, dict):
        return None
    for name, spec in fields.items():
        if not isinstance(spec, dict) or not spec.get("required", False):
            continue
        if name not in sent_body:
            return name
    return None


def _parse_error_for_field(result: dict, fields: dict) -> str | None:
    """Try to extract a missing-field name from the server's error message.

    Looks for substrings like "username required", "missing email", "field X
    is required". Only matches field names that are in the contract (so we
    don't pull arbitrary server-side names into the contract).
    """
    body = (result.get("response") or {}).get("body") or ""
    if not body or not fields:
        return None
    body_low = body.lower()
    for name in fields:
        # Match "<name> required", "missing <name>", "field <name>", "<name> is required"
        patterns = [
            f"{name.lower()} required",
            f"missing {name.lower()}",
            f"field {name.lower()}",
            f"{name.lower()} is required",
        ]
        if any(p in body_low for p in patterns):
            return name
    return None


def _detect_format_or_type(result: dict, fields: dict) -> dict | None:
    """Heuristic: did the failure look like a format/type complaint?

    We can't perfectly parse the server's error message, so this stays
    conservative: only flags when we have at least one field with a known
    format and the failure happened on a positive case.
    """
    # We have access to assertions but not the raw response body in a uniform
    # shape. Skip this for now — a future iteration can mine `describe()` and
    # `error_structure` results to refine.
    return None


def apply_contract_feedback(contract: dict, feedback: list[dict]) -> dict:
    """Roll data_issue feedback into contract.json and return the updated doc.

    Rules:
      - missing_required → add field to contract if absent, mark required: true
      - uniqueness_violation → mark existing field as unique: true
      - everything else (real_defect, format issues) → no-op
    """
    contracts = contract.setdefault("contracts", {})
    applied: list[dict] = []
    skipped: list[dict] = []

    for fb in feedback:
        if fb.get("classification") != "data_issue":
            skipped.append({"caseId": fb.get("caseId"), "reason": "not a data_issue"})
            continue
        ep_id = fb.get("endpointId")
        if not ep_id:
            continue
        issue = fb.get("issue") or {}
        kind = issue.get("kind")
        ep_contract = contracts.setdefault(ep_id, {"fields": {}})
        ep_fields = ep_contract.setdefault("fields", {})

        if kind == "missing_required":
            field = issue.get("field")
            if not field:
                continue
            if field not in ep_fields:
                ep_fields[field] = {"type": "string", "required": True, "example": ""}
                applied.append({"endpointId": ep_id, "field": field, "change": "added as required"})
            elif not ep_fields[field].get("required"):
                ep_fields[field]["required"] = True
                applied.append({"endpointId": ep_id, "field": field, "change": "marked required"})
            else:
                skipped.append({"caseId": fb.get("caseId"),
                                "reason": f"{field} already marked required"})
        elif kind == "uniqueness_violation":
            field = issue.get("field")
            if not field:
                continue
            entry = ep_fields.setdefault(field, {"type": "string", "required": True})
            if entry.get("unique"):
                skipped.append({"caseId": fb.get("caseId"),
                                "reason": f"{field} already marked unique"})
            else:
                entry["unique"] = True
                applied.append({"endpointId": ep_id, "field": field, "change": "marked unique"})
        else:
            skipped.append({"caseId": fb.get("caseId"), "reason": f"kind={kind} not auto-fixable"})

    contract.setdefault("_applied", []).extend(applied)
    contract["_lastUpdated"] = _iso_now()
    return contract


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")