"""The trust boundary of the GitHub webhook, with nothing running.

`verify_signature` is the webhook's entire authentication (the route is outside JWT auth),
and `extract_issue_keys` decides which tickets a pull request can move to Done. A false
positive in either is a stranger or a stray branch name changing someone's board.
"""
import hashlib
import hmac

import pytest

from integrations.github import parse_pull_request, verify_signature
from utils.issue_keys import MAX_KEYS_PER_TEXT, extract_issue_keys

pytestmark = pytest.mark.unit

SECRET = "s3cret-for-tests"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_accepts_the_signature_github_computes(self):
        body = b'{"zen": "Keep it logically awesome."}'
        assert verify_signature(SECRET, body, _sign(body))

    def test_refuses_everything_when_no_secret_is_configured(self):
        """An empty key is a key anyone can compute a valid HMAC with."""
        body = b"{}"
        assert not verify_signature("", body, _sign(body, secret=""))

    @pytest.mark.parametrize("header", [None, "", "sha256=", "sha1=abc", "deadbeef"])
    def test_refuses_missing_and_malformed_headers(self, header):
        assert not verify_signature(SECRET, b"{}", header)

    def test_refuses_the_legacy_sha1_header_even_when_correct(self):
        body = b"{}"
        legacy = "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
        assert not verify_signature(SECRET, body, legacy)

    def test_a_reserialised_body_does_not_verify(self):
        """The digest is over the bytes as sent; re-dumping parsed JSON changes them."""
        sent = b'{"a": 1,  "b": 2}'
        assert not verify_signature(SECRET, b'{"a":1,"b":2}', _sign(sent))


class TestIssueKeys:
    def test_finds_keys_in_first_seen_order_without_duplicates(self):
        assert extract_issue_keys("PAY-12 and OPS-3", "again PAY-12") == [("PAY", 12), ("OPS", 3)]

    def test_does_not_match_inside_a_longer_word_or_number(self):
        assert extract_issue_keys("XPAY-1", "PAY-12a", "PAY-0", "PAY-123456789012") == [
            ("XPAY", 1)]

    def test_lower_case_only_counts_for_branch_names(self):
        assert extract_issue_keys("fixes pay-12") == []
        assert extract_issue_keys("feature/pay-12-refunds", case_insensitive=True) == [
            ("PAY", 12)]

    def test_a_release_merge_naming_everything_is_capped(self):
        text = " ".join(f"PAY-{n}" for n in range(1, 100))
        assert len(extract_issue_keys(text)) == MAX_KEYS_PER_TEXT


def _payload(**pr):
    base = {
        "number": 7,
        "title": "PAY-12: refund to original card",
        "body": "Also touches OPS-3",
        "state": "open",
        "merged": False,
        "html_url": "https://github.com/acme/payments/pull/7",
        "head": {"ref": "feature/pay-12-refunds"},
        "user": {"login": "octocat"},
    }
    base.update(pr)
    return {"action": "opened", "pull_request": base,
            "repository": {"full_name": "acme/payments"}}


class TestParsePullRequest:
    def test_reads_the_fields_a_link_needs(self):
        event = parse_pull_request(_payload())
        assert event is not None
        assert event.external_id == "acme/payments#7"
        assert event.state == "open"
        assert event.author_login == "octocat"
        assert event.keys == [("PAY", 12), ("OPS", 3)]

    def test_merged_is_distinguished_from_closed(self):
        merged = parse_pull_request({**_payload(state="closed", merged=True), "action": "closed"})
        closed = parse_pull_request({**_payload(state="closed", merged=False), "action": "closed"})
        assert merged is not None and merged.state == "merged"
        assert closed is not None and closed.state == "closed"

    def test_ignores_actions_that_cannot_change_a_link(self):
        assert parse_pull_request({**_payload(), "action": "labeled"}) is None

    def test_a_malformed_payload_is_ignored_not_raised(self):
        """A KeyError here is a 500 to GitHub, which then retries the delivery forever."""
        assert parse_pull_request({"action": "opened"}) is None
        assert parse_pull_request({"action": "opened", "pull_request": [],
                                   "repository": {}}) is None
        event = parse_pull_request(_payload(head=None, user="nobody", body=None))
        assert event is not None and event.keys == [("PAY", 12)]
