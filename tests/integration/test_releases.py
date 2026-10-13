"""Releases and fix versions, through the HTTP API (docs/design/09-restructuring.md §10).

The numbers a release shows — progress, and the sections of its notes — are computed from
the issues' stage *categories*, so the tests put issues in `done`, `canceled` and open stages
and check each lands in its own count. The default workflow has no canceled stage; tests that
need one add the row directly, because publishing a workflow is another feature's route and
the category is all that matters here.
"""
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

import db.session as db_session
from models.activity_log import ActivityLog
from models.hierarchy import ProjectList, Space, WorkflowStatus
from models.issue import Issue, IssueEvent
from models.release import IssueFixVersion, Release
from schemas.releases import FixVersionsSet, ReleaseCreate, ReleaseShip, ReleaseUpdate
from services import releases as release_svc
from services.releases import render_notes
from tests import pm_fixtures as pm
from utils.time_tracking import utc_now

pytestmark = pytest.mark.integration


def _setup(client, db):
    owner, ws, space, board_list = pm.setup_board(client, db)
    member = pm.person(db, "member-1")
    pm.join(db, ws, "member-1", "member")
    return owner, member, ws, space, board_list


def _release(client, headers, ws, space_id, name="2.4", status=201, **fields):
    response = client.post(f"/workspaces/{ws}/spaces/{space_id}/releases",
                           json={"name": name, **fields}, headers=headers)
    assert response.status_code == status, response.text
    return response.json()["data"]


def _get(client, headers, ws, release_id, status=200):
    response = client.get(f"/workspaces/{ws}/releases/{release_id}", headers=headers)
    assert response.status_code == status, response.text
    return response.json()["data"]


def _patch(client, headers, ws, release_id, status=200, **body):
    response = client.patch(f"/workspaces/{ws}/releases/{release_id}", json=body,
                            headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def _ship(client, headers, ws, release_id, status=200, **body):
    response = client.post(f"/workspaces/{ws}/releases/{release_id}/release", json=body,
                           headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def _fix(client, headers, ws, issue_id, *release_ids, status=200):
    response = client.put(f"/workspaces/{ws}/issues/{issue_id}/fix-versions",
                          json={"release_ids": list(release_ids)}, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def _fix_versions(client, headers, ws, issue_id):
    return pm.ok(client.get(f"/workspaces/{ws}/issues/{issue_id}/fix-versions",
                            headers=headers))


def _canceled_stage(db, ws, space_id) -> str:
    status = WorkflowStatus(workspace_id=ws, space_id=space_id, name="Won't do",
                            category="canceled", position=4, color="neutral",
                            counts_toward_wip=False, deleted=0)
    db.add(status)
    db.commit()
    return status.id


def _move(client, headers, ws, issue_id, status_id):
    pm.ok(client.post(f"/workspaces/{ws}/issues/{issue_id}/move",
                      json={"status_id": status_id}, headers=headers))


def _events(db, name):
    rows = db.query(IssueEvent).filter(IssueEvent.event == name).all()
    return {(row.issue_id, row.from_value, row.to_value) for row in rows}


class _QueryCounter:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        self._listener = lambda conn, cursor, statement, *a: self.statements.append(statement)
        event.listen(db_session.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(db_session.engine, "before_cursor_execute", self._listener)


class TestReleases:
    def test_the_documented_example_bodies_are_accepted(self):
        """Would catch: an example in `/docs` that the API itself refuses."""
        for model in (ReleaseCreate, ReleaseUpdate, ReleaseShip, FixVersionsSet):
            for example in model.model_config["json_schema_extra"]["examples"]:
                model.model_validate(example)

    def test_progress_counts_each_outcome_by_category(self, client, db):
        """Would catch: canceled work counted as done — the plan's "do not mark canceled
        work as successfully Done" — or a deleted issue still counted."""
        owner, member, ws, space, board_list = _setup(client, db)
        canceled = _canceled_stage(db, ws, space["id"])
        release = _release(client, owner, ws, space["id"], description="Refunds",
                           start_date="2026-10-05", release_date="2026-10-30")
        assert release["state"] == "unreleased" and release["released_at"] is None
        assert release["progress"] == {"issues": 0, "done": 0, "canceled": 0, "open": 0}

        lid = board_list["id"]
        shipped = pm.issue(client, owner, ws, lid, title="Card refunds", type="story")
        dropped = pm.issue(client, owner, ws, lid, title="Cheque refunds", type="task")
        doing = pm.issue(client, owner, ws, lid, title="Partial refunds", type="story")
        todo = pm.issue(client, owner, ws, lid, title="Refund emails", type="task")
        gone = pm.issue(client, owner, ws, lid, title="Duplicate", type="bug")
        for issue in (shipped, dropped, doing, todo, gone):
            _fix(client, member, ws, issue["id"], release["id"])
        _move(client, owner, ws, shipped["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, dropped["id"], canceled)
        _move(client, owner, ws, doing["id"], pm.status_id(space, "In Review"))
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{gone['id']}", headers=owner))

        [listed] = pm.ok(client.get(f"/workspaces/{ws}/spaces/{space['id']}/releases",
                                    headers=member))
        assert listed["progress"] == {"issues": 4, "done": 1, "canceled": 1, "open": 2}
        detail = _get(client, member, ws, release["id"])
        assert detail["progress"] == listed["progress"]
        assert [card["key"] for card in detail["issues"]] == ["PAY-1", "PAY-2", "PAY-3",
                                                              "PAY-4"]
        assert detail["truncated"] is False
        assert detail["description"] == "Refunds"
        assert detail["start_date"] == "2026-10-05" and detail["release_date"] == "2026-10-30"

    def test_the_detail_caps_its_cards_and_says_so(self, client, db, monkeypatch):
        """Would catch: a release's detail growing with the release without bound, or
        `truncated` claiming a complete list when it is not. The cap is lowered rather than
        500 issues created; the rule is the same."""
        owner, member, ws, space, board_list = _setup(client, db)
        monkeypatch.setattr(release_svc, "MAX_DETAIL_ISSUES", 2)
        release = _release(client, owner, ws, space["id"])
        for title in ("a", "b", "c"):
            issue = pm.issue(client, owner, ws, board_list["id"], title=title)
            _fix(client, member, ws, issue["id"], release["id"])
        detail = _get(client, member, ws, release["id"])
        assert [card["key"] for card in detail["issues"]] == ["PAY-1", "PAY-2"]
        assert detail["truncated"] is True and detail["progress"]["issues"] == 3

    def test_names_are_unique_in_a_space_ignoring_case(self, client, db):
        """Would catch: two releases both called 2.4 in one space — every fix-version
        picker would offer the same name twice with nothing to tell them apart."""
        owner, _member, ws, space, _list = _setup(client, db)
        other = pm.space(client, owner, ws, "OPS")
        first = _release(client, owner, ws, space["id"], name="v2.4")
        _release(client, owner, ws, space["id"], name="V2.4", status=409)
        _release(client, owner, ws, space["id"], name="  v2.4  ", status=409)
        _release(client, owner, ws, other["id"], name="v2.4")
        second = _release(client, owner, ws, space["id"], name="v2.5")
        _patch(client, owner, ws, second["id"], 409, name="V2.4")
        assert _patch(client, owner, ws, first["id"], name="V2.4")["data"]["name"] == "V2.4"

        pm.ok(client.delete(f"/workspaces/{ws}/releases/{first['id']}", headers=owner))
        _release(client, owner, ws, space["id"], name="v2.4")

    def test_a_name_is_never_blank(self, client, db):
        """Would catch: a release called three spaces, which `min_length` alone accepts, or a PATCH
        clearing the name with null."""
        owner, _member, ws, space, _list = _setup(client, db)
        _release(client, owner, ws, space["id"], name="   ", status=422)
        release = _release(client, owner, ws, space["id"])
        _patch(client, owner, ws, release["id"], 422, name="  ")
        _patch(client, owner, ws, release["id"], 422, name=None)

    def test_the_release_date_is_never_before_the_start(self, client, db):
        """On create, and on an edit that moves either date — checked against the date the
        edit leaves alone, which a schema-only check cannot see."""
        owner, _member, ws, space, _list = _setup(client, db)
        _release(client, owner, ws, space["id"], start_date="2026-10-10",
                 release_date="2026-10-01", status=422)
        release = _release(client, owner, ws, space["id"], start_date="2026-10-05",
                           release_date="2026-10-30")
        _patch(client, owner, ws, release["id"], 422, release_date="2026-10-01")
        _patch(client, owner, ws, release["id"], 422, start_date="2026-11-01")
        cleared = _patch(client, owner, ws, release["id"], start_date=None)["data"]
        assert cleared["start_date"] is None and cleared["release_date"] == "2026-10-30"

    def test_only_admins_manage_releases_and_guests_read_them(self, client, db):
        """Would catch: a member managing releases (§10 makes it an admin's act), a guest refused a
        read, or a guest setting fix versions — that is a member's act."""
        owner, member, ws, space, board_list = _setup(client, db)
        guest = pm.person(db, "guest-1")
        pm.join(db, ws, "guest-1", "guest")
        release = _release(client, owner, ws, space["id"])
        base = f"/workspaces/{ws}/releases/{release['id']}"
        _release(client, member, ws, space["id"], name="Mine", status=403)
        assert client.patch(base, json={"name": "x"}, headers=member).status_code == 403
        assert client.delete(base, headers=member).status_code == 403
        assert client.post(f"{base}/release", json={}, headers=member).status_code == 403
        for path in (f"/workspaces/{ws}/spaces/{space['id']}/releases", base, f"{base}/notes"):
            assert client.get(path, headers=guest).status_code == 200, path

        issue = pm.issue(client, owner, ws, board_list["id"])
        _fix(client, guest, ws, issue["id"], release["id"], status=403)
        _fix(client, member, ws, issue["id"], release["id"])
        assert [r["id"] for r in _fix_versions(client, guest, ws, issue["id"])] == [
            release["id"]]

    def test_the_list_is_ordered_and_filters_by_state(self, client, db):
        """Unreleased soonest first with undated last, then released newest first."""
        owner, _member, ws, space, _list = _setup(client, db)
        sid = space["id"]
        later = _release(client, owner, ws, sid, name="later", release_date="2026-12-01")
        undated = _release(client, owner, ws, sid, name="undated")
        sooner = _release(client, owner, ws, sid, name="sooner", release_date="2026-11-01")
        old = _release(client, owner, ws, sid, name="old")
        newer = _release(client, owner, ws, sid, name="newer")
        _ship(client, owner, ws, old["id"], release_date="2026-09-01")
        _ship(client, owner, ws, newer["id"], release_date="2026-09-15")
        path = f"/workspaces/{ws}/spaces/{sid}/releases"
        names = [r["name"] for r in pm.ok(client.get(path, headers=owner))]
        assert names == ["sooner", "later", "undated", "newer", "old"]
        released = pm.ok(client.get(path, params={"state": "released"}, headers=owner))
        assert [r["id"] for r in released] == [newer["id"], old["id"]]
        both = pm.ok(client.get(path, params={"state": "unreleased,archived"}, headers=owner))
        assert {r["id"] for r in both} == {later["id"], undated["id"], sooner["id"]}
        assert client.get(path, params={"state": "shipped"}, headers=owner).status_code == 422

    def test_state_moves_through_an_edit(self, client, db):
        """Archive and un-archive; undo a release; never *release* through an edit — that
        would skip the release date and the move of open work."""
        owner, _member, ws, space, _list = _setup(client, db)
        release = _release(client, owner, ws, space["id"])
        rid = release["id"]
        assert _patch(client, owner, ws, rid, state="archived")["data"]["state"] == "archived"
        refused = _patch(client, owner, ws, rid, 409, state="released")
        assert "POST" in refused["message"]
        assert _patch(client, owner, ws, rid, state="unreleased")["data"]["state"] == (
            "unreleased")

        _ship(client, owner, ws, rid, release_date="2026-10-20")
        undone = _patch(client, owner, ws, rid, state="unreleased")["data"]
        assert undone["state"] == "unreleased" and undone["released_at"] is None
        assert undone["release_date"] == "2026-10-20"

        shipped = _ship(client, owner, ws, rid)["data"]["release"]
        _patch(client, owner, ws, rid, state="archived")
        back = _patch(client, owner, ws, rid, state="released")["data"]
        assert back["state"] == "released" and back["released_at"] == shipped["released_at"]

    def test_delete_is_soft_and_refuses_what_has_shipped(self, client, db):
        """Would catch: a hard delete, a deleted release still offered on its issues, or a shipped
        release deleted — its notes and fix versions are history."""
        owner, member, ws, space, board_list = _setup(client, db)
        draft = _release(client, owner, ws, space["id"], name="draft")
        issue = pm.issue(client, owner, ws, board_list["id"])
        _fix(client, member, ws, issue["id"], draft["id"])
        assert pm.ok(client.delete(f"/workspaces/{ws}/releases/{draft['id']}",
                                   headers=owner)) == {"id": draft["id"]}
        _get(client, owner, ws, draft["id"], status=404)
        assert db.query(Release).filter(Release.id == draft["id"]).one().deleted == 1
        assert _fix_versions(client, owner, ws, issue["id"]) == []

        shipped = _release(client, owner, ws, space["id"], name="shipped")
        _ship(client, owner, ws, shipped["id"])
        path = f"/workspaces/{ws}/releases/{shipped['id']}"
        assert client.delete(path, headers=owner).status_code == 409
        _patch(client, owner, ws, shipped["id"], state="archived")
        assert client.delete(path, headers=owner).status_code == 409, (
            "archived after shipping is still history")

    def test_release_writes_are_audited(self, client, db):
        """Would catch: an admin act on a release missing from the audit trail, the way sprint
        starts and closes are recorded there."""
        owner, _member, ws, space, _list = _setup(client, db)
        release = _release(client, owner, ws, space["id"])
        _patch(client, owner, ws, release["id"], description="Notes")
        _ship(client, owner, ws, release["id"])
        draft = _release(client, owner, ws, space["id"], name="draft")
        pm.ok(client.delete(f"/workspaces/{ws}/releases/{draft['id']}", headers=owner))
        audited = [row.details.split(" ")[0] for row in db.query(ActivityLog).filter(
            ActivityLog.entity_type == "release").order_by(ActivityLog.id)]
        assert sorted(audited) == ["Changed", "Created", "Created", "Deleted", "Released"]


class TestReleasing:
    def test_releasing_moves_open_work_and_keeps_what_was_delivered(self, client, db):
        """Would catch: done or canceled issues moved off the release (it would no longer
        say what it delivered), open ones left behind, or the move not recorded."""
        owner, member, ws, space, board_list = _setup(client, db)
        canceled = _canceled_stage(db, ws, space["id"])
        current = _release(client, owner, ws, space["id"], name="2.4")
        upcoming = _release(client, owner, ws, space["id"], name="2.5")
        lid = board_list["id"]
        done = pm.issue(client, owner, ws, lid, title="Done", type="story")
        dropped = pm.issue(client, owner, ws, lid, title="Dropped", type="bug")
        todo = pm.issue(client, owner, ws, lid, title="Todo", type="task")
        review = pm.issue(client, owner, ws, lid, title="In review", type="story")
        for issue in (done, dropped, todo, review):
            _fix(client, member, ws, issue["id"], current["id"])
        _move(client, owner, ws, done["id"], pm.status_id(space, "Done"))
        _move(client, owner, ws, dropped["id"], canceled)
        _move(client, owner, ws, review["id"], pm.status_id(space, "In Review"))

        outcome = _ship(client, owner, ws, current["id"], release_date="2026-10-30",
                        move_open_to_release_id=upcoming["id"])["data"]
        released = outcome["release"]
        assert released["state"] == "released" and released["released_at"] is not None
        assert released["release_date"] == "2026-10-30"
        assert released["progress"] == {"issues": 2, "done": 1, "canceled": 1, "open": 0}
        assert sorted(outcome["moved_issue_ids"]) == sorted([todo["id"], review["id"]])
        assert outcome["moved_to"]["id"] == upcoming["id"]
        assert outcome["moved_to"]["progress"]["issues"] == 2

        for issue, expected in ((done, current), (dropped, current), (todo, upcoming),
                                (review, upcoming)):
            assert [r["id"] for r in _fix_versions(client, owner, ws, issue["id"])] == [
                expected["id"]]
        assert {(issue_id, value) for issue_id, value, _ in _events(
            db, "fix_version_removed")} == {(todo["id"], current["id"]),
                                            (review["id"], current["id"])}
        moved_in = {(issue_id, value) for issue_id, _, value in _events(db, "fix_version_added")
                    if value == upcoming["id"]}
        assert moved_in == {(todo["id"], upcoming["id"]), (review["id"], upcoming["id"])}

    def test_without_a_target_open_work_stays_and_the_date_is_today(self, client, db):
        """Would catch: open work moved off a release nobody asked to move it from, or the planned
        date recorded as the day it shipped."""
        owner, member, ws, space, board_list = _setup(client, db)
        release = _release(client, owner, ws, space["id"], release_date="2026-01-01")
        issue = pm.issue(client, owner, ws, board_list["id"])
        _fix(client, member, ws, issue["id"], release["id"])
        # Bracketed, so a run that crosses midnight UTC between the two reads cannot fail.
        before = datetime.now(UTC).date().isoformat()
        outcome = _ship(client, owner, ws, release["id"])["data"]
        after = datetime.now(UTC).date().isoformat()
        assert outcome["moved_issue_ids"] == [] and outcome["moved_to"] is None
        assert outcome["release"]["release_date"] in {before, after}, (
            "today in the workspace's zone (UTC by default), not the planned 2026-01-01")
        assert outcome["release"]["progress"]["open"] == 1

    def test_what_may_be_released_and_where_open_work_may_go(self, client, db):
        """Would catch: open work moved into another space's release, a released one, or the release
        itself; a release shipped twice or from the archive; and any of those refusals leaving
        the release changed."""
        owner, _member, ws, space, _list = _setup(client, db)
        other = pm.space(client, owner, ws, "OPS")
        release = _release(client, owner, ws, space["id"], start_date="2026-10-05")
        elsewhere = _release(client, owner, ws, other["id"], name="ops-1")
        shipped = _release(client, owner, ws, space["id"], name="shipped")
        _ship(client, owner, ws, shipped["id"])
        rid = release["id"]

        _ship(client, owner, ws, rid, 404, move_open_to_release_id=elsewhere["id"])
        _ship(client, owner, ws, rid, 404, move_open_to_release_id="no-such-release")
        _ship(client, owner, ws, rid, 409, move_open_to_release_id=shipped["id"])
        _ship(client, owner, ws, rid, 422, move_open_to_release_id=rid)
        _ship(client, owner, ws, rid, 422, release_date="2026-10-01")
        assert _get(client, owner, ws, rid)["state"] == "unreleased", "refusals change nothing"
        _ship(client, owner, ws, shipped["id"], 409)
        archived = _release(client, owner, ws, space["id"], name="archived")
        _patch(client, owner, ws, archived["id"], state="archived")
        _ship(client, owner, ws, archived["id"], 409)

    def test_a_read_only_list_refuses_the_whole_move(self, client, db):
        """Would catch: a release half-shipped — some open issues moved, the one in an
        archived list silently changed or skipped."""
        owner, member, ws, space, board_list = _setup(client, db)
        archived_list = pm.task_list(client, owner, ws, space["id"], name="Old")
        current = _release(client, owner, ws, space["id"], name="2.4")
        upcoming = _release(client, owner, ws, space["id"], name="2.5")
        live = pm.issue(client, owner, ws, board_list["id"], title="Live")
        frozen_out = pm.issue(client, owner, ws, archived_list["id"], title="Archived")
        for issue in (live, frozen_out):
            _fix(client, member, ws, issue["id"], current["id"])
        db.query(ProjectList).filter(ProjectList.id == archived_list["id"]).update(
            {ProjectList.archived_at: utc_now()})
        db.commit()
        refused = _ship(client, owner, ws, current["id"], 423,
                        move_open_to_release_id=upcoming["id"])
        assert "Old" in refused["message"]
        assert _get(client, owner, ws, current["id"])["state"] == "unreleased"
        assert [r["id"] for r in _fix_versions(client, owner, ws, live["id"])] == [
            current["id"]]


class TestNotes:
    def test_notes_list_delivered_work_by_type_then_canceled_then_open(self, client, db):
        """Would catch: canceled or unfinished work listed as shipped, a section per stage
        name instead of per outcome, or a text that does not match the structure."""
        owner, member, ws, space, board_list = _setup(client, db)
        canceled = _canceled_stage(db, ws, space["id"])
        done = pm.status_id(space, "Done")
        release = _release(client, owner, ws, space["id"], name="2.4")
        lid = board_list["id"]
        epic = pm.issue(client, owner, ws, lid, title="Reliable refunds", type="epic",
                        assignee_id="owner-1", completion_criteria="Refunds reconcile daily")
        story = pm.issue(client, owner, ws, lid, title="Card refunds", type="story",
                         parent_id=epic["id"])
        bug = pm.issue(client, owner, ws, lid, title="Wrong refund total", type="bug")
        task = pm.issue(client, owner, ws, lid, title="Cheque refunds", type="task")
        late = pm.issue(client, owner, ws, lid, title="Refund emails", type="story")
        later_story = pm.issue(client, owner, ws, lid, title="Saved cards", type="story")
        for issue in (epic, story, bug, task, late, later_story):
            _fix(client, member, ws, issue["id"], release["id"])
        for issue in (epic, story, bug, later_story):
            _move(client, owner, ws, issue["id"], done)
        _move(client, owner, ws, task["id"], canceled)

        notes = pm.ok(client.get(f"/workspaces/{ws}/releases/{release['id']}/notes",
                                 headers=member))
        assert notes["release"]["id"] == release["id"]
        sections = [(s["type"], s["title"], [(i["key"], i["outcome"]) for i in s["issues"]])
                    for s in notes["sections"]]
        assert sections == [
            ("epic", "Epics", [("PAY-1", "done")]),
            ("story", "Stories", [("PAY-2", "done"), ("PAY-6", "done")]),
            ("bug", "Bugs", [("PAY-3", "done")]),
            ("canceled", "Canceled", [("PAY-4", "canceled")]),
            ("open", "Still open", [("PAY-5", "open")]),
        ]
        assert notes["sections"][3]["issues"][0] == {
            "id": task["id"], "key": "PAY-4", "title": "Cheque refunds", "type": "task",
            "outcome": "canceled"}
        assert notes["text"] == (
            "PAY space 2.4 - not released yet\n"
            "\n"
            "Epics\n"
            "PAY-1 Reliable refunds\n"
            "\n"
            "Stories\n"
            "PAY-2 Card refunds\n"
            "PAY-6 Saved cards\n"
            "\n"
            "Bugs\n"
            "PAY-3 Wrong refund total\n"
            "\n"
            "Canceled\n"
            "PAY-4 Cheque refunds\n"
            "\n"
            "Still open\n"
            "PAY-5 Refund emails\n"
        )

    def test_the_heading_says_when_it_shipped_or_is_planned(self, client, db):
        """Would catch: notes whose heading cannot tell a shipped release from a planned one, or an
        empty release producing an empty text."""
        owner, _member, ws, space, _list = _setup(client, db)
        planned = _release(client, owner, ws, space["id"], name="2.5",
                           release_date="2026-11-20")
        path = f"/workspaces/{ws}/releases/{planned['id']}/notes"
        text = pm.ok(client.get(path, headers=owner))["text"]
        assert text == ("PAY space 2.5 - not released yet, planned for 2026-11-20\n\n"
                        "No issues are assigned to this release.\n")
        _ship(client, owner, ws, planned["id"], release_date="2026-11-18")
        assert pm.ok(client.get(path, headers=owner))["text"].splitlines()[0] == (
            "PAY space 2.5 - released 2026-11-18")

    def test_a_title_with_a_line_break_stays_on_one_line(self):
        """The text promises one `KEY title` line per issue; a newline in a title would
        break every tool that reads it line by line."""
        sections = [{"title": "Stories", "issues": [{"key": "PAY-1", "title": "Two\nlines"}]}]
        assert render_notes("Heading", sections) == "Heading\n\nStories\nPAY-1 Two lines\n"


class TestFixVersions:
    def test_put_replaces_the_set_and_records_only_the_differences(self, client, db):
        """Would catch: PUT merging instead of replacing, repeats stored twice, an event for a
        release the issue kept, or fix versions bumping the issue's version."""
        owner, member, ws, space, board_list = _setup(client, db)
        r1 = _release(client, owner, ws, space["id"], name="r1", release_date="2026-10-10")
        r2 = _release(client, owner, ws, space["id"], name="r2", release_date="2026-10-20")
        r3 = _release(client, owner, ws, space["id"], name="r3")
        issue = pm.issue(client, owner, ws, board_list["id"])
        first = _fix(client, member, ws, issue["id"], r2["id"], r1["id"], r2["id"])["data"]
        assert [r["name"] for r in first] == ["r1", "r2"], "by date, repeats ignored"
        second = _fix(client, member, ws, issue["id"], r2["id"], r3["id"])["data"]
        assert [r["name"] for r in second] == ["r2", "r3"]
        assert _events(db, "fix_version_added") == {
            (issue["id"], None, r1["id"]), (issue["id"], None, r2["id"]),
            (issue["id"], None, r3["id"])}
        assert _events(db, "fix_version_removed") == {(issue["id"], r1["id"], None)}
        assert _fix(client, member, ws, issue["id"])["data"] == []
        assert db.query(IssueFixVersion).count() == 0
        detail = pm.ok(client.get(f"/workspaces/{ws}/issues/{issue['id']}", headers=owner))
        assert detail["version"] == 1, "planning is not an edit: no 409 for whoever has it open"

    def test_a_release_being_added_must_be_one_this_issue_can_ship_in(self, client, db):
        """Would catch: an issue shipping in another space's release — its progress and
        notes would count work its space never planned."""
        owner, member, ws, space, board_list = _setup(client, db)
        other = pm.space(client, owner, ws, "OPS")
        theirs = _release(client, owner, ws, other["id"], name="ops-1")
        archived = _release(client, owner, ws, space["id"], name="archived")
        _patch(client, owner, ws, archived["id"], state="archived")
        deleted = _release(client, owner, ws, space["id"], name="deleted")
        pm.ok(client.delete(f"/workspaces/{ws}/releases/{deleted['id']}", headers=owner))
        shipped = _release(client, owner, ws, space["id"], name="shipped")
        _ship(client, owner, ws, shipped["id"])
        issue = pm.issue(client, owner, ws, board_list["id"])

        for release_id in (theirs["id"], archived["id"], deleted["id"], "no-such-release"):
            refused = _fix(client, member, ws, issue["id"], release_id, status=422)
            assert refused["data"]["release_id"] == release_id
        assert db.query(IssueFixVersion).count() == 0
        _fix(client, member, ws, issue["id"], *[f"r{n}" for n in range(21)], status=422)
        assert [r["id"] for r in _fix(client, member, ws, issue["id"],
                                      shipped["id"])["data"]] == [shipped["id"]]

    def test_another_writer_can_record_itself_as_the_actor(self, client, db):
        """Would catch: a transfer dropping the fix versions an issue leaves behind in its
        old space, recorded as an everyday edit by the admin who ran it."""
        owner, _member, ws, space, board_list = _setup(client, db)
        release = _release(client, owner, ws, space["id"])
        created = pm.issue(client, owner, ws, board_list["id"])
        issue = db.query(Issue).filter(Issue.id == created["id"]).one()
        row = db.query(Release).filter(Release.id == release["id"]).one()
        assert release_svc.add_fix_version(db, issue, row, "owner-1", actor_kind="migration")
        assert not release_svc.add_fix_version(db, issue, row, "owner-1"), "already there"
        assert release_svc.remove_fix_version(db, issue, release["id"], "owner-1",
                                              actor_kind="migration")
        assert not release_svc.remove_fix_version(db, issue, release["id"], "owner-1")
        db.commit()
        kinds = {(r.event, r.actor_kind) for r in db.query(IssueEvent).filter(
            IssueEvent.event.in_(["fix_version_added", "fix_version_removed"]))}
        assert kinds == {("fix_version_added", "migration"), ("fix_version_removed", "migration")}

    def test_an_archived_release_the_issue_already_has_may_stay(self, client, db):
        """Archiving a release must not make its issues uneditable: keeping is not adding."""
        owner, member, ws, space, board_list = _setup(client, db)
        old = _release(client, owner, ws, space["id"], name="old")
        new = _release(client, owner, ws, space["id"], name="new")
        issue = pm.issue(client, owner, ws, board_list["id"])
        _fix(client, member, ws, issue["id"], old["id"])
        _patch(client, owner, ws, old["id"], state="archived")
        kept = _fix(client, member, ws, issue["id"], old["id"], new["id"])["data"]
        assert {(r["name"], r["state"]) for r in kept} == {("old", "archived"),
                                                          ("new", "unreleased")}

    def test_fix_versions_follow_the_issues_write_state(self, client, db):
        """Would catch: fix versions changed on an issue whose list is archived, or whose space a
        migration wave has frozen."""
        owner, member, ws, space, board_list = _setup(client, db)
        release = _release(client, owner, ws, space["id"])
        issue = pm.issue(client, owner, ws, board_list["id"])
        db.query(ProjectList).filter(ProjectList.id == board_list["id"]).update(
            {ProjectList.archived_at: utc_now()})
        db.commit()
        _fix(client, member, ws, issue["id"], release["id"], status=423)
        db.query(ProjectList).filter(ProjectList.id == board_list["id"]).update(
            {ProjectList.archived_at: None})
        db.query(Space).filter(Space.id == space["id"]).update({Space.frozen_at: utc_now()})
        db.commit()
        refused = _fix(client, member, ws, issue["id"], release["id"], status=423)
        assert "frozen" in refused["message"]
        assert _fix_versions(client, member, ws, issue["id"]) == []


class TestWriteGuard:
    def test_release_writes_in_a_frozen_or_archived_space_are_423(self, client, db):
        """Would catch: a release created, re-dated, deleted or shipped in a space a
        migration wave froze, or in one that is archived for good."""
        owner, _member, ws, space, _list = _setup(client, db)
        release = _release(client, owner, ws, space["id"])
        base = f"/workspaces/{ws}/releases/{release['id']}"
        for column in (Space.frozen_at, Space.archived_at):
            db.query(Space).filter(Space.id == space["id"]).update(
                {Space.frozen_at: None, Space.archived_at: None, column: utc_now()})
            db.commit()
            _release(client, owner, ws, space["id"], name="new", status=423)
            assert client.patch(base, json={"name": "x"}, headers=owner).status_code == 423
            assert client.delete(base, headers=owner).status_code == 423
            assert client.post(f"{base}/release", json={}, headers=owner).status_code == 423
            # Reads are never refused.
            assert _get(client, owner, ws, release["id"])["name"] == "2.4"
            assert client.get(f"{base}/notes", headers=owner).status_code == 200
        assert db.query(Release).count() == 1


class TestTenancy:
    def test_another_workspaces_releases_spaces_and_issues_are_404(self, client, db):
        """Would catch: any release, space or issue lookup missing its workspace filter — every
        route here must answer another tenant's id as if it did not exist."""
        headers_a, ws_a, space_a, list_a = pm.setup_board(client, db, "alice", "alpha", "ALP")
        headers_b, ws_b, space_b, list_b = pm.setup_board(client, db, "bob", "bravo", "BRA")
        theirs = _release(client, headers_b, ws_b, space_b["id"], name="secret")
        their_issue = pm.issue(client, headers_b, ws_b, list_b["id"])
        mine = _release(client, headers_a, ws_a, space_a["id"])
        my_issue = pm.issue(client, headers_a, ws_a, list_a["id"])
        base = f"/workspaces/{ws_a}/releases/{theirs['id']}"

        assert client.get(f"/workspaces/{ws_a}/spaces/{space_b['id']}/releases",
                          headers=headers_a).status_code == 404
        _release(client, headers_a, ws_a, space_b["id"], name="x", status=404)
        for method, path in (("GET", base), ("PATCH", base), ("DELETE", base),
                             ("POST", f"{base}/release"), ("GET", f"{base}/notes")):
            body = {} if method in ("PATCH", "POST") else None
            response = client.request(method, path, json=body, headers=headers_a)
            assert response.status_code == 404, (method, path)
        assert client.get(f"/workspaces/{ws_a}/issues/{their_issue['id']}/fix-versions",
                          headers=headers_a).status_code == 404
        _fix(client, headers_a, ws_a, their_issue["id"], mine["id"], status=404)
        _fix(client, headers_a, ws_a, my_issue["id"], theirs["id"], status=422)
        _ship(client, headers_a, ws_a, mine["id"], 404, move_open_to_release_id=theirs["id"])
        stranger = client.get(f"/workspaces/{ws_b}/releases/{theirs['id']}", headers=headers_a)
        assert stranger.status_code == 404
        assert stranger.json()["message"] == "Workspace not found"


class TestCost:
    def test_progress_is_counted_in_one_query_for_any_number_of_releases(self, client, db):
        """The N+1 guard: the list's progress is one grouped query, not one per release."""
        owner, member, ws, space, board_list = _setup(client, db)
        path = f"/workspaces/{ws}/spaces/{space['id']}/releases"

        def add_release(name):
            release = _release(client, owner, ws, space["id"], name=name)
            for n in range(2):
                issue = pm.issue(client, owner, ws, board_list["id"], title=f"{name} {n}")
                _fix(client, member, ws, issue["id"], release["id"])

        add_release("one")
        with _QueryCounter() as one:
            assert len(pm.ok(client.get(path, headers=owner))) == 1
        for name in ("two", "three", "four"):
            add_release(name)
        with _QueryCounter() as many:
            listed = pm.ok(client.get(path, headers=owner))
        assert [r["progress"]["issues"] for r in listed] == [2, 2, 2, 2]
        assert len(many.statements) <= len(one.statements), (
            f"queries grew with releases: {len(one.statements)} -> {len(many.statements)}")
