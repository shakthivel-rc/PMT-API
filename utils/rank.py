"""Order-preserving string keys for board columns (fractional indexing).

A card's position in a column is a string, and the column is `ORDER BY rank`. Moving a card
computes one new string that sorts between its new neighbours and writes it to **one row** —
the alternative, an integer position, rewrites every card below the insertion point, which
is the write amplification that makes drag-and-drop on a long column slow and makes two
people dragging at once overwrite each other's order.

The keys are base-36 digit strings read as fractions in (0, 1): `"i"` is 18/36, `"i8"` is a
little more. Two rules make that sound:

* **lower-case digits and letters only.** `0-9a-z` sorts the same way under every collation
  this schema will meet — MySQL's case-insensitive `utf8mb4_*_ci`, PostgreSQL's `en_US` and
  `C` — so the database's `ORDER BY` and Python's `<` always agree. A mixed-case alphabet
  would sort `"a" == "A"` in MySQL and silently reorder cards.
* **no trailing `0`.** `"i"` and `"i0"` are the same fraction as different strings, and no
  string sorts strictly between them, so a key ending in `0` could leave no room to insert.

Keys grow when many cards are dropped into the same gap. `needs_rebalance` says when a key
has grown past `REBALANCE_LENGTH`, and `spread` then re-issues evenly spaced short keys for
the whole column — O(column) writes, rare, and done by the same request that noticed.

The midpoint algorithm is the one in David Greenspan's "Implementing Fractional Indexing"
(observablehq.com/@dgreensp/implementing-fractional-indexing), restricted to the fraction
part because a column never needs keys outside (0, 1).
"""
from __future__ import annotations

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)

#: When a key grows past this, the column is re-spread. 48 is well inside the 128-character
#: column and still leaves room for ~30 more worst-case inserts into one gap before the
#: next check; the number of cards is not what grows keys, repeated inserts into the same
#: gap are.
REBALANCE_LENGTH = 48


def _check(key: str) -> None:
    if key == "":
        raise ValueError("a rank cannot be empty")
    if key[-1] == "0":
        raise ValueError(f"rank {key!r} ends in '0'")
    if any(ch not in DIGITS for ch in key):
        raise ValueError(f"rank {key!r} contains a character outside 0-9a-z")


def _midpoint(low: str, high: str | None) -> str:
    """A key strictly between `low` and `high`, where `low` may be "" (zero) and `high`
    may be None (one)."""
    if high is not None:
        # Skip the common prefix; the answer shares it.
        n = 0
        while n < len(high) and (low[n] if n < len(low) else "0") == high[n]:
            n += 1
        if n > 0:
            return high[:n] + _midpoint(low[n:], high[n:])
    digit_low = DIGITS.index(low[0]) if low else 0
    digit_high = DIGITS.index(high[0]) if high is not None else BASE
    if digit_high - digit_low > 1:
        return DIGITS[round((digit_low + digit_high) / 2)]
    # Adjacent first digits: there is no single digit between them.
    if high is not None and len(high) > 1:
        return high[0]
    return DIGITS[digit_low] + _midpoint(low[1:], None)


def rank_between(before: str | None, after: str | None) -> str:
    """A key that sorts after `before` and before `after`.

    `before` is the card that will sit directly above; `after` the one directly below. Either
    may be None — None above means "top of the column", None below means "bottom".
    """
    if before is not None:
        _check(before)
    if after is not None:
        _check(after)
    if before is not None and after is not None and not before < after:
        raise ValueError(f"rank {before!r} must sort before {after!r}")
    return _midpoint(before or "", after)


def rank_after(last: str | None) -> str:
    """A key for a card appended below `last`, keeping keys short.

    Appending is the common case — every new issue lands at the bottom of its column — and
    the plain midpoint towards "one" lengthens the key every few appends. Incrementing the
    right-most digit that is not `z`, and dropping what follows it, gives a key that is
    larger and never longer.
    """
    if last is None:
        return DIGITS[BASE // 2]
    _check(last)
    for index in range(len(last) - 1, -1, -1):
        position = DIGITS.index(last[index])
        if position < BASE - 1:
            return last[:index] + DIGITS[position + 1]
    # All `z`: nothing to increment, so extend. Still larger, one character longer.
    return last + DIGITS[BASE // 2]


def needs_rebalance(key: str) -> bool:
    return len(key) > REBALANCE_LENGTH


def spread(count: int) -> list[str]:
    """`count` evenly spaced, strictly increasing keys, as short as `count` allows.

    Used to re-issue a whole column's ranks. The keys leave headroom above the last one,
    because the next thing that happens to a column is almost always an append.
    """
    if count <= 0:
        return []
    width = 1
    while BASE**width < 4 * (count + 1):
        width += 1
    span = BASE**width
    keys: list[str] = []
    for index in range(1, count + 1):
        value = (index * span) // (count + 1)
        digits = []
        for _ in range(width):
            value, remainder = divmod(value, BASE)
            digits.append(DIGITS[remainder])
        key = "".join(reversed(digits)).rstrip("0")
        keys.append(key)
    return keys
