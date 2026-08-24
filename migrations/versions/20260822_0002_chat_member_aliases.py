"""Add verified, chat-scoped member aliases.

Revision ID: 20260822_0002
Revises: 20260822_0001
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0002"
down_revision: str | None = "20260822_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_member_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("normalized_alias", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["open_id"], ["users.open_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_id",
            "normalized_alias",
            name="uq_chat_member_aliases_chat_normalized",
        ),
    )
    op.create_index(
        "ix_chat_member_aliases_chat_user",
        "chat_member_aliases",
        ["chat_id", "open_id"],
        unique=False,
    )
    op.create_index(
        "uq_chat_member_aliases_primary",
        "chat_member_aliases",
        ["chat_id", "open_id"],
        unique=True,
        sqlite_where=sa.text("is_primary = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_chat_member_aliases_primary", table_name="chat_member_aliases"
    )
    op.drop_index(
        "ix_chat_member_aliases_chat_user", table_name="chat_member_aliases"
    )
    op.drop_table("chat_member_aliases")
