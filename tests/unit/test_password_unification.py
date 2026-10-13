"""SEC-7: one password primitive, and a maximum length measured in bytes.

Before this, hashing went through passlib's `CryptContext`, verification called
`bcrypt.checkpw` directly in two controllers, and the seeder built a third `CryptContext`
of its own — one primitive, four call sites, two libraries whose behaviour past 72 bytes
differs. These tests pin the four things that were wrong with that:

  * that the migration off passlib is not a migration at all for the stored data, so a
    database seeded last year still signs in (`TestExistingHashesStillVerify`);
  * that a password too long for bcrypt to read is refused rather than silently truncated,
    and that the limit is counted in BYTES, which is the only count bcrypt has
    (`TestTheSeventyTwoByteLimit`);
  * that `verify_password` answers "no" instead of raising, because it is called from an
    unauthenticated endpoint in a service with no global exception handler
    (`TestVerifyPasswordNeverRaises`);
  * that the call sites do not drift apart again (`TestOnePrimitive`).
"""
import pathlib
import re

import pytest

from utils.password import (
    BCRYPT_MAX_PASSWORD_BYTES,
    hash_password,
    validate_password_policy,
    verify_password,
)

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: A real hash of `KNOWN_PASSWORD`, produced by passlib 1.7.4's
#: `CryptContext(schemes=["bcrypt"], deprecated="auto")` on 2026-08-27 — the exact code
#: that was deleted from `utils/password.py` — and pasted here as a literal.
#:
#: A literal, and not a call into passlib, on purpose. The whole claim under test is that
#: rows written by the old library keep working after that library is gone, and a test that
#: imports passlib to build its fixture cannot be run in the world where passlib is gone.
#: It would also keep passlib in `requirements.txt` to serve one test, which is the pin
#: this change exists to remove.
PASSLIB_ERA_HASH = "$2b$12$CdIhJjd3.PJ.pFh1Oy64POkmODj0IMXauhY.Inn78G0/fYpAOSEJW"
KNOWN_PASSWORD = "Abcdef123!@#"

#: The same, for a 100-character password. passlib accepted it and hashed the first 72
#: bytes without saying anything. This hash is the evidence; the test named
#: `test_the_silent_truncation_this_limit_exists_to_prevent` shows what it means.
PASSLIB_ERA_HASH_OF_100_CHARS = "$2b$12$BC2BZ1U.wtGWFOd8W9z79ulyu7KqpMV92b777am4dVnKm2LRg9uKC"
HUNDRED_CHARACTER_PASSWORD = "Aa1!" + "x" * 96

#: One e-acute, written as an escape rather than as the character, because every assertion
#: below is a byte count and this is the character those counts turn on. There are two ways
#: to write it: U+00E9, which is 2 bytes in UTF-8, or a plain "e" followed by U+0301
#: COMBINING ACUTE ACCENT, which is 3. They look identical on screen and in a diff, and a
#: normalising editor or a copy-paste through the wrong tool will silently exchange one for
#: the other. The escape says which one is meant, so a test that starts failing for that
#: reason is readable rather than baffling.
E_ACUTE = "\u00e9"


class TestExistingHashesStillVerify:
    """No re-hash, no migration, no flag day — and this is what makes that claim checkable.

    If passlib's `bcrypt` scheme had been anything other than plain bcrypt, every account
    in every deployed database would have stopped signing in the moment this module
    changed — with no error anywhere, and no symptom except "wrong password" for everyone
    at once. That is not a hypothetical: the same library ships `bcrypt_sha256`, which
    HMAC-pre-hashes the password and writes a `$bcrypt-sha256$` prefix, and it is one word
    away in the `schemes=` list that used to be at the top of `utils/password.py`.
    """

    def test_a_passlib_era_hash_verifies_under_the_bcrypt_implementation(self):
        assert verify_password(KNOWN_PASSWORD, PASSLIB_ERA_HASH) is True

    def test_and_still_refuses_the_wrong_password(self):
        """The complement matters as much: an implementation that returned True for
        everything would pass the test above and open every account in the service."""
        assert verify_password("Abcdef123!@$", PASSLIB_ERA_HASH) is False

    def test_a_hash_this_module_writes_verifies_under_this_module(self):
        hashed = hash_password(KNOWN_PASSWORD)
        assert hashed != KNOWN_PASSWORD
        assert verify_password(KNOWN_PASSWORD, hashed) is True
        assert verify_password("wrong-password", hashed) is False

    def test_the_stored_format_is_unchanged(self):
        """Same `$2b$` identifier and same 12 rounds as passlib emitted.

        The cost factor is not cosmetic: dropping it to 4 would leave every test green
        while making the hashes ~256x cheaper to attack, and nothing else in this
        repository looks at the number. `$2b$12$` is what the rows already in the database
        start with, so new rows that differ would be the visible half of that mistake.
        """
        assert hash_password(KNOWN_PASSWORD).startswith("$2b$12$")
        assert PASSLIB_ERA_HASH.startswith("$2b$12$")


class TestTheSeventyTwoByteLimit:
    def test_a_seventy_two_byte_password_is_accepted(self):
        """The boundary is inclusive: 72 bytes is exactly what bcrypt reads, so it is
        legal. An off-by-one here rejects a password bcrypt handles perfectly well."""
        password = "Aa1!" + "x" * 68
        assert len(password.encode("utf-8")) == BCRYPT_MAX_PASSWORD_BYTES
        assert validate_password_policy(password) == []

    def test_seventy_three_bytes_is_refused(self):
        password = "Aa1!" + "x" * 69
        assert len(password.encode("utf-8")) == BCRYPT_MAX_PASSWORD_BYTES + 1
        errors = validate_password_policy(password)
        assert any("72 bytes" in error for error in errors)

    def test_the_limit_counts_bytes_and_not_characters(self):
        """The specific bug a character-counting maximum would have: this password is 40
        characters, comfortably under any plausible character limit, and 76 bytes — past
        what bcrypt will read. A policy written as `len(password) > 72` accepts it, and
        the last 4 bytes the user typed then silently do not exist.

        Every non-ASCII alphabet is affected, and the accented Latin here is the mildest
        case at 2 bytes per character. Devanagari and CJK are 3, an emoji is 4 — so an
        18-emoji passphrase is over the line while looking about as short as a password
        can look.
        """
        password = "Aa1!" + E_ACUTE * 36
        assert len(password) == 40
        assert len(password.encode("utf-8")) == 76
        errors = validate_password_policy(password)
        assert any("72 bytes" in error for error in errors)

    def test_a_non_ascii_password_under_the_limit_is_still_accepted(self):
        """The complement of the above, and the reason the rule is bytes rather than
        "ASCII only": an accented password that fits is a perfectly good password. 38
        characters, exactly 72 bytes — the same boundary as the ASCII case, reached at a
        different character count, which is the point."""
        password = "Aa1!" + E_ACUTE * 34
        assert len(password) == 38
        assert len(password.encode("utf-8")) == BCRYPT_MAX_PASSWORD_BYTES
        assert validate_password_policy(password) == []

    def test_the_error_message_tells_the_user_what_to_do_about_it(self):
        """A user staring at 40 characters and a "too long" refusal has no next move
        unless the message explains the disagreement. This asserts it does."""
        errors = validate_password_policy("Aa1!" + E_ACUTE * 36)
        message = next(error for error in errors if "72 bytes" in error)
        assert "76" in message, "say how far over they are"
        assert "character" in message, "say why the count disagrees with what they can see"

    def test_hash_password_refuses_rather_than_truncating(self):
        """The backstop behind the policy. bcrypt 4.3.0 truncates silently and passlib
        truncated silently, so if `hash_password` did nothing here the service would store
        a 72-byte-strong hash of a 100-character password and report success."""
        with pytest.raises(ValueError, match="72"):
            hash_password(HUNDRED_CHARACTER_PASSWORD)

    def test_the_silent_truncation_this_limit_exists_to_prevent(self):
        """Not a test of our code — a demonstration of the thing being defended against.

        `PASSLIB_ERA_HASH_OF_100_CHARS` was produced from a 100-character password. The
        first 72 bytes of that password verify against it on their own, which is the whole
        problem stated as an assertion: the other 28 characters were never hashed, the user
        was never told, and an attacker only ever needed the first 72 bytes.
        """
        first_72 = HUNDRED_CHARACTER_PASSWORD[:BCRYPT_MAX_PASSWORD_BYTES]
        assert verify_password(first_72, PASSLIB_ERA_HASH_OF_100_CHARS) is True
        assert verify_password(HUNDRED_CHARACTER_PASSWORD, PASSLIB_ERA_HASH_OF_100_CHARS) is True


class TestVerifyPasswordNeverRaises:
    """`verify_password` is called from `POST /user/login`, which is unauthenticated.

    `tests/live/test_auth_live.py:268` records the live version of this: `verify_password(
    None, hash)` raised inside passlib, and with no `@app.exception_handler` in `main.py`
    an exception there becomes a bare text/plain 500 outside the CORS and CSP middleware —
    a 500 any anonymous caller can produce on demand. Returning False costs nothing and is
    also the correct answer, because none of these inputs is the password.
    """

    def test_a_none_password_is_false(self):
        assert verify_password(None, PASSLIB_ERA_HASH) is False

    def test_a_none_hash_is_false(self):
        assert verify_password(KNOWN_PASSWORD, None) is False

    def test_a_hash_that_is_not_a_hash_is_false(self):
        """`bcrypt.checkpw` raises ValueError("Invalid salt") on these. A row whose
        password column was truncated by a migration, or written by some other tool, must
        be un-signin-able — not a 500 that tells the caller this account is special."""
        for junk in ("", "not-a-hash", "$2b$", "$2b$12$", "plaintext-password"):
            assert verify_password(KNOWN_PASSWORD, junk) is False

    def test_a_non_string_password_is_false(self):
        for junk in (0, b"Abcdef123!@#", [], {}):
            assert verify_password(junk, PASSLIB_ERA_HASH) is False

    def test_an_empty_password_is_false_rather_than_an_error(self):
        assert verify_password("", PASSLIB_ERA_HASH) is False


# ------------------------------------------------------------------------------------
# The gate: it stays one primitive
# ------------------------------------------------------------------------------------

#: Every file allowed to reach a hashing library directly. It may shrink and must not grow
#: — the same ratchet rule as `ruff-suppressions.json`, for the same reason.
#:
#: `controllers/auth_controller.py` is on it for two distinct reasons, only one of which is
#: a leftover. It hashes *OTP codes* with `bcrypt.hashpw`/`checkpw` (`_hash_otp`,
#: `_verify_otp`), which is a different secret with a different lifetime and is not what
#: SEC-7 unified. It ALSO still defines its own `verify_password` over `bcrypt.checkpw`,
#: which is SEC-7's fourth call site and belongs in `utils.password` — that file is owned
#: by another change in this pass, so this list permits it rather than pretending it is
#: gone. Removing the entry entirely once the OTP helpers move too is the end state.
FILES_ALLOWED_TO_CALL_A_HASHING_LIBRARY = {
    "utils/password.py",
    "controllers/auth_controller.py",
}

#: `bcrypt.hashpw(`, `bcrypt.checkpw(`, or a passlib context being built.
DIRECT_HASHING_CALL = re.compile(r"\bbcrypt\.(hashpw|checkpw)\s*\(|\bCryptContext\s*\(")

#: Import *syntax*, not the word appearing in prose — matched with the `import` keyword on
#: both forms, so a wrapped docstring line beginning "from passlib 1.7.4 ..." is not a
#: finding. `utils/password.py` and `tests/live/test_auth_live.py` both name passlib in
#: comments on purpose, to explain why it is no longer imported; a check that cannot tell
#: those from an import would be answered by deleting the explanation, which is the wrong
#: way round.
PASSLIB_IMPORT = re.compile(
    r"^\s*(?:from\s+passlib(?:\.\w+)*\s+import\b|import\s+passlib(?:\.\w+)*\s*$)",
    re.MULTILINE,
)

SKIPPED_DIRECTORIES = {".venv", "venv", "myenv", ".git", "__pycache__", "node_modules"}


def _project_sources():
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if SKIPPED_DIRECTORIES.intersection(relative.parts):
            continue
        yield relative, path.read_text(encoding="utf-8")


class TestOnePrimitive:
    def test_nothing_new_calls_a_hashing_library_directly(self):
        """`tests/` is exempt on purpose: a test that builds a fixture hash with bcrypt
        directly is checking the primitive, not routing around it. Everything that runs in
        production is in scope."""
        offenders = sorted(
            str(relative)
            for relative, source in _project_sources()
            if DIRECT_HASHING_CALL.search(source)
            and str(relative) not in FILES_ALLOWED_TO_CALL_A_HASHING_LIBRARY
            and not str(relative).startswith("tests/")
        )
        assert not offenders, (
            f"{offenders} hash or verify without utils.password. That is how this codebase "
            "ended up with two implementations that disagreed past 72 bytes; use "
            "hash_password/verify_password."
        )

    def test_no_module_imports_passlib_any_more(self):
        """The assertion the orchestrator needs before dropping the pin from
        requirements.txt — and the one that keeps this service able to run on Python 3.13,
        since passlib 1.7.4 imports the stdlib `crypt` module that 3.13 removed."""
        importers = sorted(
            str(relative)
            for relative, source in _project_sources()
            if PASSLIB_IMPORT.search(source)
        )
        assert importers == []

    def test_the_seeder_hashes_through_the_shared_helper(self):
        """It built its own `CryptContext` — a second set of defaults for the one account
        that exists before anyone can log in to change it."""
        source = (ROOT / "seeders" / "user_seeder.py").read_text(encoding="utf-8")
        assert "from utils.password import hash_password" in source
        assert "pwd_context" not in source

    def test_the_change_password_endpoint_verifies_through_the_shared_helper(self):
        source = (ROOT / "controllers" / "user_controller.py").read_text(encoding="utf-8")
        assert "verify_password(data.current_password, user.password)" in source

    def test_the_seeded_superadmin_password_can_be_verified_by_the_login_path(self):
        """End to end across the two files that used to disagree: hash the way the seeder
        now hashes, verify the way the login path verifies. A drift between them locks
        everyone out of a fresh install, which is the failure that has no way back in."""
        from seeders.user_seeder import hash_password as seeder_hash

        assert verify_password(KNOWN_PASSWORD, seeder_hash(KNOWN_PASSWORD)) is True
