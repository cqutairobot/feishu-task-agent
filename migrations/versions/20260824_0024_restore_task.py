"""Add administrator restore lifecycle action and notifications.

Revision ID: 20260824_0024
Revises: 20260823_0023
"""

from typing import Sequence

from alembic import op


revision: str = "20260824_0024"
down_revision: str | None = "20260823_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LIFECYCLE_ACTIONS = (
    "action IN ('confirm', 'complete', 'reschedule', 'cancel', 'rename', "
    "'reassign', 'invalidate', 'restore')"
)
_LIFECYCLE_PREVIOUS = (
    "previous_status IN ('pending', 'todo', 'overdue', 'done', 'cancelled')"
)
_LIFECYCLE_OUTCOME = (
    "(action = 'confirm' AND previous_status = 'pending' "
    "AND new_status = 'todo') OR "
    "(action = 'complete' AND new_status = 'done') OR "
    "(action = 'cancel' AND new_status = 'cancelled') OR "
    "(action = 'invalidate' AND new_status = 'cancelled') OR "
    "(action = 'restore' AND previous_status IN ('done', 'cancelled') "
    "AND new_status IN ('todo', 'overdue')) OR "
    "(action = 'reschedule' AND new_status = 'todo' "
    "AND deadline_after IS NOT NULL) OR "
    "(action IN ('rename', 'reassign') AND new_status = previous_status)"
)
_NOTIFICATION_KINDS = (
    "kind IN ('task_created_assignee', "
    "'missing_deadline_owner', 'missing_deadline_admin', "
    "'task_done_admin', 'task_cancelled_admin', "
    "'task_overdue_admin', 'task_rescheduled_admin', "
    "'task_done_coassignee', 'task_cancelled_coassignee', "
    "'task_rescheduled_coassignee', 'task_renamed_assignee', "
    "'task_assignee_added', 'task_assignee_removed', "
    "'task_assignees_changed', 'task_invalidated_assignee', "
    "'task_renamed_admin', 'task_reassigned_admin', "
    "'task_invalidated_admin', 'task_restored_coassignee', "
    "'task_restored_admin')"
)


def upgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint(
            "ck_task_lifecycle_events_previous_status", type_="check"
        )
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action", _LIFECYCLE_ACTIONS
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_previous_status", _LIFECYCLE_PREVIOUS
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome", _LIFECYCLE_OUTCOME
        )

    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind", _NOTIFICATION_KINDS
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_notifications WHERE kind IN "
        "('task_restored_coassignee', 'task_restored_admin')"
    )
    op.execute("DELETE FROM task_lifecycle_events WHERE action = 'restore'")
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint(
            "ck_task_lifecycle_events_previous_status", type_="check"
        )
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
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

    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            "kind IN ('task_created_assignee', "
            "'missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin', 'task_rescheduled_admin', "
            "'task_done_coassignee', 'task_cancelled_coassignee', "
            "'task_rescheduled_coassignee', 'task_renamed_assignee', "
            "'task_assignee_added', 'task_assignee_removed', "
            "'task_assignees_changed', 'task_invalidated_assignee', "
            "'task_renamed_admin', 'task_reassigned_admin', "
            "'task_invalidated_admin')",
        )
