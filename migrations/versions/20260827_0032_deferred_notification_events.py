"""Retain lifecycle events excluded from notification synchronization.

Revision ID: 20260827_0032
Revises: 20260824_0031
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0032"
down_revision: str | None = "20260824_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_notification_deferred_lifecycle_events",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column(
            "deferred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["task_lifecycle_events.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("task_notification_deferred_lifecycle_events")
