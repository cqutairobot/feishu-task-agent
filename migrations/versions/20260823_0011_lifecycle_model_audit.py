"""Add model-call metadata to lifecycle transition audit events.

Revision ID: 20260823_0011
Revises: 20260823_0010
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0011"
down_revision: str | None = "20260823_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.add_column(
            sa.Column("provider", sa.String(length=32), nullable=True)
        )
        batch.add_column(
            sa.Column("model", sa.String(length=128), nullable=True)
        )
        batch.add_column(
            sa.Column("response_format", sa.String(length=32), nullable=True)
        )
        batch.add_column(
            sa.Column("model_request_id", sa.String(length=128), nullable=True)
        )
        batch.add_column(
            sa.Column("prompt_tokens", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column("completion_tokens", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column("total_tokens", sa.Integer(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_prompt_tokens",
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_completion_tokens",
            "completion_tokens IS NULL OR completion_tokens >= 0",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_total_tokens",
            "total_tokens IS NULL OR total_tokens >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_total_tokens", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_completion_tokens", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_prompt_tokens", type_="check"
        )
        batch.drop_column("total_tokens")
        batch.drop_column("completion_tokens")
        batch.drop_column("prompt_tokens")
        batch.drop_column("model_request_id")
        batch.drop_column("response_format")
        batch.drop_column("model")
        batch.drop_column("provider")
