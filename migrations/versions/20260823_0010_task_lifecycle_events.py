"""Add auditable, evidence-linked task lifecycle transitions.

Revision ID: 20260823_0010
Revises: 20260823_0009
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0010"
down_revision: str | None = "20260823_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_lifecycle_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("actor_open_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_message_db_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "authorization_role", sa.String(length=16), nullable=False
        ),
        sa.Column("task_code_snapshot", sa.String(length=64), nullable=False),
        sa.Column("previous_status", sa.String(length=16), nullable=False),
        sa.Column("new_status", sa.String(length=16), nullable=False),
        sa.Column(
            "deadline_before", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "deadline_after", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "action IN ('complete', 'reschedule', 'cancel')",
            name="ck_task_lifecycle_events_action",
        ),
        sa.CheckConstraint(
            "authorization_role IN ('owner', 'administrator')",
            name="ck_task_lifecycle_events_authorization",
        ),
        sa.CheckConstraint(
            "previous_status IN ('todo', 'overdue')",
            name="ck_task_lifecycle_events_previous_status",
        ),
        sa.CheckConstraint(
            "new_status IN ('todo', 'done', 'cancelled')",
            name="ck_task_lifecycle_events_new_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_task_lifecycle_events_confidence",
        ),
        sa.CheckConstraint(
            "(action = 'complete' AND new_status = 'done') OR "
            "(action = 'cancel' AND new_status = 'cancelled') OR "
            "(action = 'reschedule' AND new_status = 'todo' "
            "AND deadline_after IS NOT NULL)",
            name="ck_task_lifecycle_events_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["trigger_message_db_id"],
            ["messages.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "trigger_message_db_id",
            name="uq_task_lifecycle_events_task_trigger",
        ),
    )
    op.create_index(
        "ix_task_lifecycle_events_task_applied",
        "task_lifecycle_events",
        ["task_id", "applied_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_lifecycle_events_actor_applied",
        "task_lifecycle_events",
        ["actor_open_id", "applied_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_lifecycle_events_trigger",
        "task_lifecycle_events",
        ["trigger_message_db_id"],
        unique=False,
    )

    op.create_table(
        "task_lifecycle_evidence",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("message_db_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name="ck_task_lifecycle_evidence_position"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["task_lifecycle_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_db_id"], ["messages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("event_id", "message_db_id"),
        sa.UniqueConstraint(
            "event_id",
            "position",
            name="uq_task_lifecycle_evidence_position",
        ),
    )
    op.create_index(
        "ix_task_lifecycle_evidence_message",
        "task_lifecycle_evidence",
        ["message_db_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_lifecycle_evidence_message",
        table_name="task_lifecycle_evidence",
    )
    op.drop_table("task_lifecycle_evidence")
    op.drop_index(
        "ix_task_lifecycle_events_trigger",
        table_name="task_lifecycle_events",
    )
    op.drop_index(
        "ix_task_lifecycle_events_actor_applied",
        table_name="task_lifecycle_events",
    )
    op.drop_index(
        "ix_task_lifecycle_events_task_applied",
        table_name="task_lifecycle_events",
    )
    op.drop_table("task_lifecycle_events")
