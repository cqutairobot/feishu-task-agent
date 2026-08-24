"""Enforce one current name per member in each chat.

Revision ID: 20260822_0003
Revises: 20260822_0002
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0003"
down_revision: str | None = "20260822_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keep each member's current primary name. If legacy data somehow has no
    # primary row, retain the newest row so the migration is still loss-safe.
    op.execute(
        sa.text(
            """
            DELETE FROM chat_member_aliases
            WHERE id NOT IN (
                SELECT COALESCE(
                    MAX(CASE WHEN is_primary = 1 THEN id END),
                    MAX(id)
                )
                FROM chat_member_aliases
                GROUP BY chat_id, open_id
            )
            """
        )
    )
    op.drop_index(
        "uq_chat_member_aliases_primary", table_name="chat_member_aliases"
    )
    op.drop_index(
        "ix_chat_member_aliases_chat_user", table_name="chat_member_aliases"
    )
    with op.batch_alter_table(
        "chat_member_aliases", recreate="always"
    ) as batch_op:
        batch_op.drop_column("is_primary")
        batch_op.create_unique_constraint(
            "uq_chat_member_aliases_chat_user", ["chat_id", "open_id"]
        )
        batch_op.create_index(
            "ix_chat_member_aliases_open_id", ["open_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "chat_member_aliases", recreate="always"
    ) as batch_op:
        batch_op.drop_index("ix_chat_member_aliases_open_id")
        batch_op.drop_constraint(
            "uq_chat_member_aliases_chat_user", type_="unique"
        )
        batch_op.add_column(
            sa.Column(
                "is_primary",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.create_index(
            "ix_chat_member_aliases_chat_user",
            ["chat_id", "open_id"],
            unique=False,
        )
        batch_op.create_index(
            "uq_chat_member_aliases_primary",
            ["chat_id", "open_id"],
            unique=True,
            sqlite_where=sa.text("is_primary = 1"),
        )
