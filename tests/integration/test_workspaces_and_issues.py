"""Tenancy, the hierarchy, issues and the board, through the HTTP API.

The tests at the top are the ones to read first: **tenant isolation**. Every other test in
this file would still pass if one customer could read another's issues; these would not.
"""
import pytest

from models.issue import IssueEvent
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


class TestTenantIsolation:
    def test_a_non_member_gets_404_for_the_workspace_and_everything_in_it(self, client, db):
        """404, not 403: a 403 would confirm to a stranger that the id is a real tenant."""
        headers, workspace_id, _space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"])
        stranger = pm.person(db, "stranger")

        for path in (
            f"/workspaces/{workspace_id}",
            f"/workspaces/{workspace_id}/members",
            f"/workspaces/{workspace_id}/issues",
            f"/workspaces/{workspace_id}/issues/{issue['id']}",
            f"/workspaces/{workspace_id}/board?list_id={board_list['id']}",
        ):
            response = client.get(path, headers=stranger)
            assert response.status_code == 404, path
            assert response.json()["message"] == "Workspace not found"

    def test_an_id_from_another_workspace_is_not_found_inside_yours(self, client, db):
        """The workspace in the path is yours, the issue id is someone else's. Membership
        passes; the tenant filter on the lookup must still refuse it."""
        headers_a, ws_a, _, _list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, _, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = pm.issue(client, headers_b, ws_b, list_b["id"], title="Bravo secret")

        assert client.get(f"/workspaces/{ws_a}/issues/{theirs['id']}",
                          headers=headers_a).status_code == 404
        assert client.post(f"/workspaces/{ws_a}/issues/{theirs['id']}/comments",
                           json={"body": "hi"}, headers=headers_a).status_code == 404
        # Creating in *your* workspace against *their* list is refused as well.
        assert client.post(f"/workspaces/{ws_a}/issues",
                           json={"list_id": list_b["id"], "title": "x"},
                           headers=headers_a).status_code == 404
        listed = pm.ok(client.get(f"/workspaces/{ws_a}/issues", headers=headers_a))
        assert all(card["title"] != "Bravo secret" for card in listed["records"])

    def test_roles_inside_a_workspace(self, client, db):
        _headers, workspace_id, _space, board_list = pm.setup_board(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, workspace_id, "guest-1", "guest")
        member = pm.person(db, "member-1")
        pm.join(db, workspace_id, "member-1", "member")

        # A guest reads but does not write.
        assert client.get(f"/workspaces/{workspace_id}", headers=guest).status_code == 200
        assert client.post(f"/workspaces/{workspace_id}/issues",
                           json={"list_id": board_list["id"], "title": "x"},
                           headers=guest).status_code == 403
        # A member writes issues but does not change structure.
        pm.issue(client, member, workspace_id, board_list["id"])
        assert client.post(f"/workspaces/{workspace_id}/spaces",
                           json={"name": "Ops", "key": "OPS"},
                           headers=member).status_code == 403


class TestHierarchy:
    def test_a_new_space_has_the_default_workflow_and_shows_in_the_tree(self, client, db):
        headers, workspace_id, _space, board_list = pm.setup_board(client, db)
        tree = pm.ok(client.get(f"/workspaces/{workspace_id}", headers=headers))
        assert tree["role"] == "owner"
        [only] = tree["spaces"]
        assert [s["name"] for s in only["statuses"]] == ["To Do", "In Progress", "In Review",
                                                          "Done"]
        assert [s["category"] for s in only["statuses"]] == ["todo", "in_progress",
                                                              "in_progress", "done"]
        assert only["lists"][0]["id"] == board_list["id"]

    def test_space_keys_and_workspace_slugs_are_unique(self, client, db):
        headers, workspace_id, _, _ = pm.setup_board(client, db)
        assert client.post(f"/workspaces/{workspace_id}/spaces",
                           json={"name": "Again", "key": "PAY"},
                           headers=headers).status_code == 409
        assert client.post("/workspaces", json={"name": "x", "slug": "acme"},
                           headers=headers).status_code == 409

    def test_a_list_can_live_in_a_folder(self, client, db):
        headers, workspace_id, space, _ = pm.setup_board(client, db)
        folder = pm.ok(client.post(f"/workspaces/{workspace_id}/spaces/{space['id']}/folders",
                                   json={"name": "Q4"}, headers=headers), 201)
        pm.ok(client.post(f"/workspaces/{workspace_id}/lists",
                          json={"space_id": space["id"], "folder_id": folder["id"],
                                "name": "Sprint 12"}, headers=headers), 201)
        tree = pm.ok(client.get(f"/workspaces/{workspace_id}", headers=headers))
        assert tree["spaces"][0]["folders"][0]["lists"][0]["name"] == "Sprint 12"


class TestIssues:
    def test_keys_count_up_per_space(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        keys = [pm.issue(client, headers, workspace_id, board_list["id"], title=f"t{n}")["key"]
                for n in range(3)]
        assert keys == ["PAY-1", "PAY-2", "PAY-3"]

    def test_the_hierarchy_rules(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = pm.issue(client, headers, workspace_id, lid, type="epic", title="Refunds",
                        assignee_id="owner-1", completion_criteria="Refunds reach the card.")
        story = pm.issue(client, headers, workspace_id, lid, type="story",
                         parent_id=epic["id"], title="Card refunds")
        pm.issue(client, headers, workspace_id, lid, type="subtask", parent_id=story["id"])

        def refused(**fields):
            response = client.post(f"/workspaces/{workspace_id}/issues",
                                   json={"list_id": lid, "title": "x", **fields},
                                   headers=headers)
            return response.status_code == 422

        assert refused(type="subtask")                          # needs a parent
        assert refused(type="epic", parent_id=story["id"])      # epics are top level
        assert refused(type="story", parent_id=story["id"])     # story under story

        detail = pm.ok(client.get(f"/workspaces/{workspace_id}/issues/{epic['id']}",
                                  headers=headers))
        assert [child["id"] for child in detail["children"]] == [story["id"]]

    def test_a_reparent_that_would_make_a_cycle_is_refused(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        task = pm.issue(client, headers, workspace_id, lid, type="task")
        subtask = pm.issue(client, headers, workspace_id, lid, type="subtask",
                           parent_id=task["id"])
        response = client.patch(f"/workspaces/{workspace_id}/issues/{task['id']}",
                                json={"version": task["version"], "type": "bug",
                                      "parent_id": subtask["id"]}, headers=headers)
        assert response.status_code == 422

    def test_a_stale_version_is_a_409_and_changes_nothing(self, client, db):
        """Two people editing at once: the second save must not silently win."""
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"], title="Original")
        path = f"/workspaces/{workspace_id}/issues/{issue['id']}"

        first = pm.ok(client.patch(path, json={"version": 1, "title": "First"}, headers=headers))
        assert first["version"] == 2
        second = client.patch(path, json={"version": 1, "title": "Second"}, headers=headers)
        assert second.status_code == 409
        assert second.json()["data"]["current_version"] == 2
        assert pm.ok(client.get(path, headers=headers))["title"] == "First"

    def test_null_clears_and_absent_leaves_alone(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"], story_points=5,
                         priority="high")
        path = f"/workspaces/{workspace_id}/issues/{issue['id']}"
        updated = pm.ok(client.patch(path, json={"version": 1, "story_points": None},
                                     headers=headers))
        assert updated["story_points"] is None and updated["priority"] == "high"

    def test_assignees_must_be_members(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        pm.person(db, "outsider")
        response = client.post(f"/workspaces/{workspace_id}/issues",
                               json={"list_id": board_list["id"], "title": "x",
                                     "assignee_id": "outsider"}, headers=headers)
        assert response.status_code == 422

    def test_list_view_filters_search_and_pages(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        for title in ("Refund 100% of fee", "Refund partial", "Export CSV"):
            pm.issue(client, headers, workspace_id, board_list["id"], title=title)
        page = pm.ok(client.get(f"/workspaces/{workspace_id}/issues",
                                params={"q": "refund", "limit": 1}, headers=headers))
        assert page["total"] == 2 and len(page["records"]) == 1
        # LIKE wildcards in the search term are literal.
        literal = pm.ok(client.get(f"/workspaces/{workspace_id}/issues",
                                   params={"q": "100%"}, headers=headers))
        assert [r["title"] for r in literal["records"]] == ["Refund 100% of fee"]
        assert client.get(f"/workspaces/{workspace_id}/issues",
                          params={"sort_by": "title; drop table issues"},
                          headers=headers).status_code == 400

    def test_delete_refuses_a_parent_and_hides_the_issue(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        story = pm.issue(client, headers, workspace_id, board_list["id"], type="story")
        task = pm.issue(client, headers, workspace_id, board_list["id"], type="subtask",
                        parent_id=story["id"])
        base = f"/workspaces/{workspace_id}/issues"
        assert client.delete(f"{base}/{story['id']}", headers=headers).status_code == 409
        assert client.delete(f"{base}/{task['id']}", headers=headers).status_code == 200
        assert client.get(f"{base}/{task['id']}", headers=headers).status_code == 404

    def test_comments_round_trip_oldest_first(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"])
        path = f"/workspaces/{workspace_id}/issues/{issue['id']}/comments"
        for body in ("first", "second"):
            pm.ok(client.post(path, json={"body": body}, headers=headers), 201)
        comments = pm.ok(client.get(path, headers=headers))
        assert [c["body"] for c in comments] == ["first", "second"]
        assert comments[0]["author"]["display_name"] == "Test User"

    def test_comment_update_and_delete(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"])
        path = f"/workspaces/{workspace_id}/issues/{issue['id']}/comments"
        created = pm.ok(client.post(path, json={"body": "original comment"}, headers=headers), 201)
        comment_id = created["id"]

        updated = pm.ok(
            client.patch(
                f"{path}/{comment_id}", json={"body": "edited comment"}, headers=headers
            ),
            200,
        )
        assert updated["body"] == "edited comment"

        # Verify in list
        comments = pm.ok(client.get(path, headers=headers))
        assert len(comments) == 1
        assert comments[0]["body"] == "edited comment"

        # Delete comment
        pm.ok(client.delete(f"{path}/{comment_id}", headers=headers), 200)

        # Verify deleted
        comments_after = pm.ok(client.get(path, headers=headers))
        assert len(comments_after) == 0


class TestBoard:
    def test_every_column_is_present_even_when_empty(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        pm.issue(client, headers, workspace_id, board_list["id"], title="A")
        payload = pm.board(client, headers, workspace_id, board_list["id"])
        assert [c["status"]["name"] for c in payload["columns"]] == [
            "To Do", "In Progress", "In Review", "Done"]
        assert pm.column_titles(payload, "To Do") == ["A"]
        assert pm.column_titles(payload, "Done") == []

    def test_new_issues_append_to_the_bottom(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        for title in ("A", "B", "C"):
            pm.issue(client, headers, workspace_id, board_list["id"], title=title)
        payload = pm.board(client, headers, workspace_id, board_list["id"])
        assert pm.column_titles(payload, "To Do") == ["A", "B", "C"]

    def test_moving_between_and_within_columns(self, client, db):
        headers, workspace_id, space, board_list = pm.setup_board(client, db)
        a, b, c = (pm.issue(client, headers, workspace_id, board_list["id"], title=t)
                   for t in ("A", "B", "C"))
        todo, doing = pm.status_id(space, "To Do"), pm.status_id(space, "In Progress")

        def move(issue, status, after=None, before=None):
            return pm.ok(client.post(
                f"/workspaces/{workspace_id}/issues/{issue['id']}/move",
                json={"status_id": status, "after_id": after and after["id"],
                      "before_id": before and before["id"]}, headers=headers))

        move(c, todo, before=a)                 # C to the top
        move(a, doing)                          # A to the next column
        move(b, doing, after=a)                 # B under A
        payload = pm.board(client, headers, workspace_id, board_list["id"])
        assert pm.column_titles(payload, "To Do") == ["C"]
        assert pm.column_titles(payload, "In Progress") == ["A", "B"]

        events = db.query(IssueEvent).filter(IssueEvent.event == "status_changed").all()
        assert len(events) == 2, "a reorder within a column is not a status change"

    def test_a_stale_neighbour_does_not_break_the_drop(self, client, db):
        """The client thinks the order is A, B, but someone already put C between them.
        Dropping X "between A and B" must still land directly under A."""
        headers, workspace_id, space, board_list = pm.setup_board(client, db)
        a, b, c, x = (pm.issue(client, headers, workspace_id, board_list["id"], title=t)
                      for t in ("A", "B", "C", "X"))
        todo = pm.status_id(space, "To Do")
        base = f"/workspaces/{workspace_id}/issues"
        pm.ok(client.post(f"{base}/{c['id']}/move",
                          json={"status_id": todo, "after_id": a["id"], "before_id": b["id"]},
                          headers=headers))
        pm.ok(client.post(f"{base}/{x['id']}/move",
                          json={"status_id": todo, "after_id": a["id"], "before_id": b["id"]},
                          headers=headers))
        payload = pm.board(client, headers, workspace_id, board_list["id"])
        assert pm.column_titles(payload, "To Do") == ["A", "X", "C", "B"]

    def test_a_status_from_another_space_is_refused(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        other = pm.space(client, headers, workspace_id, "OPS")
        issue = pm.issue(client, headers, workspace_id, board_list["id"])
        response = client.post(f"/workspaces/{workspace_id}/issues/{issue['id']}/move",
                               json={"status_id": pm.status_id(other, "Done")}, headers=headers)
        assert response.status_code == 422

    def test_the_board_needs_exactly_one_scope(self, client, db):
        headers, workspace_id, space, board_list = pm.setup_board(client, db)
        base = f"/workspaces/{workspace_id}/board"
        assert client.get(base, headers=headers).status_code == 400
        assert client.get(base, params={"list_id": board_list["id"], "space_id": space["id"]},
                          headers=headers).status_code == 400
        whole = pm.ok(client.get(base, params={"space_id": space["id"]}, headers=headers))
        assert whole["scope"] == "space"
