"""Add durable notification kinds for accepted completion submissions.

Revision ID: 20260831_0039
Revises: 20260831_0038
"""

from typing import Sequence

from alembic import op


revision: str = "20260831_0039"
down_revision: str | None = "20260831_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KINDS = (
    "kind IN ('task_created_assignee', 'missing_deadline_owner', "
    "'missing_deadline_admin', 'task_done_admin', 'task_cancelled_admin', "
    "'task_overdue_admin', 'task_rescheduled_admin', "
    "'task_done_coassignee', 'task_cancelled_coassignee', "
    "'task_rescheduled_coassignee', 'task_renamed_assignee', "
    "'task_assignee_added', 'task_assignee_removed', "
    "'task_assignees_changed', 'task_invalidated_assignee', "
    "'task_renamed_admin', 'task_reassigned_admin', "
    "'task_invalidated_admin', 'task_restored_coassignee', "
    "'task_restored_admin', 'task_reopened_coassignee', "
    "'task_reopened_admin', 'task_accepted_coassignee', "
    "'task_accepted_admin')"
)


def upgrade() -> None:
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint("ck_task_notifications_kind", _KINDS)
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_notifications "
        "WHERE kind IN ('task_accepted_coassignee', 'task_accepted_admin')"
    )
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            _KINDS.replace(
                ", 'task_accepted_coassignee', 'task_accepted_admin'", ""
            ),
        )
