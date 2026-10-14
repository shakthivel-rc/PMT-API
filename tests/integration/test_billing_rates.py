"""Integration tests for Phase 1 Billing Rates & Financial Budget Tracking."""
import pytest

from models.workspace import WorkspaceMemberRate
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def test_workspace_rates_lifecycle(client, db):
    """Test getting, setting, and clearing workspace billing rates and currencies."""
    owner, ws, space, board_list = pm.setup_board(client, db, "rates_owner", "rates-ws", "RATE")
    member = pm.person(db, "rates_member")
    pm.join(db, ws, "rates_member", "member")
    member_user_id = "rates_member"

    # 1. Default rates read
    res = client.get(f"/workspaces/{ws}/settings/rates", headers=owner)
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["currency"] == "USD"
    assert data["default_billing_rate"] is None
    assert len(data["member_rates"]) >= 2  # owner + member

    # 2. Non-admin cannot update rates
    forbidden = client.put(
        f"/workspaces/{ws}/settings/rates",
        headers=member,
        json={"currency": "EUR", "default_billing_rate": 100.0},
    )
    assert forbidden.status_code == 403

    # 3. Admin updates currency, default rate, and member override
    update_res = client.put(
        f"/workspaces/{ws}/settings/rates",
        headers=owner,
        json={
            "currency": "EUR",
            "default_billing_rate": 100.0,
            "member_rates": [{"user_id": member_user_id, "hourly_rate": 150.0}],
        },
    )
    assert update_res.status_code == 200
    rates_data = update_res.json()["data"]
    assert rates_data["currency"] == "EUR"
    assert rates_data["default_billing_rate"] == 100.0

    member_entry = next(r for r in rates_data["member_rates"] if r["user_id"] == member_user_id)
    assert member_entry["hourly_rate"] == 150.0

    # 4. Log time as member with override (150/hr for 3600s = 150 EUR)
    issue = pm.issue(client, owner, ws, board_list["id"], title="Rate testing issue")
    time_res = client.post(
        f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
        headers=member,
        json={"duration_seconds": 3600, "work_date": "2026-10-01", "billable": True},
    )
    assert time_res.status_code == 201
    entry = time_res.json()["data"]
    assert entry["billing_rate"] == 150.0
    assert entry["cost_amount"] == 150.0

    # 5. Log time as owner (uses default rate 100/hr for 1800s = 50 EUR)
    time_res2 = client.post(
        f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
        headers=owner,
        json={"duration_seconds": 1800, "work_date": "2026-10-01", "billable": True},
    )
    assert time_res2.status_code == 201
    entry2 = time_res2.json()["data"]
    assert entry2["billing_rate"] == 100.0
    assert entry2["cost_amount"] == 50.0

    # 6. Check timesheet list calculates totals
    entries_res = client.get(f"/workspaces/{ws}/time-entries", headers=owner)
    assert entries_res.status_code == 200
    page_data = entries_res.json()["data"]
    assert page_data["currency"] == "EUR"
    assert page_data["total_seconds"] == 5400
    assert page_data["total_cost_amount"] == 200.0


def test_space_budget_settings(client, db):
    """Test space creation and update with budget amounts and alert thresholds."""
    owner, ws, _, _ = pm.setup_board(client, db, "budget_owner", "budget-ws", "BDG")

    # 1. Create space with budget
    create_res = client.post(
        f"/workspaces/{ws}/spaces",
        headers=owner,
        json={
            "name": "Finance Platform",
            "key": "FIN",
            "budget_amount": 25000.0,
            "budget_alert_threshold": 85,
        },
    )
    assert create_res.status_code == 201
    space_data = create_res.json()["data"]
    assert space_data["budget_amount"] == 25000.0
    assert space_data["budget_alert_threshold"] == 85

    # 2. Update space budget
    update_res = client.patch(
        f"/workspaces/{ws}/spaces/{space_data['id']}",
        headers=owner,
        json={"budget_amount": 30000.0, "budget_alert_threshold": 90},
    )
    assert update_res.status_code == 200
    updated_space = update_res.json()["data"]
    assert updated_space["budget_amount"] == 30000.0
    assert updated_space["budget_alert_threshold"] == 90
