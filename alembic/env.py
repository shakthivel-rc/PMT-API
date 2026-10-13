import importlib
import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# loading env variables
load_dotenv()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# A URL the caller already set wins. `alembic upgrade head` from a shell has none, and
# takes DATABASE_URL; a caller driving Alembic in-process (the migration tests) sets one
# on the Config and must not have to mutate the environment to be heard.
database_url = config.get_main_option("sqlalchemy.url", None) or os.getenv("DATABASE_URL")

if not database_url:
    raise ValueError("DATABASE_URL is not set in the .env file")

config.set_main_option("sqlalchemy.url", database_url)

# Interpret the config file for Python logging.
#
# `disable_existing_loggers=False` is load-bearing, not tidiness. `fileConfig` defaults to
# True, which disables every logger that already exists — so running Alembic *in process*
# silently switches off the application's logging and pytest's log capture for the rest of
# the run. It costs nothing here and it is very hard to diagnose from the other end.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

def _load_metadata():
    db_session = importlib.import_module("db.session")
    # Every model module, so autogenerate diffs against the whole schema. Until 2026-10-05
    # only the seven identity modules were listed, and `make revision` drafted a
    # `drop_table` for all thirteen project-management tables because it could not see them
    # (verified that day; docs/design/07-time-tracking.md). A new model module goes here.
    for module in (
        "models.activity_log",
        "models.permissions",
        "models.role_permissions",
        "models.roles",
        "models.user",
        "models.user_roles",
        "models.user_session",
        "models.workspace",
        "models.hierarchy",
        "models.issue",
        "models.vcs",
        "models.ai_job",
        "models.time_tracking",
        "models.sprint",
        "models.reporting",
        # 2026-10-05, docs/design/09-restructuring.md.
        "models.migration",
        "models.release",
        "models.saved_view",
        "models.storage",
    ):
        importlib.import_module(module)
    return db_session.Base.metadata


target_metadata = _load_metadata()

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
