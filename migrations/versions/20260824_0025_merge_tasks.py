"""Add administrator duplicate-task merge provenance.

Revision ID: 20260824_0025
Revises: 20260824_0024
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0025"
down_revision: str | None = "20260824_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LIFECYCLE_ACTIONS = (
    "action IN ('confirm', 'complete', 'reschedule', 'cancel', 'rename', "
    "'reassign', 'invalidate', 'restore', 'merge')"
)
_LIFECYCLE_OUTCOME = (
    "(action = 'confirm' AND previous_status = 'pending' "
    "AND new_status = 'todo') OR "
    "(action = 'complete' AND new_status = 'done') OR "
    "(action = 'cancel' AND new_status = 'cancelled') OR "
    "(action = 'invalidate' AND new_status = 'cancelled') OR "
    "(action = 'restore' AND previous_status IN ('done', 'cancelled') "
    "AND new_status IN ('todo', 'overdue')) OR "
    "(action = 'merge' AND previous_status IN "
    "('pending', 'todo', 'overdue', 'done') "
    "AND new_status = 'cancelled') OR "
    "(action = 'reschedule' AND new_status = 'todo' "
    "AND deadline_after IS NOT NULL) OR "
    "(action IN ('rename', 'reassign') AND new_status = previous_status)"
)


def upgrade() -> None:
    # SQLite cannot recreate the parent tasks table while its many child
    # tables contain rows. The service validates the self-reference and the
    # paired timestamp atomically; the columns are added directly here.
    op.add_column("tasks", sa.Column("merged_into_task_id", sa.Integer()))
    op.add_column("tasks", sa.Column("merged_at", sa.DateTime()))
    op.create_index(
        "ix_tasks_merged_into_task", "tasks", ["merged_into_task_id"], unique=False
    )

    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.add_column(sa.Column("merge_target_task_id", sa.Integer()))
        batch.create_foreign_key(
            "fk_task_lifecycle_events_merge_target",
            "tasks",
            ["merge_target_task_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action", _LIFECYCLE_ACTIONS
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome", _LIFECYCLE_OUTCOME
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_merge_target",
            "(action = 'merge' AND merge_target_task_id IS NOT NULL) OR "
            "(action != 'merge' AND merge_target_task_id IS NULL)",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET merged_into_task_id = NULL, merged_at = NULL "
        "WHERE merged_into_task_id IS NOT NULL"
    )
    op.execute("DELETE FROM task_lifecycle_events WHERE action = 'merge'")

    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_merge_target", type_="check"
        )
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint(
            "fk_task_lifecycle_events_merge_target", type_="foreignkey"
        )
        batch.drop_column("merge_target_task_id")
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            "action IN ('confirm', 'complete', 'reschedule', 'cancel', "
            "'rename', 'reassign', 'invalidate', 'restore')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome",
            "(action = 'confirm' AND previous_status = 'pending' "
            "AND new_status = 'todo') OR "
            "(action = 'complete' AND new_status = 'done') OR "
            "(action = 'cancel' AND new_status = 'cancelled') OR "
            "(action = 'invalidate' AND new_status = 'cancelled') OR "
            "(action = 'restore' AND previous_status IN ('done', 'cancelled') "
            "AND new_status IN ('todo', 'overdue')) OR "
            "(action = 'reschedule' AND new_status = 'todo' "
            "AND deadline_after IS NOT NULL) OR "
            "(action IN ('rename', 'reassign') "
            "AND new_status = previous_status)",
        )

    op.drop_index("ix_tasks_merged_into_task", table_name="tasks")
    op.drop_column("tasks", "merged_at")
    op.drop_column("tasks", "merged_into_task_id")
