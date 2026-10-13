"""Board ordering keys (utils/rank.py).

The property every test here protects: **the database's `ORDER BY rank` and the order the
person dragged the cards into are the same order.** If a generated key ever sorts on the
wrong side of a neighbour, a card jumps somewhere nobody put it, and nothing errors.
"""
import random

import pytest

from utils.rank import (
    DIGITS,
    REBALANCE_LENGTH,
    needs_rebalance,
    rank_after,
    rank_between,
    spread,
)

pytestmark = pytest.mark.unit


def _valid(key: str) -> bool:
    return bool(key) and key[-1] != "0" and all(ch in DIGITS for ch in key)


class TestRankBetween:
    def test_between_two_keys_sorts_strictly_between(self):
        assert "a" < rank_between("a", "b") < "b"

    def test_top_and_bottom_of_an_empty_column(self):
        key = rank_between(None, None)
        assert _valid(key)
        assert rank_between(None, key) < key < rank_between(key, None)

    def test_adjacent_digits_still_leave_room(self):
        """`a` and `b` differ by one digit; the answer must extend rather than collide."""
        key = rank_between("a", "b")
        assert "a" < key < "b" and _valid(key)

    def test_rejects_keys_in_the_wrong_order(self):
        with pytest.raises(ValueError):
            rank_between("b", "a")

    def test_rejects_a_trailing_zero_and_foreign_characters(self):
        for bad in ("a0", "A", "a-b", ""):
            with pytest.raises(ValueError):
                rank_between(bad, None)

    def test_random_insertions_keep_the_order_the_person_made(self):
        """Simulates a few thousand drags into random gaps of one column.

        Python's `sorted` over the keys must reproduce the list order exactly — that is
        what the database's ORDER BY will do with them.
        """
        rng = random.Random(20260929)
        column: list[str] = []
        for _ in range(3000):
            gap = rng.randint(0, len(column))
            above = column[gap - 1] if gap > 0 else None
            below = column[gap] if gap < len(column) else None
            key = rank_between(above, below)
            assert _valid(key)
            column.insert(gap, key)
        assert sorted(column) == column
        assert len(set(column)) == len(column)

    def test_repeated_drops_into_one_gap_grow_the_key_and_trip_the_rebalance(self):
        """The worst case: always dropping just below the same card."""
        top, bottom = "i", "j"
        for _ in range(400):
            bottom = rank_between(top, bottom)
        assert needs_rebalance(bottom)
        assert len(bottom) <= 128, "outgrew the column before the rebalance threshold fired"


class TestRankAfter:
    def test_appending_never_lengthens_the_key_until_it_must(self):
        key = rank_after(None)
        for _ in range(500):
            following = rank_after(key)
            assert following > key and _valid(following)
            key = following
        assert len(key) < REBALANCE_LENGTH

    def test_all_z_extends(self):
        assert rank_after("zz") > "zz"


class TestSpread:
    @pytest.mark.parametrize("count", [0, 1, 2, 35, 36, 500, 5000])
    def test_spread_is_strictly_increasing_short_and_valid(self, count):
        keys = spread(count)
        assert len(keys) == count
        assert keys == sorted(keys) and len(set(keys)) == count
        assert all(_valid(key) and not needs_rebalance(key) for key in keys)

    def test_spread_leaves_room_to_append(self):
        keys = spread(50)
        assert rank_after(keys[-1]) > keys[-1]
        assert rank_between(keys[-1], None) > keys[-1]
