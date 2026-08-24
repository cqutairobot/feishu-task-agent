"""Add shared task assignees and recipient-scoped reminders.

Revision ID: 20260823_0014
Revises: 20260823_0013
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0014"
down_revision: str | None = "20260823_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_assignees",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "position >= 0", name="ck_task_assignees_position"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "open_id", name="uq_task_assignees_task_user"
        ),
        sa.UniqueConstraint(
            "task_id", "position", name="uq_task_assignees_task_position"
        ),
    )
    op.create_index(
        "ix_task_assignees_user_task",
        "task_assignees",
        ["open_id", "task_id"],
        unique=False,
    )
    op.execute(
        "INSERT INTO task_assignees "
        "(task_id, open_id, name_snapshot, position, created_at) "
        "SELECT id, owner_open_id, owner_name_snapshot, 0, created_at "
        "FROM tasks"
    )

    with op.batch_alter_table("task_reminders") as batch:
        batch.add_column(
            sa.Column("recipient_open_id", sa.String(length=128))
        )
        batch.add_column(
            sa.Column("recipient_name_snapshot", sa.String(length=255))
        )
    op.execute(
        "UPDATE task_reminders SET "
        "recipient_open_id = ("
        "SELECT owner_open_id FROM tasks WHERE tasks.id = task_reminders.task_id"
        "), recipient_name_snapshot = ("
        "SELECT owner_name_snapshot FROM tasks "
        "WHERE tasks.id = task_reminders.task_id)"
    )
    with op.batch_alter_table("task_reminders") as batch:
        batch.alter_column(
            "recipient_open_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch.alter_column(
            "recipient_name_snapshot",
            existing_type=sa.String(length=255),
            nullable=False,
        )
        batch.drop_constraint(
            "uq_task_reminders_task_kind_deadline", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_task_reminders_task_recipient_kind_deadline",
            [
                "task_id",
                "recipient_open_id",
                "kind",
                "deadline_snapshot",
            ],
        )
        batch.create_foreign_key(
            "fk_task_reminders_recipient_open_id_users",
            "users",
            ["recipient_open_id"],
            ["open_id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_task_reminders_recipient_status",
            ["recipient_open_id", "status"],
            unique=False,
        )

    with op.batch_alter_table("task_notifications") as batch:
        batch.add_column(
            sa.Column(
                "deadline_before_snapshot",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.drop_constraint(
            "ck_task_notifications_kind", type_="check"
        )
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            "kind IN ('missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin', 'task_rescheduled_admin', "
            "'task_done_coassignee', 'task_cancelled_coassignee', "
            "'task_rescheduled_coassignee')",
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_notifications WHERE kind IN ("
        "'task_rescheduled_admin', 'task_done_coassignee', "
        "'task_cancelled_coassignee', 'task_rescheduled_coassignee')"
    )
    with op.batch_alter_table("task_notifications") as batch:
        batch.drop_constraint(
            "ck_task_notifications_kind", type_="check"
        )
        batch.create_check_constraint(
            "ck_task_notifications_kind",
            "kind IN ('missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin')",
        )
        batch.drop_column("deadline_before_snapshot")

    op.execute(
        "DELETE FROM task_reminders WHERE recipient_open_id != ("
        "SELECT owner_open_id FROM tasks WHERE tasks.id = task_reminders.task_id"
        ")"
    )
    with op.batch_alter_table("task_reminders") as batch:
        batch.drop_index("ix_task_reminders_recipient_status")
        batch.drop_constraint(
            "fk_task_reminders_recipient_open_id_users", type_="foreignkey"
        )
        batch.drop_constraint(
            "uq_task_reminders_task_recipient_kind_deadline", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_task_reminders_task_kind_deadline",
            ["task_id", "kind", "deadline_snapshot"],
        )
        batch.drop_column("recipient_name_snapshot")
        batch.drop_column("recipient_open_id")

    op.drop_index(
        "ix_task_assignees_user_task", table_name="task_assignees"
    )
    op.drop_table("task_assignees")
