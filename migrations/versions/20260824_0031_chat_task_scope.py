"""Add chat-scoped task recognition scope.

Revision ID: 20260824_0031
Revises: 20260824_0030
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0031"
down_revision: str | None = "20260824_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "task_scope",
                sa.String(length=16),
                nullable=False,
                server_default="broad",
            )
        )
        batch_op.create_check_constraint(
            "ck_chat_settings_task_scope",
            "task_scope IN ('broad', 'work_only')",
        )


def downgrade() -> None:
    with op.batch_alter_table("chat_settings") as batch_op:
        batch_op.drop_constraint(
            "ck_chat_settings_task_scope", type_="check"
        )
        batch_op.drop_column("task_scope")
