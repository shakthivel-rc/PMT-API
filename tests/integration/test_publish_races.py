"""A board write racing a workflow publish (docs/design/09-restructuring.md §4.4).

A publish holds its space's row while it moves the issues out of the stages it removes and
soft-deletes them. A move or a create that had read its destination as live before that
publish committed must not then write the issue into the removed stage — an issue no board
draws, which the publish promised could not happen ("nothing is moved, completed or deleted
silently"). services/issue.py takes the space's row first and re-reads the stage with a
locking read.

SQLite has one writer and ignores FOR UPDATE / FOR SHARE, so the interleaving cannot happen
here by itself. These tests make it happen: the publish "commits" — from another session,
as a concurrent request would — at the exact moment the write is waiting for the space's
lock, which is the window the locking re-read exists for. Each would fail with the re-read
removed: the issue would land in the removed stage.
"""
from datetime import UTC, datetime

import pytest

import db.session as db_session
from models.hierarchy import WorkflowStatus
from models.issue import Issue
from services import issue as issue_svc
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


def _publish_removes(status_id: str) -> None:
    """What a concurrent publish commits for one removed stage: the soft delete."""
    other = db_session.SessionLocal()
    try:
        (other.query(WorkflowStatus).filter(WorkflowStatus.id == status_id)
         .update({"deleted": 1, "deleted_at": datetime.now(UTC)}))
        other.commit()
    finally:
        other.close()


class TestMoveRacingAPublish:
    def test_a_move_into_a_stage_removed_while_it_waited_is_refused(self, client, db,
                                                                     monkeypatch):
        """The publish commits while the move waits for the space's lock; the move then
        finds its destination removed and refuses, leaving the issue where it was."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        review = pm.status_id(space, "In Review")
        locked = issue_svc.lock_space_shared

        def publish_commits_while_waiting(session, workspace_id, space_id):
            _publish_removes(review)
            locked(session, workspace_id, space_id)

        # raising=False, so that without the lock step this test still runs — and fails
        # because the move lands in the removed stage.
        monkeypatch.setattr(issue_svc, "lock_space_shared", publish_commits_while_waiting,
                            raising=False)
        refused = client.post(f"/workspaces/{ws}/issues/{issue['id']}/move",
                              json={"status_id": review, "after_id": None, "before_id": None},
                              headers=headers)
        assert refused.status_code == 422, refused.text
        db.expire_all()
        assert db.get(Issue, issue["id"]).status_id == pm.status_id(space, "To Do")

    def test_a_move_reads_the_issue_as_last_committed_after_the_lock(self, client, db,
                                                                     monkeypatch):
        """The publish moved this very issue out of a removed stage while the move waited:
        the move must start from where the issue is NOW, not from the copy it read before
        the lock — or it would compute its transition, version and history from a stage
        the issue has already left."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, ws, board_list["id"])
        doing, done = pm.status_id(space, "In Progress"), pm.status_id(space, "Done")
        locked = issue_svc.lock_space_shared

        def publish_moves_it_while_waiting(session, workspace_id, space_id):
            other = db_session.SessionLocal()
            try:
                (other.query(Issue).filter(Issue.id == issue["id"])
                 .update({"status_id": doing, "version": Issue.version + 1}))
                other.commit()
            finally:
                other.close()
            locked(session, workspace_id, space_id)

        monkeypatch.setattr(issue_svc, "lock_space_shared", publish_moves_it_while_waiting,
                            raising=False)
        moved = pm.ok(client.post(f"/workspaces/{ws}/issues/{issue['id']}/move",
                                  json={"status_id": done, "after_id": None, "before_id": None},
                                  headers=headers))
        # Two bumps: the publish's and this move's. A move computed from the stale copy
        # would write the publish's version again.
        assert moved["status_id"] == done and moved["version"] == issue["version"] + 2


class TestCreateRacingAPublish:
    def test_a_create_into_a_stage_removed_while_it_waited_is_refused(self, client, db,
                                                                      monkeypatch):
        """Allocating the issue number takes the space's row; a publish that committed
        just before that lock was granted removed the stage, so nothing is created."""
        headers, ws, space, board_list = pm.setup_board(client, db)
        review = pm.status_id(space, "In Review")
        allocate = issue_svc.allocate_number

        def publish_commits_first(session, space_id):
            _publish_removes(review)
            return allocate(session, space_id)

        monkeypatch.setattr(issue_svc, "allocate_number", publish_commits_first)
        refused = client.post(f"/workspaces/{ws}/issues",
                              json={"list_id": board_list["id"], "title": "Late arrival",
                                    "status_id": review},
                              headers=headers)
        assert refused.status_code == 409, refused.text
        assert "In Review was removed" in refused.json()["message"]
        db.expire_all()
        assert db.query(Issue).filter(Issue.title == "Late arrival").count() == 0

    def test_a_create_into_a_live_stage_is_unaffected(self, client, db):
        """The re-read costs nothing when nothing raced: the stage is live and the issue is
        created in it, numbered as before."""
        headers, ws, _space, board_list = pm.setup_board(client, db)
        first = pm.issue(client, headers, ws, board_list["id"], "One")
        second = pm.issue(client, headers, ws, board_list["id"], "Two")
        assert first["key"].endswith("-1") and second["key"].endswith("-2")
