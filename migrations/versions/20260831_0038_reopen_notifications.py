"""Add durable notification kinds for administrator-requested rework.

Revision ID: 20260831_0038
Revises: 20260830_0037
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0038"
down_revision: str | None = "20260830_0037"
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
    "'task_reopened_admin')"
)


def upgrade() -> None:
    op.add_column("task_notifications", sa.Column("reason_snapshot", sa.Text()))
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint("ck_task_notifications_kind", _KINDS)
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_notifications "
        "WHERE kind IN ('task_reopened_coassignee', 'task_reopened_admin')"
    )
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint("ck_task_notifications_kind", type_="check")
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            _KINDS.replace(
                ", 'task_reopened_coassignee', 'task_reopened_admin'", ""
            ),
        )
        batch.drop_column("reason_snapshot")
