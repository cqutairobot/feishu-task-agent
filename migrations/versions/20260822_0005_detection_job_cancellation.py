"""Add an auditable cancelled state to detection jobs.

Revision ID: 20260822_0005
Revises: 20260822_0004
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_0005"
down_revision: str | None = "20260822_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_COLUMNS = (
    "id, job_id, attempt, status, provider, model, response_format, "
    "context_version, context_fingerprint, context_message_ids_json, "
    "request_id, prompt_tokens, completion_tokens, total_tokens, "
    "result_json, error_code, error_message, latency_ms, started_at, finished_at"
)


def upgrade() -> None:
    _backup_detection_runs()
    with op.batch_alter_table(
        "detection_jobs", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_detection_jobs_status", type_="check"
        )
        batch_op.add_column(
            sa.Column(
                "cancelled_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("cancel_reason", sa.String(length=500), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_detection_jobs_status",
            "status IN ('queued', 'running', 'completed', 'dead', 'cancelled')",
        )
        batch_op.create_check_constraint(
            "ck_detection_jobs_cancelled_state",
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancel_reason IS NULL)",
        )
    _restore_detection_runs()


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE detection_jobs "
            "SET status = 'dead', cancelled_at = NULL, cancel_reason = NULL, "
            "last_error_code = 'cancelled_before_downgrade' "
            "WHERE status = 'cancelled'"
        )
    )
    _backup_detection_runs()
    with op.batch_alter_table(
        "detection_jobs", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_detection_jobs_cancelled_state", type_="check"
        )
        batch_op.drop_constraint(
            "ck_detection_jobs_status", type_="check"
        )
        batch_op.drop_column("cancel_reason")
        batch_op.drop_column("cancelled_at")
        batch_op.create_check_constraint(
            "ck_detection_jobs_status",
            "status IN ('queued', 'running', 'completed', 'dead')",
        )
    _restore_detection_runs()


def _backup_detection_runs() -> None:
    # SQLite's batch table recreation drops the parent table. With foreign keys
    # enabled, that invokes detection_runs.job_id ON DELETE CASCADE. Preserve
    # the child audit rows explicitly and restore them after the parent exists.
    op.execute(
        sa.text(
            "CREATE TEMPORARY TABLE phase3c4_detection_runs_backup AS "
            f"SELECT {RUN_COLUMNS} FROM detection_runs"
        )
    )


def _restore_detection_runs() -> None:
    op.execute(sa.text("DELETE FROM detection_runs"))
    op.execute(
        sa.text(
            f"INSERT INTO detection_runs ({RUN_COLUMNS}) "
            f"SELECT {RUN_COLUMNS} FROM phase3c4_detection_runs_backup"
        )
    )
    op.execute(sa.text("DROP TABLE phase3c4_detection_runs_backup"))
