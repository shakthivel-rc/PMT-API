"""Time on archived and frozen containers is read-only (docs/design/09-restructuring.md §3.4).

Logging, changing and deleting time, and starting a timer, answer 423 while the issue's list,
folder or space is archived or its space is frozen for a migration wave. A frozen space's
seconds-logged-per-issue are one of the numbers the wave's `history_reconciles` gate compares
with its baseline, so time written during the freeze fails the wave for a reason nobody would
find; an archived container is history and must stay as it was.

The timer is the case with a person on the other end: stopping it logs time, so stopping is
refused too — but the timer is never lost. It keeps running, unchanged, until the space is
writable again; discarding it, which logs nothing, is always allowed. Starting a timer
elsewhere would stop the locked one and log its time, so that is refused for the same reason.

Containers are frozen or archived by setting their columns through the `db` fixture: the
archive and freeze endpoints are another lane's, and the guard reads only the columns.
"""
import pytest

from models.hierarchy import Folder, ProjectList, Space
from models.time_tracking import ActiveTimer, TimeEntry
from tests import pm_fixtures as pm
from utils.time_tracking import utc_now

pytestmark = pytest.mark.integration


def _freeze(db, space_id: str) -> None:
    space = db.get(Space, space_id)
    space.frozen_at, space.freeze_reason = utc_now(), "wave 1"
    db.commit()


def _unfreeze(db, space_id: str) -> None:
    space = db.get(Space, space_id)
    space.frozen_at = space.freeze_reason = None
    db.commit()


def _lock(db, space_id: str, how: str) -> None:
    if how == "frozen":
        _freeze(db, space_id)
    else:
        db.get(Space, space_id).archived_at = utc_now()
        db.commit()


def _entries(db) -> list[tuple[str, int, int]]:
    db.expire_all()
    return sorted((e.issue_id, e.duration_seconds, e.deleted) for e in db.query(TimeEntry))


class TestEntries:
    @pytest.mark.parametrize("how", ["frozen", "archived"])
    def test_logging_changing_and_deleting_time_are_refused(self, client, db, how):
        """Each of the three writes would succeed on a writable space; locked, each is 423,
        names the space, and leaves the entries exactly as they were. Reading the time is
        never refused."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, owner, ws, board_list["id"], title="Fix refunds")
        entry = pm.log_time(client, owner, ws, issue["id"], 600)
        before = _entries(db)
        _lock(db, space["id"], how)

        for method, path, body in (
            ("POST", f"/workspaces/{ws}/issues/{issue['id']}/time-entries",
             {"duration_seconds": 60, "work_date": "2026-10-01"}),
            ("PATCH", f"/workspaces/{ws}/time-entries/{entry['id']}",
             {"duration_seconds": 1200}),
            ("DELETE", f"/workspaces/{ws}/time-entries/{entry['id']}", None),
        ):
            response = client.request(method, path, json=body, headers=owner)
            assert response.status_code == 423, (method, path, response.text)
            assert space["name"] in response.json()["message"]
        assert _entries(db) == before
        summary = pm.issue_time(client, owner, ws, issue["id"])["summary"]
        assert summary["spent_seconds"] == 600
        assert pm.ok(client.get(f"/workspaces/{ws}/time-entries",
                                headers=owner))["total_seconds"] == 600

    def test_an_archived_list_or_folder_locks_only_its_own_issues(self, client, db):
        """The guard reads the issue's own containers: an archived list refuses time on its
        issues and says which list; an archived folder refuses time on its lists' issues;
        an issue of another list of the same space is untouched."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        old = pm.task_list(client, owner, ws, space["id"], "Old work")
        folder = pm.ok(client.post(f"/workspaces/{ws}/spaces/{space['id']}/folders",
                                   json={"name": "Programme"}, headers=owner), 201)
        filed = pm.ok(client.post(f"/workspaces/{ws}/lists",
                                  json={"space_id": space["id"], "folder_id": folder["id"],
                                        "name": "Filed"}, headers=owner), 201)
        stuck = pm.issue(client, owner, ws, old["id"], title="Stuck")
        boxed = pm.issue(client, owner, ws, filed["id"], title="Boxed")
        fine = pm.issue(client, owner, ws, board_list["id"], title="Fine")
        db.get(ProjectList, old["id"]).archived_at = utc_now()
        db.get(Folder, folder["id"]).archived_at = utc_now()
        db.commit()

        body = {"duration_seconds": 60, "work_date": "2026-10-01"}
        refused = client.post(f"/workspaces/{ws}/issues/{stuck['id']}/time-entries",
                              json=body, headers=owner)
        assert refused.status_code == 423 and "Old work" in refused.json()["message"]
        refused = client.post(f"/workspaces/{ws}/issues/{boxed['id']}/time-entries",
                              json=body, headers=owner)
        assert refused.status_code == 423 and "Programme" in refused.json()["message"]
        pm.log_time(client, owner, ws, fine["id"], 60)
        assert _entries(db) == [(fine["id"], 60, 0)]

    def test_a_member_is_refused_the_same_way_and_unfreezing_lifts_it(self, client, db):
        """The lock is about the container, not the person: a member logging their own time
        gets the same 423 as the owner, and the same request succeeds once the space is
        unfrozen."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        member = pm.person(db, "member-1")
        pm.join(db, ws, "member-1", "member")
        issue = pm.issue(client, owner, ws, board_list["id"])
        _freeze(db, space["id"])
        path = f"/workspaces/{ws}/issues/{issue['id']}/time-entries"
        body = {"duration_seconds": 300, "work_date": "2026-10-01"}
        refused = client.post(path, json=body, headers=member)
        assert refused.status_code == 423
        assert "wave 1" in refused.json()["message"]
        _unfreeze(db, space["id"])
        assert pm.ok(client.post(path, json=body, headers=member), 201)["duration_seconds"] == 300


class TestTimers:
    def test_a_timer_cannot_start_on_a_locked_issue(self, client, db):
        """Starting writes the timer, and stopping it would write time where the guard
        refuses it; it is refused at the start instead of at the stop."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, owner, ws, board_list["id"])
        _freeze(db, space["id"])
        response = client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                               headers=owner)
        assert response.status_code == 423, response.text
        assert db.query(ActiveTimer).count() == 0

    def test_stopping_is_refused_and_the_timer_keeps_running_until_discarded(self, client,
                                                                             db):
        """A timer started before the freeze cannot be stopped during it: stopping logs its
        time on a frozen issue. The refusal says the timer keeps running and how to get out
        of it; the timer is untouched — same issue, same start — and still in the header;
        discarding it works and logs nothing. Without the guard the stop wrote an entry into
        the frozen space."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, owner, ws, board_list["id"])
        started = pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                                    headers=owner), 201)["timer"]
        _freeze(db, space["id"])

        refused = client.post("/timer/stop", json={"note": "done"}, headers=owner)
        assert refused.status_code == 423, refused.text
        message = refused.json()["message"]
        assert "frozen" in message and "keeps running" in message and "discard" in message
        assert refused.json()["data"] == {"timer_running": True, "issue_id": issue["id"]}
        assert _entries(db) == []
        running = pm.ok(client.get("/timer", headers=owner))
        assert running["issue"]["id"] == issue["id"]
        assert running["started_at"] == started["started_at"]

        assert pm.ok(client.delete("/timer", headers=owner)) == {"discarded": True}
        assert pm.ok(client.get("/timer", headers=owner)) is None
        assert _entries(db) == []

    def test_after_the_unfreeze_the_same_timer_stops_and_logs(self, client, db):
        """Nothing about the refused stop was lost: once the space is writable, the stop that
        was refused logs the timer's time as if the freeze had never happened."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, owner, ws, board_list["id"])
        pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                          headers=owner), 201)
        _freeze(db, space["id"])
        assert client.post("/timer/stop", json={}, headers=owner).status_code == 423
        _unfreeze(db, space["id"])
        stopped = pm.ok(client.post("/timer/stop", json={}, headers=owner))
        assert stopped["entry"]["issue"]["id"] == issue["id"]
        assert stopped["entry"]["source"] == "timer"
        assert [e[0] for e in _entries(db)] == [issue["id"]]

    def test_starting_elsewhere_would_save_the_locked_timer_so_it_is_refused(self, client,
                                                                             db):
        """Starting a timer stops the running one and logs its time. When that one is on a
        frozen issue, starting another — even on a writable issue — would be the write the
        stop route refuses, so it is refused the same way, and the running timer stays as it
        was. Throwing the old timer away instead would lose time nobody chose to lose."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        frozen_issue = pm.issue(client, owner, ws, board_list["id"], title="In the wave")
        other = pm.space(client, owner, ws, "OPS")
        other_list = pm.task_list(client, owner, ws, other["id"])
        writable = pm.issue(client, owner, ws, other_list["id"], title="Elsewhere")
        pm.ok(client.post(f"/workspaces/{ws}/issues/{frozen_issue['id']}/timer", json={},
                          headers=owner), 201)
        _freeze(db, space["id"])

        refused = client.post(f"/workspaces/{ws}/issues/{writable['id']}/timer", json={},
                              headers=owner)
        assert refused.status_code == 423, refused.text
        assert "keeps running" in refused.json()["message"]
        assert refused.json()["data"]["issue_id"] == frozen_issue["id"]
        db.expire_all()
        [timer] = db.query(ActiveTimer).all()
        assert timer.issue_id == frozen_issue["id"]
        assert _entries(db) == []

    def test_a_timer_whose_issue_is_gone_is_still_discarded_on_stop(self, client, db):
        """The guard protects time that would be written. A timer whose issue was deleted
        writes none — the stop discards it, as it always has — so a frozen space does not
        trap it."""
        owner, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, owner, ws, board_list["id"])
        pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/timer", json={},
                          headers=owner), 201)
        pm.ok(client.delete(f"/workspaces/{ws}/issues/{issue['id']}", headers=owner))
        _freeze(db, space["id"])
        assert pm.ok(client.post("/timer/stop", json={}, headers=owner))["entry"] is None
        assert db.query(ActiveTimer).count() == 0
