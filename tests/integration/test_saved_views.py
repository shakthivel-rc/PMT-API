"""Saved board filters, through the HTTP API (docs/design/09-restructuring.md §5.4).

The visibility rules are the part to read first: someone else's private view must be a 404
for everyone — an admin and the workspace owner included — never a 403, and a shared view is
changed by its owner alone. The filter document is the board's vocabulary, so the tests also
pin what it refuses: a stored view nobody can open is worse than a refused save.
"""
import pytest

from models.activity_log import ActivityLog
from models.saved_view import SavedView
from schemas.saved_views import SavedViewCreate, SavedViewUpdate, ViewFilters
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def _setup(client, db):
    owner, ws, space, board_list = pm.setup_board(client, db)
    people = {"owner": owner}
    for user_id, role in (("member-1", "member"), ("member-2", "member"),
                          ("admin-1", "admin"), ("guest-1", "guest")):
        people[user_id] = pm.person(db, user_id)
        pm.join(db, ws, user_id, role)
    return people, ws, space, board_list


def _save(client, headers, ws, scope_id, status=201, **body):
    payload = {"name": "My board", "scope_kind": "list", "scope_id": scope_id, **body}
    response = client.post(f"/workspaces/{ws}/saved-views", json=payload, headers=headers)
    assert response.status_code == status, response.text
    return response.json()["data"]


def _views(client, headers, ws, **params):
    return pm.ok(client.get(f"/workspaces/{ws}/saved-views", params=params, headers=headers))


def _path(ws, view_id):
    return f"/workspaces/{ws}/saved-views/{view_id}"


class TestSaving:
    def test_the_documented_example_bodies_are_accepted(self):
        """Would catch: an example in `/docs` that the API itself refuses."""
        for model in (ViewFilters, SavedViewCreate, SavedViewUpdate):
            for example in model.model_config["json_schema_extra"]["examples"]:
                model.model_validate(example)

    def test_a_saved_view_comes_back_as_it_was_saved_normalised(self, client, db):
        """Would catch: filters stored as sent — repeats kept, a padded search — so two
        views that mean the same filter compare different."""
        people, ws, _space, board_list = _setup(client, db)
        saved = _save(client, people["member-1"], ws, board_list["id"], name="Triage",
                      filters={"assignee_ids": ["me", "none", "me"],
                               "types": ["bug", "story", "bug"], "priorities": ["high"],
                               "q": "  refund  ", "blocked": True, "sprint": "active",
                               "status_categories": ["todo", "canceled"],
                               "epic_id": "epic-1"})
        assert saved["name"] == "Triage" and saved["view_type"] == "board"
        assert saved["scope_kind"] == "list" and saved["scope_id"] == board_list["id"]
        assert saved["scope_name"] == "Backlog"
        assert saved["visibility"] == "private"
        assert saved["owner"] == {"id": "member-1", "display_name": "Test User"}
        assert saved["can_edit"] is True and saved["can_delete"] is True
        assert saved["filters"] == {
            "assignee_ids": ["me", "none"], "types": ["bug", "story"], "priorities": ["high"],
            "q": "refund", "blocked": True, "sprint": "active",
            "status_categories": ["todo", "canceled"], "epic_id": "epic-1"}
        assert pm.ok(client.get(_path(ws, saved["id"]), headers=people["member-1"])) == saved
        assert [v["id"] for v in _views(client, people["member-1"], ws)] == [saved["id"]]

    def test_an_empty_filter_document_shows_everything(self, client, db):
        """Would catch: a view saved without filters coming back with a missing key the SPA has to
        guess a default for."""
        people, ws, space, _list = _setup(client, db)
        saved = _save(client, people["member-1"], ws, space["id"], scope_kind="space")
        assert saved["filters"] == {"assignee_ids": [], "types": [], "priorities": [],
                                    "q": None, "blocked": None, "sprint": None,
                                    "status_categories": [], "epic_id": None}

    def test_the_filter_document_refuses_what_no_board_could_apply(self, client, db):
        """Would catch: a filter the board cannot apply stored anyway — too many people, an unknown
        category, type or key — so the view opens to something other than what was saved; and
        nothing refused may leave a row behind."""
        people, ws, _space, board_list = _setup(client, db)
        member, lid = people["member-1"], board_list["id"]
        for filters in ({"assignee_ids": [f"user-{n}" for n in range(51)]},
                        {"assignee_ids": ["x" * 37]},
                        {"q": "x" * 201},
                        {"status_categories": ["blocked"]},
                        {"types": ["feature"]},
                        {"priorities": ["p0"]},
                        {"sprint": ""},
                        {"status_id": "a column"}):
            _save(client, member, ws, lid, status=422, filters=filters)
        _save(client, member, ws, lid, name="   ", status=422)
        _save(client, member, ws, lid, name="x" * 121, status=422)
        _save(client, member, ws, lid, view_type="calendar", status=422)
        _save(client, member, ws, lid, visibility="public", status=422)
        assert db.query(SavedView).count() == 0

    def test_the_scope_must_exist_in_this_workspace(self, client, db):
        """Would catch: a view saved against an id from another workspace (or none at all),
        which no board in this workspace could ever open."""
        people, ws, space, _list = _setup(client, db)
        member = people["member-1"]
        _headers_b, _ws_b, space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        sprint_b = pm.ok(client.post(f"/workspaces/{_ws_b}/spaces/{space_b['id']}/sprints",
                                     json={"name": "Theirs"}, headers=_headers_b), 201)
        for kind, scope_id, message in (("list", "nope", "List not found"),
                                        ("list", list_b["id"], "List not found"),
                                        ("space", space_b["id"], "Space not found"),
                                        ("sprint", sprint_b["id"], "Sprint not found")):
            response = client.post(f"/workspaces/{ws}/saved-views",
                                   json={"name": "x", "scope_kind": kind, "scope_id": scope_id},
                                   headers=member)
            assert response.status_code == 404, (kind, scope_id)
            assert response.json()["message"] == message
        sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                   json={"name": "Sprint 1"}, headers=member), 201)
        assert _save(client, member, ws, sprint["id"], scope_kind="sprint")["scope_name"] == (
            "Sprint 1")

    def test_a_backlog_view_is_of_a_space(self, client, db):
        """A backlog is one ordered list per space (§5.2); a backlog of a list or a sprint
        does not exist to be opened."""
        people, ws, space, board_list = _setup(client, db)
        member = people["member-1"]
        _save(client, member, ws, board_list["id"], view_type="backlog", status=422)
        saved = _save(client, member, ws, space["id"], view_type="backlog", scope_kind="space")
        assert saved["view_type"] == "backlog"

    def test_guests_do_not_save_views(self, client, db):
        """Would catch: the member gate missing — a guest's shared view would land in every member's
        picker."""
        people, ws, _space, board_list = _setup(client, db)
        _save(client, people["guest-1"], ws, board_list["id"], status=403)


class TestVisibility:
    def test_a_private_view_is_404_for_everyone_but_its_owner(self, client, db):
        """404, never 403 — for another member, an admin and the workspace's owner alike. A
        403 would confirm the id is a real view that belongs to someone."""
        people, ws, _space, board_list = _setup(client, db)
        mine = _save(client, people["member-1"], ws, board_list["id"], name="Mine")
        for who in ("member-2", "admin-1", "owner", "guest-1"):
            headers = people[who]
            assert _views(client, headers, ws) == [], who
            assert client.get(_path(ws, mine["id"]), headers=headers).status_code == 404, who
            if who != "guest-1":
                for method, body in (("PATCH", {"name": "Theirs now"}), ("DELETE", None)):
                    response = client.request(method, _path(ws, mine["id"]), json=body,
                                              headers=headers)
                    assert response.status_code == 404, (who, method)
        assert pm.ok(client.get(_path(ws, mine["id"]), headers=people["member-1"]))[
            "name"] == "Mine"

    def test_a_shared_view_is_seen_by_all_and_changed_by_its_owner_only(self, client, db):
        """Would catch: another member (or an admin) rewriting a shared view everyone opens,
        under its owner's name."""
        people, ws, _space, board_list = _setup(client, db)
        shared = _save(client, people["member-1"], ws, board_list["id"], name="Stand-up",
                       visibility="workspace")
        path = _path(ws, shared["id"])
        for who in ("member-2", "admin-1", "owner", "guest-1"):
            [seen] = _views(client, people[who], ws)
            assert seen["id"] == shared["id"], who
            assert seen["can_edit"] is False, who
        for who in ("member-2", "admin-1", "owner"):
            response = client.patch(path, json={"name": "Mine now"}, headers=people[who])
            assert response.status_code == 403, who
        assert client.delete(path, headers=people["member-2"]).status_code == 403
        assert client.patch(path, json={"name": "x"},
                            headers=people["guest-1"]).status_code == 403
        flags = {who: pm.ok(client.get(path, headers=people[who]))["can_delete"]
                 for who in ("member-1", "member-2", "admin-1", "guest-1")}
        assert flags == {"member-1": True, "member-2": False, "admin-1": True,
                         "guest-1": False}
        renamed = pm.ok(client.patch(path, json={"name": "Daily stand-up"},
                                     headers=people["member-1"]))
        assert renamed["name"] == "Daily stand-up"

    def test_an_admin_may_delete_a_shared_view_and_it_is_audited(self, client, db):
        """Would catch: a stale shared view only its (possibly departed) owner could remove, or an
        admin removing someone else's work without a trace."""
        people, ws, _space, board_list = _setup(client, db)
        shared = _save(client, people["member-1"], ws, board_list["id"],
                       visibility="workspace")
        assert pm.ok(client.delete(_path(ws, shared["id"]), headers=people["admin-1"])) == {
            "id": shared["id"]}
        assert _views(client, people["member-1"], ws) == []
        audited = db.query(ActivityLog).filter(ActivityLog.entity_type == "saved_view").all()
        assert [(row.user_id, row.entity_id) for row in audited] == [("admin-1",
                                                                     shared["id"])]

    def test_an_owner_deletes_their_own_view_without_an_audit_line(self, client, db):
        """Would catch: a hard delete, or the audit trail filled with people tidying their own views
        — only acts on someone else's are administrative."""
        people, ws, _space, board_list = _setup(client, db)
        mine = _save(client, people["member-1"], ws, board_list["id"])
        pm.ok(client.delete(_path(ws, mine["id"]), headers=people["member-1"]))
        assert client.get(_path(ws, mine["id"]), headers=people["member-1"]).status_code == 404
        assert db.query(SavedView).one().deleted == 1
        assert db.query(ActivityLog).filter(
            ActivityLog.entity_type == "saved_view").count() == 0


class TestEditing:
    def test_an_edit_changes_only_what_is_sent(self, client, db):
        """Would catch: a PATCH resetting the fields it does not name, or filters merged into the
        old document instead of replacing it."""
        people, ws, space, board_list = _setup(client, db)
        member = people["member-1"]
        saved = _save(client, member, ws, board_list["id"], filters={"types": ["bug"]})
        path = _path(ws, saved["id"])
        updated = pm.ok(client.patch(path, json={"filters": {"blocked": True}}, headers=member))
        assert updated["filters"]["blocked"] is True and updated["filters"]["types"] == []
        assert updated["name"] == "My board" and updated["scope_id"] == board_list["id"]
        moved = pm.ok(client.patch(path, json={"scope_kind": "space", "scope_id": space["id"],
                                              "view_type": "backlog",
                                              "visibility": "workspace"}, headers=member))
        assert (moved["scope_kind"], moved["scope_name"], moved["view_type"],
                moved["visibility"]) == ("space", "PAY space", "backlog", "workspace")

    def test_an_edit_keeps_the_view_openable(self, client, db):
        """Would catch: an edit that leaves a backlog view on a list, a scope kind changed
        without its id (now naming a different kind of thing), or a scope that is gone."""
        people, ws, space, board_list = _setup(client, db)
        member = people["member-1"]
        saved = _save(client, member, ws, space["id"], scope_kind="space", view_type="backlog")
        path = _path(ws, saved["id"])
        for body in ({"scope_kind": "list", "scope_id": board_list["id"]},
                     {"scope_kind": "list"}, {"scope_id": board_list["id"]},
                     {"name": None}, {"filters": None}, {"visibility": None},
                     {"filters": {"types": ["feature"]}}):
            assert client.patch(path, json=body, headers=member).status_code == 422, body
        missing = client.patch(path, json={"scope_kind": "space", "scope_id": "nope"},
                               headers=member)
        assert missing.status_code == 404
        assert pm.ok(client.get(path, headers=member))["scope_id"] == space["id"]

    def test_the_scope_name_follows_the_scope_and_goes_null_when_it_is_deleted(self, client,
                                                                               db):
        """Would catch: a name copied at save time and stale ever after, or a view whose scope is
        gone presented as if it could still be opened."""
        people, ws, space, _list = _setup(client, db)
        member = people["member-1"]
        sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/sprints",
                                   json={"name": "Sprint 1"}, headers=member), 201)
        saved = _save(client, member, ws, sprint["id"], scope_kind="sprint")
        pm.ok(client.patch(f"/workspaces/{ws}/sprints/{sprint['id']}",
                           json={"name": "Sprint 1 (Payments)"}, headers=member))
        assert pm.ok(client.get(_path(ws, saved["id"]), headers=member))["scope_name"] == (
            "Sprint 1 (Payments)")
        pm.ok(client.delete(f"/workspaces/{ws}/sprints/{sprint['id']}", headers=people["owner"]))
        assert pm.ok(client.get(_path(ws, saved["id"]), headers=member))["scope_name"] is None


class TestListing:
    def test_the_list_filters_by_scope_and_type_and_is_alphabetical(self, client, db):
        """Would catch: a board's view picker offered views of other scopes, someone else's private
        view listed, or an order that depends on capitalisation."""
        people, ws, space, board_list = _setup(client, db)
        member = people["member-1"]
        _save(client, member, ws, board_list["id"], name="beta")
        _save(client, member, ws, space["id"], name="Alpha", scope_kind="space",
              view_type="backlog")
        _save(client, people["member-2"], ws, board_list["id"], name="gamma",
              visibility="workspace")
        _save(client, people["member-2"], ws, board_list["id"], name="delta (private)")
        assert [v["name"] for v in _views(client, member, ws)] == ["Alpha", "beta", "gamma"]
        assert [v["name"] for v in _views(client, member, ws, scope_kind="list")] == [
            "beta", "gamma"]
        assert [v["name"] for v in _views(client, member, ws, scope_id=space["id"])] == [
            "Alpha"]
        assert [v["name"] for v in _views(client, member, ws, view_type="backlog")] == [
            "Alpha"]
        assert client.get(f"/workspaces/{ws}/saved-views", params={"scope_kind": "folder"},
                          headers=member).status_code == 422

    def test_a_stored_document_that_no_longer_validates_reads_as_no_filters(self, client,
                                                                             db):
        """Would catch: one bad stored row turning every listing of the workspace's views
        into a 500."""
        people, ws, _space, board_list = _setup(client, db)
        db.add(SavedView(workspace_id=ws, owner_id="member-1", name="Old", view_type="board",
                         scope_kind="list", scope_id=board_list["id"],
                         filters={"types": ["feature"], "legacy": 1}, visibility="private"))
        db.commit()
        [view] = _views(client, people["member-1"], ws)
        assert view["filters"]["types"] == [] and view["filters"]["q"] is None


class TestTenancy:
    def test_another_workspaces_views_are_404(self, client, db):
        """Would catch: a view lookup missing its workspace filter — another tenant's shared view is
        not shared with you."""
        headers_a, ws_a, _space_a, list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, _space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = _save(client, headers_b, ws_b, list_b["id"], name="Bravo shared",
                       visibility="workspace")
        _save(client, headers_a, ws_a, list_a["id"], name="Alpha mine")
        assert [v["name"] for v in _views(client, headers_a, ws_a)] == ["Alpha mine"]
        for method, body in (("GET", None), ("PATCH", {"name": "x"}), ("DELETE", None)):
            response = client.request(method, _path(ws_a, theirs["id"]), json=body,
                                      headers=headers_a)
            assert response.status_code == 404, method
        stranger = client.get(f"/workspaces/{ws_b}/saved-views", headers=headers_a)
        assert stranger.status_code == 404
        assert stranger.json()["message"] == "Workspace not found"
