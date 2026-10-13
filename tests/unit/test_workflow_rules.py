"""The pure rules of a workflow: categories, allowed moves, reachability, dead ends.

docs/design/09-restructuring.md §4. These are the functions a publish validates with and the
board move obeys, so a wrong answer here is either a workflow nobody can finish work in or a
restriction anyone can walk around.
"""
import pytest

from utils import workflow_rules as rules

pytestmark = pytest.mark.unit

STAGES = ["todo", "dev", "review", "qa", "done"]
FLOW = [("todo", "dev"), ("dev", "review"), ("review", "dev"), ("review", "qa"),
        ("qa", "done"), ("qa", "dev")]
CATEGORIES = {"todo": "todo", "dev": "in_progress", "review": "in_progress",
              "qa": "in_progress", "done": "done"}


class TestCategories:
    def test_canceled_is_closed_but_not_completed(self):
        """The reason the category exists: closed without being delivered."""
        assert rules.is_closed("canceled")
        assert not rules.is_completed("canceled")
        assert rules.is_completed("done")

    def test_open_categories_are_not_closed(self):
        for category in ("todo", "in_progress"):
            assert not rules.is_closed(category)
            assert rules.category_class(category) == "open"

    def test_an_unknown_category_reads_as_open_not_as_closed(self):
        """An issue the rules cannot place must not be counted as finished."""
        assert rules.category_class("shipped") == "open"
        assert not rules.is_closed(None)

    def test_every_category_has_a_label_and_a_default_colour(self):
        for category in rules.STATUS_CATEGORIES:
            assert rules.CATEGORY_LABELS[category]
            assert rules.DEFAULT_COLOR_BY_CATEGORY[category] in rules.STAGE_COLORS


class TestTransitions:
    def test_any_mode_allows_every_move_whatever_the_stored_transitions(self):
        """Column order alone must not impose restrictions (plan §4)."""
        assert rules.transition_allowed("any", [], "done", "todo")

    def test_restricted_mode_allows_only_stored_transitions(self):
        assert rules.transition_allowed("restricted", FLOW, "review", "qa")
        assert not rules.transition_allowed("restricted", FLOW, "review", "done")

    def test_staying_in_the_same_stage_is_always_allowed(self):
        """A reorder inside a column is not a transition."""
        assert rules.transition_allowed("restricted", [], "qa", "qa")

    def test_a_global_transition_leaves_every_stage(self):
        canceled = [*FLOW, (None, "canceled")]
        assert rules.transition_allowed("restricted", canceled, "todo", "canceled")
        assert rules.transition_allowed("restricted", canceled, "review", "canceled")

    def test_destinations_keep_board_order_and_exclude_the_current_stage(self):
        assert rules.destinations("restricted", STAGES, FLOW, "qa") == ["dev", "done"]
        assert rules.destinations("any", STAGES, [], "dev") == ["todo", "review", "qa", "done"]


class TestReachability:
    def test_every_stage_reachable_through_a_chain(self):
        assert rules.reachable("todo", STAGES, FLOW) == set(STAGES)

    def test_a_stage_with_no_way_in_is_unreachable(self):
        flow = [("todo", "dev"), ("dev", "done")]
        assert "review" not in rules.reachable("todo", STAGES, flow)

    def test_a_global_transition_target_is_reachable_from_the_start(self):
        assert "qa" in rules.reachable("todo", STAGES, [(None, "qa")])

    def test_an_initial_stage_that_does_not_exist_reaches_nothing(self):
        assert rules.reachable("gone", STAGES, FLOW) == set()

    def test_transitions_into_unknown_stages_are_ignored(self):
        assert rules.reachable("todo", ["todo"], [("todo", "elsewhere")]) == {"todo"}


class TestDeadEnds:
    def test_an_open_stage_nothing_leaves_is_a_dead_end(self):
        flow = [("todo", "dev"), ("dev", "review")]
        assert rules.dead_ends(STAGES, CATEGORIES, flow) == ["review", "qa"]

    def test_a_closed_stage_may_be_terminal(self):
        assert "done" not in rules.dead_ends(STAGES, CATEGORIES, FLOW)

    def test_a_global_transition_rescues_every_stage_but_its_own_target(self):
        stuck = rules.dead_ends(STAGES, CATEGORIES, [(None, "dev")])
        assert stuck == ["dev"]
