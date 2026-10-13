"""SEC-6 and SEC-5 through the endpoint they share: what login answers, and what it stores.

Two findings, one file, because they are two properties of one request. `POST /user/login`
must give an attacker exactly one answer no matter which account state it hit (SEC-6), and
the row it writes on success must not contain a credential (SEC-5). Splitting them would
mean seeding the same user and posting the same form twice.

What the suite already covers and this does not repeat: `tests/integration/test_auth_login.py`
owns the lockout arithmetic and the counter reset, and `tests/integration/test_auth_middleware.py`
owns the JWT decode and the allow-list. This file owns the *disclosure* properties — the ones
where the code is functionally correct either way and the difference is only visible to
somebody trying to learn something they should not.
"""
import datetime

import pytest
from sqlalchemy import text as sa_text

from controllers import auth_controller
from models.user import User
from models.user_session import UserSession, hash_session_token
from utils.password import hash_password

pytestmark = pytest.mark.integration

VALID_PASSWORD = "Abcdef123!@#"
WRONG_PASSWORD = "WrongPassword1!"


def _seed_user(db, email, username, **overrides):
    user = User(
        email=email,
        username=username,
        first_name="Ada",
        last_name="Lovelace",
        organization="Analytical Engines",
        department="R&D",
        password=hash_password(VALID_PASSWORD),
        status=True,
        deleted=0,
        failed_login_attempts=0,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    db.add(user)
    db.commit()
    return user


def _login(client, username, password=VALID_PASSWORD):
    return client.post("/user/login", json={"username": username, "password": password})


class TestEveryCredentialFailureGivesTheSameAnswer:
    """The four ways to fail a sign-in must be one answer, or login is an address book."""

    def test_unknown_unverified_locked_and_wrong_password_are_indistinguishable(
        self, client, db
    ):
        """Catches any branch of login that answers differently from the others.

        Before 2026-08-27 three of these four returned different bodies and two returned
        different statuses: an unverified account said "Verify your account to login" and a
        locked one answered 423. Both of those are a "yes, that address is registered" that
        anybody who can reach the login form can read off, one request per address, and the
        rate limiter (10/min on credential paths) bounds how fast that is walked without
        stopping it.

        Compares the whole response body rather than just the message, because `status`,
        `status_code` and `data` are all in the envelope and any of them could carry the
        distinction back in. Compares the status code separately so the failure message says
        which of the two went wrong.
        """
        _seed_user(db, "known@example.com", "known")
        _seed_user(db, "unverified@example.com", "unverified", status=False)
        _seed_user(db, "locked@example.com", "locked",
                   locked_until=datetime.datetime.utcnow() + datetime.timedelta(minutes=15))

        answers = {
            # The correct password everywhere except the wrong-password case, so that account
            # *state* is the only thing that differs between the first three.
            "no such account": _login(client, "nobody@example.com"),
            "unverified": _login(client, "unverified@example.com"),
            "locked": _login(client, "locked@example.com"),
            "wrong password": _login(client, "known@example.com", WRONG_PASSWORD),
        }

        statuses = {name: response.status_code for name, response in answers.items()}
        assert set(statuses.values()) == {401}, f"the status code discloses the account: {statuses}"

        bodies = {name: response.json() for name, response in answers.items()}
        assert len({repr(body) for body in bodies.values()}) == 1, (
            f"the response body discloses the account: {bodies}"
        )
        assert bodies["no such account"]["message"] == "Invalid username or password"

    def test_the_submitted_address_is_never_echoed_back(self, client):
        """An address reflected in the response is the same disclosure by another route.

        A message like "no account for x@y" confirms the address exists the moment it stops
        being echoed for the ones that do. Nothing echoes it today; this fails the moment
        something starts.
        """
        response = _login(client, "nobody-here@example.invalid")

        assert response.status_code == 401
        assert "nobody-here@example.invalid" not in response.text

    def test_an_unknown_account_still_pays_for_a_password_comparison(
        self, client, monkeypatch
    ):
        """Catches the early return that makes the miss branch measurably faster.

        This is the half of SEC-6 that no amount of matching the response body fixes. A
        known account spends ~0.43 s in bcrypt (measured 2026-08-27 at the shipped 12
        rounds); an unknown one has no stored hash to compare against and, before this fix,
        returned in microseconds. That is the same oracle, readable with a stopwatch instead
        of a diff, and it needs exactly one request per address.

        `verify_password` is stubbed rather than run: what is being asserted is that the call
        *happens*, against the pre-computed dummy hash. That the dummy hash is a real bcrypt
        string of the current cost — which is what makes the call take the same time as the
        real one — is asserted in `tests/unit/test_session_token_hashing.py`, where it costs
        nothing to check.
        """
        calls = []
        monkeypatch.setattr(auth_controller, "verify_password",
                            lambda plain, hashed: calls.append((plain, hashed)) or False)

        response = _login(client, "nobody@example.com", WRONG_PASSWORD)

        assert response.status_code == 401
        assert calls == [(WRONG_PASSWORD, auth_controller._DUMMY_PASSWORD_HASH)], (
            "the unknown-account branch returned without spending a bcrypt comparison"
        )

    def test_the_log_line_keeps_the_distinction_the_response_gave_up(self, client, db, caplog):
        """The operator must still be able to tell these apart; only the attacker may not.

        Flattening the response is only affordable because the reason moves somewhere an
        attacker cannot read. If that log line goes too — an editor tidying an "unused"
        argument, say — then support answering "why can I not log in?" has nothing left, and
        the honest trade recorded in `login_controller` quietly becomes a straight loss.
        """
        _seed_user(db, "locked@example.com", "locked",
                   locked_until=datetime.datetime.utcnow() + datetime.timedelta(minutes=15))

        with caplog.at_level("INFO", logger="base_project.auth"):
            assert _login(client, "locked@example.com").status_code == 401

        reasons = [getattr(record, "extra_fields", {}).get("reason") for record in caplog.records]
        assert "account locked" in reasons, f"nothing in the log says why: {reasons}"


class TestASessionRowIsNotASetOfCredentials:
    """SEC-5: the row that backs a session must not contain anything that authenticates."""

    def test_a_successful_login_writes_no_raw_token_to_any_column(self, client, db):
        """Catches a raw token being stored again, in any column, mapped or not.

        `SELECT *` rather than reading the ORM object on purpose: a column the model has
        stopped mapping is still a column in the backup, and this finding was about what a
        backup, a replica or a support query can read — not about what the application
        happens to load. Both tokens are searched for, because the refresh token was the one
        that sat in plaintext beside its own hash for months.
        """
        _seed_user(db, "ada@example.com", "ada")
        response = _login(client, "ada@example.com")
        assert response.status_code == 200
        issued = response.json()["data"]

        db.expire_all()
        rows = db.execute(sa_text("SELECT * FROM user_sessions")).mappings().all()
        assert len(rows) == 1

        for column, value in rows[0].items():
            for name, token in (("access", issued["access_token"]),
                                ("refresh", issued["refresh_token"])):
                assert token not in str(value), f"user_sessions.{column} holds the raw {name} token"

        assert rows[0]["access_token_hash"] == hash_session_token(issued["access_token"])
        assert rows[0]["refresh_token_hash"] == hash_session_token(issued["refresh_token"])

    def test_the_token_issued_by_login_authenticates_the_next_request(self, client, db):
        """The hash is written and read by the same definition, proven the only way that counts.

        Every other assertion about hashing in this suite compares a hash to a hash. This one
        compares a *write* to a *read*: login writes `access_token_hash` and the middleware
        looks a row up by it, in two different modules. If those two ever disagreed, the
        login response would still say "success" and every request after it would be a 401 —
        which is exactly the failure that has no error message anywhere.
        """
        _seed_user(db, "ada@example.com", "ada")
        token = _login(client, "ada@example.com").json()["data"]["access_token"]

        assert client.get("/", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    def test_logout_stops_the_token_working_immediately(self, client, db):
        """Business rule 3, restated against the indexed lookup that replaced the scan.

        A signed, unexpired JWT whose session row is gone must stop authenticating on the
        very next request — that property is the entire reason the middleware pays for a
        database round trip it could otherwise skip, and moving the lookup from
        `access_token` to `access_token_hash` is exactly the kind of change that could break
        it by having logout delete a row nothing was going to match anyway.
        """
        _seed_user(db, "ada@example.com", "ada")
        token = _login(client, "ada@example.com").json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/", headers=headers).status_code == 200

        assert client.post("/user/logout", headers=headers).status_code == 200

        rejected = client.get("/", headers=headers)
        assert rejected.status_code == 401
        assert rejected.json()["message"] == "Invalid session or token revoked"

    def test_revoking_the_row_stops_the_token_working_immediately(self, client, db):
        """The other half of rule 3: revocation, which is what refresh and reset both use.

        `set_password_after_registration_controller` and the password-reset path revoke by
        `UPDATE ... SET revoked_at` rather than deleting, so a lookup that forgot the
        `revoked_at IS NULL` filter would leave every session alive through a password reset
        — the one moment a user is most likely to be resetting *because* something is wrong.
        """
        _seed_user(db, "ada@example.com", "ada")
        token = _login(client, "ada@example.com").json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/", headers=headers).status_code == 200

        db.expire_all()
        session = db.query(UserSession).filter(
            UserSession.access_token_hash == hash_session_token(token)
        ).one()
        session.revoked_at = datetime.datetime.utcnow()
        db.commit()

        assert client.get("/", headers=headers).status_code == 401
