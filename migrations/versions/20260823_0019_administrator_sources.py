"""Add self-service and membership reconciliation administrator sources.

Revision ID: 20260823_0019
Revises: 20260823_0018
"""

from typing import Sequence

from alembic import op


revision: str = "20260823_0019"
down_revision: str | None = "20260823_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_SOURCE = "source IN ('local_cli', 'management_page', 'bootstrap')"
_ADMIN_SOURCE = (
    "source IN ('local_cli', 'management_page', 'bootstrap', "
    "'group_owner_init', 'group_owner_takeover')"
)
_EVENT_SOURCE = (
    "source IN ('local_cli', 'management_page', 'bootstrap', "
    "'group_owner_init', 'group_owner_takeover', 'membership_sync')"
)


def upgrade() -> None:
    with op.batch_alter_table("chat_administrators") as batch:
        batch.drop_constraint(
            "ck_chat_administrators_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_chat_administrators_source", _ADMIN_SOURCE
        )
    with op.batch_alter_table("chat_administrator_events") as batch:
        batch.drop_constraint(
            "ck_chat_administrator_events_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_chat_administrator_events_source", _EVENT_SOURCE
        )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    with op.batch_alter_table("chat_administrator_events") as batch:
        batch.drop_constraint(
            "ck_chat_administrator_events_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_chat_administrator_events_source", _OLD_SOURCE
        )
    with op.batch_alter_table("chat_administrators") as batch:
        batch.drop_constraint(
            "ck_chat_administrators_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_chat_administrators_source", _OLD_SOURCE
        )
