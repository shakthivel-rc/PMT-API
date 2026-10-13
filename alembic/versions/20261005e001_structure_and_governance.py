"""Structure and governance: space charter, archived containers, blocked work, backlog order.

Revision ID: 20261005e001
Revises: 20261005d001
Create Date: 2026-10-05

docs/design/09-restructuring.md §2.3, §3 and §5. What changes, and what every existing row
becomes:

* `spaces`: `description`, `owner_id`, `delivery_method`, `charter` (all empty — the
  inventory reports which spaces still lack them), and the write state `archived_*`,
  `frozen_*` (writable, as every space was). `frozen_by_plan_id` comes in 20261005f001 with
  the table it references.
* `folders`, `lists`: `archived_at`, `archived_by` (writable) and `updated_at` (from
  `created_at`).
* `issues`: `completion_criteria`, the blocked flag (not blocked), `status_entered_at` — the
  time of the issue's last `status_changed` event, or its creation when it never moved — and
  `backlog_rank`, the space-wide backlog order, assigned in creation order per space.
* `space_key_aliases` (new, empty).

`backlog_rank` is computed here in Python. The key generator is a **copy** of
`utils.rank.spread` as it was on 2026-10-05, not an import of it: a revision has to keep
meaning what it meant when it was written, and an import would change this backfill the day
someone changes the live function.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261005e001"
down_revision: Union[str, None] = "20261005d001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _spread(count: int) -> list[str]:
    """`utils.rank.spread` as of 2026-10-05: evenly spaced, increasing base-36 keys."""
    if count <= 0:
        return []
    base = len(_DIGITS)
    width = 1
    while base**width < 4 * (count + 1):
        width += 1
    span = base**width
    keys = []
    for index in range(1, count + 1):
        value = (index * span) // (count + 1)
        digits = []
        for _ in range(width):
            value, remainder = divmod(value, base)
            digits.append(_DIGITS[remainder])
        keys.append("".join(reversed(digits)).rstrip("0"))
    return keys


def upgrade() -> None:
    with op.batch_alter_table("spaces") as batch:
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch.add_column(sa.Column("owner_id", _ID, nullable=True))
        batch.add_column(sa.Column("delivery_method", sa.String(10), nullable=True))
        batch.add_column(sa.Column("charter", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("archived_by", _ID, nullable=True))
        batch.add_column(sa.Column("frozen_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("frozen_by", _ID, nullable=True))
        batch.add_column(sa.Column("freeze_reason", sa.String(500), nullable=True))
        batch.create_foreign_key("fk_spaces_owner_id_users", "users", ["owner_id"], ["id"])
        batch.create_foreign_key("fk_spaces_archived_by_users", "users", ["archived_by"],
                                 ["id"])
        batch.create_foreign_key("fk_spaces_frozen_by_users", "users", ["frozen_by"], ["id"])

    for table in ("folders", "lists"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
            batch.add_column(sa.Column("archived_by", _ID, nullable=True))
            batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
            batch.create_foreign_key(f"fk_{table}_archived_by_users", "users",
                                     ["archived_by"], ["id"])
        op.execute(f"UPDATE {table} SET updated_at = created_at")  # noqa: S608 - fixed names
        with op.batch_alter_table(table) as batch:
            batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)

    with op.batch_alter_table("issues") as batch:
        batch.add_column(sa.Column("completion_criteria", sa.Text(), nullable=True))
        batch.add_column(sa.Column("blocked", sa.Boolean(), nullable=False,
                                   server_default=sa.text("0")))
        batch.add_column(sa.Column("blocked_reason", sa.String(1000), nullable=True))
        batch.add_column(sa.Column("blocked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("blocked_by_id", _ID, nullable=True))
        batch.add_column(sa.Column("status_entered_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("backlog_rank", sa.String(128), nullable=True))
        batch.create_foreign_key("fk_issues_blocked_by_id_users", "users", ["blocked_by_id"],
                                 ["id"])

    op.execute(
        "UPDATE issues SET status_entered_at = ("
        " SELECT MAX(e.created_at) FROM issue_events e"
        " WHERE e.issue_id = issues.id AND e.event = 'status_changed')")
    op.execute("UPDATE issues SET status_entered_at = created_at "
               "WHERE status_entered_at IS NULL")

    bind = op.get_bind()
    issues = sa.table("issues", sa.column("id"), sa.column("space_id"),
                      sa.column("created_at"), sa.column("number"),
                      sa.column("backlog_rank"))
    rows = bind.execute(sa.select(issues.c.id, issues.c.space_id)
                        .order_by(issues.c.space_id, issues.c.created_at, issues.c.number,
                                  issues.c.id)).all()
    by_space: dict[str, list[str]] = {}
    for issue_id, space_id in rows:
        by_space.setdefault(space_id, []).append(issue_id)
    for ids in by_space.values():
        for issue_id, key in zip(ids, _spread(len(ids)), strict=True):
            bind.execute(issues.update().where(issues.c.id == issue_id)
                         .values(backlog_rank=key))

    with op.batch_alter_table("issues") as batch:
        batch.alter_column("backlog_rank", existing_type=sa.String(128), nullable=False)
        batch.create_index("ix_issues_space_backlog_rank", ["space_id", "backlog_rank"])

    op.create_table(
        "space_key_aliases",
        sa.Column("id", _ID, primary_key=True, nullable=False),
        sa.Column("workspace_id", _ID, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("space_id", _ID, sa.ForeignKey("spaces.id"), nullable=False),
        sa.Column("key", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "key", name="uq_space_key_aliases_workspace_key"),
    )
    op.create_index("ix_space_key_aliases_space_id", "space_key_aliases", ["space_id"])


def downgrade() -> None:
    op.drop_index("ix_space_key_aliases_space_id", table_name="space_key_aliases")
    op.drop_table("space_key_aliases")
    with op.batch_alter_table("issues") as batch:
        batch.drop_index("ix_issues_space_backlog_rank")
        batch.drop_constraint("fk_issues_blocked_by_id_users", type_="foreignkey")
        batch.drop_column("backlog_rank")
        batch.drop_column("status_entered_at")
        batch.drop_column("blocked_by_id")
        batch.drop_column("blocked_at")
        batch.drop_column("blocked_reason")
        batch.drop_column("blocked")
        batch.drop_column("completion_criteria")
    for table in ("lists", "folders"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_archived_by_users", type_="foreignkey")
            batch.drop_column("updated_at")
            batch.drop_column("archived_by")
            batch.drop_column("archived_at")
    with op.batch_alter_table("spaces") as batch:
        batch.drop_constraint("fk_spaces_frozen_by_users", type_="foreignkey")
        batch.drop_constraint("fk_spaces_archived_by_users", type_="foreignkey")
        batch.drop_constraint("fk_spaces_owner_id_users", type_="foreignkey")
        batch.drop_column("freeze_reason")
        batch.drop_column("frozen_by")
        batch.drop_column("frozen_at")
        batch.drop_column("archived_by")
        batch.drop_column("archived_at")
        batch.drop_column("charter")
        batch.drop_column("delivery_method")
        batch.drop_column("owner_id")
        batch.drop_column("description")
