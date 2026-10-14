"""Unit tests for timeline calculations: critical path and cascade rescheduling."""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from models.hierarchy import Space, WorkflowStatus
from models.issue import Issue, IssueLink
from services.timeline import _calculate_critical_path

pytestmark = pytest.mark.unit


def test_calculate_critical_path_linear():
    """Three sequential blocking issues: A -> B -> C forms the critical path."""
    i1 = MagicMock(spec=Issue, id="i-1", start_date=date(2026, 11, 1), due_date=date(2026, 11, 3), story_points=3)
    i2 = MagicMock(spec=Issue, id="i-2", start_date=date(2026, 11, 4), due_date=date(2026, 11, 6), story_points=3)
    i3 = MagicMock(spec=Issue, id="i-3", start_date=date(2026, 11, 7), due_date=date(2026, 11, 9), story_points=3)

    l1 = MagicMock(spec=IssueLink, kind="blocks", source_issue_id="i-1", target_issue_id="i-2")
    l2 = MagicMock(spec=IssueLink, kind="blocks", source_issue_id="i-2", target_issue_id="i-3")

    issues = [i1, i2, i3]
    links = [l1, l2]

    cp = _calculate_critical_path(issues, links)
    assert cp == ["i-1", "i-2", "i-3"]


def test_calculate_critical_path_branching():
    """Diamond graph: A blocks B and C, B and C block D. Longest branch wins."""
    iA = MagicMock(spec=Issue, id="A", start_date=date(2026, 11, 1), due_date=date(2026, 11, 2), story_points=2)
    # B takes 5 days
    iB = MagicMock(spec=Issue, id="B", start_date=date(2026, 11, 3), due_date=date(2026, 11, 7), story_points=5)
    # C takes 1 day
    iC = MagicMock(spec=Issue, id="C", start_date=date(2026, 11, 3), due_date=date(2026, 11, 3), story_points=1)
    iD = MagicMock(spec=Issue, id="D", start_date=date(2026, 11, 8), due_date=date(2026, 11, 10), story_points=3)

    links = [
        MagicMock(spec=IssueLink, kind="blocks", source_issue_id="A", target_issue_id="B"),
        MagicMock(spec=IssueLink, kind="blocks", source_issue_id="A", target_issue_id="C"),
        MagicMock(spec=IssueLink, kind="blocks", source_issue_id="B", target_issue_id="D"),
        MagicMock(spec=IssueLink, kind="blocks", source_issue_id="C", target_issue_id="D"),
    ]

    cp = _calculate_critical_path([iA, iB, iC, iD], links)
    assert cp == ["A", "B", "D"]


def test_calculate_critical_path_empty_or_no_blocks():
    i1 = MagicMock(spec=Issue, id="i-1", start_date=date(2026, 11, 1), due_date=date(2026, 11, 3), story_points=3)
    i2 = MagicMock(spec=Issue, id="i-2", start_date=date(2026, 11, 4), due_date=date(2026, 11, 6), story_points=3)
    l_rel = MagicMock(spec=IssueLink, kind="relates", source_issue_id="i-1", target_issue_id="i-2")

    assert _calculate_critical_path([i1, i2], [l_rel]) == []
