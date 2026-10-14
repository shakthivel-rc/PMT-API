"""Integration tests for Phase 2 Sprint Planning Poker."""
import pytest

from models.issue import Issue, IssueEvent
from models.poker import PokerSession, PokerVote
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def test_planning_poker_workflow(client, db):
    """Test full planning poker lifecycle: start round, privacy during voting, reveal, and accept."""
    owner, ws, space, board_list = pm.setup_board(client, db, "poker_owner", "poker-ws", "POK")
    dev1 = pm.person(db, "dev1")
    pm.join(db, ws, "dev1", "member")
    dev2 = pm.person(db, "dev2")
    pm.join(db, ws, "dev2", "member")

    # 1. Create a sprint and an issue
    sprint = pm.ok(
        client.post(
            f"/workspaces/{ws}/spaces/{space['id']}/sprints",
            json={"name": "Sprint 1"},
            headers=owner,
        ),
        201,
    )
    sprint_id = sprint["id"]
    issue = pm.issue(client, owner, ws, board_list["id"], title="Build Auth Module")
    issue_id = issue["id"]

    # 2. Start poker session
    start_res = client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/poker/start",
        json={"issue_id": issue_id},
        headers=owner,
    )
    assert start_res.status_code == 201
    session_data = start_res.json()["data"]
    assert session_data["status"] == "voting"
    assert session_data["active_issue_id"] == issue_id
    assert session_data["average"] is None

    # 3. Dev1 votes 5 points
    v1_res = client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/poker/vote",
        json={"point_vote": 5},
        headers=dev1,
    )
    assert v1_res.status_code == 200
    d1_state = v1_res.json()["data"]
    assert d1_state["status"] == "voting"

    # Dev1 can see their own vote (5), but cannot see anyone else's
    dev1_voter = next(v for v in d1_state["voters"] if v["user_id"] == "dev1")
    assert dev1_voter["has_voted"] is True
    assert dev1_voter["point_vote"] == 5

    # 4. Dev2 polls state while voting is still active -> PRIVACY TEST
    d2_poll = client.get(
        f"/workspaces/{ws}/sprints/{sprint_id}/poker/state",
        headers=dev2,
    )
    assert d2_poll.status_code == 200
    d2_state = d2_poll.json()["data"]
    dev1_from_d2 = next(v for v in d2_state["voters"] if v["user_id"] == "dev1")
    assert dev1_from_d2["has_voted"] is True
    assert dev1_from_d2["point_vote"] is None  # MUST BE MASKED!
    assert d2_state["average"] is None  # Stats cannot leak before reveal

    # 5. Dev2 votes 8 points
    v2_res = client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/poker/vote",
        json={"point_vote": 8},
        headers=dev2,
    )
    assert v2_res.status_code == 200

    # 6. Reveal votes
    reveal_res = client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/poker/reveal",
        headers=owner,
    )
    assert reveal_res.status_code == 200
    rev_state = reveal_res.json()["data"]
    assert rev_state["status"] == "revealed"
    assert rev_state["average"] == 6.5  # (5 + 8) / 2
    assert rev_state["lowest_vote"] == 5
    assert rev_state["highest_vote"] == 8

    # Now dev2's poll shows all revealed votes
    d2_voter_rev = next(v for v in rev_state["voters"] if v["user_id"] == "dev2")
    dev1_voter_rev = next(v for v in rev_state["voters"] if v["user_id"] == "dev1")
    assert dev1_voter_rev["point_vote"] == 5
    assert d2_voter_rev["point_vote"] == 8

    # 7. Accept Consensus (team agrees on 5 points)
    accept_res = client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/poker/accept",
        json={"story_points": 5},
        headers=owner,
    )
    assert accept_res.status_code == 200
    acc_state = accept_res.json()["data"]
    assert acc_state["status"] == "completed"

    # Verify database issue was updated and IssueEvent logged
    db_issue = db.query(Issue).filter_by(id=issue_id).first()
    assert db_issue.story_points == 5

    event = (
        db.query(IssueEvent)
        .filter_by(issue_id=issue_id, event="points_changed")
        .first()
    )
    assert event is not None
    assert event.to_value == "5"
