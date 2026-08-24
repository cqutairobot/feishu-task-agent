"""Add one-time management login tokens and browser sessions.

Revision ID: 20260823_0017
Revises: 20260823_0016
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0017"
down_revision: str | None = "20260823_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "management_login_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_open_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_management_login_tokens_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_management_login_tokens_consumed",
        ),
        sa.ForeignKeyConstraint(
            ["actor_open_id"], ["users.open_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_hash", name="uq_management_login_tokens_hash"
        ),
    )
    op.create_index(
        "ix_management_login_tokens_actor_expiry",
        "management_login_tokens",
        ["actor_open_id", "expires_at"],
        unique=False,
    )

    op.create_table(
        "management_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_open_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_management_sessions_expiry",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_management_sessions_revoked",
        ),
        sa.ForeignKeyConstraint(
            ["actor_open_id"], ["users.open_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_hash", name="uq_management_sessions_hash"
        ),
    )
    op.create_index(
        "ix_management_sessions_actor_expiry",
        "management_sessions",
        ["actor_open_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_management_sessions_expiry",
        "management_sessions",
        ["expires_at"],
        unique=False,
    )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.drop_index(
        "ix_management_sessions_expiry",
        table_name="management_sessions",
    )
    op.drop_index(
        "ix_management_sessions_actor_expiry",
        table_name="management_sessions",
    )
    op.drop_table("management_sessions")
    op.drop_index(
        "ix_management_login_tokens_actor_expiry",
        table_name="management_login_tokens",
    )
    op.drop_table("management_login_tokens")
