#!/usr/bin/env python3
"""A readable digest of the API contract, so drift from the SPA is visible in a diff.

The SPA is a separate git repository, cloned by `scripts/bootstrap.sh`, and it carries its
own hand-maintained copy of every response shape in `src/types/index.ts`. Nothing connected
the two: a field renamed here compiled fine there, passed both test suites, and broke a
screen in a browser.

This is the smallest thing that closes that. Not the full `openapi.json` — that is a large,
deeply nested document and a rename produces a diff nobody reads, which is the same as no
gate. One sorted line per operation and one per schema, each carrying a short hash of the
part that matters, so:

* a new or removed endpoint is one added or removed line,
* a changed parameter, status code or response model is one changed line,
* a changed field inside a schema is one changed line, in the schema section.

    python scripts/openapi_digest.py            # print it
    python scripts/openapi_digest.py --write     # update docs/openapi-digest.txt

`tests/unit/test_openapi_contract.py` fails when the committed digest and the live app
disagree. Updating it is one command; the point is that it has to be a deliberate one, in
the same commit as the change, where a reviewer sees both.

Note the constraint this works within: OpenAPI is not served in production (`main.py`
disables `/docs`, `/redoc` and `/openapi.json` there). That is fine — the digest is
generated from the app object, not fetched over HTTP, so it needs no running service.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = ROOT / "docs" / "openapi-digest.txt"

HEADER = """\
# API contract digest — generated, do not hand-edit.
#
# Regenerate with `make openapi` (or `python scripts/openapi_digest.py --write`) IN THE SAME
# COMMIT as the change that moved it, so the diff shows both. A line here changing without
# a matching change in the SPA's src/types/index.ts is the drift this file exists to catch.
#
# Columns: METHOD PATH | operationId | params | responses (status:schema) | body:<hash>
"""


def _short(value: object) -> str:
    blob = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:8]


def _ref_name(node: object) -> str:
    """The schema name behind a `$ref`, `items`, `anyOf` — or a hash when it is inline."""
    if not isinstance(node, dict):
        return "-"
    if not node:
        # An operation with no `response_model`. FastAPI documents it as `{}`, which is
        # honest — it really does promise nothing — and rendering that as `any` rather
        # than as a hash of the empty object says so in the digest.
        return "any"
    if "$ref" in node:
        return str(node["$ref"]).rsplit("/", 1)[-1]
    for key in ("items", "additionalProperties"):
        if key in node:
            return f"[{_ref_name(node[key])}]"
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            return "|".join(_ref_name(part) for part in node[key])
    if node.get("type"):
        return str(node["type"])
    return _short(node)


def build(spec: dict) -> str:
    lines: list[str] = [HEADER]

    lines.append("## operations")
    rows: list[str] = []
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            params = ",".join(
                sorted(
                    f"{p.get('name')}:{p.get('in')}:{'req' if p.get('required') else 'opt'}"
                    for p in operation.get("parameters", [])
                )
            ) or "-"
            body = operation.get("requestBody", {})
            body_schema = "-"
            if body:
                content = body.get("content", {}).get("application/json", {})
                body_schema = _ref_name(content.get("schema", {}))
            def _response_schema(entry: dict) -> str:
                body = entry.get("content", {}).get("application/json", {})
                return _ref_name(body.get("schema", {}))

            responses = ",".join(
                f"{status}:{_response_schema(entry)}"
                for status, entry in sorted(operation.get("responses", {}).items())
            ) or "-"
            rows.append(
                f"{method.upper():<7}{path:<52} | {operation.get('operationId', '-'):<48}"
                f" | {params:<40} | {responses} | body:{body_schema}"
            )
    lines.extend(sorted(rows))

    lines.append("")
    lines.append("## schemas")
    for name, schema in sorted(spec.get("components", {}).get("schemas", {}).items()):
        fields = ",".join(
            f"{field}:{_ref_name(definition)}"
            for field, definition in sorted(schema.get("properties", {}).items())
        ) or "-"
        required = ",".join(sorted(schema.get("required", []))) or "-"
        lines.append(f"{name:<40} required[{required}] {fields}")

    return "\n".join(lines) + "\n"


def current() -> str:
    sys.path.insert(0, str(ROOT))
    from main import app  # imported late: it reads settings and builds the whole app

    return build(app.openapi())


def main() -> int:
    text = current()
    if "--write" in sys.argv:
        DIGEST.parent.mkdir(parents=True, exist_ok=True)
        DIGEST.write_text(text)
        print(f"{DIGEST.relative_to(ROOT)} updated ({len(text.splitlines())} lines)")
        return 0
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
