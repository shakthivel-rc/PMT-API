"""The Alembic chain: up → down → up.

Runs on SQLite **and** on MySQL. It used to skip on SQLite because the revisions used
operations SQLite has no equivalent for; every revision now puts `alter_column` through
`op.batch_alter_table` (CLAUDE.md §17), which Alembic implements on SQLite by recreating
the table. A migration that only ran in CI was a migration nobody exercised while writing
it.

Every test here builds its **own** database from a temporary file and points Alembic at it
explicitly. Two reasons, and the second is the one that bit: an in-memory SQLite database is
per-connection and Alembic opens its own, so the chain would run against an empty database
that then vanishes; and running the chain against the shared test database would drop the
schema every other test in the session depends on.

What this proves, and what it does not:

* **Proves:** the chain applies, reverses and re-applies; `downgrade` leaves nothing behind;
  the schema the migrations build matches the one the models declare.
* **Does not prove:** MySQL-specific behaviour. SQLite ignores `VARCHAR` limits and is far
  more permissive about types, so a length overflow that MySQL raises as error 1406 passes
  here silently. Point `TEST_DATABASE_URL` at a real MySQL to close that gap.
"""
import os

import pytest

pytestmark = pytest.mark.integration

_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite://")
_IN_MEMORY = _DATABASE_URL in ("sqlite://", "sqlite:///:memory:")


def _config(url: str):
    """An Alembic config pointed at `url`, whatever `DATABASE_URL` says.

    `alembic/env.py` prefers an explicitly-set `sqlalchemy.url` over the environment for
    exactly this: a test that wants its own database should not have to mutate the process
    environment to get one.
    """
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_migration_chain_upgrades_and_downgrades(tmp_path):
    """§17: `alembic upgrade head` then `downgrade base` clean, then up again.

    Up-then-down is not enough. A revision whose `downgrade()` leaves a table, an index or a
    constraint behind passes an up→down check and fails the *next* `upgrade` — which is what
    an operator hits when they roll back and roll forward again.

    Against the configured database when there is a real one (so CI exercises this on MySQL,
    where `downgrade` actually has to drop foreign keys in the right order), and against a
    temporary SQLite file when the configured one is in-memory.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect
    from sqlalchemy import text as sa_text

    from db.session import Base, engine

    own_database = _IN_MEMORY
    url = f"sqlite:///{tmp_path / 'chain.db'}" if own_database else _DATABASE_URL
    if not own_database:
        # The shared database: start from nothing, and put the ORM-built schema back
        # afterwards so the rest of the session still has its tables.
        #
        # `alembic_version` is dropped explicitly. It is not an ORM model, so `drop_all`
        # leaves it — and Alembic then reads it, concludes it is already at head, and
        # `upgrade` becomes a silent no-op. The test passes having exercised nothing, which
        # is how this ran green on MySQL without ever applying a revision.
        Base.metadata.drop_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(sa_text("DROP TABLE IF EXISTS alembic_version"))

    try:
        cfg = _config(url)
        command.upgrade(cfg, "head")
        after_up = set(inspect(create_engine(url)).get_table_names())
        assert len(after_up) > 1, "the chain created nothing"

        command.downgrade(cfg, "base")
        after_down = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
        assert after_down == set(), f"downgrade left tables behind: {sorted(after_down)}"

        command.upgrade(cfg, "head")
        assert set(inspect(create_engine(url)).get_table_names()) == after_up
    finally:
        if not own_database:
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
            # Stamp, so the database is left the way the test found it. Without this it
            # carries a full schema and no `alembic_version`, and the next `upgrade head`
            # — a container restart, say — tries to create tables that already exist.
            command.stamp(_config(url), "head")


def test_every_model_table_is_created_by_the_migrations(tmp_path):
    """The models and the migrations must describe the same schema.

    They are written separately — `create_all` builds the test database, Alembic builds the
    real one — so nothing but a comparison stops them drifting. A table that exists only in
    the models works in every test and is missing in production.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from db.session import Base

    url = f"sqlite:///{tmp_path / 'tables.db'}"
    command.upgrade(_config(url), "head")

    migrated = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    declared = set(Base.metadata.tables)
    assert declared - migrated == set(), "declared by a model, never created by a migration"
    assert migrated - declared == set(), "created by a migration, described by no model"


def test_every_model_column_is_created_by_the_migrations(tmp_path):
    """The same comparison, one level down.

    This is the one that catches the realistic mistake: a column added to a model and
    forgotten in the revision. It passes every test — `create_all` builds it — and is
    absent from the database the migrations actually produce.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from db.session import Base

    url = f"sqlite:///{tmp_path / 'columns.db'}"
    command.upgrade(_config(url), "head")
    inspector = inspect(create_engine(url))

    missing: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        migrated = {column["name"] for column in inspector.get_columns(name)}
        gap = {column.name for column in table.columns} - migrated
        if gap:
            missing[name] = gap
    assert missing == {}, f"columns declared by a model and never migrated: {missing}"
