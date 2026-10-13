"""The GitHub webhook end to end: signature, linking, the merge → Done rule, redelivery.

This is the test the route's entry in `tests/unit/test_route_inventory.PUBLIC` cites. That
entry claims the route authenticates every delivery with an HMAC and refuses everything when
no secret is set; the first two tests here are what make the claim true rather than present.
"""
import dataclasses
import hashlib
import hmac
import json
import uuid

import pytest

import routes.integrations as webhook_route
from core.config import get_settings
from models.issue import IssueEvent
from models.vcs import VcsLink
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration

SECRET = "whsec-for-tests"
URL = "/integrations/github/webhook"


@pytest.fixture()
def secret(monkeypatch):
    configured = dataclasses.replace(get_settings(), github_webhook_secret=SECRET)
    monkeypatch.setattr(webhook_route, "get_settings", lambda: configured)


def _deliver(client, payload, *, event="pull_request", delivery=None, secret=SECRET,
             signature=None):
    body = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "X-Hub-Signature-256": signature if signature is not None else f"sha256={digest}",
    }
    return client.post(URL, content=body, headers=headers)


def _pr(action="opened", *, merged=False, state="open", title="PAY-1 refunds",
        branch="feature/pay-1-refunds", repo="acme/payments", number=7):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "title": title, "body": "", "state": state,
                         "merged": merged, "html_url": f"https://github.com/{repo}/pull/{number}",
                         "head": {"ref": branch}, "user": {"login": "octocat"}},
    }


def _linked_board(client, db, *, user="owner-1", slug="acme", repo="acme/payments"):
    headers, workspace_id, _space, board_list = pm.setup_board(client, db, user, slug)
    issue = pm.issue(client, headers, workspace_id, board_list["id"], title="Refunds")
    pm.ok(client.post(f"/workspaces/{workspace_id}/repositories",
                      json={"full_name": repo}, headers=headers), 201)
    return headers, workspace_id, board_list, issue


class TestAuthentication:
    def test_with_no_secret_configured_every_delivery_is_refused(self, client, db):
        response = _deliver(client, _pr(), secret="")
        assert response.status_code == 401

    def test_a_wrong_signature_is_refused_and_changes_nothing(self, client, db, secret):
        _linked_board(client, db)
        response = _deliver(client, _pr(), signature="sha256=" + "0" * 64)
        assert response.status_code == 401
        assert db.query(VcsLink).count() == 0

    def test_needs_no_bearer_token(self, client, db, secret):
        """GitHub cannot hold a session; the HMAC is the credential."""
        response = _deliver(client, {"zen": "Design for failure."}, event="ping")
        assert response.status_code == 200
        assert response.json()["data"]["note"] == "pong"


class TestLinking:
    def test_a_pull_request_links_to_the_issue_its_branch_names(self, client, db, secret):
        headers, workspace_id, _, issue = _linked_board(client, db)
        result = pm.ok(_deliver(client, _pr(title="Refunds, finally")))
        assert result["linked"] == ["PAY-1"]

        detail = pm.ok(client.get(f"/workspaces/{workspace_id}/issues/{issue['id']}",
                                  headers=headers))
        assert detail["pull_request_state"] == "open"
        assert detail["links"][0]["external_id"] == "acme/payments#7"

    def test_merging_moves_the_issue_to_done_once(self, client, db, secret):
        headers, workspace_id, board_list, _issue = _linked_board(client, db)
        _deliver(client, _pr())
        delivery = str(uuid.uuid4())
        merged = _pr("closed", merged=True, state="closed")
        result = pm.ok(_deliver(client, merged, delivery=delivery))
        assert result["moved_to_done"] == ["PAY-1"]

        board = pm.board(client, headers, workspace_id, board_list["id"])
        assert pm.column_titles(board, "Done") == ["Refunds"]
        moves = db.query(IssueEvent).filter(IssueEvent.event == "status_changed").all()
        assert [(m.actor_kind, m.actor_id) for m in moves] == [("automation", None)]

        # GitHub redelivers: nothing happens twice.
        again = pm.ok(_deliver(client, merged, delivery=delivery))
        assert again["duplicate"] is True
        assert db.query(IssueEvent).filter(IssueEvent.event == "status_changed").count() == 1

    def test_a_closed_unmerged_pr_does_not_move_anything(self, client, db, secret):
        headers, workspace_id, board_list, _ = _linked_board(client, db)
        pm.ok(_deliver(client, _pr("closed", state="closed")))
        board = pm.board(client, headers, workspace_id, board_list["id"])
        assert pm.column_titles(board, "Done") == []

    def test_an_unlinked_repository_is_acknowledged_and_ignored(self, client, db, secret):
        _linked_board(client, db)
        result = pm.ok(_deliver(client, _pr(repo="someone/else")))
        assert result["handled"] is False and "not linked" in result["note"]

    def test_a_repository_only_reaches_issues_in_its_own_workspace(self, client, db, secret):
        """Two tenants both have a PAY-1. The repository is linked to the first; a merge
        there must not move the second tenant's PAY-1."""
        _linked_board(client, db)
        headers_b, ws_b, _, list_b = pm.setup_board(client, db, "bob", "bravo", "PAY")
        pm.issue(client, headers_b, ws_b, list_b["id"], title="Bravo's PAY-1")

        pm.ok(_deliver(client, _pr("closed", merged=True, state="closed")))
        board_b = pm.board(client, headers_b, ws_b, list_b["id"])
        assert pm.column_titles(board_b, "To Do") == ["Bravo's PAY-1"]

    def test_one_repository_cannot_be_linked_by_two_workspaces(self, client, db):
        _linked_board(client, db)
        headers_b, ws_b, _, _ = pm.setup_board(client, db, "bob", "bravo", "OPS")
        response = client.post(f"/workspaces/{ws_b}/repositories",
                               json={"full_name": "ACME/payments"}, headers=headers_b)
        assert response.status_code == 409
