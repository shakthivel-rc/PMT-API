"""Finding issue keys (`PAY-142`) in the text developers write: branches, titles, commits.

This is how a pull request finds its ticket without anyone clicking "link". The convention
is Jira's, and deliberately so — every developer tool already offers to put the key in the
branch name.

The pattern is conservative in both directions. It requires a boundary on both sides of the
key, so `XPAY-1` does not match `PAY-1` and `PAY-12a` is not `PAY-12`; and it matches the key
case-sensitively, because lower-case `pay-1` is as likely to be a slug as a reference, and a
false link is worse than a missed one — a missed link costs one click, a false one can move
the wrong ticket to Done. Branch names are the exception: `feature/pay-142-fix-refunds` is
how most tooling writes a branch, so `extract_issue_keys(..., case_insensitive=True)` is used
for them alone.
"""
from __future__ import annotations

import re

#: A space key: a letter then 1-9 letters or digits. The same shape the space-creation
#: schema accepts, so anything a space can be called can be referenced.
SPACE_KEY_PATTERN = r"[A-Z][A-Z0-9]{1,9}"

_KEY_SOURCE = rf"(?<![A-Za-z0-9])({SPACE_KEY_PATTERN})-([1-9][0-9]{{0,8}})(?![A-Za-z0-9])"
_KEY = re.compile(_KEY_SOURCE)
_KEY_ANY_CASE = re.compile(_KEY_SOURCE, re.IGNORECASE)

#: A pull request that names more issues than this is a merge of a release branch, not work
#: on those issues; linking all of them would bury every ticket under the same PR.
MAX_KEYS_PER_TEXT = 20


def extract_issue_keys(
    *texts: str | None, case_insensitive: bool = False
) -> list[tuple[str, int]]:
    """Every distinct `(SPACE_KEY, number)` referenced, in first-seen order.

    Space keys come back upper-cased, so a branch's `pay-142` and a title's `PAY-142` are
    one reference.
    """
    pattern = _KEY_ANY_CASE if case_insensitive else _KEY
    seen: dict[tuple[str, int], None] = {}
    for text in texts:
        if not text:
            continue
        for match in pattern.finditer(text):
            seen.setdefault((match.group(1).upper(), int(match.group(2))), None)
            if len(seen) >= MAX_KEYS_PER_TEXT:
                return list(seen)
    return list(seen)


def format_issue_key(space_key: str, number: int) -> str:
    return f"{space_key}-{number}"
