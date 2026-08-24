"""Audit the final receive target for sent reminders.

Revision ID: 20260823_0009
Revises: 20260823_0008
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0009"
down_revision: str | None = "20260823_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_reminders") as batch:
        batch.add_column(
            sa.Column(
                "delivery_receive_id_type",
                sa.String(length=16),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "delivery_receive_id",
                sa.String(length=128),
                nullable=True,
            )
        )

    # Phase 5A did not have a production sender, but the old schema allowed a
    # manually imported/audited sent row. Preserve such rows by attributing
    # their historical delivery to the source chat before enforcing the new
    # sent-target invariant.
    op.execute(
        sa.text(
            """
            UPDATE task_reminders
            SET delivery_receive_id_type = 'chat_id',
                delivery_receive_id = (
                    SELECT tasks.chat_id
                    FROM tasks
                    WHERE tasks.id = task_reminders.task_id
                )
            WHERE status = 'sent'
            """
        )
    )

    with op.batch_alter_table("task_reminders") as batch:
        batch.create_check_constraint(
            "ck_task_reminders_delivery_state",
            "(status = 'sent' AND delivery_receive_id_type IS NOT NULL "
            "AND delivery_receive_id IS NOT NULL) OR "
            "(status != 'sent' AND delivery_receive_id_type IS NULL "
            "AND delivery_receive_id IS NULL)",
        )
        batch.create_check_constraint(
            "ck_task_reminders_delivery_receive_type",
            "delivery_receive_id_type IS NULL OR "
            "delivery_receive_id_type IN ('open_id', 'chat_id')",
        )


def downgrade() -> None:
    with op.batch_alter_table("task_reminders") as batch:
        batch.drop_constraint(
            "ck_task_reminders_delivery_receive_type", type_="check"
        )
        batch.drop_constraint(
            "ck_task_reminders_delivery_state", type_="check"
        )
        batch.drop_column("delivery_receive_id")
        batch.drop_column("delivery_receive_id_type")
