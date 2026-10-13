"""Unit tests for the password policy — the single validator behind all four
password entry points.

Mirrors pmt-app/src/lib/passwordRules.ts rule-for-rule. SEC-7 added a 72-BYTE
maximum here on 2026-08-27 that the SPA lacked until 2026-09-29; the SPA's character classes
were ASCII until the same day, so an accented letter counted as a symbol there and not here.
Both now match: Unicode classes, and the byte cap. The byte rules live in
`test_password_unification.py`; this file stays the mirror of the character rules.
"""
import pytest

from utils.password import hash_password, validate_password_policy, verify_password

pytestmark = pytest.mark.unit


class TestValidatePasswordPolicy:
    def test_accepts_a_fully_compliant_password(self):
        assert validate_password_policy("Abcdef123!@#") == []

    def test_flags_a_password_that_is_too_short(self):
        errors = validate_password_policy("Abc1!")
        assert any("at least" in e for e in errors)

    def test_requires_an_uppercase_letter(self):
        assert any("uppercase" in e for e in validate_password_policy("abcdef123!@#"))

    def test_requires_a_lowercase_letter(self):
        assert any("lowercase" in e for e in validate_password_policy("ABCDEF123!@#"))

    def test_requires_a_digit(self):
        assert any("number" in e for e in validate_password_policy("Abcdefghi!@#"))

    def test_requires_a_special_character(self):
        assert any("special" in e for e in validate_password_policy("Abcdef1234567"))

    def test_rejects_whitespace(self):
        assert any("whitespace" in e for e in validate_password_policy("Abcdef 123!@#"))

    def test_reports_every_failing_rule_at_once(self):
        # "short" fails length, uppercase, digit and special simultaneously.
        assert len(validate_password_policy("short")) >= 4


def test_hash_password_is_verifiable_and_not_plaintext():
    # Checked through `verify_password` rather than through the hashing library, which is
    # the whole of SEC-7 in one line: this used to reach for passlib's `pwd_context` here
    # while the login path used `bcrypt.checkpw`, so the test verified a way round that no
    # caller took, and the two could have disagreed without failing anything.
    hashed = hash_password("Abcdef123!@#")
    assert hashed != "Abcdef123!@#"
    assert verify_password("Abcdef123!@#", hashed)
    assert not verify_password("wrong-password", hashed)
