"""Password hashing and the password policy. One primitive, one place.

This module hashed through passlib's `CryptContext` while `controllers/auth_controller.py`
and `controllers/user_controller.py` verified with `bcrypt.checkpw` directly and
`seeders/user_seeder.py` built a third `CryptContext` of its own: one primitive, four call
sites, two libraries that do not agree at the edges. They come here now.

**Existing hashes did not need re-hashing, and none was done.** passlib's `bcrypt` scheme
*is* bcrypt: it emits a standard `$2b$` modular-crypt string and hands the KDF to this same
`bcrypt` package. A row written by the old code therefore verifies under `bcrypt.checkpw`
byte for byte. `tests/unit/test_password_unification.py` pins that against a hash captured
from passlib 1.7.4 *before* it was removed, so the proof survives passlib's uninstall — the
hashes in a deployed database will outlive the library that wrote them, which is exactly
why the fixture is a frozen literal and not a call into passlib.

The work factor did not move either: passlib's bcrypt default and `bcrypt.gensalt()`'s
default are both 12 rounds (measured 2026-08-27). Had they differed, this change would have
silently re-priced every new sign-up against the rows next to it.

Dropping passlib also unpins the interpreter. passlib 1.7.4 imports the stdlib `crypt`
module at import time, and `crypt` was removed in Python 3.13:

    $ .venv/bin/python -W error::DeprecationWarning -c "import passlib.utils"
      File ".../passlib/utils/__init__.py", line 854, in <module>
        from crypt import crypt as _crypt
    DeprecationWarning: 'crypt' is deprecated and slated for removal in Python 3.13

That one import was the whole reason this service could not move off 3.12.
"""
import bcrypt

from core.config import get_settings

#: bcrypt hashes at most the first 72 bytes of a password and ignores everything after
#: them. That is a property of the algorithm — the Blowfish key schedule takes a 72-byte
#: key — not of any particular library, so no future release lifts it.
#:
#: What the libraries do *past* the limit is where they disagree, and that disagreement is
#: why the limit is refused here instead of being left to whichever one happens to run.
#: passlib truncates silently. bcrypt 4.x has changed its mind about this more than once;
#: 4.3.0, measured on 2026-08-27, also truncates silently — `bcrypt.hashpw(b"a" * 200,
#: gensalt())` returns a hash, and that hash then verifies against the first 72 bytes on
#: their own. Either way a user who types a 100-character passphrase gets 72 bytes of
#: strength and is told nothing about it. Refusing at the door is the only version of this
#: the user can act on.
BCRYPT_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """Hash a password for storage. Never call this on something you have not policed.

    Raises `ValueError` past `BCRYPT_MAX_PASSWORD_BYTES` rather than truncating. Every
    entry point in the service runs `validate_password_policy` first, so the raise is a
    backstop and not a code path a user can reach — but a backstop that truncates instead
    would store a hash of the first 72 bytes of what the caller typed, and nothing
    anywhere would ever say so. Loud beats quietly-weaker.
    """
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        # No password material in the message: this can surface in a log or a 500 body.
        raise ValueError(
            f"Password is {len(encoded)} bytes; bcrypt hashes at most "
            f"{BCRYPT_MAX_PASSWORD_BYTES}. Validate with validate_password_policy first."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Is `plain` the password behind `hashed`? False for anything that is not, including junk.

    **This returns False; it does not raise.** It is called from the login path, which is
    unauthenticated and reachable by anyone who can reach the SPA, and every raise there is
    a 500 that a caller can trigger on demand. Two real ways in:

      * a `None` — `verify_password(None, hash)` used to raise `TypeError` out of passlib,
        which `tests/live/test_auth_live.py:268` records together with the fact that this
        service has no global exception handler to turn it into an envelope;
      * a stored value that is not a bcrypt hash — an empty string, a legacy row, a column
        someone truncated in a migration. `bcrypt.checkpw` raises `ValueError("Invalid
        salt")` on those, so an unverifiable account would answer 500 instead of "wrong
        password" and thereby announce itself.

    Neither is a match, so both are False. The annotations say `str` because that is what
    callers should pass; the isinstance check is for the ones that do not.
    """
    if not isinstance(plain, str) or not isinstance(hashed, str):
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # ValueError also covers UnicodeEncodeError, which a lone surrogate in `plain`
        # raises on .encode() before bcrypt ever sees it.
        return False


def validate_password_policy(password: str) -> list[str]:
    """Every rule the password breaks, so the form can show them all at once."""
    settings = get_settings()
    errors: list[str] = []

    # The minimum counts CHARACTERS and the maximum counts BYTES, and the mismatch is
    # deliberate: the minimum is a proxy for how much a human chose, while the maximum is
    # the hard edge of what bcrypt will read. A ten-character password written in Cyrillic
    # is ten characters to the person who typed it and sixteen bytes to the algorithm;
    # counting either number for both jobs answers a different question from the one being
    # asked. (Spelled out rather than shown, because a homoglyph in a comment about
    # passwords is the one place nobody would look for it — which is what ruff's RUF003
    # exists to say.)
    #
    # Note for whoever tunes PASSWORD_MIN_LENGTH: setting it above 72 makes the two rules
    # unsatisfiable for an ASCII password, and above 36 makes them unsatisfiable for one
    # written in a two-byte script such as Cyrillic or Greek — 24 for a three-byte one such
    # as Devanagari or CJK. Nothing enforces that; it is a configuration mistake whose
    # symptom is that no password anyone types is ever accepted.
    if len(password) < settings.password_min_length:
        errors.append(f"Password must be at least {settings.password_min_length} characters long.")
    encoded_length = len(password.encode("utf-8"))
    if encoded_length > BCRYPT_MAX_PASSWORD_BYTES:
        # Bytes, not characters, and the message says so — a user whose password is
        # rejected at 40 visible characters needs to know why the count disagrees with
        # what they can see, or the only move left is to guess.
        errors.append(
            f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes long, and yours is "
            f"{encoded_length}. Accents, emoji and non-Latin scripts take 2-4 bytes per "
            "character, so a password can be too long while still looking short."
        )
    if not any(char.isupper() for char in password):
        errors.append("Password must include at least one uppercase letter.")
    if not any(char.islower() for char in password):
        errors.append("Password must include at least one lowercase letter.")
    if not any(char.isdigit() for char in password):
        errors.append("Password must include at least one number.")
    if not any(not char.isalnum() for char in password):
        errors.append("Password must include at least one special character.")
    if any(char.isspace() for char in password):
        errors.append("Password cannot contain whitespace.")

    return errors
