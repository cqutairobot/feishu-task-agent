"""Audit the batch-focus messages used by each detection run.

Revision ID: 20260822_0007
Revises: 20260822_0006
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0007"
down_revision: str | None = "20260822_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "detection_run_focus_messages",
        sa.Column("detection_run_id", sa.Integer(), nullable=False),
        sa.Column("message_db_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_detection_run_focus_position",
        ),
        sa.ForeignKeyConstraint(
            ["detection_run_id"],
            ["detection_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_db_id"],
            ["messages.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("detection_run_id", "message_db_id"),
        sa.UniqueConstraint(
            "detection_run_id",
            "position",
            name="uq_detection_run_focus_position",
        ),
    )
    op.create_index(
        "ix_detection_run_focus_message",
        "detection_run_focus_messages",
        ["message_db_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_detection_run_focus_message",
        table_name="detection_run_focus_messages",
    )
    op.drop_table("detection_run_focus_messages")
