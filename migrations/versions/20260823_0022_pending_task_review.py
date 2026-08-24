"""Add audited administrator confirmation for pending tasks.

Revision ID: 20260823_0022
Revises: 20260823_0021
"""

from typing import Sequence

from alembic import op


revision: str = "20260823_0022"
down_revision: str | None = "20260823_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_outcome", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_previous_status", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_action", type_="check"
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            "action IN ('confirm', 'complete', 'reschedule', 'cancel', "
            "'rename', 'reassign', 'invalidate')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_previous_status",
            "previous_status IN ('pending', 'todo', 'overdue')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome",
            "(action = 'confirm' AND previous_status = 'pending' "
            "AND new_status = 'todo') OR "
            "(action = 'complete' AND new_status = 'done') OR "
            "(action = 'cancel' AND new_status = 'cancelled') OR "
            "(action = 'invalidate' AND new_status = 'cancelled') OR "
            "(action = 'reschedule' AND new_status = 'todo' "
            "AND deadline_after IS NOT NULL) OR "
            "(action IN ('rename', 'reassign') "
            "AND new_status = previous_status)",
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_lifecycle_events WHERE action = 'confirm'"
    )
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_outcome", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_previous_status", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_action", type_="check"
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            "action IN ('complete', 'reschedule', 'cancel', 'rename', "
            "'reassign', 'invalidate')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_previous_status",
            "previous_status IN ('todo', 'overdue')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome",
            "(action = 'complete' AND new_status = 'done') OR "
            "(action = 'cancel' AND new_status = 'cancelled') OR "
            "(action = 'invalidate' AND new_status = 'cancelled') OR "
            "(action = 'reschedule' AND new_status = 'todo' "
            "AND deadline_after IS NOT NULL) OR "
            "(action IN ('rename', 'reassign') "
            "AND new_status = previous_status)",
        )
