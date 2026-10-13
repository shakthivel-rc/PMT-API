"""Unit tests for the Fernet token helpers, which encrypt the verify and password-reset
links. Importing this module at all proves SECRET_KEY is a valid Fernet key in the test
environment."""
from urllib.parse import parse_qs, urlsplit

import pytest

from utils.token import (
    app_link,
    decode_base64,
    decrypt_string,
    encode_to_base64,
    encrypt_string,
    generate_random_code,
)

pytestmark = pytest.mark.unit


def test_encrypt_decrypt_round_trip():
    ciphertext = encrypt_string("super-secret-value")
    assert ciphertext != b"super-secret-value"
    assert decrypt_string(ciphertext) == "super-secret-value"


def test_encryption_is_non_deterministic():
    # Fernet embeds a random IV + timestamp, so two encryptions differ.
    assert encrypt_string("same") != encrypt_string("same")


def test_generate_random_code_returns_hex_of_expected_length():
    code = generate_random_code(16)
    assert len(code) == 32  # token_hex(n) → 2 hex chars per byte
    assert all(c in "0123456789abcdef" for c in code)


class TestAppLink:
    """The emailed-link builder. The end-to-end half is tests/integration/test_emailed_links.py."""

    def test_the_token_travels_as_a_query_parameter_and_comes_back_byte_identical(
            self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://app.example.com")
        # A real token: Base64 of a Fernet token, whose `=` padding must survive the URL.
        token = encode_to_base64(encrypt_string(generate_random_code(16)))

        link = urlsplit(app_link("/reset-password", token))

        assert (link.scheme, link.netloc, link.path) == (
            "https", "app.example.com", "/reset-password")
        assert parse_qs(link.query) == {"token": [token]}
        assert decrypt_string(decode_base64(token).encode()) is not None

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://app.example.com/")
        assert app_link("/verify-account", "t").startswith("https://app.example.com/verify-account?")

    def test_the_base_url_is_read_when_the_mail_is_sent_not_at_import(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://one.example.com")
        first = app_link("/reset-password", "t")
        monkeypatch.setenv("APP_BASE_URL", "https://two.example.com")
        assert urlsplit(first).netloc == "one.example.com"
        assert urlsplit(app_link("/reset-password", "t")).netloc == "two.example.com"
