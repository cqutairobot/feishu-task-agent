"""Add an explicit blocker category for task provenance notes.

Revision ID: 20260830_0035
Revises: 20260830_0034
"""

from typing import Sequence

from alembic import op


revision: str = "20260830_0035"
down_revision: str | None = "20260830_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NOTE_TYPES = (
    "note_type IN ('progress', 'blocker', 'completion', 'delay', "
    "'reopen', 'general', 'correction')"
)
_PREVIOUS_NOTE_TYPES = (
    "note_type IN ('progress', 'completion', 'delay', 'reopen', "
    "'general', 'correction')"
)


def upgrade() -> None:
    with op.batch_alter_table("task_notes") as batch:
        batch.drop_constraint("ck_task_notes_note_type", type_="check")
        batch.create_check_constraint(
            "ck_task_notes_note_type",
            _NOTE_TYPES,
        )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    # Preserve the note text on downgrade; only its more specific category is
    # folded into the previous schema's general category.
    op.execute(
        "UPDATE task_notes SET note_type = 'general' "
        "WHERE note_type = 'blocker'"
    )
    with op.batch_alter_table("task_notes") as batch:
        batch.drop_constraint("ck_task_notes_note_type", type_="check")
        batch.create_check_constraint(
            "ck_task_notes_note_type",
            _PREVIOUS_NOTE_TYPES,
        )
