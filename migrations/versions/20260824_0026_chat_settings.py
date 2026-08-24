"""Add per-chat detection settings and audit events.

Revision ID: 20260824_0026
Revises: 20260824_0025
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0026"
down_revision: str | None = "20260824_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_settings",
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("detection_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_todo_confidence", sa.Float(), nullable=False, server_default="0.85"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("updated_by_open_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "auto_todo_confidence >= 0 AND auto_todo_confidence <= 1",
            name="ck_chat_settings_auto_todo_confidence",
        ),
        sa.CheckConstraint("timezone <> ''", name="ck_chat_settings_timezone_nonempty"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_open_id"], ["users.open_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_table(
        "chat_setting_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("actor_open_id", sa.String(length=128), nullable=False),
        sa.Column("changed_fields_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_open_id"], ["users.open_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_setting_events_chat_created",
        "chat_setting_events",
        ["chat_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_setting_events_chat_created", table_name="chat_setting_events")
    op.drop_table("chat_setting_events")
    op.drop_table("chat_settings")
