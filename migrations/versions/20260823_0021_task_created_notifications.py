"""Add private notification kind for newly assigned tasks.

Revision ID: 20260823_0021
Revises: 20260823_0020
"""

from typing import Sequence

from alembic import op


revision: str = "20260823_0021"
down_revision: str | None = "20260823_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KIND_CHECK = (
    "kind IN ('task_created_assignee', "
    "'missing_deadline_owner', 'missing_deadline_admin', "
    "'task_done_admin', 'task_cancelled_admin', "
    "'task_overdue_admin', 'task_rescheduled_admin', "
    "'task_done_coassignee', 'task_cancelled_coassignee', "
    "'task_rescheduled_coassignee', 'task_renamed_assignee', "
    "'task_assignee_added', 'task_assignee_removed', "
    "'task_assignees_changed', 'task_invalidated_assignee', "
    "'task_renamed_admin', 'task_reassigned_admin', "
    "'task_invalidated_admin')"
)

_PREVIOUS_KIND_CHECK = (
    "kind IN ('missing_deadline_owner', 'missing_deadline_admin', "
    "'task_done_admin', 'task_cancelled_admin', "
    "'task_overdue_admin', 'task_rescheduled_admin', "
    "'task_done_coassignee', 'task_cancelled_coassignee', "
    "'task_rescheduled_coassignee', 'task_renamed_assignee', "
    "'task_assignee_added', 'task_assignee_removed', "
    "'task_assignees_changed', 'task_invalidated_assignee', "
    "'task_renamed_admin', 'task_reassigned_admin', "
    "'task_invalidated_admin')"
)


def upgrade() -> None:
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            _KIND_CHECK,
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_notifications WHERE kind = 'task_created_assignee'"
    )
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            _PREVIOUS_KIND_CHECK,
        )
