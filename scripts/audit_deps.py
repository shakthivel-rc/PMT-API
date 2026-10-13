"""The dependency audit, as a gate rather than a report.

`make audit` used to be `pip_audit ... || true`, with a note in the Makefile saying it would
start gating "the day the current backlog is zero". Nobody ran it, so nobody found out the
backlog was not zero: on 2026-08-27 the first real run reported **22 advisories across 5
packages**, seven of them in PyJWT — the library that authenticates every request in this
service. An audit that cannot fail is an audit nobody runs.

This script is what makes it fail. It exists rather than a bare `pip-audit` invocation for
one reason: **waivers need names.**

A blanket `|| true` waives everything, forever, and is invisible in a diff. A waiver here is
a line in `docs/security-exceptions.md` with an advisory id, a reason and a date, which a
reviewer sees when it is added and which this script re-reads on every run. When a waiver
expires, the build goes red and somebody has to look at it again — which is the entire point
of writing an expiry down.

Two audits, because this product is two repositories:

* `pip-audit` over `requirements.txt` — the RUNTIME set only. An advisory in a test-only
  package is worth knowing about and is not worth failing a nightly for; `requirements-dev.txt`
  is audited separately and reported, not gated (see `--dev`).
* `npm audit --omit=dev` in the SPA, when the SPA is checked out beside this repository. It
  was clean at the last measurement and the only way that stays true is if something checks.
  When the sibling directory is absent — which is the normal case in CI, where each
  repository is checked out on its own — the npm half is skipped and *says* it skipped.
  A silent skip is how a gate stops being a gate.

Usage:
    python scripts/audit_deps.py                # both halves, gating
    python scripts/audit_deps.py --python-only  # skip the npm half entirely
    python scripts/audit_deps.py --dev          # also report (never gate) the dev set
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXCEPTIONS = ROOT / "docs" / "security-exceptions.md"
APP_DIR = ROOT.parent / "pmt-app"

#: A waiver row in `docs/security-exceptions.md`. The table is the source of truth and this
#: regex is the only thing that reads it, so the format is pinned here and in that file's
#: own header — deliberately in both places, because the person adding a row reads the file
#: and the person debugging a failed gate reads this.
#:
#:     | PYSEC-2026-1234 | package | 2026-12-31 | why it cannot be upgraded |
_WAIVER = re.compile(
    r"^\|\s*(?P<id>(?:PYSEC|GHSA|CVE)-[A-Za-z0-9-]+)\s*\|"
    r"\s*(?P<package>[^|]+?)\s*\|"
    r"\s*(?P<expires>\d{4}-\d{2}-\d{2})\s*\|"
    r"\s*(?P<reason>[^|]+?)\s*\|",
    re.MULTILINE,
)


def load_waivers() -> tuple[dict[str, dict[str, str]], list[str]]:
    """Live waivers, and the ids of ones that have expired.

    An expired waiver is NOT treated as absent-and-therefore-fine, and it is not treated as
    still-valid either: it is returned separately so the caller can fail the build and say
    *why* it went red today when it was green yesterday. "Your waiver ran out" is an
    actionable message; "22 advisories" on a build that passed last night is not.
    """
    if not EXCEPTIONS.exists():
        return {}, []

    today = date.today().isoformat()
    live: dict[str, dict[str, str]] = {}
    expired: list[str] = []
    for match in _WAIVER.finditer(EXCEPTIONS.read_text(encoding="utf-8")):
        row = match.groupdict()
        # String comparison is correct for ISO-8601 dates and avoids a parse that could
        # raise on a typo in a documentation file — a malformed date reads as expired,
        # which fails safe.
        if row["expires"] < today:
            expired.append(row["id"])
        else:
            live[row["id"]] = row
    return live, expired


def audit_python(requirements: pathlib.Path) -> list[dict]:
    """Every advisory pip-audit reports against one requirements file, as flat dicts."""
    result = subprocess.run(
        [sys.executable, "-m", "pip_audit", "-r", str(requirements),
         "--format", "json", "--progress-spinner", "off"],
        capture_output=True, text=True, cwd=ROOT,
    )
    # pip-audit exits non-zero *because* it found something, which is not an error here —
    # finding things is its job. Only an unparseable stdout means it actually failed.
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        print(f"pip-audit did not produce JSON (exit {result.returncode}):", file=sys.stderr)
        print(result.stdout[-2000:], file=sys.stderr)
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(2) from None

    findings = []
    for dep in payload.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            findings.append({
                "id": vuln.get("id", "?"),
                "package": dep.get("name", "?"),
                "version": dep.get("version", "?"),
                "fix": ", ".join(vuln.get("fix_versions") or []) or "none published",
            })
    return findings


def audit_npm() -> tuple[list[dict], str | None]:
    """SPA advisories, or a reason the check did not run.

    The reason is returned rather than printed so the caller decides whether "not run" is
    acceptable. It is acceptable here — the SPA is a separate repository and is not checked
    out beside this one in CI — but it must be *stated*, not inferred from silence.
    """
    if not APP_DIR.is_dir():
        return [], f"{APP_DIR} is not checked out"
    if not (APP_DIR / "node_modules").is_dir():
        return [], "node_modules is absent (run `npm ci` in the SPA first)"

    result = subprocess.run(
        ["npm", "audit", "--omit=dev", "--json"],
        capture_output=True, text=True, cwd=APP_DIR,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return [], f"npm audit did not produce JSON (exit {result.returncode})"

    findings = []
    for name, entry in (payload.get("vulnerabilities") or {}).items():
        for via in entry.get("via", []):
            if isinstance(via, dict):
                findings.append({
                    "id": via.get("url", via.get("source", "?")),
                    "package": name,
                    "version": entry.get("range", "?"),
                    "fix": "npm audit fix",
                })
    return findings, None


def report(title: str, findings: list[dict], waivers: dict[str, dict[str, str]]) -> int:
    """Print one audit's outcome. Returns the number of findings that are NOT waived."""
    waived = [f for f in findings if f["id"] in waivers]
    live = [f for f in findings if f["id"] not in waivers]

    if not findings:
        print(f"  {title}: clean")
        return 0

    for finding in live:
        print(f"  {finding['package']:<20} {finding['version']:<12} {finding['id']:<22}"
              f" fix: {finding['fix']}")
    for finding in waived:
        reason = waivers[finding['id']]['reason']
        expires = waivers[finding['id']]['expires']
        print(f"  WAIVED until {expires}  {finding['package']:<16} {finding['id']:<22} {reason}")

    print(f"  {title}: {len(live)} unwaived, {len(waived)} waived")
    return len(live)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-only", action="store_true",
                        help="skip the SPA half (CI checks out one repository at a time)")
    parser.add_argument("--dev", action="store_true",
                        help="also report requirements-dev.txt — reported, never gated")
    args = parser.parse_args()

    waivers, expired = load_waivers()
    if expired:
        print("Waivers in docs/security-exceptions.md have EXPIRED and no longer apply:")
        for advisory in expired:
            print(f"  {advisory}")
        print("Upgrade the package, or extend the waiver with a new reason and date.\n")

    print("Dependency advisories")
    unwaived = report("runtime (requirements.txt)",
                      audit_python(ROOT / "requirements.txt"), waivers)

    if args.dev:
        # Reported, never gated: see the module docstring. The count is printed and the
        # return value is deliberately discarded.
        report("development (requirements-dev.txt, reported only)",
               audit_python(ROOT / "requirements-dev.txt"), waivers)

    if not args.python_only:
        npm_findings, skipped = audit_npm()
        if skipped:
            print(f"  SPA (npm audit): skipped — {skipped}")
        else:
            unwaived += report("SPA (npm audit --omit=dev)", npm_findings, waivers)

    if expired:
        print("\nFAILED: an advisory waiver has expired.")
        return 1
    if unwaived:
        print(f"\nFAILED: {unwaived} advisory/advisories with no waiver.")
        print("Upgrade the package, or add a row to docs/security-exceptions.md with an")
        print("advisory id, the reason it cannot move, and the date the waiver expires.")
        return 1

    print("\nOK: no unwaived advisories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
