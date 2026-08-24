"""Add durable provenance for management-created tasks.

Revision ID: 20260823_0023
Revises: 20260823_0022
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0023"
down_revision: str | None = "20260823_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_creation_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("actor_open_id", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("title_snapshot", sa.String(length=200), nullable=False),
        sa.Column("description_snapshot", sa.Text(), nullable=False),
        sa.Column("deadline_snapshot", sa.DateTime(), nullable=True),
        sa.Column("assignees_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source = 'management_page'",
            name="ck_task_creation_events_source",
        ),
        sa.ForeignKeyConstraint(
            ["actor_open_id"],
            ["users.open_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id", name="uq_task_creation_events_request"
        ),
        sa.UniqueConstraint("task_id", name="uq_task_creation_events_task"),
    )
    op.create_index(
        "ix_task_creation_events_actor_created",
        "task_creation_events",
        ["actor_open_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_creation_events_actor_created",
        table_name="task_creation_events",
    )
    op.drop_table("task_creation_events")
