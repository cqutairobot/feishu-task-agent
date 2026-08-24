"""Add chat-scoped task administrators and membership audit.

Revision ID: 20260823_0016
Revises: 20260823_0015
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0016"
down_revision: str | None = "20260823_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SOURCE_CHECK = "source IN ('local_cli', 'management_page', 'bootstrap')"


def upgrade() -> None:
    op.create_table(
        "chat_administrators",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("granted_by_open_id", sa.String(length=128)),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            _SOURCE_CHECK, name="ck_chat_administrators_source"
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_id",
            "open_id",
            name="uq_chat_administrators_chat_user",
        ),
    )
    op.create_index(
        "ix_chat_administrators_user_chat",
        "chat_administrators",
        ["open_id", "chat_id"],
        unique=False,
    )

    op.create_table(
        "chat_administrator_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("target_open_id", sa.String(length=128), nullable=False),
        sa.Column("actor_open_id", sa.String(length=128)),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "action IN ('grant', 'revoke')",
            name="ck_chat_administrator_events_action",
        ),
        sa.CheckConstraint(
            _SOURCE_CHECK, name="ck_chat_administrator_events_source"
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_administrator_events_chat_created",
        "chat_administrator_events",
        ["chat_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_chat_administrator_events_target_created",
        "chat_administrator_events",
        ["target_open_id", "created_at"],
        unique=False,
    )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.drop_index(
        "ix_chat_administrator_events_target_created",
        table_name="chat_administrator_events",
    )
    op.drop_index(
        "ix_chat_administrator_events_chat_created",
        table_name="chat_administrator_events",
    )
    op.drop_table("chat_administrator_events")
    op.drop_index(
        "ix_chat_administrators_user_chat",
        table_name="chat_administrators",
    )
    op.drop_table("chat_administrators")
