"""Create the Phase 2 message store.

Revision ID: 20260822_0001
Revises: None
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("chat_type", sa.String(length=32), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
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
        sa.PrimaryKeyConstraint("chat_id"),
    )

    op.create_table(
        "users",
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("union_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("tenant_key", sa.String(length=128), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("open_id"),
    )
    op.create_index("ix_users_union_id", "users", ["union_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_key", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("sender_open_id", sa.String(length=128), nullable=False),
        sa.Column("sender_name_snapshot", sa.String(length=255), nullable=True),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("raw_event_json", sa.Text(), nullable=False),
        sa.Column("root_id", sa.String(length=128), nullable=True),
        sa.Column("parent_id", sa.String(length=128), nullable=True),
        sa.Column(
            "message_created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "is_from_bot", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["sender_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_key", "event_id", name="uq_messages_tenant_event"
        ),
        sa.UniqueConstraint(
            "tenant_key", "message_id", name="uq_messages_tenant_message"
        ),
    )
    op.create_index(
        "ix_messages_chat_created",
        "messages",
        ["chat_id", "message_created_at"],
        unique=False,
    )
    op.create_index(
        "ix_messages_sender_created",
        "messages",
        ["sender_open_id", "message_created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_messages_sender_created", table_name="messages")
    op.drop_index("ix_messages_chat_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_users_union_id", table_name="users")
    op.drop_table("users")
    op.drop_table("chats")
