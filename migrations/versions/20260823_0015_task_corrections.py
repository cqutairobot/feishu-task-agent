"""Add audited task correction lifecycle actions.

Revision ID: 20260823_0015
Revises: 20260823_0014
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0015"
down_revision: str | None = "20260823_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CORRECTION_NOTIFICATION_KINDS = (
    "task_renamed_assignee",
    "task_assignee_added",
    "task_assignee_removed",
    "task_assignees_changed",
    "task_invalidated_assignee",
    "task_renamed_admin",
    "task_reassigned_admin",
    "task_invalidated_admin",
)


def upgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.add_column(sa.Column("title_before", sa.String(length=200)))
        batch.add_column(sa.Column("title_after", sa.String(length=200)))
        batch.add_column(sa.Column("assignees_before_json", sa.Text()))
        batch.add_column(sa.Column("assignees_after_json", sa.Text()))
        batch.drop_constraint(
            "ck_task_lifecycle_events_action", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_new_status", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_outcome", type_="check"
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            "action IN ('complete', 'reschedule', 'cancel', 'rename', "
            "'reassign', 'invalidate')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_new_status",
            "new_status IN ('todo', 'overdue', 'done', 'cancelled')",
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
        batch.create_check_constraint(
            "ck_task_lifecycle_events_correction_payload",
            "(action = 'rename' AND title_before IS NOT NULL "
            "AND title_after IS NOT NULL AND title_before != title_after "
            "AND assignees_before_json IS NULL "
            "AND assignees_after_json IS NULL) OR "
            "(action = 'reassign' AND title_before IS NULL "
            "AND title_after IS NULL AND assignees_before_json IS NOT NULL "
            "AND assignees_after_json IS NOT NULL "
            "AND assignees_before_json != assignees_after_json) OR "
            "(action NOT IN ('rename', 'reassign') "
            "AND title_before IS NULL AND title_after IS NULL "
            "AND assignees_before_json IS NULL "
            "AND assignees_after_json IS NULL)",
        )

    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            "kind IN ('missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin', 'task_rescheduled_admin', "
            "'task_done_coassignee', 'task_cancelled_coassignee', "
            "'task_rescheduled_coassignee', 'task_renamed_assignee', "
            "'task_assignee_added', 'task_assignee_removed', "
            "'task_assignees_changed', 'task_invalidated_assignee', "
            "'task_renamed_admin', 'task_reassigned_admin', "
            "'task_invalidated_admin')",
        )


def downgrade() -> None:
    quoted_kinds = ", ".join(f"'{kind}'" for kind in _CORRECTION_NOTIFICATION_KINDS)
    op.execute(
        f"DELETE FROM task_notifications WHERE kind IN ({quoted_kinds})"
    )
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            "kind IN ('missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin', 'task_rescheduled_admin', "
            "'task_done_coassignee', 'task_cancelled_coassignee', "
            "'task_rescheduled_coassignee')",
        )

    op.execute(
        "DELETE FROM task_lifecycle_events WHERE action IN "
        "('rename', 'reassign', 'invalidate')"
    )
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_correction_payload", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_outcome", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_new_status", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_action", type_="check"
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            "action IN ('complete', 'reschedule', 'cancel')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_new_status",
            "new_status IN ('todo', 'done', 'cancelled')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome",
            "(action = 'complete' AND new_status = 'done') OR "
            "(action = 'cancel' AND new_status = 'cancelled') OR "
            "(action = 'reschedule' AND new_status = 'todo' "
            "AND deadline_after IS NOT NULL)",
        )
        batch.drop_column("assignees_after_json")
        batch.drop_column("assignees_before_json")
        batch.drop_column("title_after")
        batch.drop_column("title_before")
