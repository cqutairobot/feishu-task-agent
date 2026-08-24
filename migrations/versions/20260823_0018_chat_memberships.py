"""Add authoritative chat membership and owner snapshots.

Revision ID: 20260823_0018
Revises: 20260823_0017
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0018"
down_revision: str | None = "20260823_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_memberships",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column(
            "display_name_snapshot", sa.String(length=255), nullable=False
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "is_owner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("first_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "is_owner = 0 OR active = 1",
            name="ck_chat_memberships_owner_active",
        ),
        sa.CheckConstraint(
            "(active = 1 AND left_at IS NULL) OR "
            "(active = 0 AND left_at IS NOT NULL)",
            name="ck_chat_memberships_active_state",
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["open_id"], ["users.open_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_id", "open_id", name="uq_chat_memberships_chat_user"
        ),
    )
    op.create_index(
        "ix_chat_memberships_chat_active",
        "chat_memberships",
        ["chat_id", "active"],
        unique=False,
    )
    op.create_index(
        "ix_chat_memberships_user_active",
        "chat_memberships",
        ["open_id", "active"],
        unique=False,
    )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.drop_index(
        "ix_chat_memberships_user_active", table_name="chat_memberships"
    )
    op.drop_index(
        "ix_chat_memberships_chat_active", table_name="chat_memberships"
    )
    op.drop_table("chat_memberships")
