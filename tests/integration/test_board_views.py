"""The board's three scopes, its filters, its WIP counts, the list view's new filters, and
deep links by key — through the HTTP API (docs/design/09-restructuring.md §5.1, §7.3).

The property most worth defending here is that **WIP is counted for the team, not for the
viewer**: a column's `wip` must be the same number however the board is scoped and filtered,
because the limit is the team's capacity. A filtered count would let a person looking at
"my work" see a column comfortably under a limit the team is over.

Old keys (`issue_key_aliases`, `space_key_aliases`) are written through the `db` fixture:
the transfer and space-rename routes that create them are built elsewhere, and what is
pinned here is that the search filter and the deep-link route resolve them through the one
resolver.
"""
import pytest
from sqlalchemy import event

import db.session as db_session
from models.hierarchy import Space, SpaceKeyAlias, WorkflowStatus, WorkflowTransition
from models.issue import IssueKeyAlias
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration

OWNER = "owner-1"


def _board(client, headers, ws, **params) -> dict:
    return pm.ok(client.get(f"/workspaces/{ws}/board", params=params, headers=headers))


def _titles(board: dict) -> list[str]:
    return sorted(card["title"] for column in board["columns"] for card in column["cards"])


def _column(board: dict, name: str) -> dict:
    return next(column for column in board["columns"] if column["status"]["name"] == name)


def _move(client, headers, ws, issue_id: str, status_id: str) -> dict:
    return pm.ok(client.post(f"/workspaces/{ws}/issues/{issue_id}/move",
                             json={"status_id": status_id}, headers=headers))


def _sprint(client, headers, ws, space_id: str, name: str, *issue_ids: str) -> dict:
    sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space_id}/sprints",
                               json={"name": name}, headers=headers), 201)
    if issue_ids:
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": list(issue_ids)}, headers=headers))
    return sprint


def _block(client, headers, ws, issue: dict, reason: str = "Waiting on a vendor") -> dict:
    return pm.ok(client.patch(f"/workspaces/{ws}/issues/{issue['id']}",
                              json={"version": issue["version"], "blocked": True,
                                    "blocked_reason": reason}, headers=headers))


def _epic(client, headers, ws, list_id: str, title: str = "Epic") -> dict:
    return pm.issue(client, headers, ws, list_id, title, type="epic", assignee_id=OWNER,
                    completion_criteria="The outcome is met.")


class _QueryCounter:
    def __enter__(self):
        self.statements: list[str] = []
        self._listener = lambda conn, cursor, statement, *a: self.statements.append(statement)
        event.listen(db_session.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(db_session.engine, "before_cursor_execute", self._listener)


class TestScopes:
    def test_exactly_one_scope_and_unknown_ones_are_404(self, client, db):
        """Two scopes would be ambiguous and none would be the whole tenant. An id from
        nowhere — or from another workspace — is a 404, never an empty board."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        base = f"/workspaces/{ws}/board"
        sprint = _sprint(client, headers, ws, space["id"], "S1")
        for params in ({}, {"list_id": board_list["id"], "space_id": space["id"]},
                       {"space_id": space["id"], "sprint_id": sprint["id"]}):
            assert client.get(base, params=params, headers=headers).status_code == 400
        for params in ({"list_id": "nope"}, {"space_id": "nope"}, {"sprint_id": "nope"}):
            assert client.get(base, params=params, headers=headers).status_code == 404
        other_headers, other_ws, other_space, _ = pm.setup_board(client, db, "bob", "bravo",
                                                                 "BRA")
        theirs = _sprint(client, other_headers, other_ws, other_space["id"], "Theirs")
        assert client.get(base, params={"sprint_id": theirs["id"]},
                          headers=headers).status_code == 404

    def test_the_space_overview_draws_every_list(self, client, db):
        """The plan's "space overview": one board over every list of the space, each card
        saying which list it lives in."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        support = pm.task_list(client, headers, ws, space["id"], "Support")
        pm.issue(client, headers, ws, board_list["id"], "Delivery work")
        pm.issue(client, headers, ws, support["id"], "Support work")
        board = _board(client, headers, ws, space_id=space["id"])
        assert board["scope"] == "space" and board["space_id"] == space["id"]
        cards = {card["title"]: card["list_id"] for card in _column(board, "To Do")["cards"]}
        assert cards == {"Delivery work": board_list["id"], "Support work": support["id"]}
        listed = _board(client, headers, ws, list_id=support["id"])
        assert _titles(listed) == ["Support work"] and listed["scope"] == "list"

    def test_the_sprint_board_draws_the_sprint_across_lists_in_any_state(self, client, db):
        """The active-sprint board: every issue in the sprint, whichever list it lives in,
        in the space's columns — and still drawable once the sprint is closed, where the done
        work stays. Would catch the board drawing a list instead of the sprint."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        support = pm.task_list(client, headers, ws, space["id"], "Support")
        a = pm.issue(client, headers, ws, board_list["id"], "A")
        b = pm.issue(client, headers, ws, support["id"], "B")
        pm.issue(client, headers, ws, board_list["id"], "Not planned")
        sprint = _sprint(client, headers, ws, space["id"], "S1", a["id"], b["id"])

        planned = _board(client, headers, ws, sprint_id=sprint["id"])
        assert planned["scope"] == "sprint" and planned["scope_id"] == sprint["id"]
        assert planned["space_id"] == space["id"]
        assert [c["status"]["name"] for c in planned["columns"]] == [
            "To Do", "In Progress", "In Review", "Done"]
        assert _titles(planned) == ["A", "B"] and planned["total"] == 2

        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                          headers=headers))
        _move(client, headers, ws, a["id"], pm.status_id(space, "Done"))
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close", json={},
                          headers=headers))
        closed = _board(client, headers, ws, sprint_id=sprint["id"])
        assert _titles(closed) == ["A"]
        assert [c["title"] for c in _column(closed, "Done")["cards"]] == ["A"]

    def test_the_board_carries_its_workflow_so_a_drag_can_be_checked(self, client, db):
        """Mode, initial stage, transitions and WIP policy travel with the board so the SPA
        can refuse an impossible drag before sending it."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        free = _board(client, headers, ws, list_id=board_list["id"])
        assert free["transition_mode"] == "any" and free["transitions"] == []
        assert free["initial_status_id"] == pm.status_id(space, "To Do")
        assert free["wip_policy"] == "warn" and free["filtered"] is False

        todo, doing = pm.status_id(space, "To Do"), pm.status_id(space, "In Progress")
        done = pm.status_id(space, "Done")
        db.add(WorkflowTransition(workspace_id=ws, space_id=space["id"],
                                  from_status_id=todo, to_status_id=doing))
        db.add(WorkflowTransition(workspace_id=ws, space_id=space["id"],
                                  from_status_id=None, to_status_id=done))
        db.query(Space).filter(Space.id == space["id"]).update(
            {Space.transition_mode: "restricted", Space.wip_policy: "block"})
        db.commit()
        restricted = _board(client, headers, ws, space_id=space["id"])
        assert restricted["transition_mode"] == "restricted"
        assert restricted["wip_policy"] == "block"
        assert restricted["transitions"] == [
            {"from_status_id": None, "to_status_id": done},
            {"from_status_id": todo, "to_status_id": doing}]


class TestFilters:
    def _setup(self, client, db):
        headers, ws, space, board_list = pm.setup_board(client, db)
        pm.person(db, "member-1")
        pm.join(db, ws, "member-1", "member")
        lid = board_list["id"]
        mine = pm.issue(client, headers, ws, lid, "Mine", type="story", priority="high",
                        assignee_id=OWNER)
        theirs = pm.issue(client, headers, ws, lid, "Theirs", type="bug", priority="low",
                          assignee_id="member-1")
        nobody = pm.issue(client, headers, ws, lid, "Nobody's refund", type="task",
                          priority="urgent")
        return headers, ws, space, board_list, mine, theirs, nobody

    def test_assignee_me_none_or_an_id(self, client, db):
        """`me` is the caller, `none` is unassigned work, anything else an account id."""
        headers, ws, _space, board_list, *_ = self._setup(client, db)
        lid = board_list["id"]
        assert _titles(_board(client, headers, ws, list_id=lid, assignee_id="me")) == ["Mine"]
        assert _titles(_board(client, headers, ws, list_id=lid, assignee_id="none")) == [
            "Nobody's refund"]
        assert _titles(_board(client, headers, ws, list_id=lid,
                              assignee_id="member-1")) == ["Theirs"]

    def test_type_and_priority_repeat(self, client, db):
        """Each is "any of these"; together they narrow."""
        headers, ws, _space, board_list, *_ = self._setup(client, db)
        lid = board_list["id"]
        assert _titles(_board(client, headers, ws, list_id=lid,
                              type=["story", "bug"])) == ["Mine", "Theirs"]
        assert _titles(_board(client, headers, ws, list_id=lid,
                              priority=["urgent", "low"])) == ["Nobody's refund", "Theirs"]
        assert _titles(_board(client, headers, ws, list_id=lid, type=["story", "bug"],
                              priority=["urgent", "low"])) == ["Theirs"]
        assert client.get(f"/workspaces/{ws}/board",
                          params={"list_id": lid, "type": "initiative"},
                          headers=headers).status_code == 422

    def test_q_matches_a_title_or_a_key_old_keys_included(self, client, db):
        """A person pastes `PAY-2`, or a key from before the issue moved, or one from before
        its space was renamed, and finds the issue. Would catch `q` matching titles only, or
        resolving keys anywhere but through the one resolver."""
        headers, ws, space, board_list, mine, theirs, _nobody = self._setup(client, db)
        lid = board_list["id"]
        assert _titles(_board(client, headers, ws, list_id=lid, q="REFUND")) == [
            "Nobody's refund"]
        assert _titles(_board(client, headers, ws, list_id=lid, q=theirs["key"])) == [
            "Theirs"]
        assert _titles(_board(client, headers, ws, list_id=lid,
                              q=theirs["key"].lower())) == ["Theirs"]
        db.add(IssueKeyAlias(workspace_id=ws, issue_id=mine["id"], space_id=space["id"],
                             space_key="OLD", number=41))
        db.add(SpaceKeyAlias(workspace_id=ws, space_id=space["id"], key="PYM"))
        db.commit()
        assert _titles(_board(client, headers, ws, list_id=lid, q="OLD-41")) == ["Mine"]
        number = theirs["key"].split("-")[1]
        assert _titles(_board(client, headers, ws, list_id=lid, q=f"PYM-{number}")) == [
            "Theirs"]
        assert _titles(_board(client, headers, ws, list_id=lid, q="PAY-999")) == []

    def test_blocked_true_shows_only_blocked_work(self, client, db):
        """The daily "what is blocked" view; false is its complement."""
        headers, ws, _space, board_list, mine, _theirs, _nobody = self._setup(client, db)
        lid = board_list["id"]
        _block(client, headers, ws, mine)
        assert _titles(_board(client, headers, ws, list_id=lid, blocked="true")) == ["Mine"]
        assert _titles(_board(client, headers, ws, list_id=lid, blocked="false")) == [
            "Nobody's refund", "Theirs"]

    def test_epic_id_is_the_epic_and_everything_beneath_it(self, client, db):
        """Filtering by an epic shows the outcome's whole tree, subtasks included."""
        headers, ws, _space, board_list, *_ = self._setup(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        story = pm.issue(client, headers, ws, lid, "Under the epic", type="story",
                         parent_id=epic["id"])
        pm.issue(client, headers, ws, lid, "Deep", type="subtask", parent_id=story["id"])
        board = _board(client, headers, ws, list_id=lid, epic_id=epic["id"])
        assert _titles(board) == ["Deep", "Epic", "Under the epic"]
        assert board["filtered"] is True and board["total"] == 3
        assert _titles(_board(client, headers, ws, list_id=lid, epic_id="nothing")) == []

    def test_wip_is_counted_space_wide_whatever_the_scope_and_filters(self, client, db):
        """The property this module exists for. Two lists, three WIP-type issues and an epic
        and a subtask in one stage with a limit of 3: a list board filtered to one person
        still shows 3 of 3, and a fourth story makes every view say over."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        support = pm.task_list(client, headers, ws, space["id"], "Support")
        review = pm.status_id(space, "In Review")
        db.query(WorkflowStatus).filter(WorkflowStatus.id == review).update(
            {WorkflowStatus.wip_limit: 3})
        db.commit()
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        story = pm.issue(client, headers, ws, lid, "Story", type="story", assignee_id=OWNER)
        subtask = pm.issue(client, headers, ws, lid, "Sub", type="subtask",
                           parent_id=story["id"])
        task = pm.issue(client, headers, ws, support["id"], "Task", type="task")
        bug = pm.issue(client, headers, ws, support["id"], "Bug", type="bug")
        for issue in (epic, story, subtask, task, bug):
            _move(client, headers, ws, issue["id"], review)

        views = [_board(client, headers, ws, list_id=lid, assignee_id="me"),
                 _board(client, headers, ws, space_id=space["id"], type=["epic"]),
                 _board(client, headers, ws, list_id=support["id"], q="nothing matches")]
        for view in views:
            assert _column(view, "In Review")["wip"] == {"count": 3, "limit": 3,
                                                         "over_limit": False}
            assert view["filtered"] is True
        # The epic is the owner's too (an epic's assignee is its owner): shown, not counted.
        assert [c["title"] for c in _column(views[0], "In Review")["cards"]] == ["Epic",
                                                                               "Story"]
        assert views[2]["total"] == 0
        assert _column(views[0], "To Do")["wip"] == {"count": 0, "limit": None,
                                                     "over_limit": False}

        late = pm.issue(client, headers, ws, lid, "Late", type="story")
        _move(client, headers, ws, late["id"], review)
        over = _board(client, headers, ws, list_id=support["id"], assignee_id="none")
        assert _column(over, "In Review")["wip"] == {"count": 4, "limit": 3,
                                                     "over_limit": True}

    def test_board_queries_do_not_grow_with_cards(self, client, db):
        """The N+1 guard for the new fields, the WIP count and the filters together: the same
        board with four times the cards costs no more queries."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        # Every filter at once, chosen so that every card still matches: a board that
        # filtered everything away would project no cards and prove nothing.
        params = {"list_id": lid, "type": ["story", "task"], "priority": ["none", "high"],
                  "assignee_id": "none", "epic_id": epic["id"], "q": "I",
                  "blocked": "false"}

        def add(count):
            for n in range(count):
                issue = pm.issue(client, headers, ws, lid, f"I{n}", type="story",
                                 parent_id=epic["id"])
                _move(client, headers, ws, issue["id"], pm.status_id(space, "In Progress"))

        def measure() -> tuple[int, int]:
            with _QueryCounter() as counter:
                board = pm.ok(client.get(f"/workspaces/{ws}/board", params=params,
                                         headers=headers))
            return len(counter.statements), board["total"]

        add(2)
        small, small_total = measure()
        add(8)
        large, large_total = measure()
        assert (small_total, large_total) == (2, 10)
        assert large <= small, f"board queries grew with cards: {small} -> {large}"


class TestListView:
    def test_sprint_blocked_priority_and_key_filters(self, client, db):
        """The list view takes the board's filters plus `sprint_id` — an id, or `none` for
        unscheduled work — so a saved view means the same thing in either."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        planned = pm.issue(client, headers, ws, lid, "Planned", priority="high")
        loose = pm.issue(client, headers, ws, lid, "Loose", priority="low")
        sprint = _sprint(client, headers, ws, space["id"], "S1", planned["id"])
        _block(client, headers, ws, loose)

        def titles(**params) -> list[str]:
            page = pm.ok(client.get(f"/workspaces/{ws}/issues", params=params,
                                    headers=headers))
            return sorted(record["title"] for record in page["records"])

        assert titles(sprint_id=sprint["id"]) == ["Planned"]
        assert titles(sprint_id="none") == ["Loose"]
        assert titles(blocked="true") == ["Loose"]
        assert titles(priority=["high", "urgent"]) == ["Planned"]
        assert titles(q=loose["key"]) == ["Loose"]
        assert titles(type=["task"], assignee_id="none") == ["Loose", "Planned"]
        record = pm.ok(client.get(f"/workspaces/{ws}/issues", params={"blocked": "true"},
                                  headers=headers))["records"][0]
        assert record["blocked_reason"] == "Waiting on a vendor"


class TestDeepLinks:
    def test_a_current_key_resolves_to_the_card_and_where_it_lives(self, client, db):
        """A deep link to `PAY-1` opens the issue; the answer says where it lives now."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], "Linked")
        found = pm.ok(client.get(f"/workspaces/{ws}/issue-keys/{issue['key']}",
                                 headers=headers))
        assert found["issue"]["id"] == issue["id"]
        assert (found["list_id"], found["space_id"]) == (board_list["id"], space["id"])
        assert (found["current_key"], found["resolved_via"]) == (issue["key"], "current")

    def test_old_keys_resolve_through_their_aliases(self, client, db):
        """A space renamed from PYM to PAY, and an issue that used to be OLD-7: links that
        quote either keep working, and say which key to redirect to."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"], "Moved")
        db.add(SpaceKeyAlias(workspace_id=ws, space_id=space["id"], key="PYM"))
        db.add(IssueKeyAlias(workspace_id=ws, issue_id=issue["id"], space_id=space["id"],
                             space_key="OLD", number=7))
        db.commit()
        number = issue["key"].split("-")[1]
        for key, how in ((f"PYM-{number}", "space_alias"), ("OLD-7", "issue_alias"),
                         ("old-7", "issue_alias")):
            found = pm.ok(client.get(f"/workspaces/{ws}/issue-keys/{key}", headers=headers))
            assert found["issue"]["id"] == issue["id"], key
            assert (found["current_key"], found["resolved_via"]) == (issue["key"], how)

    def test_unknown_malformed_and_foreign_keys_are_404(self, client, db):
        """Nothing that is not a key of this workspace resolves — including a real key of
        another tenant, which must look exactly like one that does not exist."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        pm.issue(client, headers, ws, board_list["id"])
        other_headers, other_ws, _other_space, other_list = pm.setup_board(
            client, db, "bob", "bravo", "BRA")
        theirs = pm.issue(client, other_headers, other_ws, other_list["id"])
        for key in ("PAY-99", "nonsense", "PAY-0", "P-1", "PAY-1x", "PAY-", "-1",
                    theirs["key"]):
            response = client.get(f"/workspaces/{ws}/issue-keys/{key}", headers=headers)
            assert response.status_code == 404, key

    def test_a_guest_may_follow_a_link(self, client, db):
        """Reading by key is a read: guests follow links like everyone else."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        assert client.get(f"/workspaces/{ws}/issue-keys/{issue['key']}",
                          headers=guest).status_code == 200
