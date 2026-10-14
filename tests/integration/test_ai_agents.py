"""Integration tests for Phase 3 Autonomous AI Agents & Workflows."""
from datetime import UTC, datetime, timedelta
import pytest

from models.issue import Issue, IssueComment, IssueLink
from models.sprint import Sprint, SprintIssue
from models.time_tracking import TimeEntry
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def test_ai_triage_and_duplicate_detection(client, db):
    """Test autonomous bug triage, duplicate detection, auto-linking and comment posting."""
    owner, ws, space, board_list = pm.setup_board(client, db, "ai_owner", "ai-ws", "AIW")

    # 1. Create original issue
    orig_issue = pm.issue(
        client,
        owner,
        ws,
        board_list["id"],
        title="Checkout payment token expired failure during processing",
        description="Users experience a fatal token expiration when paying via stripe gateway.",
    )
    orig_id = orig_issue["id"]

    # 2. Create duplicate candidate issue
    dup_issue = pm.issue(
        client,
        owner,
        ws,
        board_list["id"],
        title="Payment token expired failure during checkout payment step",
        description="Checkout token expired failure when processing card payments.",
    )
    dup_id = dup_issue["id"]

    # 3. Call triage endpoint
    res = client.post(
        f"/workspaces/{ws}/issues/{dup_id}/triage",
        headers=owner,
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["potential_duplicate"] is True
    assert data["duplicate_issue_id"] == orig_id
    assert data["similarity_score"] >= 0.70
    assert data["suggested_priority"] in ["urgent", "high", "medium"]
    assert "Steps to Reproduce" in data["structured_reproduction_steps"]

    # 4. Verify dependency link created in database
    link = (
        db.query(IssueLink)
        .filter(
            IssueLink.workspace_id == ws,
            IssueLink.source_issue_id == dup_id,
            IssueLink.target_issue_id == orig_id,
            IssueLink.kind == "relates",
        )
        .first()
    )
    assert link is not None

    # 5. Verify automated triage comment posted
    comment = (
        db.query(IssueComment)
        .filter(IssueComment.workspace_id == ws, IssueComment.issue_id == dup_id)
        .first()
    )
    assert comment is not None
    assert "Potential Duplicate Detected" in comment.body


def test_ai_daily_standup_synthesis(client, db):
    """Test daily standup generation with activity synthesis and velocity forecast."""
    owner, ws, space, board_list = pm.setup_board(client, db, "standup_owner", "standup-ws", "STU")

    # Create sprint and activate it
    sprint = pm.ok(
        client.post(
            f"/workspaces/{ws}/spaces/{space['id']}/sprints",
            json={"name": "Sprint 1", "starts_on": datetime.now(UTC).date().isoformat()},
            headers=owner,
        ),
        201,
    )
    sprint_id = sprint["id"]
    client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/start",
        json={},
        headers=owner,
    )

    # Create issue and log time
    iss = pm.issue(client, owner, ws, board_list["id"], title="Implement login API")
    iss_id = iss["id"]

    client.post(
        f"/workspaces/{ws}/issues/{iss_id}/time-entries",
        json={
            "work_date": datetime.now(UTC).date().isoformat(),
            "duration_seconds": 7200,
            "note": "Initial auth setup",
        },
        headers=owner,
    )

    # Generate daily standup
    res = client.post(
        f"/workspaces/{ws}/ai/standup/generate",
        headers=owner,
    )
    assert res.status_code == 200
    standup = res.json()["data"]
    assert "Daily Standup Briefing" in standup["summary"]
    assert len(standup["team_updates"]) >= 1

    user_update = standup["team_updates"][0]
    assert user_update["hours_logged"] >= 2.0
    assert standup["velocity_forecast"] is not None
    assert standup["velocity_forecast"]["active_sprint_name"] == "Sprint 1"

    # Get cached standup
    get_res = client.get(
        f"/workspaces/{ws}/ai/standup",
        headers=owner,
    )
    assert get_res.status_code == 200
    assert get_res.json()["data"]["id"] == standup["id"]


def test_ai_sprint_retrospective(client, db):
    """Test sprint retrospective generation with metrics and scope creep detection."""
    owner, ws, space, board_list = pm.setup_board(client, db, "retro_owner", "retro-ws", "RTR")

    # Create planned issue
    iss1 = pm.issue(client, owner, ws, board_list["id"], title="Initial planned task", story_points=15)

    # Create sprint and add planned issue
    sprint = pm.ok(
        client.post(
            f"/workspaces/{ws}/spaces/{space['id']}/sprints",
            json={"name": "Sprint Retrospective Test"},
            headers=owner,
        ),
        201,
    )
    sprint_id = sprint["id"]
    client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/issues",
        json={"issue_ids": [iss1["id"]]},
        headers=owner,
    )

    # Start sprint (freezes committed_points at 15)
    client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/start",
        json={},
        headers=owner,
    )

    # Add mid-sprint issue (scope creep)
    iss2 = pm.issue(client, owner, ws, board_list["id"], title="Mid-sprint critical patch", story_points=3)
    client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/issues",
        json={"issue_ids": [iss2["id"]]},
        headers=owner,
    )

    # Close sprint
    client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/close",
        json={"carry_over": "backlog"},
        headers=owner,
    )

    # Generate retro
    retro_res = client.post(
        f"/workspaces/{ws}/sprints/{sprint_id}/retrospective/generate",
        headers=owner,
    )
    assert retro_res.status_code == 200
    retro = retro_res.json()["data"]
    assert retro["sprint_id"] == sprint_id
    assert len(retro["wins"]) > 0
    assert retro["metrics"]["committed_points"] == 15


def test_ai_backlog_hygiene(client, db):
    """Test backlog hygiene scanning identifies inactive issues."""
    owner, ws, space, board_list = pm.setup_board(client, db, "hygiene_owner", "hygiene-ws", "HYG")

    # Create stale issue and update its timestamp
    stale_iss = pm.issue(client, owner, ws, board_list["id"], title="Ancient forgotten task")
    stale_id = stale_iss["id"]

    db_issue = db.query(Issue).filter(Issue.id == stale_id).first()
    old_time = datetime.now(UTC) - timedelta(days=120)
    db_issue.updated_at = old_time
    db.commit()

    # Query backlog hygiene
    res = client.get(
        f"/workspaces/{ws}/spaces/{space['id']}/backlog/hygiene?threshold_days=90",
        headers=owner,
    )
    assert res.status_code == 200
    report = res.json()["data"]
    assert report["space_id"] == space["id"]
    assert report["stale_count"] >= 1
    found = next((item for item in report["items"] if item["issue_id"] == stale_id), None)
    assert found is not None
    assert found["days_inactive"] >= 90
    assert "Recommend archiving" in found["recommendation"]
