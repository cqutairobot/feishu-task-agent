"""Store private-note source chat and model audit metadata.

Revision ID: 20260830_0036
Revises: 20260830_0035
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0036"
down_revision: str | None = "20260830_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_notes") as batch:
        batch.add_column(sa.Column("source_chat_id", sa.String(length=128)))
        batch.add_column(sa.Column("confidence", sa.Float()))
        batch.add_column(sa.Column("provider", sa.String(length=32)))
        batch.add_column(sa.Column("model", sa.String(length=128)))
        batch.add_column(sa.Column("response_format", sa.String(length=32)))
        batch.add_column(sa.Column("model_request_id", sa.String(length=128)))
        batch.add_column(sa.Column("prompt_tokens", sa.Integer()))
        batch.add_column(sa.Column("completion_tokens", sa.Integer()))
        batch.add_column(sa.Column("total_tokens", sa.Integer()))
        batch.create_foreign_key(
            "fk_task_notes_source_chat_id_chats",
            "chats",
            ["source_chat_id"],
            ["chat_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_task_notes_confidence",
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
        )
        batch.create_check_constraint(
            "ck_task_notes_prompt_tokens",
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
        )
        batch.create_check_constraint(
            "ck_task_notes_completion_tokens",
            "completion_tokens IS NULL OR completion_tokens >= 0",
        )
        batch.create_check_constraint(
            "ck_task_notes_total_tokens",
            "total_tokens IS NULL OR total_tokens >= 0",
        )
    op.create_index(
        "ix_task_notes_source_chat_created",
        "task_notes",
        ["source_chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_notes_source_chat_created", table_name="task_notes")
    with op.batch_alter_table("task_notes") as batch:
        batch.drop_constraint("ck_task_notes_total_tokens", type_="check")
        batch.drop_constraint("ck_task_notes_completion_tokens", type_="check")
        batch.drop_constraint("ck_task_notes_prompt_tokens", type_="check")
        batch.drop_constraint("ck_task_notes_confidence", type_="check")
        batch.drop_constraint(
            "fk_task_notes_source_chat_id_chats", type_="foreignkey"
        )
        batch.drop_column("total_tokens")
        batch.drop_column("completion_tokens")
        batch.drop_column("prompt_tokens")
        batch.drop_column("model_request_id")
        batch.drop_column("response_format")
        batch.drop_column("model")
        batch.drop_column("provider")
        batch.drop_column("confidence")
        batch.drop_column("source_chat_id")
