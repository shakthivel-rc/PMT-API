"""The space's backlog and its order, through the HTTP API (docs/design/09-restructuring.md
§5.2).

The backlog is the plan's "one ordered backlog per delivery unit": open story, task and bug
work of a space that is in no sprint, in `backlog_rank` order — a key over the whole space,
kept apart from the board's per-column `rank`. What is pinned here:

* **what is in it and in what order** — and what is not: subtasks, epics, closed work and
  sprint members are elsewhere in the same answer, or nowhere;
* **the sprints come first**, active then planned oldest first, each in backlog order;
* **at most 500 cards**, with the full count and a `truncated` flag rather than a silent cut;
* **reordering** with the board move's stale-neighbour rule, without bumping `version`, and
  re-spreading the space when keys grow too long.

Large backlogs and long rank keys are written through the `db` fixture: five hundred HTTP
creates would test the create route five hundred times and the backlog once.
"""
import pytest

from models.issue import Issue, IssueEvent
from tests import pm_fixtures as pm
from utils.rank import REBALANCE_LENGTH, rank_after

pytestmark = pytest.mark.integration

OWNER = "owner-1"


def _backlog(client, headers, ws, space_id: str, **params) -> dict:
    return pm.ok(client.get(f"/workspaces/{ws}/spaces/{space_id}/backlog", params=params,
                            headers=headers))


def _titles(cards: list[dict]) -> list[str]:
    return [card["title"] for card in cards]


def _rank(client, headers, ws, issue_id: str, **body):
    return client.post(f"/workspaces/{ws}/issues/{issue_id}/backlog-rank", json=body,
                       headers=headers)


def _sprint(client, headers, ws, space_id: str, name: str, *issue_ids: str) -> dict:
    sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space_id}/sprints",
                               json={"name": name}, headers=headers), 201)
    if issue_ids:
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": list(issue_ids)}, headers=headers))
    return sprint


def _epic(client, headers, ws, list_id: str, title: str = "Epic") -> dict:
    return pm.issue(client, headers, ws, list_id, title, type="epic", assignee_id=OWNER,
                    completion_criteria="The outcome is met.")


class TestBacklogContents:
    def test_open_delivery_work_in_no_sprint_in_backlog_order(self, client, db):
        """Story, task and bug items, open, unscheduled, in creation order until someone
        reorders them; epics beside it for filtering; subtasks, sprint members and closed
        work not in it — unless `include_closed` asks for closed work. Would catch the
        backlog drawing the wrong set, or in board-rank order."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        story = pm.issue(client, headers, ws, lid, "Story", type="story",
                         parent_id=epic["id"])
        pm.issue(client, headers, ws, lid, "Subtask", type="subtask", parent_id=story["id"])
        pm.issue(client, headers, ws, lid, "Task", type="task")
        bug = pm.issue(client, headers, ws, lid, "Bug", type="bug")
        done = pm.issue(client, headers, ws, lid, "Shipped", type="task")
        pm.ok(client.post(f"/workspaces/{ws}/issues/{done['id']}/move",
                          json={"status_id": pm.status_id(space, "Done")}, headers=headers))
        planned = pm.issue(client, headers, ws, lid, "Planned", type="story")
        _sprint(client, headers, ws, space["id"], "S1", planned["id"])
        # A board move into another column must not reorder the backlog.
        pm.ok(client.post(f"/workspaces/{ws}/issues/{bug['id']}/move",
                          json={"status_id": pm.status_id(space, "In Progress")},
                          headers=headers))

        backlog = _backlog(client, headers, ws, space["id"])
        assert backlog["space_id"] == space["id"]
        assert _titles(backlog["backlog"]["cards"]) == ["Story", "Task", "Bug"]
        assert backlog["backlog"]["total"] == 3 and backlog["backlog"]["truncated"] is False
        assert _titles(backlog["epics"]) == ["Epic"]
        assert [_titles(s["cards"]) for s in backlog["sprints"]] == [["Planned"]]

        closed_too = _backlog(client, headers, ws, space["id"], include_closed="true")
        assert _titles(closed_too["backlog"]["cards"]) == ["Story", "Task", "Bug", "Shipped"]
        assert _titles(closed_too["epics"]) == ["Epic"]

    def test_sprints_come_first_active_then_planned_oldest_first(self, client, db):
        """The order of Jira's backlog, and of a planning meeting: what is running, then
        what is next. Closed sprints are history, not planning. Cards inside a sprint are in
        backlog order, so the team's priority reads the same everywhere."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        a, b, c, d = (pm.issue(client, headers, ws, lid, title) for title in "ABCD")
        first = _sprint(client, headers, ws, space["id"], "First", a["id"])
        second = _sprint(client, headers, ws, space["id"], "Second", c["id"], b["id"])
        third = _sprint(client, headers, ws, space["id"], "Third", d["id"])
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{third['id']}/start", json={},
                          headers=headers))
        pm.ok(_rank(client, headers, ws, c["id"], before_id=b["id"]))

        backlog = _backlog(client, headers, ws, space["id"])
        assert [s["sprint"]["name"] for s in backlog["sprints"]] == ["Third", "First",
                                                                     "Second"]
        assert backlog["sprints"][0]["sprint"]["state"] == "active"
        assert backlog["sprints"][2]["sprint"]["issue_count"] == 2
        assert _titles(backlog["sprints"][2]["cards"]) == ["C", "B"]
        assert backlog["backlog"]["cards"] == []

        pm.ok(client.post(f"/workspaces/{ws}/sprints/{third['id']}/close", json={},
                          headers=headers))
        after_close = _backlog(client, headers, ws, space["id"])
        assert [s["sprint"]["id"] for s in after_close["sprints"]] == [first["id"],
                                                                      second["id"]]
        assert _titles(after_close["backlog"]["cards"]) == ["D"]

    def test_the_filters_narrow_the_sprints_and_the_backlog_but_not_the_epics(self, client,
                                                                               db):
        """A quick filter applies to the whole planning view, or planning by it misleads;
        the epics are the list `epic_id` is picked from, so they are never filtered away."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        lid = board_list["id"]
        epic = _epic(client, headers, ws, lid)
        other_epic = _epic(client, headers, ws, lid, "Other epic")
        inside = pm.issue(client, headers, ws, lid, "Inside", type="story",
                          parent_id=epic["id"])
        pm.issue(client, headers, ws, lid, "Outside", type="story",
                 parent_id=other_epic["id"])
        sprinted = pm.issue(client, headers, ws, lid, "Sprinted inside", type="bug",
                            parent_id=epic["id"])
        elsewhere = pm.issue(client, headers, ws, lid, "Sprinted outside", type="bug")
        _sprint(client, headers, ws, space["id"], "S1", sprinted["id"], elsewhere["id"])

        narrowed = _backlog(client, headers, ws, space["id"], epic_id=epic["id"])
        assert _titles(narrowed["backlog"]["cards"]) == ["Inside"]
        assert narrowed["backlog"]["total"] == 1
        assert _titles(narrowed["sprints"][0]["cards"]) == ["Sprinted inside"]
        assert _titles(narrowed["epics"]) == ["Epic", "Other epic"]
        by_key = _backlog(client, headers, ws, space["id"], q=inside["key"])
        assert _titles(by_key["backlog"]["cards"]) == ["Inside"]
        assert _backlog(client, headers, ws, space["id"],
                        type=["bug"])["backlog"]["cards"] == []

    def test_at_most_500_cards_with_the_full_count(self, client, db):
        """A backlog of 501 returns the top 500 and says there is more — a silent cut would
        let a planner believe the last item they see is the last there is."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        todo = pm.status_id(space, "To Do")
        backlog_rank = rank = None
        for number in range(1, 502):
            rank = rank_after(rank)
            backlog_rank = rank_after(backlog_rank)
            db.add(Issue(workspace_id=ws, space_id=space["id"], list_id=board_list["id"],
                         number=number, type="task", title=f"Item {number:03d}",
                         status_id=todo, reporter_id=OWNER, priority="none", rank=rank,
                         backlog_rank=backlog_rank, version=1, deleted=0))
        db.commit()

        backlog = _backlog(client, headers, ws, space["id"])["backlog"]
        assert len(backlog["cards"]) == 500
        assert backlog["total"] == 501 and backlog["truncated"] is True
        assert backlog["cards"][0]["title"] == "Item 001"
        assert backlog["cards"][-1]["title"] == "Item 500"
        narrowed = _backlog(client, headers, ws, space["id"], q="Item 50")["backlog"]
        assert narrowed["total"] == 2 and narrowed["truncated"] is False

    def test_an_unknown_space_is_404_and_a_guest_may_read(self, client, db):
        """A read like the board: guests plan nothing but see the plan."""
        headers, ws, space, _board_list = pm.setup_board(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        assert client.get(f"/workspaces/{ws}/spaces/nope/backlog",
                          headers=headers).status_code == 404
        assert client.get(f"/workspaces/{ws}/spaces/{space['id']}/backlog",
                          headers=guest).status_code == 200


class TestBacklogRank:
    def _three(self, client, db):
        headers, ws, space, board_list = pm.setup_board(client, db)
        issues = [pm.issue(client, headers, ws, board_list["id"], title, type="story")
                  for title in ("A", "B", "C")]
        return headers, ws, space, board_list, issues

    def _order(self, client, headers, ws, space_id) -> list[str]:
        return _titles(_backlog(client, headers, ws, space_id)["backlog"]["cards"])

    def test_after_before_and_bottom(self, client, db):
        """The three ways a drop is described: under a card, above one, or at the end."""
        headers, ws, space, _list, (a, b, c) = self._three(client, db)
        moved = pm.ok(_rank(client, headers, ws, c["id"], after_id=a["id"]))
        assert moved["id"] == c["id"]
        assert self._order(client, headers, ws, space["id"]) == ["A", "C", "B"]
        pm.ok(_rank(client, headers, ws, b["id"], before_id=a["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["B", "A", "C"]
        pm.ok(_rank(client, headers, ws, b["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["A", "C", "B"]

    def test_a_stale_neighbour_does_not_break_the_drop(self, client, db):
        """The client thinks the order is A, B, but someone already put C between them.
        Dropping X "between A and B" must land directly under A — the anchor — rather than
        fail on keys that no longer bracket a gap. The same rule as the board's move."""
        headers, ws, space, board_list, (a, b, c) = self._three(client, db)
        x = pm.issue(client, headers, ws, board_list["id"], "X", type="task")
        pm.ok(_rank(client, headers, ws, c["id"], after_id=a["id"], before_id=b["id"]))
        pm.ok(_rank(client, headers, ws, x["id"], after_id=a["id"], before_id=b["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["A", "X", "C", "B"]
        # Only a card below is named, and it has moved: the drop lands right above it.
        pm.ok(_rank(client, headers, ws, a["id"], before_id=b["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["X", "C", "A", "B"]
        # The worst case: the card named below now sits ABOVE the anchor, so the two keys
        # the client sent bracket no gap at all. The anchor still decides.
        pm.ok(_rank(client, headers, ws, b["id"], before_id=x["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["B", "X", "C", "A"]
        pm.ok(_rank(client, headers, ws, c["id"], after_id=a["id"], before_id=b["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["B", "X", "A", "C"]

    def test_neighbours_that_are_gone_or_elsewhere_are_ignored(self, client, db):
        """A neighbour deleted since the client drew the list, or one in another space, is
        not an anchor; the drop falls back to the other one, or to the bottom."""
        headers, ws, space, _list, (a, b, c) = self._three(client, db)
        other = pm.space(client, headers, ws, "OPS")
        other_list = pm.task_list(client, headers, ws, other["id"])
        foreign = pm.issue(client, headers, ws, other_list["id"], "Foreign")
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{b['id']}", headers=headers))
        pm.ok(_rank(client, headers, ws, a["id"], after_id=b["id"], before_id=c["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["A", "C"]
        pm.ok(_rank(client, headers, ws, c["id"], after_id=foreign["id"]))
        assert self._order(client, headers, ws, space["id"]) == ["A", "C"]
        assert _rank(client, headers, ws, "nope").status_code == 404

    def test_ranking_is_planning_not_an_edit(self, client, db):
        """Reordering the backlog must not hand someone with the issue open a 409, and is
        not history: no version bump, no event — like sprint membership."""
        headers, ws, _space, _list, (a, b, _c) = self._three(client, db)
        before = db.query(IssueEvent).count()
        moved = pm.ok(_rank(client, headers, ws, b["id"], before_id=a["id"]))
        assert moved["version"] == b["version"]
        assert db.query(IssueEvent).count() == before
        # The board's order is untouched: the two orders are separate keys.
        assert moved["rank"] == b["rank"]
        stale_edit = client.patch(f"/workspaces/{ws}/issues/{b['id']}",
                                  json={"version": b["version"], "title": "Still mine"},
                                  headers=headers)
        assert stale_edit.status_code == 200

    def test_a_guest_cannot_reorder(self, client, db):
        """Planning is a member's act."""
        _headers, ws, _space, _list, (a, b, _c) = self._three(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        assert _rank(client, guest, ws, a["id"], after_id=b["id"]).status_code == 403

    def test_a_key_grown_too_long_re_spreads_the_whole_space(self, client, db):
        """Keys lengthen when drops keep landing in one gap; past `REBALANCE_LENGTH` the
        space's backlog is re-issued short keys in the same order, sprint members and closed
        work included, because they share the order."""
        headers, ws, space, board_list, (a, b, c) = self._three(client, db)
        planned = pm.issue(client, headers, ws, board_list["id"], "Planned")
        _sprint(client, headers, ws, space["id"], "S1", planned["id"])
        # A and B as close as two keys can be: the next key between them is longer than
        # the rebalance threshold.
        long_key = "i" + "0" * (REBALANCE_LENGTH - 1) + "1"
        db.query(Issue).filter(Issue.id == a["id"]).update({Issue.backlog_rank: "i"})
        db.query(Issue).filter(Issue.id == b["id"]).update({Issue.backlog_rank: long_key})
        db.query(Issue).filter(Issue.id == c["id"]).update({Issue.backlog_rank: "j"})
        db.query(Issue).filter(Issue.id == planned["id"]).update({Issue.backlog_rank: "k"})
        db.commit()

        pm.ok(_rank(client, headers, ws, c["id"], after_id=a["id"]))
        db.expire_all()
        rows = (db.query(Issue).filter(Issue.space_id == space["id"])
                .order_by(Issue.backlog_rank).all())
        assert [row.title for row in rows] == ["A", "C", "B", "Planned"]
        assert all(len(row.backlog_rank) < 4 for row in rows), [r.backlog_rank for r in rows]
