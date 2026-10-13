"""The standard and legacy parent rules (docs/design/09-restructuring.md §2).

The standard convention decides every relationship that is created or changed; the legacy
table only recognises relationships PMT itself used to allow, so that existing trees are
kept rather than refused. A mistake in either direction is costly: a too-loose standard lets
the deep trees the plan is removing grow back, and a too-loose "known" check lets a
reconciliation pass data that no version of PMT would ever have written.
"""
import pytest

from utils import hierarchy_rules as rules

pytestmark = pytest.mark.unit


class TestStandard:
    @pytest.mark.parametrize("child", ["story", "task", "bug"])
    def test_story_task_and_bug_are_peers_under_an_epic_or_alone(self, child):
        assert rules.standard_parent_problem(child, "epic") is None
        assert rules.standard_parent_problem(child, None) is None

    @pytest.mark.parametrize("parent", ["story", "task", "bug"])
    def test_a_subtask_sits_under_any_delivery_item(self, parent):
        assert rules.standard_parent_problem("subtask", parent) is None

    def test_a_subtask_needs_a_parent(self):
        assert rules.standard_parent_problem("subtask", None) == "A subtask needs a parent."

    def test_an_epic_is_top_level(self):
        assert rules.standard_parent_problem("epic", "epic") == "An epic cannot have a parent."

    @pytest.mark.parametrize("child,parent", [("task", "story"), ("bug", "task"),
                                              ("bug", "subtask"), ("story", "story"),
                                              ("subtask", "subtask"), ("subtask", "epic")])
    def test_deep_and_nested_shapes_are_not_standard(self, child, parent):
        assert rules.standard_parent_problem(child, parent) is not None

    def test_the_refusal_names_what_is_allowed(self):
        assert rules.standard_parent_problem("task", "story") == "A task can sit under: epic."

    def test_an_unknown_type_is_refused_rather_than_allowed(self):
        assert rules.standard_parent_problem("initiative", None) is not None


class TestLegacy:
    @pytest.mark.parametrize("child,parent", [("task", "story"), ("bug", "story"),
                                              ("bug", "task"), ("bug", "subtask")])
    def test_shapes_only_the_old_rules_allowed_are_legacy(self, child, parent):
        assert rules.is_legacy_relationship(child, parent)
        assert rules.is_known_relationship(child, parent)

    def test_a_standard_relationship_is_not_reported_as_legacy(self):
        assert not rules.is_legacy_relationship("subtask", "story")
        assert not rules.is_legacy_relationship("story", "epic")

    def test_shapes_no_rule_ever_allowed_are_unknown(self):
        """What a reconciliation must flag: neither PMT version would have written these."""
        assert not rules.is_known_relationship("story", "story")
        assert not rules.is_known_relationship("epic", "story")
        assert not rules.is_known_relationship("subtask", None)

    def test_the_standard_never_accepts_more_than_the_legacy_rules_did_except_subtask_under_bug(
            self):
        """The one genuinely new relationship is a subtask under a bug; everything else in
        the standard was already allowed, so no old tree becomes newly legal by accident."""
        added = {(child, parent)
                 for child, parents in rules.STANDARD_PARENT_TYPES.items()
                 for parent in parents
                 if parent not in rules.LEGACY_PARENT_TYPES[child]}
        assert added == {("subtask", "bug")}
