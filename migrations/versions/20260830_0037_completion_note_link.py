"""Link message completion submissions to their immutable completion note.

Revision ID: 20260830_0037
Revises: 20260830_0036
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_0037"
down_revision: str | None = "20260830_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_completion_submissions") as batch:
        batch.add_column(sa.Column("completion_note_id", sa.Integer()))
        batch.create_foreign_key(
            "fk_task_completion_submissions_completion_note_id_task_notes",
            "task_notes",
            ["completion_note_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_task_completion_submissions_completion_note",
            ["completion_note_id"],
        )
    op.create_index(
        "ix_task_completion_submissions_completion_note",
        "task_completion_submissions",
        ["completion_note_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_completion_submissions_completion_note",
        table_name="task_completion_submissions",
    )
    with op.batch_alter_table("task_completion_submissions") as batch:
        batch.drop_constraint(
            "uq_task_completion_submissions_completion_note",
            type_="unique",
        )
        batch.drop_constraint(
            "fk_task_completion_submissions_completion_note_id_task_notes",
            type_="foreignkey",
        )
        batch.drop_column("completion_note_id")
