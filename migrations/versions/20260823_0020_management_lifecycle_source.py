"""Allow lifecycle events to originate from the management page.

Revision ID: 20260823_0020
Revises: 20260823_0019
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0020"
down_revision: str | None = "20260823_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TRIGGER_SOURCE_CHECK = (
    "(trigger_source = 'message' "
    "AND trigger_message_db_id IS NOT NULL "
    "AND trigger_card_action_id IS NULL "
    "AND trigger_card_message_id IS NULL "
    "AND trigger_card_chat_id IS NULL "
    "AND trigger_management_request_id IS NULL) OR "
    "(trigger_source = 'card_action' "
    "AND trigger_message_db_id IS NULL "
    "AND trigger_card_action_id IS NOT NULL "
    "AND trigger_card_message_id IS NOT NULL "
    "AND trigger_card_chat_id IS NOT NULL "
    "AND trigger_management_request_id IS NULL) OR "
    "(trigger_source = 'management_page' "
    "AND trigger_message_db_id IS NULL "
    "AND trigger_card_action_id IS NULL "
    "AND trigger_card_message_id IS NULL "
    "AND trigger_card_chat_id IS NULL "
    "AND trigger_management_request_id IS NOT NULL)"
)

_LEGACY_TRIGGER_SOURCE_CHECK = (
    "(trigger_source = 'message' "
    "AND trigger_message_db_id IS NOT NULL "
    "AND trigger_card_action_id IS NULL "
    "AND trigger_card_message_id IS NULL "
    "AND trigger_card_chat_id IS NULL) OR "
    "(trigger_source = 'card_action' "
    "AND trigger_message_db_id IS NULL "
    "AND trigger_card_action_id IS NOT NULL "
    "AND trigger_card_message_id IS NOT NULL "
    "AND trigger_card_chat_id IS NOT NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_trigger_source", type_="check"
        )
        batch.add_column(
            sa.Column(
                "trigger_management_request_id",
                sa.String(length=128),
                nullable=True,
            )
        )
        batch.create_unique_constraint(
            "uq_task_lifecycle_events_management_request",
            ["trigger_management_request_id"],
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_trigger_source",
            _TRIGGER_SOURCE_CHECK,
        )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    op.execute(
        "DELETE FROM task_lifecycle_evidence WHERE event_id IN "
        "(SELECT id FROM task_lifecycle_events "
        "WHERE trigger_source = 'management_page')"
    )
    op.execute(
        "DELETE FROM task_lifecycle_events "
        "WHERE trigger_source = 'management_page'"
    )
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint(
            "ck_task_lifecycle_events_trigger_source", type_="check"
        )
        batch.drop_constraint(
            "uq_task_lifecycle_events_management_request", type_="unique"
        )
        batch.drop_column("trigger_management_request_id")
        batch.create_check_constraint(
            "ck_task_lifecycle_events_trigger_source",
            _LEGACY_TRIGGER_SOURCE_CHECK,
        )
