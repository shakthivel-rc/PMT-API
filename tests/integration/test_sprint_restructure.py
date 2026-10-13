"""Sprints under the restructuring: canceled work, and archived and frozen projects.

docs/design/09-restructuring.md §9 and §3.4. Two rules, each with the numbers it protects:

* **Canceled is closed but not delivered.** Closing a sprint keeps a canceled issue in it,
  does not carry it over and does not count its points as completed; the commitment leaves
  out an issue that was already canceled; the sprint report gives it its own outcome; and the
  burndown takes it out of the scope at the instant it was canceled. `completed_points` is
  frozen at the close and read by the velocity chart for good, so a wrong number here is
  wrong for ever.
* **A locked project refuses every sprint write with 423** — create, change, delete, start,
  close, and moving issues in or out, where an issue's own archived list or folder refuses
  it too. Reads keep working. A frozen space is one a migration wave is moving; a sprint
  write under it changes what the wave reconciles against.

The canceled stage is inserted through the `db` fixture and issues reach it through the
board move; a project is frozen or archived by setting its columns the same way.
"""
from datetime import timedelta

import pytest

from models.hierarchy import ProjectList, Space, WorkflowStatus
from models.issue import Issue, IssueEvent
from models.sprint import Sprint, SprintIssue
from tests import pm_fixtures as pm
from utils.time_tracking import utc_now

pytestmark = pytest.mark.integration


def _canceled_stage(db, ws: str, space_id: str) -> str:
    last = (db.query(WorkflowStatus.position)
            .filter(WorkflowStatus.space_id == space_id)
            .order_by(WorkflowStatus.position.desc()).first())
    row = WorkflowStatus(workspace_id=ws, space_id=space_id, name="Canceled",
                         category="canceled", position=last[0] + 1, color="neutral",
                         counts_toward_wip=False, deleted=0)
    db.add(row)
    db.commit()
    return row.id


def _move(client, headers, ws: str, issue_id: str, status_id: str) -> dict:
    return pm.ok(client.post(f"/workspaces/{ws}/issues/{issue_id}/move",
                             json={"status_id": status_id}, headers=headers))


def _sprint(client, headers, ws: str, space_id: str, name: str = "Sprint 1",
            issues: tuple = ()) -> dict:
    sprint = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space_id}/sprints",
                               json={"name": name}, headers=headers), 201)
    if issues:
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                          json={"issue_ids": [i["id"] for i in issues]}, headers=headers))
    return sprint


def _start(client, headers, ws: str, sprint_id: str) -> dict:
    return pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint_id}/start", json={},
                             headers=headers))


def _kpis(data: dict) -> dict:
    return {k["key"]: k["value"] for k in data["kpis"]}


def _rewind_start(db, sprint_id: str, hours: int = 1) -> None:
    """Move the sprint's start — and the memberships and issues it began with — back, so a
    change made now is clearly after it. A change within a moment of the start counts as
    part of it (`_SAME_ACT`), and the burndown only counts an issue from its creation."""
    sprint = db.get(Sprint, sprint_id)
    sprint.started_at -= timedelta(hours=hours)
    for member in db.query(SprintIssue).filter(SprintIssue.sprint_id == sprint_id):
        member.added_at -= timedelta(hours=hours)
        issue = db.get(Issue, member.issue_id)
        issue.created_at -= timedelta(hours=hours)
    db.commit()


def _team(client, db):
    """A space with a Canceled stage and three issues: A (5 points), B (3), C (2)."""
    owner, ws, space, board_list = pm.setup_board(client, db)
    canceled = _canceled_stage(db, ws, space["id"])
    a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=5)
    b = pm.issue(client, owner, ws, board_list["id"], title="B", story_points=3)
    c = pm.issue(client, owner, ws, board_list["id"], title="C", story_points=2)
    return owner, ws, space, board_list, canceled, a, b, c


class TestCloseWithCanceledWork:
    def test_canceled_issues_stay_are_not_carried_over_and_do_not_count(self, client, db):
        """A done, B canceled, C open; close carrying the unfinished to Sprint 2. Before the
        category, B was "not done", so it was carried into Sprint 2 as if it were still to
        be done — two carried over, not one — and the sprint looked as if it failed it."""
        owner, ws, space, _list, canceled, a, b, c = _team(client, db)
        sprint = _sprint(client, owner, ws, space["id"], issues=(a, b, c))
        _start(client, owner, ws, sprint["id"])
        _move(client, owner, ws, a["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, b["id"], canceled)

        live = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}", headers=owner))
        assert (live["issue_count"], live["done_issue_count"],
                live["canceled_issue_count"]) == (3, 1, 1)
        assert live["done_points"] == 5

        nxt = _sprint(client, owner, ws, space["id"], "Sprint 2")
        closed = pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close",
                                   json={"carry_over": "sprint", "target_sprint_id": nxt["id"]},
                                   headers=owner))
        assert closed["completed_issues"] == 1 and closed["canceled_issues"] == 1
        assert closed["carried_over"] == 1 and closed["completed_points"] == 5
        assert closed["sprint"]["completed_points"] == 5
        assert (closed["sprint"]["issue_count"], closed["sprint"]["done_issue_count"],
                closed["sprint"]["canceled_issue_count"]) == (2, 1, 1)

        kept = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/issues",
                                headers=owner))
        assert {card["id"] for card in kept} == {a["id"], b["id"]}
        carried = pm.ok(client.get(f"/workspaces/{ws}/sprints/{nxt['id']}/issues",
                                   headers=owner))
        assert [card["id"] for card in carried] == [c["id"]]
        # B's membership was never ended: it is still a member of the closed sprint.
        [b_row] = db.query(SprintIssue).filter(SprintIssue.issue_id == b["id"]).all()
        assert b_row.sprint_id == sprint["id"] and b_row.removed_at is None

    def test_an_issue_canceled_before_the_start_is_not_committed(self, client, db):
        """Committing work nobody will do lowers every "completed of committed" for good,
        and the burndown, which leaves canceled work out of its scope, would start from a
        different number than the commitment. One canceled DURING the sprint stays
        committed — that is the record of a commitment not kept."""
        owner, ws, space, _list, canceled, a, b, c = _team(client, db)
        sprint = _sprint(client, owner, ws, space["id"], issues=(a, b, c))
        _move(client, owner, ws, c["id"], canceled)
        started = _start(client, owner, ws, sprint["id"])
        assert started["committed_points"] == 8

        _move(client, owner, ws, b["id"], canceled)
        _move(client, owner, ws, a["id"], pm.status_id(space, "Done"))
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close", json={},
                          headers=owner))
        velocity = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/velocity",
                                    headers=owner))
        [row] = velocity["rows"]
        assert (row["committed"], row["completed"]) == (8, 5)
        assert row["pct"] == 62.5

    def test_velocity_completes_only_done_work(self, client, db):
        """Two sprints: each closes with one done and one canceled issue. The canceled
        points are in neither sprint's completed number."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        canceled = _canceled_stage(db, ws, space["id"])
        for name, (done_points, dropped_points) in (("S1", (5, 3)), ("S2", (2, 8))):
            done = pm.issue(client, owner, ws, board_list["id"], title=f"{name} done",
                            story_points=done_points)
            dropped = pm.issue(client, owner, ws, board_list["id"], title=f"{name} dropped",
                               story_points=dropped_points)
            sprint = _sprint(client, owner, ws, space["id"], name, (done, dropped))
            _start(client, owner, ws, sprint["id"])
            _move(client, owner, ws, done["id"], pm.status_id(space, "Done"))
            _move(client, owner, ws, dropped["id"], canceled)
            pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close", json={},
                              headers=owner))
        data = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/velocity",
                                headers=owner))
        rows = {r["sprint"]: (r["committed"], r["completed"]) for r in data["rows"]}
        assert rows == {"S1": (8, 5), "S2": (10, 2)}
        assert _kpis(data)["average"] == 3.5


def _assert_outcomes(client, headers, ws: str, sprint_id: str) -> None:
    """A done, B canceled, C open — whether the sprint is still running or closed."""
    data = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint_id}/report", headers=headers))
    sections = {s["key"]: [r["title"] for r in s["rows"]] for s in data["sections"]}
    assert sections["completed"] == ["A"]
    assert sections["canceled"] == ["B"]
    assert sections["not_completed"] == ["C"]
    assert sections["removed"] == []
    kpis = _kpis(data)
    assert (kpis["completed"], kpis["canceled"], kpis["not_completed"]) == (1, 1, 1)
    assert kpis["completed_points"] == 5
    assert {r["title"]: r["outcome"] for r in data["rows"]}["B"] == "Canceled"
    assert [r["title"] for r in data["rows"]] == ["A", "C", "B"]


class TestSprintReportsWithCanceledWork:
    def test_the_sprint_report_names_canceled_work_as_its_own_outcome(self, client, db):
        """Completed, not completed, canceled: three sections, three counts. As "not
        completed" a canceled issue reads as work the sprint failed to finish; as
        "completed" it would be work nobody delivered. The closed sprint's report says the
        same — the canceled issue stayed in it — with the frozen completed points."""
        owner, ws, space, _list, canceled, a, b, c = _team(client, db)
        sprint = _sprint(client, owner, ws, space["id"], issues=(a, b, c))
        _start(client, owner, ws, sprint["id"])
        _move(client, owner, ws, a["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, b["id"], canceled)
        _assert_outcomes(client, owner, ws, sprint["id"])
        pm.ok(client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/close", json={},
                          headers=owner))
        _assert_outcomes(client, owner, ws, sprint["id"])

    def test_canceling_takes_an_issue_out_of_the_burndown_at_that_instant(self, client, db):
        """A (5) and B (3) start the sprint; B is canceled an hour later. Remaining falls to
        5 without anything burned, the scope-change list says why, and the burnup's scope
        falls with it. Before, B stayed in the scope as 3 points never to be done, and the
        burndown could not reach zero."""
        owner, ws, space, _list, canceled, a, b, _c = _team(client, db)
        sprint = _sprint(client, owner, ws, space["id"], issues=(a, b))
        _start(client, owner, ws, sprint["id"])
        _rewind_start(db, sprint["id"])
        _move(client, owner, ws, b["id"], canceled)

        down = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/burndown",
                                headers=owner))
        kpis = _kpis(down)
        assert kpis["start"] == 8 and kpis["remaining"] == 5
        assert kpis["canceled"] == 3 and kpis["removed"] == 0 and kpis["added"] == 0
        today = down["rows"][0]
        assert today["remaining"] == 5 and today["completed"] == 0
        [change] = next(s["rows"] for s in down["sections"] if s["key"] == "scope_changes")
        assert (change["issue"], change["change"], change["value"]) == (b["key"], "Canceled",
                                                                       -3)
        count = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/burndown",
                                 params={"unit": "count"}, headers=owner))
        assert _kpis(count)["remaining"] == 1

        up = pm.ok(client.get(f"/workspaces/{ws}/reports/burnup",
                              params={"scope": f"sprint:{sprint['id']}"}, headers=owner))
        assert (up["rows"][0]["scope"], up["rows"][0]["completed"]) == (5, 0)

    def test_moving_out_of_canceled_puts_it_back_in_scope(self, client, db):
        """Canceled by mistake and restored: the scope steps down and back up, and the
        change list has both steps, so the line is explained either way."""
        owner, ws, space, _list, canceled, a, b, _c = _team(client, db)
        sprint = _sprint(client, owner, ws, space["id"], issues=(a, b))
        _start(client, owner, ws, sprint["id"])
        _rewind_start(db, sprint["id"])
        _move(client, owner, ws, b["id"], canceled)
        cancel_event = (db.query(IssueEvent)
                        .filter(IssueEvent.issue_id == b["id"],
                                IssueEvent.event == "status_changed").one())
        cancel_event.created_at = utc_now() - timedelta(minutes=30)
        db.commit()
        _move(client, owner, ws, b["id"], pm.status_id(space, "To Do"))

        down = pm.ok(client.get(f"/workspaces/{ws}/sprints/{sprint['id']}/burndown",
                                headers=owner))
        changes = next(s["rows"] for s in down["sections"] if s["key"] == "scope_changes")
        assert [(c["change"], c["value"]) for c in changes] == [("Canceled", -3),
                                                                ("Restored", 3)]
        assert _kpis(down)["remaining"] == 8 and _kpis(down)["canceled"] == 0


def _lock(db, space_id: str, how: str) -> None:
    space = db.get(Space, space_id)
    if how == "frozen":
        space.frozen_at, space.freeze_reason = utc_now(), "wave 1"
    else:
        space.archived_at = utc_now()
    db.commit()


def _unlock(db, space_id: str) -> None:
    space = db.get(Space, space_id)
    space.frozen_at = space.freeze_reason = space.archived_at = None
    db.commit()


def _archive_list(db, list_id: str) -> None:
    db.get(ProjectList, list_id).archived_at = utc_now()
    db.commit()


class TestLockedProjects:
    @pytest.mark.parametrize("how", ["frozen", "archived"])
    def test_every_sprint_write_is_refused_and_nothing_changes(self, client, db, how):
        """Each write that would otherwise succeed (or, for start, answer about the state)
        is 423 and names the project; afterwards the sprints and their memberships are
        exactly as they were. Without the guard every one of them writes."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=1)
        b = pm.issue(client, owner, ws, board_list["id"], title="B", story_points=1)
        c = pm.issue(client, owner, ws, board_list["id"], title="C")
        active = _sprint(client, owner, ws, space["id"], "Active", (a,))
        _start(client, owner, ws, active["id"])
        planned = _sprint(client, owner, ws, space["id"], "Planned", (b,))
        before = sorted((r.sprint_id, r.issue_id, r.removed_at)
                        for r in db.query(SprintIssue).all())
        _lock(db, space["id"], how)

        base = f"/workspaces/{ws}"
        for method, path, body in (
            ("POST", f"{base}/spaces/{space['id']}/sprints", {"name": "New"}),
            ("PATCH", f"{base}/sprints/{planned['id']}", {"name": "Renamed"}),
            ("DELETE", f"{base}/sprints/{planned['id']}", None),
            ("POST", f"{base}/sprints/{planned['id']}/start", {}),
            ("POST", f"{base}/sprints/{active['id']}/close", {}),
            ("POST", f"{base}/sprints/{planned['id']}/issues", {"issue_ids": [c["id"]]}),
            ("DELETE", f"{base}/sprints/{planned['id']}/issues", {"issue_ids": [b["id"]]}),
        ):
            response = client.request(method, path, json=body, headers=owner)
            assert response.status_code == 423, (method, path, response.text)
            assert space["name"] in response.json()["message"], (method, path)

        db.expire_all()
        assert db.query(Sprint).filter(Sprint.deleted == 0).count() == 2
        assert db.get(Sprint, planned["id"]).name == "Planned"
        assert db.get(Sprint, planned["id"]).state == "planned"
        assert db.get(Sprint, active["id"]).state == "active"
        assert sorted((r.sprint_id, r.issue_id, r.removed_at)
                      for r in db.query(SprintIssue).all()) == before
        # Reads are never refused: an archived project's sprints are history to look at.
        for path in (f"{base}/spaces/{space['id']}/sprints", f"{base}/sprints/{active['id']}",
                     f"{base}/sprints/{active['id']}/issues",
                     f"{base}/sprints/{active['id']}/report",
                     f"{base}/sprints/{active['id']}/burndown"):
            assert client.get(path, headers=owner).status_code == 200, path

    def test_starting_a_sprint_in_a_frozen_project_waits_for_the_unfreeze(self, client, db):
        """A start that would succeed is refused while the project is frozen — the
        commitment it freezes would be computed from issues the wave is moving — and goes
        through once the freeze is lifted."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        a = pm.issue(client, owner, ws, board_list["id"], title="A", story_points=2)
        sprint = _sprint(client, owner, ws, space["id"], issues=(a,))
        _lock(db, space["id"], "frozen")
        refused = client.post(f"/workspaces/{ws}/sprints/{sprint['id']}/start", json={},
                              headers=owner)
        assert refused.status_code == 423
        assert "frozen" in refused.json()["message"] and "wave 1" in refused.json()["message"]
        db.expire_all()
        assert db.get(Sprint, sprint["id"]).state == "planned"
        _unlock(db, space["id"])
        assert _start(client, owner, ws, sprint["id"])["committed_points"] == 2

    def test_an_archived_list_locks_its_issues_sprint_membership(self, client, db):
        """The project is writable, but one issue's list is archived: adding it to a sprint,
        or taking it out, is a write to that issue and is refused, naming it — and the whole
        batch is refused, so a drag never half-lands. Issues of other lists still move."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        archived = pm.task_list(client, owner, ws, space["id"], "Old work")
        live = pm.issue(client, owner, ws, board_list["id"], title="Live")
        stuck = pm.issue(client, owner, ws, archived["id"], title="Stuck")
        kept = pm.issue(client, owner, ws, archived["id"], title="Kept")
        sprint = _sprint(client, owner, ws, space["id"], issues=(kept,))
        _archive_list(db, archived["id"])

        base = f"/workspaces/{ws}/sprints/{sprint['id']}/issues"
        added = client.post(base, json={"issue_ids": [live["id"], stuck["id"]]},
                            headers=owner)
        assert added.status_code == 423, added.text
        assert added.json()["data"]["issue_id"] == stuck["id"]
        assert stuck["key"] in added.json()["message"]
        assert "Old work" in added.json()["message"]
        removed = client.request("DELETE", base, json={"issue_ids": [kept["id"]]},
                                 headers=owner)
        assert removed.status_code == 423
        db.expire_all()
        assert {r.issue_id for r in db.query(SprintIssue).filter(
            SprintIssue.removed_at.is_(None))} == {kept["id"]}
        assert pm.ok(client.post(base, json={"issue_ids": [live["id"]]},
                                 headers=owner))["changed"] == [live["id"]]

    def test_carrying_an_archived_issue_into_another_sprint_is_refused(self, client, db):
        """Carrying over is adding to the target sprint, so it asks what adding asks — and
        says so rather than quietly sending that issue to the backlog instead. Closing to the
        backlog only ends memberships of the closing sprint, which is its own bookkeeping,
        and works."""
        owner, ws, space, _list = pm.setup_board(client, db)
        archived = pm.task_list(client, owner, ws, space["id"], "Old work")
        stuck = pm.issue(client, owner, ws, archived["id"], title="Stuck")
        sprint = _sprint(client, owner, ws, space["id"], issues=(stuck,))
        _start(client, owner, ws, sprint["id"])
        nxt = _sprint(client, owner, ws, space["id"], "Sprint 2")
        _archive_list(db, archived["id"])

        close = f"/workspaces/{ws}/sprints/{sprint['id']}/close"
        refused = client.post(close, json={"carry_over": "sprint",
                                           "target_sprint_id": nxt["id"]}, headers=owner)
        assert refused.status_code == 423, refused.text
        assert refused.json()["data"]["issue_id"] == stuck["id"]
        assert "Sprint 2" in refused.json()["message"]
        db.expire_all()
        assert db.get(Sprint, sprint["id"]).state == "active"

        closed = pm.ok(client.post(close, json={"carry_over": "backlog"}, headers=owner))
        assert closed["carried_over"] == 1
        assert pm.ok(client.get(f"/workspaces/{ws}/sprints/{nxt['id']}/issues",
                                headers=owner)) == []
