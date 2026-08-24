"""Allow lifecycle events to originate from audited card actions.

Revision ID: 20260823_0012
Revises: 20260823_0011
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0012"
down_revision: str | None = "20260823_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.add_column(
            sa.Column(
                "trigger_source",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'message'"),
            )
        )
        batch.add_column(
            sa.Column(
                "trigger_card_action_id", sa.String(length=128), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "trigger_card_message_id", sa.String(length=128), nullable=True
            )
        )
        batch.add_column(
            sa.Column(
                "trigger_card_chat_id", sa.String(length=128), nullable=True
            )
        )
        batch.alter_column(
            "trigger_message_db_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch.create_unique_constraint(
            "uq_task_lifecycle_events_card_action",
            ["trigger_card_action_id"],
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_trigger_source",
            "(trigger_source = 'message' "
            "AND trigger_message_db_id IS NOT NULL "
            "AND trigger_card_action_id IS NULL "
            "AND trigger_card_message_id IS NULL "
            "AND trigger_card_chat_id IS NULL) OR "
            "(trigger_source = 'card_action' "
            "AND trigger_message_db_id IS NULL "
            "AND trigger_card_action_id IS NOT NULL "
            "AND trigger_card_message_id IS NOT NULL "
            "AND trigger_card_chat_id IS NOT NULL)",
        )


def downgrade() -> None:
    # The legacy schema has no honest message trigger for card actions.
    op.execute(
        "DELETE FROM task_lifecycle_evidence WHERE event_id IN "
        "(SELECT id FROM task_lifecycle_events "
        "WHERE trigger_source = 'card_action')"
    )
    op.execute(
        "DELETE FROM task_lifecycle_events "
        "WHERE trigger_source = 'card_action'"
    )
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_trigger_source", type_="check"
        )
        batch.drop_constraint(
            "uq_task_lifecycle_events_card_action", type_="unique"
        )
        batch.alter_column(
            "trigger_message_db_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.drop_column("trigger_card_chat_id")
        batch.drop_column("trigger_card_message_id")
        batch.drop_column("trigger_card_action_id")
        batch.drop_column("trigger_source")
