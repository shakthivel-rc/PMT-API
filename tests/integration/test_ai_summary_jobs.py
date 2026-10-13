"""The AI summary, end to end, with the model replaced: request → queue → worker → result.

What these pin is the *shape of the system*, not the model's prose: that the API never calls
the model, that asking twice does not pay twice, that an unchanged thread is answered from
the last result, that a transient failure retries and a permanent one stops, and that a
worker whose lease was taken over cannot overwrite the job's new owner.
"""
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

import controllers.ai_controller as ai_controller
import workers.ai_worker as worker
from ai.client import AIError, Completion
from core.config import get_settings
from models.ai_job import AIJob
from services import ai_jobs
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration

GOOD = {"tldr": "Refunds are blocked on the PSP sandbox.", "decisions": ["Refund to card"],
        "open_questions": [], "action_items": [{"owner": "Ana", "text": "Chase the PSP"}]}


class FakeModel:
    """Stands in for ClaudeJSONClient. Records what it was sent; answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers) or [GOOD]
        self.prompts: list[str] = []

    def complete_json(self, *, system, user, schema):
        self.prompts.append(user)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return Completion(data=answer, model="claude-opus-5", input_tokens=900,
                          output_tokens=120, request_id="req_test")


@pytest.fixture()
def ai_on(monkeypatch):
    configured = dataclasses.replace(get_settings(), ai_enabled=True)
    monkeypatch.setattr(ai_controller, "get_settings", lambda: configured)


def _thread(client, db):
    headers, workspace_id, _, board_list = pm.setup_board(client, db)
    issue = pm.issue(client, headers, workspace_id, board_list["id"], title="Refunds",
                     description="Refund to the original card.")
    pm.ok(client.post(f"/workspaces/{workspace_id}/issues/{issue['id']}/comments",
                      json={"body": "Blocked on the PSP sandbox."}, headers=headers), 201)
    return headers, workspace_id, issue


def _request(client, headers, workspace_id, issue_id):
    return client.post(f"/workspaces/{workspace_id}/issues/{issue_id}/summary", headers=headers)


def _job(client, headers, workspace_id, job_id):
    return pm.ok(client.get(f"/workspaces/{workspace_id}/ai-jobs/{job_id}", headers=headers))


def test_off_by_default_and_says_so(client, db):
    headers, workspace_id, issue = _thread(client, db)
    response = _request(client, headers, workspace_id, issue["id"])
    assert response.status_code == 503
    assert "AI_ENABLED" in response.json()["message"]


def test_request_queues_and_the_worker_answers(client, db, ai_on):
    headers, workspace_id, issue = _thread(client, db)
    queued = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)
    assert queued["status"] == "queued" and queued["result"] is None

    model = FakeModel()
    assert worker.run_once(model, max_attempts=3) is True
    assert worker.run_once(model, max_attempts=3) is False, "one job, claimed once"

    done = _job(client, headers, workspace_id, queued["id"])
    assert done["status"] == "succeeded"
    assert done["result"]["action_items"][0]["owner"] == "Ana"
    assert "Blocked on the PSP sandbox." in model.prompts[0]
    assert "PAY-1" in model.prompts[0]


def test_asking_twice_while_queued_returns_the_same_job(client, db, ai_on):
    headers, workspace_id, issue = _thread(client, db)
    first = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)
    second = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)
    assert first["id"] == second["id"]
    assert db.query(AIJob).count() == 1


def test_an_unchanged_thread_is_answered_from_the_last_summary(client, db, ai_on):
    headers, workspace_id, issue = _thread(client, db)
    pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)
    model = FakeModel()
    worker.run_once(model, max_attempts=3)

    cached = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)
    assert cached["cached"] is True and cached["status"] == "succeeded"
    assert worker.run_once(model, max_attempts=3) is False
    assert len(model.prompts) == 1, "the model was called for a thread that had not changed"

    # A new comment changes the input, so the next request is real work again.
    pm.ok(client.post(f"/workspaces/{workspace_id}/issues/{issue['id']}/comments",
                      json={"body": "PSP sandbox is back."}, headers=headers), 201)
    fresh = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)
    assert fresh["cached"] is False and fresh["status"] == "queued"


def test_a_transient_failure_retries_and_a_permanent_one_stops(client, db, ai_on):
    headers, workspace_id, issue = _thread(client, db)
    job = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)

    worker.run_once(FakeModel(AIError("busy", retryable=True)), max_attempts=3)
    retrying = _job(client, headers, workspace_id, job["id"])
    assert retrying["status"] == "queued"
    assert retrying["error"] is None, "a retry in progress must not read as a final error"

    # Make it due now rather than after the back-off, then fail it for good.
    row = db.query(AIJob).filter(AIJob.id == job["id"]).one()
    row.available_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    worker.run_once(FakeModel(AIError("The model declined.", retryable=False)), max_attempts=3)
    failed = _job(client, headers, workspace_id, job["id"])
    assert failed["status"] == "failed" and failed["error"] == "The model declined."


def test_a_worker_that_lost_its_lease_cannot_overwrite_the_new_owner(client, db, ai_on):
    headers, workspace_id, issue = _thread(client, db)
    job_id = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)["id"]

    slow = ai_jobs.claim_next(db, lease_seconds=60)
    assert slow is not None and slow.attempts == 1
    later = datetime.now(UTC) + timedelta(minutes=5)
    fast = ai_jobs.claim_next(db, lease_seconds=60, now=later)
    assert fast is not None and fast.id == job_id and fast.attempts == 2

    assert ai_jobs.complete(db, job_id=job_id, attempt=2, result=GOOD, model="m",
                            input_tokens=1, output_tokens=1, input_hash="h") is True
    assert ai_jobs.complete(db, job_id=job_id, attempt=1, result={"tldr": "stale"}, model="m",
                            input_tokens=1, output_tokens=1, input_hash="h") is False
    assert _job(client, headers, workspace_id, job_id)["result"]["tldr"] == GOOD["tldr"]


def test_jobs_are_tenant_scoped(client, db, ai_on):
    headers, workspace_id, issue = _thread(client, db)
    job_id = pm.ok(_request(client, headers, workspace_id, issue["id"]), 202)["id"]
    headers_b, ws_b, _, _ = pm.setup_board(client, db, "bob", "bravo", "BRA")
    assert client.get(f"/workspaces/{ws_b}/ai-jobs/{job_id}",
                      headers=headers_b).status_code == 404


def test_a_guest_cannot_spend_money(client, db, ai_on):
    _headers, workspace_id, issue = _thread(client, db)
    guest = pm.person(db, "guest-1")
    pm.join(db, workspace_id, "guest-1", "guest")
    assert _request(client, guest, workspace_id, issue["id"]).status_code == 403
