"""Unit tests for permission_is_satisfied — the three-branch RBAC resolver
(direct / legacy-implies / reverse-implication). A regression here is a silent
privilege change, so every branch is pinned."""
import pytest

from core.permissions import (
    LEGACY_PERMISSION_IMPLICATIONS,
    TARGET_PERMISSION_CATALOG,
    permission_is_satisfied,
)

pytestmark = pytest.mark.unit


def test_direct_grant():
    assert permission_is_satisfied({"users:read"}, "users:read") is True


def test_missing_grant():
    assert permission_is_satisfied({"users:read"}, "users:delete") is False


def test_empty_permission_set():
    assert permission_is_satisfied(set(), "users:read") is False


class TestLegacyImplication:
    def test_legacy_key_implies_its_members(self):
        # manage_users implies users:delete (and 12 others)
        assert permission_is_satisfied({"manage_users"}, "users:delete") is True

    def test_legacy_key_does_not_imply_unrelated_permission(self):
        # manage_users does not grant session management
        assert permission_is_satisfied({"manage_users"}, "sessions:revoke") is False


class TestReverseImplication:
    def test_holding_the_full_implied_set_satisfies_the_legacy_key(self):
        implied = set(LEGACY_PERMISSION_IMPLICATIONS["manage_users"])
        assert permission_is_satisfied(implied, "manage_users") is True

    def test_holding_a_partial_set_does_not_satisfy_the_legacy_key(self):
        implied = set(LEGACY_PERMISSION_IMPLICATIONS["manage_users"])
        implied.discard(next(iter(implied)))  # drop one member
        assert permission_is_satisfied(implied, "manage_users") is False


class TestTheCatalogue:
    def test_every_slug_a_coarse_grant_implies_is_in_the_catalogue(self):
        """A slug implied but never catalogued is never seeded, so no role can be granted it
        on its own and the reverse implication above can never be satisfied."""
        catalogue = {slug for _, slug in TARGET_PERMISSION_CATALOG}
        for coarse, implied in LEGACY_PERMISSION_IMPLICATIONS.items():
            assert implied <= catalogue, f"{coarse} implies {sorted(implied - catalogue)}"

    def test_no_slug_is_catalogued_twice(self):
        slugs = [slug for _, slug in TARGET_PERMISSION_CATALOG]
        assert len(slugs) == len(set(slugs))
