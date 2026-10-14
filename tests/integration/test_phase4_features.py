"""Integration tests for Phase 4: Timeline / Gantt, Portfolio Initiatives, and CI/CD Deployments."""
from datetime import UTC, date, datetime, timedelta

import pytest

from models.deployment import Deployment, IssueDeployment
from models.initiative import Initiative, InitiativeIssue, InitiativeSpace
from models.issue import Issue, IssueLink
from models.vcs import VcsRepository
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def test_timeline_view_and_cascade_rescheduling(client, db):
    """Test timeline retrieval with dependencies and cascade rescheduling."""
    owner, ws, space, board_list = pm.setup_board(client, db, "tl_owner", "tl-ws", "TLW")

    # 1. Create two issues
    issue_a = pm.issue(
        client,
        owner,
        ws,
        board_list["id"],
        title="Foundation architecture",
    )
    issue_b = pm.issue(
        client,
        owner,
        ws,
        board_list["id"],
        title="Dependent UI implementation",
    )
    id_a = issue_a["id"]
    id_b = issue_b["id"]

    # Set dates on issue A and B
    db.query(Issue).filter(Issue.id == id_a).update({
        "start_date": date(2026, 11, 1),
        "due_date": date(2026, 11, 5),
    })
    db.query(Issue).filter(Issue.id == id_b).update({
        "start_date": date(2026, 11, 6),
        "due_date": date(2026, 11, 10),
    })
    db.commit()
    # Add blocking link: A blocks B via API
    res_link = client.post(
        f"/workspaces/{ws}/issues/{id_a}/dependencies",
        headers=owner,
        json={"target_issue_id": id_b, "kind": "blocks"},
    )
    assert res_link.status_code == 201

    # 2. Get timeline view
    res = client.get(
        f"/workspaces/{ws}/spaces/{space['id']}/timeline?zoom=week",
        headers=owner,
    )
    assert res.status_code == 200
    tl_data = res.json()["data"]
    assert tl_data["space_id"] == space["id"]
    assert len(tl_data["items"]) >= 2
    assert len(tl_data["dependencies"]) >= 1
    dep = tl_data["dependencies"][0]
    assert dep["source_issue_id"] == id_a
    assert dep["target_issue_id"] == id_b
    assert dep["kind"] == "blocks"

    # 3. Reschedule Issue A with cascade=True
    # Moving due_date of A from Nov 5 to Nov 8 (+3 days delta)
    res_sched = client.patch(
        f"/workspaces/{ws}/issues/{id_a}/reschedule",
        headers=owner,
        json={
            "start_date": "2026-11-04",
            "due_date": "2026-11-08",
            "cascade": True,
            "cascade_mode": "push_all_dependents",
        },
    )
    assert res_sched.status_code == 200
    sched_data = res_sched.json()["data"]
    assert sched_data["issue"]["start_date"] == "2026-11-04"
    assert sched_data["issue"]["due_date"] == "2026-11-08"
    assert sched_data["delta_days"] == 3
    assert len(sched_data["cascaded_issues"]) == 1
    cascaded_b = sched_data["cascaded_issues"][0]
    assert cascaded_b["id"] == id_b
    # B was 11-06..11-10 (+3 days -> 11-09..11-13)
    assert cascaded_b["start_date"] == "2026-11-09"
    assert cascaded_b["due_date"] == "2026-11-13"


def test_portfolio_initiatives_lifecycle_and_rollups(client, db):
    """Test creating, linking spaces/issues, and computing progress rollups on an initiative."""
    owner, ws, space1, list1 = pm.setup_board(client, db, "init_owner", "init-ws", "IN1")

    # 1. Create portfolio initiative
    res = client.post(
        f"/workspaces/{ws}/initiatives",
        headers=owner,
        json={
            "key": "INIT-2026",
            "title": "Platform Cloud Migration",
            "description": "Migrate core microservices to multi-region cloud cluster",
            "status": "active",
            "target_quarter": "2026-Q4",
            "start_date": "2026-10-01",
            "target_date": "2026-12-31",
            "budget_amount": 50000.0,
            "space_ids": [space1["id"]],
        },
    )
    assert res.status_code == 201
    init_data = res.json()["data"]
    init_id = init_data["id"]
    assert init_data["key"] == "INIT-2026"
    assert len(init_data["contributing_spaces"]) == 1

    # 2. Create issues in space1 with story points
    issue1 = pm.issue(client, owner, ws, list1["id"], title="Database migration scripts")
    issue2 = pm.issue(client, owner, ws, list1["id"], title="Kubernetes cluster provisioning")

    # Set story points: issue1 = 5, issue2 = 8
    db.query(Issue).filter(Issue.id == issue1["id"]).update({"story_points": 5})
    db.query(Issue).filter(Issue.id == issue2["id"]).update({"story_points": 8})
    db.commit()

    # 3. Link issues to initiative
    res_link = client.post(
        f"/workspaces/{ws}/initiatives/{init_id}/issues",
        headers=owner,
        json={"issue_ids": [issue1["id"], issue2["id"]]},
    )
    assert res_link.status_code == 200
    linked_data = res_link.json()["data"]
    assert len(linked_data["linked_issues"]) == 2
    assert linked_data["rollup"]["total_issues"] == 2
    assert linked_data["rollup"]["total_points"] == 13.0
    assert linked_data["rollup"]["completed_points"] == 0.0
    assert linked_data["rollup"]["progress_percentage"] == 0.0

    # 4. List initiatives
    res_list = client.get(
        f"/workspaces/{ws}/initiatives",
        headers=owner,
    )
    assert res_list.status_code == 200
    items = res_list.json()["data"]
    assert len(items) >= 1
    found = next((i for i in items if i["id"] == init_id), None)
    assert found is not None
    assert found["title"] == "Platform Cloud Migration"

    # 5. Unlink one issue
    res_unlink = client.delete(
        f"/workspaces/{ws}/initiatives/{init_id}/issues/{issue2['id']}",
        headers=owner,
    )
    assert res_unlink.status_code == 200
    assert len(res_unlink.json()["data"]["linked_issues"]) == 1

    # 6. Delete initiative
    res_del = client.delete(
        f"/workspaces/{ws}/initiatives/{init_id}",
        headers=owner,
    )
    assert res_del.status_code == 200


def test_deployments_and_issue_markers(client, db):
    """Test registering deployments, linking commit SHAs and issues, and retrieving markers."""
    owner, ws, space, board_list = pm.setup_board(client, db, "dep_owner", "dep-ws", "DEP")

    # 1. Create issues
    issue1 = pm.issue(client, owner, ws, board_list["id"], title="Auth token security fix")
    issue2 = pm.issue(client, owner, ws, board_list["id"], title="Rate limiting upgrade")
    k1 = issue1["key"]  # e.g. DEP-1
    k2 = issue2["key"]  # e.g. DEP-2

    # 2. Register a production deployment mentioning DEP-1 and DEP-2
    res_dep = client.post(
        f"/workspaces/{ws}/deployments",
        headers=owner,
        json={
            "environment": "production",
            "status": "success",
            "commit_sha": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            "ref_name": "release/v1.4.0",
            "deployer_name": "CI Bot",
            "deployment_url": "https://ci.example.com/deploy/456",
            "commit_messages": [f"feat: [{k1}] Token security patches", f"fix: [{k2}] Rate limiter adjustments"],
        },
    )
    assert res_dep.status_code == 201
    dep_data = res_dep.json()["data"]
    assert dep_data["environment"] == "production"
    assert dep_data["status"] == "success"
    assert dep_data["shipped_issue_count"] == 2

    # 3. Retrieve deployment list for workspace
    res_list = client.get(
        f"/workspaces/{ws}/deployments?environment=production",
        headers=owner,
    )
    assert res_list.status_code == 200
    deps = res_list.json()["data"]
    assert len(deps) >= 1
    assert deps[0]["environment"] == "production"

    # 4. Check issue deployment markers
    res_markers = client.get(
        f"/workspaces/{ws}/issues/{issue1['id']}/deployments",
        headers=owner,
    )
    assert res_markers.status_code == 200
    markers = res_markers.json()["data"]
    assert len(markers) == 1
    assert markers[0]["environment"] == "production"
    assert markers[0]["status"] == "success"
    assert markers[0]["commit_sha"] == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def test_gitlab_and_bitbucket_webhooks(client, db, monkeypatch):
    """Test GitLab and Bitbucket inbound webhook processing and authentication."""
    import dataclasses
    import hashlib
    import hmac
    import json
    from core.config import get_settings
    import routes.integrations as webhook_route

    owner, ws, space, board_list = pm.setup_board(client, db, "vcs_owner", "vcs-ws", "VCS")
    issue = pm.issue(client, owner, ws, board_list["id"], title="Cross provider bug")
    k = issue["key"]  # e.g. VCS-1

    from models.user import User
    user = db.query(User).filter(User.username == "vcs_owner").first()

    # 1. Register repos in database
    db.add(VcsRepository(
        workspace_id=ws,
        provider="gitlab",
        full_name="gitlab-org/my-app",
        created_by=user.id,
    ))
    db.add(VcsRepository(
        workspace_id=ws,
        provider="bitbucket",
        full_name="bb-org/my-app",
        created_by=user.id,
    ))
    db.commit()

    # Configure secrets
    gl_secret = "gl-secret-123"
    bb_secret = "bb-secret-456"
    configured = dataclasses.replace(
        get_settings(),
        gitlab_webhook_secret=gl_secret,
        bitbucket_webhook_secret=bb_secret,
    )
    monkeypatch.setattr(webhook_route, "get_settings", lambda: configured)

    # 2. Send GitLab Pipeline Webhook
    gl_payload = {
        "object_kind": "pipeline",
        "object_attributes": {
            "ref": f"feature/{k}-fix",
            "sha": "1111222233334444555566667777888899990000",
            "status": "success",
        },
        "project": {"path_with_namespace": "gitlab-org/my-app"},
        "user": {"name": "GitLab CI"},
        "commit": {"message": f"[{k}] Fix backend race condition"},
    }
    res_gl = client.post(
        "/integrations/gitlab/webhook",
        headers={
            "X-Gitlab-Token": gl_secret,
            "X-Gitlab-Event": "Pipeline Hook",
            "X-Gitlab-Delivery": "gl-deliv-001",
        },
        json=gl_payload,
    )
    assert res_gl.status_code == 200
    assert res_gl.json()["data"]["handled"] is True

    # 3. Send Bitbucket Commit Status Webhook
    bb_payload = {
        "repository": {"full_name": "bb-org/my-app"},
        "commit_status": {
            "name": f"deployment to staging for [{k}]",
            "state": "SUCCESSFUL",
            "commit": {"hash": "aaaabbbbccccddddeeeeffff0000111122223333"},
            "refname": "main",
        },
    }
    bb_body = json.dumps(bb_payload).encode()
    bb_sig = "sha256=" + hmac.new(bb_secret.encode(), bb_body, hashlib.sha256).hexdigest()

    res_bb = client.post(
        "/integrations/bitbucket/webhook",
        headers={
            "X-Hub-Signature": bb_sig,
            "X-Event-Key": "repo:commit_status_created",
            "X-Hook-UUID": "bb-deliv-001",
            "Content-Type": "application/json",
        },
        content=bb_body,
    )
    assert res_bb.status_code == 200
    assert res_bb.json()["data"]["handled"] is True
