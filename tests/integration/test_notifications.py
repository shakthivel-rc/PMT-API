"""Integration tests for the user notifications system."""
import pytest
from tests import pm_fixtures as pm
from services.notification import create_notification

pytestmark = pytest.mark.integration


def _setup(client, db):
    owner, ws, space, board_list = pm.setup_board(client, db)
    people = {"owner": owner}
    for user_id, role in (("member-1", "member"), ("member-2", "member")):
        people[user_id] = pm.person(db, user_id)
        pm.join(db, ws, user_id, role)
    return people, ws, space, board_list


class TestNotifications:
    def test_direct_notification_flow(self, client, db):
        people, ws, space, board_list = _setup(client, db)

        # 1. Create a notification directly for member-1
        notif = create_notification(
            db,
            user_id="member-1",
            title="Assigned to CP-101",
            message="You were assigned to: Fix database timeout",
            type="assigned",
            actor_id="member-2",
            workspace_id=ws,
            link="/projects?issue=CP-101",
        )
        db.commit()

        # 2. Member-1 lists notifications
        resp = client.get("/notifications", headers=people["member-1"])
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["unread_count"] == 1
        assert data["items"][0]["title"] == "Assigned to CP-101"
        assert data["items"][0]["is_read"] is False

        # 3. Member-2 does NOT see Member-1's notifications
        resp_m2 = client.get("/notifications", headers=people["member-2"])
        assert resp_m2.status_code == 200
        assert resp_m2.json()["data"]["total"] == 0
        assert resp_m2.json()["data"]["unread_count"] == 0

        # 4. Member-1 marks single notification as read
        notif_id = data["items"][0]["id"]
        patch_resp = client.patch(f"/notifications/{notif_id}/read", headers=people["member-1"])
        assert patch_resp.status_code == 200
        assert patch_resp.json()["data"]["is_read"] is True

        # 5. Check unread count is now 0
        resp_after = client.get("/notifications", headers=people["member-1"])
        assert resp_after.json()["data"]["unread_count"] == 0
        assert resp_after.json()["data"]["items"][0]["is_read"] is True

    def test_mark_all_read(self, client, db):
        people, ws, space, board_list = _setup(client, db)

        for i in range(3):
            create_notification(
                db,
                user_id="member-1",
                title=f"Notification {i}",
                message=f"Message {i}",
                type="system",
            )
        db.commit()

        resp = client.get("/notifications", headers=people["member-1"])
        assert resp.json()["data"]["unread_count"] == 3

        # Mark all read
        mark_all_resp = client.post("/notifications/mark-all-read", headers=people["member-1"])
        assert mark_all_resp.status_code == 200
        assert mark_all_resp.json()["data"]["marked_count"] == 3

        # Unread count should now be 0
        resp_unread = client.get("/notifications?unread_only=true", headers=people["member-1"])
        assert resp_unread.json()["data"]["total"] == 0
        assert resp_unread.json()["data"]["unread_count"] == 0

    def test_issue_assignment_creates_notification(self, client, db):
        people, ws, space, board_list = _setup(client, db)

        # Owner creates an issue assigned to member-1
        create_resp = client.post(
            f"/workspaces/{ws}/issues",
            json={
                "list_id": board_list["id"],
                "title": "Build user notification dropdown",
                "assignee_id": "member-1",
                "type": "task",
            },
            headers=people["owner"],
        )
        assert create_resp.status_code == 201, create_resp.text

        # Member-1 should have an unread notification
        resp = client.get("/notifications", headers=people["member-1"])
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["unread_count"] == 1
        assert "Assigned to" in data["items"][0]["title"]
        assert "Build user notification dropdown" in data["items"][0]["message"]
