"""Add durable task-detection jobs and attempt records.

Revision ID: 20260822_0004
Revises: 20260822_0003
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0004"
down_revision: str | None = "20260822_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "detection_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            "status IN ('queued', 'running', 'completed', 'dead')",
            name="ck_detection_jobs_status",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10",
            name="ck_detection_jobs_max_attempts",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_detection_jobs_attempt_count",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL "
            "AND leased_at IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'running' AND worker_id IS NULL "
            "AND leased_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_detection_jobs_lease_state",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) "
            "OR (status != 'completed' AND completed_at IS NULL)",
            name="ck_detection_jobs_completed_state",
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chat_id",
            "trigger_message_id",
            name="uq_detection_jobs_chat_trigger",
        ),
    )
    op.create_index(
        "ix_detection_jobs_ready",
        "detection_jobs",
        ["status", "available_at", "priority", "id"],
        unique=False,
    )
    op.create_index(
        "ix_detection_jobs_chat_status",
        "detection_jobs",
        ["chat_id", "status"],
        unique=False,
    )

    op.create_table(
        "detection_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("response_format", sa.String(length=32), nullable=False),
        sa.Column("context_version", sa.String(length=16), nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("context_message_ids_json", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_detection_runs_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_detection_runs_attempt"),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_detection_runs_latency",
        ),
        sa.CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_detection_runs_prompt_tokens",
        ),
        sa.CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_detection_runs_completion_tokens",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_detection_runs_total_tokens",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) "
            "OR (status != 'running' AND finished_at IS NOT NULL)",
            name="ck_detection_runs_finished_state",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND result_json IS NOT NULL "
            "AND error_code IS NULL) "
            "OR (status = 'failed' AND result_json IS NULL "
            "AND error_code IS NOT NULL) "
            "OR status = 'running'",
            name="ck_detection_runs_result_state",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["detection_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "attempt", name="uq_detection_runs_job_attempt"
        ),
    )
    op.create_index(
        "ix_detection_runs_status_started",
        "detection_runs",
        ["status", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_detection_runs_status_started", table_name="detection_runs"
    )
    op.drop_table("detection_runs")
    op.drop_index(
        "ix_detection_jobs_chat_status", table_name="detection_jobs"
    )
    op.drop_index("ix_detection_jobs_ready", table_name="detection_jobs")
    op.drop_table("detection_jobs")
