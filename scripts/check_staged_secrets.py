#!/usr/bin/env python3
"""Refuse a commit that stages a secret file, or a line that looks like a live credential.

`.gitignore` already carries the rule and the reason:

    .env.* / !.env.example  — "`.env` and `*.env` between them miss `.env.bak.<timestamp>`,
    `.env.local`, `.env.prod` — files that hold the same SECRET_KEY and SMTP_PASSWORD as
    the original and were one `git add -A` from being published."

An ignore rule is not a gate, though: `git add -f`, a rename, or a path the pattern does not
quite cover all get through it silently. This is the gate. It runs on staged content only,
so it costs nothing and it fires before the thing it is preventing has happened.

The patterns are deliberately few and specific to the credential shapes this service holds:
Fernet keys (SECRET_KEY), JWTs, private keys and basic-auth URLs. A broad entropy scanner
would find base64 in a test fixture on day one and be turned off by day two. A derived
project that starts holding a new kind of credential adds its shape here, with a test.

    python scripts/check_staged_secrets.py          # staged changes (the pre-commit hook)
    python scripts/check_staged_secrets.py --all    # every tracked file (CI, or by hand)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import NamedTuple

# Filenames that must never be committed, whatever they contain.
#
# Two shapes, because `.gitignore` names two and for the same reason: `.env`, `.env.local`,
# `.env.bak.20260826` (leading dot) AND `prod.env`, `deploy/dev.env` (trailing suffix). The
# first version of this only had the leading-dot form, which left the exact case the module
# docstring says the gate exists to catch — "a rename" — walking straight through it.
FORBIDDEN_PATHS = re.compile(
    r"(^|/)(\.env($|\.)(?!example$)"      # .env, .env.local, .env.prod  (not .env.example)
    r"|[^/]+\.env$)"                       # prod.env, dev.env
)

# Content patterns. Each must match a *value*, not a variable name — `SECRET_KEY=` in
# .env.example is documentation and has to stay committable.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 44-char urlsafe-base64 ending in '=' — the exact shape of a Fernet key.
    ("Fernet key (SECRET_KEY)",
     re.compile(r"\bSECRET_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{42,44}=['\"]?")),
    ("JWT",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private key block",
     re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("basic-auth credentials in a URL",
     re.compile(r"://[^/\s:@]+:[^/\s:@]{8,}@")),
]

# A comment describing a URL shape is not a credential. `alembic.ini` carries a commented
# `sqlalchemy.url = mysql+pymysql://user:password@...` and `scripts/bootstrap.sh` explains
# in prose why passwords must be hex; both matched on day one, and a scanner whose first
# run is two false positives is a scanner someone deletes.
# `--` is deliberately NOT in this list. It was, for one revision, and it made
# `-----BEGIN RSA PRIVATE KEY-----` look like a SQL comment — the scanner's single most
# important pattern, silently disabled by its own noise filter.
COMMENT = re.compile(r"^\s*(#|;|//)")

# A value that says of itself that it is not a secret. `.github/workflows/ci.yml` needs a
# MySQL password for a service container that exists for ninety seconds and is reachable
# from nothing; there is no way to write one that a credential scanner should not flag, so
# the convention is to say so in the value. Explicit, greppable, and impossible to write by
# accident — which is more than can be said for an exemption by filename.
NOT_A_SECRET = re.compile(r"(?i)not[-_]a[-_]secret")

# Placeholder values, in any case. These appear in documentation on purpose.
PLACEHOLDER = re.compile(
    r"(?i)(:|=)\s*['\"]?(password|passwd|pass|secret|changeme|your[-_]?\w+|xxx+|\*{3,}"
    r"|<[^>]+>|\$\{?\w+\}?)['\"]?([@/\s]|$)"
)

# The example file states the shapes on purpose, and the test suite builds deterministic
# keys from `b"0" * 32`. Neither is a leak; both would match forever.
ALLOWED = (r"\.env\.example$", r"^tests/", r"^scripts/check_staged_secrets\.py$")
ALLOWED_RE = re.compile("|".join(ALLOWED))


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def all_files() -> tuple[list[str], str]:
    """Every tracked file, or — when this tree is not a checkout of its own — every file git
    would track.

    A base project is as often unpacked from an archive, or copied into a larger repository,
    as cloned. Outside any repository `git ls-files` fails; inside an enclosing one that does
    not track this tree (a monorepo it was copied into, a workspace repo one level up) it
    *succeeds and lists nothing*, and "0 tracked files clean" is a pass that scanned nothing.
    Both mean there is no index to ask, so both walk the tree instead, honouring the
    top-level `.gitignore` (not nested ones — so the walk scans more than git would, never
    less).
    """
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return walked_files(), "working-tree"
    tracked = [line for line in out.stdout.splitlines() if line]
    if not tracked:
        return walked_files(), "working-tree"
    return tracked, "tracked"


# A NamedTuple rather than a dataclass: the test suite loads this file by path without
# registering it in `sys.modules`, and a dataclass resolves its postponed annotations there.
class IgnoreRule(NamedTuple):
    negated: bool
    #: Matched against the whole path, not just the last component: the pattern had a
    #: leading or a middle `/`, which is how git anchors a pattern to the .gitignore's own
    #: directory.
    anchored: bool
    #: A trailing `/`: the pattern names directories only, never a file of the same name.
    dir_only: bool
    regex: re.Pattern[str]


def _glob_regex(glob: str) -> re.Pattern[str]:
    """A gitignore glob as a regex, with git's path semantics rather than fnmatch's.

    `fnmatch`'s `*` crosses `/`, so `docs/*.md` would swallow `docs/sub/x.md`, which git
    keeps. Here `*`, `?` and `[...]` stay inside one path component, and only a `**` that
    is a whole component (`**/x`, `x/**`, `x/**/y`) spans several.
    """
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            j = i
            while j < n and glob[j] == "*":
                j += 1
            whole_component = (i == 0 or glob[i - 1] == "/") and (j == n or glob[j] == "/")
            if j - i >= 2 and whole_component:
                if j < n:  # `**/`: zero or more leading directories
                    out.append("(?:.*/)?")
                    j += 1
                else:      # a trailing `**`: everything inside
                    out.append(".*")
            else:
                out.append("[^/]*")
            i = j
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            k = i + 1
            if k < n and glob[k] in "!^":
                k += 1
            if k < n and glob[k] == "]":  # a `]` first in the class is a literal
                k += 1
            close = glob.find("]", k)
            if close == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1:close]
                negated = body[:1] in ("!", "^")
                body = body[1:] if negated else body
                body = body.replace("\\", "\\\\").replace("^", "\\^")
                out.append(f"[^/{body}]" if negated else f"[{body}]")
                i = close + 1
                continue
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(glob[i + 1]))
            i += 2
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))


def parse_ignore_rules(lines: list[str]) -> list[IgnoreRule]:
    rules: list[IgnoreRule] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        # The `!` is syntax; so is the `\` in `\#` and `\!`, which spell those literally.
        if negated or line.startswith("\\"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        # Decided before the leading slash is dropped: `/config` is anchored to the root
        # and must not match `core/config`.
        anchored = "/" in line
        rules.append(IgnoreRule(negated, anchored, dir_only, _glob_regex(line.lstrip("/"))))
    return rules


def _gitignore_rules() -> list[IgnoreRule]:
    try:
        with open(".gitignore", encoding="utf-8") as handle:
            return parse_ignore_rules(handle.read().splitlines())
    except OSError:
        return []


def _excluded(path: str, is_dir: bool, rules: list[IgnoreRule]) -> bool:
    # Last matching rule wins, as in git. An unanchored pattern matches the last component
    # at any depth; an anchored one matches the whole path from the root.
    name = path.rsplit("/", 1)[-1]
    excluded = False
    for rule in rules:
        if rule.dir_only and not is_dir:
            continue
        if rule.regex.fullmatch(path if rule.anchored else name):
            excluded = not rule.negated
    return excluded


def ignored(path: str, rules: list[IgnoreRule], is_dir: bool = False) -> bool:
    """Whether git would ignore `path` (relative, `/`-separated) under `rules`.

    Every ancestor directory is checked first: git never looks inside an excluded directory,
    so nothing under one can be re-included by a later `!` rule either.
    """
    parts = path.split("/")
    for depth in range(1, len(parts)):
        if _excluded("/".join(parts[:depth]), True, rules):
            return True
    return _excluded(path, is_dir, rules)


def walked_files() -> list[str]:
    rules = [*_gitignore_rules(), *parse_ignore_rules([".git"])]
    found: list[str] = []
    for directory, subdirectories, filenames in os.walk("."):
        relative = os.path.relpath(directory, ".").replace(os.sep, "/")
        prefix = "" if relative == "." else f"{relative}/"
        subdirectories[:] = sorted(
            name for name in subdirectories if not ignored(prefix + name, rules, is_dir=True)
        )
        found.extend(
            prefix + name for name in sorted(filenames) if not ignored(prefix + name, rules)
        )
    return found


def content(path: str, staged: bool) -> str:
    if staged:
        result = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return ""


def main() -> int:
    staged = "--all" not in sys.argv
    paths, scope = (staged_files(), "staged") if staged else all_files()
    problems: list[str] = []

    for path in paths:
        if FORBIDDEN_PATHS.search(path):
            problems.append(f"  {path}: an environment file. Ship .env.example and nothing else.")
            continue
        if ALLOWED_RE.search(path):
            continue
        text = content(path, staged)
        if not text or "\0" in text[:1024]:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if COMMENT.match(line) or PLACEHOLDER.search(line) or NOT_A_SECRET.search(line):
                continue
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    problems.append(f"  {path}:{line_number}: {label}")
                    break

    if problems:
        print("Refusing to commit — staged content looks like a credential:\n")
        print("\n".join(problems))
        print(
            "\nIf this is a false positive, narrow the pattern in "
            "scripts/check_staged_secrets.py\nwith a comment saying why. Do not pass "
            "--no-verify: the hook is the only thing between\na developer's real "
            "SECRET_KEY and a public remote."
        )
        return 1

    print(f"Secret scan: OK — {len(paths)} {scope} files clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
