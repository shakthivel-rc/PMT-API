"""Secret redaction.

`core/redact.py` runs over every log record on its way to stdout
(`core/logging.SecretRedactingFilter`), so a miss here writes a live credential into a log
that is shipped, retained and read by people who were never meant to hold it. The shapes
below are the ones this service's logs can actually carry: exception text from a driver or
client that quotes the URL or DSN it failed on, a settings or environment dump, and a request
header or form field echoed into an error.
"""
import pytest

from core.redact import MASK, redact

pytestmark = pytest.mark.unit

GITHUB_TOKEN = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEpAIBAAKCAQEA1234\n"
    "abcd\n"
    "-----END RSA PRIVATE KEY-----"
)


class TestKnownSecretShapes:
    @pytest.mark.parametrize(
        ("line", "secret"),
        [
            ("Using AKIAIOSFODNN7EXAMPLE for upload", "AKIAIOSFODNN7EXAMPLE"),
            (f"token: {GITHUB_TOKEN}", GITHUB_TOKEN),
            ("GITLAB_TOKEN glpat-xxxxxxxxxxxxxxxxxxxx", "glpat-xxxxxxxxxxxxxxxxxxxx"),
            ("webhook xoxb-123456789012-abcdefghijklm", "xoxb-123456789012-abcdefghijklm"),
            ("model call failed with sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wx-yz",
             "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wx-yz"),
            (f"Bearer {JWT}", JWT),
        ],
    )
    def test_the_secret_never_survives(self, line, secret):
        cleaned = redact(line)
        assert secret not in cleaned
        assert MASK in cleaned

    def test_a_password_in_a_dsn_is_masked_but_the_host_survives(self):
        """A driver's connection error quotes the DSN it failed on. The host is diagnostic —
        "could not connect to db.example.com" is the useful half."""
        cleaned = redact(
            "could not connect to mysql+pymysql://app_user:hunter2secret@db.example.com/app"
        )
        assert "hunter2secret" not in cleaned
        assert "db.example.com" in cleaned
        assert "app_user" in cleaned

    def test_a_settings_dump_is_masked(self):
        """A settings or environment dump prints every assignment, and the prefixed names
        (`DEPLOY_TOKEN`, `SMTP_PASSWORD`) are how real secret variables are spelled."""
        cleaned = redact(
            "API_KEY=abcdef123456\nSMTP_PASSWORD=pw-value-9876\nDEPLOY_TOKEN=zzz-secret-value"
        )
        assert "abcdef123456" not in cleaned
        assert "pw-value-9876" not in cleaned
        assert "zzz-secret-value" not in cleaned

    def test_a_password_field_echoed_into_an_error_is_masked(self):
        cleaned = redact("rejected form body: email=a%40example.com&password=hunter2secret")
        assert "hunter2secret" not in cleaned
        assert "email=a%40example.com" in cleaned

    def test_a_private_key_block_is_masked_whole(self):
        """The armoured body, not just the header — a masked header over a live key body
        is worse than no masking, because it looks handled."""
        cleaned = redact(PRIVATE_KEY)
        assert "MIIEpAIBAAKCAQEA1234" not in cleaned
        assert "PRIVATE-KEY" in cleaned

    def test_an_authorization_header_is_masked_whatever_the_scheme(self):
        for scheme in ("Bearer", "Basic", "token"):
            cleaned = redact(f"Authorization: {scheme} s3cr3tvalue123")
            assert "s3cr3tvalue123" not in cleaned


class TestSiteSuppliedSecrets:
    def test_a_named_literal_is_masked(self):
        """The escape hatch for a secret with no shape: a key issued as a plain word."""
        cleaned = redact("upstream rejected key orchid-harbour-42",
                         extra_secrets=["orchid-harbour-42"])
        assert "orchid-harbour-42" not in cleaned

    def test_a_very_short_value_is_ignored(self):
        """Masking every occurrence of a 3-character "secret" would shred the log without
        protecting anything."""
        assert redact("the cat sat on the mat", extra_secrets=["cat"]) == "the cat sat on the mat"


class TestOrdinaryOutputSurvives:
    def test_a_normal_log_line_is_untouched(self):
        line = "request_completed method=GET path=/users status=200"
        assert redact(line) == line

    def test_an_ordinary_assignment_is_not_mistaken_for_a_secret(self):
        line = "PAGE_SIZE=25"
        assert redact(line) == line

    def test_empty_input_is_returned_as_is(self):
        assert redact("") == ""
