"""Add comment_id to issue_attachments table.

Revision ID: 20261008a001
Revises: 20261007a001
Create Date: 2026-10-08

Allows attachments to be associated with individual comments as well as issues.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261008a001"
down_revision: Union[str, None] = "20261007a001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID = sa.CHAR(36)


def upgrade() -> None:
    with op.batch_alter_table("issue_attachments") as batch:
        batch.add_column(sa.Column("comment_id", _ID, nullable=True))
        batch.create_foreign_key(
            "fk_issue_attachments_comment_id_comments",
            "issue_comments",
            ["comment_id"],
            ["id"],
        )
        batch.create_index("ix_issue_attachments_comment", ["comment_id"])


def downgrade() -> None:
    with op.batch_alter_table("issue_attachments") as batch:
        batch.drop_index("ix_issue_attachments_comment")
        batch.drop_constraint("fk_issue_attachments_comment_id_comments", type_="foreignkey")
        batch.drop_column("comment_id")
