"""
Shared pytest configuration and fixtures for the Base Project API.

CRITICAL ORDERING: environment variables are set at the very top, BEFORE importing any
application module. core/config.py, db/session.py and utils/token.py all read os.environ
at *import* time (utils/token.py even builds Fernet(SECRET_KEY) at import), so the values
must exist first.

By default the whole suite runs against a throwaway in-memory SQLite database, so unit
AND integration tests run anywhere with zero infrastructure. Set TEST_DATABASE_URL to
point the integration suite at a real MySQL (as the CI workflow does).
"""
import base64
import os

# A valid 32-byte Fernet key (also serves as the HS256 JWT secret).
os.environ.setdefault("SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
# The inbound rate limiter counts per client address, in this process, and every request
# a TestClient makes comes from the same one. A suite that signs in a few hundred times
# would exhaust a 10-per-minute credential budget somewhere around the fortieth test and
# then fail whatever ran next, for a reason unrelated to what it asserts. Off by default
# here; `tests/unit/test_rate_limit.py` builds its own app with it on and is where the
# limiter's behaviour is pinned.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite://")
os.environ["DATABASE_URL"] = _TEST_DB_URL

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# Importing main registers every model on Base.metadata (via the router import chain)
# and builds the FastAPI app. Nothing else starts: the app has no lifespan, and the
# TestClient below is not entered with `with`, so one a derived project adds stays out.
import db.session as db_session  # noqa: E402
from main import app  # noqa: E402

_IS_SQLITE = _TEST_DB_URL.startswith("sqlite")

if _IS_SQLITE:
    # One shared in-memory connection, reachable from the TestClient's portal thread and
    # from the worker threads `asyncio.to_thread` dispatches to.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    # READ COMMITTED, deliberately, and only in the test harness.
    #
    # MySQL defaults to REPEATABLE READ: a transaction's first read pins a snapshot, and
    # every later read in that transaction sees it rather than what other connections have
    # since committed. Production is unaffected — a request opens a session, does its work
    # and closes it — but a test holds ONE inspecting session for its whole body while the
    # code under test commits from a request handler or a worker thread. Under REPEATABLE
    # READ those commits are invisible, so `db.query(...)` returns None for a row that
    # demonstrably exists and the failure reads as "the endpoint did not write it".
    #
    # `expire_all()` does not help: it clears the identity map, not the snapshot.
    #
    # This makes the MySQL harness behave like the SQLite one, which is the point of being
    # able to run the suite either way. It changes no application code path.
    engine = create_engine(_TEST_DB_URL, pool_pre_ping=True,
                           isolation_level="READ COMMITTED")

if _IS_SQLITE:
    # SQLite does not enforce foreign keys unless asked, once per connection. Without this
    # the test database is *more permissive than production*, which is the worst direction
    # for a test database to differ in: a delete that MySQL refuses with error 1451 passes
    # here silently, and the bug ships. Turning it on cost one real defect the first time it
    # was switched on, which is the argument for it.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Re-point every module that captured the original engine/SessionLocal at import time
# so the app, the JWT middleware and get_db all talk to the test database.
db_session.engine = engine
db_session.SessionLocal = TestingSessionLocal
# The JWT middleware opens its own session per request rather than taking a request-scoped
# one, so it holds its own reference and has to be re-pointed too.
import routes.auth_middleware as _auth_mw  # noqa: E402

_auth_mw.SessionLocal = TestingSessionLocal

db_session.Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db():
    """A session for seeding and inspecting rows inside a test."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client():
    """
    A TestClient constructed directly rather than entered with `with`. The app has no
    lifespan today; constructing it this way keeps any startup hook a derived project adds
    out of the suite, so a test sees only the request it made.
    raise_server_exceptions=False mirrors production: an uncaught error surfaces as the
    500 envelope `core/errors.py` renders, not as a test crash.
    """
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Isolate tests: clear every table after each one."""
    yield
    with engine.begin() as conn:
        for table in reversed(db_session.Base.metadata.sorted_tables):
            # A self-referencing foreign key (`issues.parent_id`) makes one `DELETE FROM
            # issues` order-dependent: MySQL checks the constraint row by row, so deleting an
            # epic before its story fails with error 1451 and leaves every later test looking
            # at the rows this one wrote. SQLite checks at the end of the statement and never
            # showed it; the first MySQL run did, on 2026-09-29. Break the links first.
            for foreign_key in table.foreign_keys:
                if foreign_key.column.table is table:
                    conn.execute(table.update().values({foreign_key.parent.name: None}))
            conn.execute(table.delete())
