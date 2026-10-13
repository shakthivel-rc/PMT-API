"""The engine, the session factory, and the `get_db` dependency.

**Synchronous SQLAlchemy, deliberately.** Every model, every migration, every test fixture
and every route handler is written against it. Moving to the async engine is a change to
all of them at once, and this file is not the place that decision gets made quietly. What
follows from it is the rule in CLAUDE.md §9 — a database call may block the thread it is
on, so it must not be on the event loop — and `tests/unit/test_no_blocking_db_calls.py`
keeps it true of the request handlers.

**Pool sizing.** The size, overflow and recycle values come from `Settings`; the reasoning
for the numbers is at their definition in `core/config.py`. They are passed only for
pooled dialects: SQLite's `SingletonThreadPool` and `StaticPool` accept none of them and
raise `TypeError` on sight, and the test suite runs on SQLite.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from core.config import get_settings

settings = get_settings()

_pool_options = {}
if not settings.database_url.startswith("sqlite"):
    _pool_options = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # Recycle before the server or a load balancer closes an idle connection for us,
        # which is cheaper than discovering it on the next query.
        "pool_recycle": settings.db_pool_recycle_seconds,
    }

engine = create_engine(
    settings.database_url,
    # Kept alongside `pool_recycle` rather than replaced by it. Recycle handles the timeout
    # you know about; pre-ping handles the connection something else severed — a restarted
    # database, a NAT table that forgot, a proxy with its own idle rule. It costs one
    # trivial round trip per checkout, which is the price of never serving a request that
    # fails on a dead socket.
    pool_pre_ping=True,
    **_pool_options,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

#: The declarative base every model inherits from. `tests/integration/test_migrations.py`
#: walks its metadata, so a new table joins the model/migration parity check for free.
Base = declarative_base()


def get_db():
    """A request-scoped session, closed when the request ends.

    Note what this does NOT do: it does not commit. A handler that writes commits its own
    work, so a handler that raises half way through leaves nothing behind.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
