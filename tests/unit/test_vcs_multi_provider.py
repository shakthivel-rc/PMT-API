"""Unit tests for GitLab and Bitbucket webhook signature verification and payload parsing."""
import hashlib
import hmac

import pytest

from integrations.bitbucket import parse_bitbucket_event, verify_bitbucket_signature
from integrations.gitlab import parse_gitlab_event, verify_gitlab_token

pytestmark = pytest.mark.unit


def test_gitlab_token_verification():
    secret = "gitlab-secret-xyz"
    assert verify_gitlab_token(secret, "gitlab-secret-xyz") is True
    assert verify_gitlab_token(secret, "wrong-token") is False
    assert verify_gitlab_token(secret, None) is False
    assert verify_gitlab_token("", "gitlab-secret-xyz") is False


def test_gitlab_pipeline_event_parsing():
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {
            "ref": "feature/PAY-42-checkout",
            "sha": "9a8b7c6d5e4f3a2b1c0d",
            "status": "success",
        },
        "project": {"path_with_namespace": "org/payment-service"},
        "user": {"name": "Jane Developer"},
        "commit": {"message": "[PAY-42] Implement secure checkout form"},
    }

    event = parse_gitlab_event("Pipeline Hook", payload)
    assert event is not None
    assert event.event_type == "pipeline"
    assert event.repository == "org/payment-service"
    assert event.ref == "feature/PAY-42-checkout"
    assert event.sha == "9a8b7c6d5e4f3a2b1c0d"
    assert event.status == "success"
    assert ("PAY", 42) in event.keys


def test_gitlab_deployment_event_parsing():
    payload = {
        "object_kind": "deployment",
        "environment": "production",
        "status": "success",
        "ref": "release/v1.4.0",
        "sha": "1234567890abcdef",
        "project": {"path_with_namespace": "org/frontend-app"},
    }

    event = parse_gitlab_event("Deployment Hook", payload)
    assert event is not None
    assert event.event_type == "deployment"
    assert event.environment == "production"
    assert event.sha == "1234567890abcdef"


def test_bitbucket_signature_verification():
    secret = "bb-secret-key"
    body = b'{"repository": {"full_name": "org/repo"}}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={sig}"

    assert verify_bitbucket_signature(secret, body, header) is True
    assert verify_bitbucket_signature(secret, body, "sha256=invalid") is False
    assert verify_bitbucket_signature(secret, body, None) is False
    assert verify_bitbucket_signature("", body, header) is False


def test_bitbucket_pull_request_parsing():
    payload = {
        "repository": {"full_name": "team/core-api"},
        "pullrequest": {
            "id": 105,
            "title": "Fix [PAY-99] deadlock on concurrent refund",
            "description": "Resolves race condition in refund queue",
            "state": "OPEN",
            "source": {"branch": {"name": "fix/pay-99"}},
            "author": {"display_name": "Bob Builder"},
        },
    }

    event = parse_bitbucket_event("pullrequest:created", payload)
    assert event is not None
    assert event.event_type == "pull_request"
    assert event.repository == "team/core-api"
    assert ("PAY", 99) in event.keys
