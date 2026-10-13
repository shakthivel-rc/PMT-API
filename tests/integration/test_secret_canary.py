"""A credential submitted to the API must not survive anywhere the API can write it.

The credential here is the one every deployment of this service handles: a password. The
scenario signs a user in and has them change it, which sends two plaintext passwords through
the middleware stack, the controllers and the activity log. Afterwards neither may appear in
any row of any table in `Base.metadata`, nor in any log record.

Walking the metadata rather than naming columns is the point: a new column that starts
carrying request text fails on the commit that adds it rather than on the day someone reads
a row.

**What that does and does not buy, stated precisely.** Walking the metadata guarantees the
scan *reaches* every table. It does not put a row in one — an assertion over an empty table
is true and carries no information. `test_the_scan_is_not_vacuous` below pins which tables
the scenario actually fills, so that is a measurement rather than a claim, and if a refactor
empties one of them the test says so.

The row scan cannot see the logs, so they are checked here as well: the password hash
protects the database, and a `logger.info(payload)` added in good faith during a debugging
session writes the same secret to stdout, where it is shipped to whatever collects container
logs and is not subject to retention.

Plain `def` tests, deliberately: the suite runs no async test plugin, and an `async def`
test would be collected as a coroutine nobody awaits.
"""
import pytest

from db.session import Base
from models.user import User
from utils.password import hash_password

pytestmark = pytest.mark.integration

#: Both satisfy the password policy, so the scenario reaches the code that stores them
#: rather than stopping at a validation error that stores nothing.
PASSWORD = "Canary!Passw0rd-9f3b21c7"
NEW_PASSWORD = "Canary!Rotated-5e1d77aa"

EMAIL = "canary@example.com"


def _every_stored_string(db) -> str:
    """Every value in every row of every table, as one blob of text.

    Deliberately indiscriminate. The question this test asks is not "did the column I
    thought of stay clean" — it is "is the secret anywhere at all".
    """
    chunks: list[str] = []
    for table in Base.metadata.sorted_tables:
        for row in db.execute(table.select()):
            chunks.extend("" if value is None else str(value) for value in row)
    return "\n".join(chunks)


def _sign_in_and_rotate(client, db) -> None:
    """Sign in, then change the password — two plaintext credentials through the API.

    No logout at the end: it deletes the session row, and the scan wants that row there.
    """
    db.add(User(
        email=EMAIL,
        username="canary",
        first_name="Canary",
        last_name="User",
        organization="Org",
        department="Ops",
        password=hash_password(PASSWORD),
        status=True,
        deleted=0,
        failed_login_attempts=0,
    ))
    db.commit()

    login = client.post("/user/login", json={"username": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.json()
    token = login.json()["data"]["access_token"]

    changed = client.post(
        "/users/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD,
              "confirm_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert changed.status_code == 200, changed.json()


class TestSecretCanary:
    def test_a_submitted_password_reaches_no_table_and_no_log(self, client, db, caplog):
        with caplog.at_level("DEBUG"):
            _sign_in_and_rotate(client, db)

        db.expire_all()
        stored = _every_stored_string(db)

        # Not vacuous: the scenario has to have stored something, or a flow that silently
        # wrote nothing would pass every assertion below.
        assert EMAIL in stored, "the scenario stored nothing — this would pass for the wrong reason"

        for secret in (PASSWORD, NEW_PASSWORD):
            assert secret not in stored, f"{secret!r} survived into a database row"

        logged = "\n".join(record.getMessage() for record in caplog.records)
        logged += "\n".join(str(getattr(r, "extra_fields", "")) for r in caplog.records)
        for secret in (PASSWORD, NEW_PASSWORD):
            assert secret not in logged, f"{secret!r} was written to the logs"

    def test_the_scan_is_not_vacuous(self, client, db):
        """Name the tables this scenario actually fills.

        A canary that scans empty tables passes perfectly and proves nothing. This records
        the reach of the scenario above so the claim in the docstring is a measurement
        rather than an assumption, and so a change that stops populating one of them is
        visible instead of silently reducing the test to a formality.
        """
        _sign_in_and_rotate(client, db)
        db.expire_all()

        populated = {
            table.name
            for table in Base.metadata.sorted_tables
            if db.execute(table.select().limit(1)).first() is not None
        }
        # The account itself; the session the sign-in opened; and the activity log, which
        # receives both a LOGIN and a SECURITY (password changed) entry. Every one of them
        # held a row, so the canary assertions over them mean something.
        for name in ("users", "user_sessions", "activity_logs"):
            assert name in populated, f"{name} is empty — the canary over it proves nothing"
