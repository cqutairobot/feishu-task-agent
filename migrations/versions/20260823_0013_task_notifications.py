"""Add durable private task notification queue.

Revision ID: 20260823_0013
Revises: 20260823_0012
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0013"
down_revision: str | None = "20260823_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_notification_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "last_lifecycle_event_id",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "id = 1", name="ck_task_notification_state_singleton"
        ),
        sa.CheckConstraint(
            "last_lifecycle_event_id >= 0",
            name="ck_task_notification_state_event_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO task_notification_state "
        "(id, last_lifecycle_event_id, updated_at) "
        "SELECT 1, COALESCE(MAX(id), 0), CURRENT_TIMESTAMP "
        "FROM task_lifecycle_events"
    )
    op.create_table(
        "task_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("source_lifecycle_event_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("recipient_open_id", sa.String(length=128), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("task_code_snapshot", sa.String(length=16), nullable=False),
        sa.Column(
            "owner_open_id_snapshot", sa.String(length=128), nullable=False
        ),
        sa.Column(
            "owner_name_snapshot", sa.String(length=255), nullable=False
        ),
        sa.Column("title_snapshot", sa.String(length=200), nullable=False),
        sa.Column("status_snapshot", sa.String(length=16), nullable=False),
        sa.Column(
            "deadline_snapshot", sa.DateTime(timezone=True), nullable=True
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
        sa.Column(
            "delivery_receive_id_type", sa.String(length=16), nullable=True
        ),
        sa.Column("delivery_receive_id", sa.String(length=128), nullable=True),
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
            "kind IN ('missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin')",
            name="ck_task_notifications_kind",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'leased', 'sent', 'cancelled', 'dead')",
            name="ck_task_notifications_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_task_notifications_attempt_count",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10",
            name="ck_task_notifications_max_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND worker_id IS NOT NULL "
            "AND leased_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'leased' AND worker_id IS NULL "
            "AND leased_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_notifications_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL "
            "AND feishu_message_id IS NOT NULL "
            "AND delivery_receive_id_type IS NOT NULL "
            "AND delivery_receive_id IS NOT NULL) OR "
            "(status != 'sent' AND sent_at IS NULL "
            "AND feishu_message_id IS NULL "
            "AND delivery_receive_id_type IS NULL "
            "AND delivery_receive_id IS NULL)",
            name="ck_task_notifications_sent_state",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancel_reason IS NULL)",
            name="ck_task_notifications_cancelled_state",
        ),
        sa.CheckConstraint(
            "delivery_receive_id_type IS NULL OR "
            "delivery_receive_id_type IN ('open_id', 'chat_id')",
            name="ck_task_notifications_delivery_receive_type",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_lifecycle_event_id"],
            ["task_lifecycle_events.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "kind",
            "recipient_open_id",
            "dedupe_key",
            name="uq_task_notifications_dedupe",
        ),
    )
    op.create_index(
        "ix_task_notifications_ready",
        "task_notifications",
        ["status", "available_at", "scheduled_for"],
        unique=False,
    )
    op.create_index(
        "ix_task_notifications_task_status",
        "task_notifications",
        ["task_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_task_notifications_recipient_status",
        "task_notifications",
        ["recipient_open_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_notifications_recipient_status",
        table_name="task_notifications",
    )
    op.drop_index(
        "ix_task_notifications_task_status",
        table_name="task_notifications",
    )
    op.drop_index(
        "ix_task_notifications_ready",
        table_name="task_notifications",
    )
    op.drop_table("task_notifications")
    op.drop_table("task_notification_state")
