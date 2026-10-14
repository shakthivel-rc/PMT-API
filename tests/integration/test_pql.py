"""Integration tests for PQL API endpoints."""
import pytest

from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def test_pql_api_validate_and_execute(client, db):
    """Test /pql/validate and /pql/execute via HTTP."""
    owner, ws, space, board_list = pm.setup_board(client, db, "pql_user", "pql-ws", "PQL")

    # Seed issues
    i1 = pm.issue(client, owner, ws, board_list["id"], title="Bug in checkout", priority="urgent", type="bug")
    i2 = pm.issue(client, owner, ws, board_list["id"], title="Feature login", priority="low", type="story")

    # 1. Validate endpoint
    valid_res = client.post(
        f"/workspaces/{ws}/pql/validate",
        headers=owner,
        json={"query": 'priority = "urgent" AND type = "bug"'},
    )
    assert valid_res.status_code == 200
    assert valid_res.json()["data"]["valid"] is True

    invalid_res = client.post(
        f"/workspaces/{ws}/pql/validate",
        headers=owner,
        json={"query": 'unknown_field == 123'},
    )
    assert invalid_res.status_code == 200
    assert invalid_res.json()["data"]["valid"] is False

    # 2. Execute endpoint
    exec_res = client.post(
        f"/workspaces/{ws}/pql/execute",
        headers=owner,
        json={"query": 'priority = "urgent"'},
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()["data"]
    assert exec_data["total"] == 1
    assert exec_data["records"][0]["title"] == "Bug in checkout"

    # Execute empty or search all
    all_res = client.post(
        f"/workspaces/{ws}/pql/execute",
        headers=owner,
        json={"query": ""},
    )
    assert all_res.status_code == 200
    assert all_res.json()["data"]["total"] >= 2
