"""Add chat-scoped reminder-stage switches.

Revision ID: 20260824_0027
Revises: 20260824_0026
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0027"
down_revision: str | None = "20260824_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in (
        "reminder_due_72h_enabled",
        "reminder_due_24h_enabled",
        "reminder_due_today_enabled",
        "reminder_overdue_enabled",
    ):
        op.add_column(
            "chat_settings",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    for name in reversed(
        (
            "reminder_due_72h_enabled",
            "reminder_due_24h_enabled",
            "reminder_due_today_enabled",
            "reminder_overdue_enabled",
        )
    ):
        op.drop_column("chat_settings", name)
