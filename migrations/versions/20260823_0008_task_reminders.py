"""Add durable, deadline-versioned task reminder plans.

Revision ID: 20260823_0008
Revises: 20260822_0007
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0008"
down_revision: str | None = "20260822_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_reminders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "deadline_snapshot", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feishu_message_id", sa.String(length=128), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=2000), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('due_72h', 'due_24h', 'due_today', 'overdue')",
            name="ck_task_reminders_kind",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'leased', 'sent', 'cancelled', 'dead')",
            name="ck_task_reminders_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_task_reminders_attempt_count",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10",
            name="ck_task_reminders_max_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND worker_id IS NOT NULL "
            "AND leased_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'leased' AND worker_id IS NULL "
            "AND leased_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_reminders_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL "
            "AND feishu_message_id IS NOT NULL) OR "
            "(status != 'sent' AND sent_at IS NULL "
            "AND feishu_message_id IS NULL)",
            name="ck_task_reminders_sent_state",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancel_reason IS NULL)",
            name="ck_task_reminders_cancelled_state",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "kind",
            "deadline_snapshot",
            name="uq_task_reminders_task_kind_deadline",
        ),
    )
    op.create_index(
        "ix_task_reminders_ready",
        "task_reminders",
        ["status", "available_at", "scheduled_for"],
        unique=False,
    )
    op.create_index(
        "ix_task_reminders_task_status",
        "task_reminders",
        ["task_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_reminders_task_status", table_name="task_reminders"
    )
    op.drop_index("ix_task_reminders_ready", table_name="task_reminders")
    op.drop_table("task_reminders")
