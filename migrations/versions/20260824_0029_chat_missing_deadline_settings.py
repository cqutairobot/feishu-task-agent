"""Add chat-scoped missing-deadline notification settings.

Revision ID: 20260824_0029
Revises: 20260824_0028
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0029"
down_revision: str | None = "20260824_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in (
        "missing_deadline_owner_enabled",
        "missing_deadline_admin_enabled",
    ):
        op.add_column(
            "chat_settings",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    for name, default in (
        ("missing_deadline_owner_delay_hours", 24),
        ("missing_deadline_admin_delay_hours", 72),
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
            "missing_deadline_owner_enabled",
            "missing_deadline_admin_enabled",
            "missing_deadline_owner_delay_hours",
            "missing_deadline_admin_delay_hours",
        )
    ):
        op.drop_column("chat_settings", name)
