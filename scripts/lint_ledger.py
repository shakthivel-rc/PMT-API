#!/usr/bin/env python3
"""Ruff's remaining findings, counted per file and per rule, as a ratchet.

The SPA half of this product has had a lint gate for months and no disable comments,
because ESLint 10 can record its existing violations in `eslint-suppressions.json`
(`--suppress-all`) and fail only on *new* ones. Ruff has no equivalent, so this is it:
same idea, same file shape, same rule.

    ruff-suppressions.json
    {
      "controllers/user_controller.py": { "E501": { "count": 12 }, "UP045": { "count": 3 } }
    }

Why a ledger rather than a `# noqa` on every finding: a `# noqa` is invisible from outside
the file it sits in, so nobody can tell whether the backlog is growing or shrinking, and
nobody can pick the next thing to fix. A ledger is a work list, sorted by whoever wants a
small one.

    python scripts/lint_ledger.py            # check — the gate. Non-zero on any increase.
    python scripts/lint_ledger.py --update    # re-record, AFTER fixing something.

The rule, matching every other ratchet in this repository: **a count may fall or stay
flat; a build that raises one fails.** `--update` is how you record a fix, never how you
make a failure go away — if the diff it produces contains a number going *up*, that is the
gate working and the commit is wrong.

A file with no entry is held at zero, which is what makes a brand-new file clean by
default rather than grandfathered by accident.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ruff-suppressions.json"


def measure() -> dict[str, dict[str, int]]:
    """Current ruff findings as {path: {rule: count}}.

    `--output-format json` rather than parsing the human table: the table elides the rule
    code for some formats and has no stable column contract.
    """
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # ruff exits 1 when it finds anything, which is the normal case here. Only a crash --
    # a bad config, an unreadable file — is an error, and that shows up as empty stdout.
    if result.returncode not in (0, 1) or not result.stdout.strip():
        sys.stderr.write(result.stderr or "ruff produced no output\n")
        raise SystemExit(2)

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for finding in json.loads(result.stdout):
        path = Path(finding["filename"]).resolve().relative_to(ROOT).as_posix()
        counts[path][finding["code"]] += 1
    return {path: dict(sorted(rules.items())) for path, rules in sorted(counts.items())}


def load() -> dict[str, dict[str, int]]:
    if not LEDGER.exists():
        return {}
    raw = json.loads(LEDGER.read_text())
    return {
        path: {rule: entry["count"] for rule, entry in rules.items()}
        for path, rules in raw.items()
        if not path.startswith("_")
    }


def save(counts: dict[str, dict[str, int]]) -> None:
    document = {
        "_comment": [
            "Ruff's remaining findings, per file and per rule. A ratchet: counts may fall,",
            "never rise. Enforced by scripts/lint_ledger.py, which `make lint` and CI run.",
            "",
            "Regenerate with `python scripts/lint_ledger.py --update` AFTER fixing",
            "something, and read the diff: a number going up means the commit is wrong, not",
            "that the ledger is stale.",
            "",
            "The shape mirrors the SPA's eslint-suppressions.json on purpose, so the two",
            "halves of this product answer 'how much lint debt is left' the same way.",
        ],
    }
    document.update(
        {path: {rule: {"count": n} for rule, n in rules.items()} for path, rules in counts.items()}
    )
    LEDGER.write_text(json.dumps(document, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="re-record the current counts")
    args = parser.parse_args()

    current = measure()
    total = sum(n for rules in current.values() for n in rules.values())

    if args.update:
        save(current)
        print(f"ruff-suppressions.json updated: {total} findings across {len(current)} files")
        return 0

    allowed = load()
    regressions: list[str] = []
    for path, rules in current.items():
        for rule, count in rules.items():
            budget = allowed.get(path, {}).get(rule, 0)
            if count > budget:
                regressions.append(f"  {path}: {rule} {budget} -> {count}")

    # Entries that no longer fire. Not a failure — that is the ratchet working — but worth
    # printing, because a stale ledger quietly re-grants budget the code no longer needs.
    stale = [
        f"  {path}: {rule} ({count} recorded, 0 found)"
        for path, rules in allowed.items()
        for rule, count in rules.items()
        if current.get(path, {}).get(rule, 0) == 0
    ]

    if regressions:
        print("Lint ledger: NEW findings that are not in ruff-suppressions.json\n")
        print("\n".join(sorted(regressions)))
        print(
            "\nFix them, or — if the finding is wrong — narrow the rule in pyproject.toml "
            "with a comment saying why.\nDo NOT run --update to make this pass."
        )
        return 1

    print(f"Lint ledger: OK — {total} recorded findings, none new.")
    if stale:
        print(f"\n{len(stale)} ledger entries no longer fire; run --update to bank them:")
        print("\n".join(sorted(stale)[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
