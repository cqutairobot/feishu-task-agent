"""Add lifecycle tasks, evidence, candidate sources, and materialization audit.

Revision ID: 20260822_0006
Revises: 20260822_0005
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0006"
down_revision: str | None = "20260822_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("owner_open_id", sa.String(length=128), nullable=False),
        sa.Column("owner_name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("normalized_title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="todo",
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'todo', 'done', 'cancelled', 'overdue')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_tasks_confidence",
        ),
        sa.CheckConstraint(
            "(status = 'done' AND completed_at IS NOT NULL) OR "
            "(status != 'done' AND completed_at IS NULL)",
            name="ck_tasks_completed_state",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL)",
            name="ck_tasks_cancelled_state",
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tasks_chat_status", "tasks", ["chat_id", "status"], unique=False
    )
    op.create_index(
        "ix_tasks_owner_status",
        "tasks",
        ["owner_open_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_deadline_status",
        "tasks",
        ["deadline", "status"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_dedupe_lookup",
        "tasks",
        ["chat_id", "owner_open_id", "normalized_title", "deadline"],
        unique=False,
    )

    op.create_table(
        "task_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("message_db_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["message_db_id"], ["messages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "message_db_id", name="uq_task_evidence_task_message"
        ),
    )
    op.create_index(
        "ix_task_evidence_message",
        "task_evidence",
        ["message_db_id"],
        unique=False,
    )

    op.create_table(
        "task_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("detection_run_id", sa.Integer(), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "candidate_index >= 0 AND candidate_index < 10",
            name="ck_task_sources_candidate_index",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_task_sources_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["detection_run_id"],
            ["detection_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "detection_run_id",
            "candidate_index",
            name="uq_task_sources_run_candidate",
        ),
    )
    op.create_index(
        "ix_task_sources_task", "task_sources", ["task_id"], unique=False
    )

    op.create_table(
        "detection_materializations",
        sa.Column("detection_run_id", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("created_task_count", sa.Integer(), nullable=False),
        sa.Column("reused_task_count", sa.Integer(), nullable=False),
        sa.Column(
            "materialized_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.CheckConstraint(
            "candidate_count >= 0 AND candidate_count <= 10",
            name="ck_detection_materializations_candidate_count",
        ),
        sa.CheckConstraint(
            "created_task_count >= 0 AND reused_task_count >= 0 AND "
            "created_task_count + reused_task_count = candidate_count",
            name="ck_detection_materializations_counts",
        ),
        sa.ForeignKeyConstraint(
            ["detection_run_id"],
            ["detection_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("detection_run_id"),
    )


def downgrade() -> None:
    op.drop_table("detection_materializations")
    op.drop_index("ix_task_sources_task", table_name="task_sources")
    op.drop_table("task_sources")
    op.drop_index("ix_task_evidence_message", table_name="task_evidence")
    op.drop_table("task_evidence")
    op.drop_index("ix_tasks_dedupe_lookup", table_name="tasks")
    op.drop_index("ix_tasks_deadline_status", table_name="tasks")
    op.drop_index("ix_tasks_owner_status", table_name="tasks")
    op.drop_index("ix_tasks_chat_status", table_name="tasks")
    op.drop_table("tasks")
