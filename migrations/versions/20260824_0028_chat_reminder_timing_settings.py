"""Add chat-scoped reminder timing settings.

Revision ID: 20260824_0028
Revises: 20260824_0027
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0028"
down_revision: str | None = "20260824_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, default in (
        ("reminder_due_72h_offset_hours", 72),
        ("reminder_due_24h_offset_hours", 24),
        ("reminder_due_today_hour", 9),
        ("reminder_overdue_grace_minutes", 1),
    ):
        op.add_column(
            "chat_settings",
            sa.Column(
                name,
                sa.Integer(),
                nullable=False,
                server_default=str(default),
            ),
        )


def downgrade() -> None:
    for name in reversed(
        (
            "reminder_due_72h_offset_hours",
            "reminder_due_24h_offset_hours",
            "reminder_due_today_hour",
            "reminder_overdue_grace_minutes",
        )
    ):
        op.drop_column("chat_settings", name)
