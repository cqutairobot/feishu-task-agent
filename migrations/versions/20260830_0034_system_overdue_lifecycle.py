"""Record automatic overdue transitions as system lifecycle events.

Revision ID: 20260830_0034
Revises: 20260828_0033
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0034"
down_revision: str | None = "20260828_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LIFECYCLE_ACTIONS = (
    "action IN ('confirm', 'complete', 'accept', 'reopen', 'reschedule', "
    "'cancel', 'rename', 'reassign', 'invalidate', 'restore', 'merge', "
    "'overdue')"
)
_PREVIOUS_LIFECYCLE_ACTIONS = (
    "action IN ('confirm', 'complete', 'accept', 'reopen', 'reschedule', "
    "'cancel', 'rename', 'reassign', 'invalidate', 'restore', 'merge')"
)
_LIFECYCLE_AUTHORIZATION = (
    "authorization_role IN ('owner', 'administrator', 'system')"
)
_LIFECYCLE_TRIGGER_SOURCE = (
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
    "AND trigger_management_request_id IS NOT NULL) OR "
    "(trigger_source = 'system' "
    "AND trigger_message_db_id IS NULL "
    "AND trigger_card_action_id IS NULL "
    "AND trigger_card_message_id IS NULL "
    "AND trigger_card_chat_id IS NULL "
    "AND trigger_management_request_id IS NULL)"
)
_LIFECYCLE_SYSTEM_ORIGIN = (
    "(trigger_source = 'system' AND action = 'overdue' "
    "AND authorization_role = 'system') OR "
    "(trigger_source != 'system' AND action != 'overdue' "
    "AND authorization_role != 'system')"
)
_LIFECYCLE_OUTCOME = (
    "(action = 'confirm' AND previous_status = 'pending' "
    "AND new_status = 'todo') OR "
    "(action = 'complete' AND new_status = 'done') OR "
    "(action = 'accept' AND previous_status = 'done' "
    "AND new_status = 'done') OR "
    "(action = 'reopen' AND previous_status = 'done' "
    "AND new_status IN ('todo', 'overdue')) OR "
    "(action = 'overdue' AND previous_status = 'todo' "
    "AND new_status = 'overdue' AND deadline_before IS NOT NULL "
    "AND deadline_after = deadline_before) OR "
    "(action = 'cancel' AND new_status = 'cancelled') OR "
    "(action = 'invalidate' AND new_status = 'cancelled') OR "
    "(action = 'restore' AND previous_status IN ('done', 'cancelled') "
    "AND new_status IN ('todo', 'overdue')) OR "
    "(action = 'merge' AND previous_status IN "
    "('pending', 'todo', 'overdue', 'done') "
    "AND new_status = 'cancelled') OR "
    "(action = 'reschedule' AND new_status = 'todo' "
    "AND deadline_after IS NOT NULL) OR "
    "(action IN ('rename', 'reassign') "
    "AND new_status = previous_status)"
)
_PREVIOUS_LIFECYCLE_OUTCOME = (
    "(action = 'confirm' AND previous_status = 'pending' "
    "AND new_status = 'todo') OR "
    "(action = 'complete' AND new_status = 'done') OR "
    "(action = 'accept' AND previous_status = 'done' "
    "AND new_status = 'done') OR "
    "(action = 'reopen' AND previous_status = 'done' "
    "AND new_status IN ('todo', 'overdue')) OR "
    "(action = 'cancel' AND new_status = 'cancelled') OR "
    "(action = 'invalidate' AND new_status = 'cancelled') OR "
    "(action = 'restore' AND previous_status IN ('done', 'cancelled') "
    "AND new_status IN ('todo', 'overdue')) OR "
    "(action = 'merge' AND previous_status IN "
    "('pending', 'todo', 'overdue', 'done') "
    "AND new_status = 'cancelled') OR "
    "(action = 'reschedule' AND new_status = 'todo' "
    "AND deadline_after IS NOT NULL) OR "
    "(action IN ('rename', 'reassign') "
    "AND new_status = previous_status)"
)


def upgrade() -> None:
    # SQLite requires a table rebuild to replace CHECK constraints. Alembic's
    # batch operation preserves all existing rows and child foreign keys.
    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint("ck_task_lifecycle_events_trigger_source", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_authorization", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.create_check_constraint(
            "ck_task_lifecycle_events_trigger_source", _LIFECYCLE_TRIGGER_SOURCE
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action", _LIFECYCLE_ACTIONS
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_authorization", _LIFECYCLE_AUTHORIZATION
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_system_origin", _LIFECYCLE_SYSTEM_ORIGIN
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome", _LIFECYCLE_OUTCOME
        )

    op.execute("PRAGMA optimize")


def downgrade() -> None:
    # Do not leave events that the previous schema cannot represent. The
    # associated task status is left intact; a later reminder sync can restore
    # the old behavior if the downgrade is only temporary.
    op.execute(
        "DELETE FROM task_lifecycle_evidence WHERE event_id IN "
        "(SELECT id FROM task_lifecycle_events WHERE action = 'overdue')"
    )
    op.execute("DELETE FROM task_lifecycle_events WHERE action = 'overdue'")

    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_constraint("ck_task_lifecycle_events_system_origin", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_trigger_source", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_authorization", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.create_check_constraint(
            "ck_task_lifecycle_events_trigger_source",
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
            "AND trigger_management_request_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            _PREVIOUS_LIFECYCLE_ACTIONS,
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_authorization",
            "authorization_role IN ('owner', 'administrator')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome",
            _PREVIOUS_LIFECYCLE_OUTCOME,
        )
