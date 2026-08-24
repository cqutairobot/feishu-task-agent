"""Add chat-scoped administrator notification recipient policy.

Revision ID: 20260824_0030
Revises: 20260824_0029
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0030"
down_revision: str | None = "20260824_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_settings",
        sa.Column(
            "administrator_notification_mode",
            sa.String(length=16),
            nullable=False,
            server_default="all",
        ),
    )
    op.add_column(
        "chat_settings",
        sa.Column(
            "administrator_notification_open_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "chat_settings", "administrator_notification_open_ids_json"
    )
    op.drop_column("chat_settings", "administrator_notification_mode")
