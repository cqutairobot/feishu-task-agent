"""Add task provenance, completion cycles, notes, and submissions.

Revision ID: 20260828_0033
Revises: 20260827_0032
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0033"
down_revision: str | None = "20260827_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LIFECYCLE_ACTIONS = (
    "action IN ('confirm', 'complete', 'accept', 'reopen', 'reschedule', "
    "'cancel', 'rename', 'reassign', 'invalidate', 'restore', 'merge')"
)
_LIFECYCLE_OUTCOME = (
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
    # These defaults intentionally describe legacy rows as unknown. They do
    # not infer a publisher or completion actor from old task data.
    # SQLite cannot recreate the tasks parent table while reminders,
    # assignees, evidence and notifications reference it. Add the nullable
    # provenance columns in place, then enforce their domains with triggers.
    # The fresh schema still carries ordinary CHECK constraints from the
    # declarative model; these triggers provide the same protection for
    # databases upgraded in place without deleting child rows.
    op.add_column(
        "tasks",
        sa.Column("created_by_open_id", sa.String(length=128)),
    )
    op.add_column("tasks", sa.Column("created_by_name", sa.String(length=255)))
    op.add_column(
        "tasks",
        sa.Column(
            "created_via",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "creator_attribution_basis",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("creator_attribution_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("reviewed_by_open_id", sa.String(length=128)),
    )
    op.add_column("tasks", sa.Column("reviewed_by_name", sa.String(length=255)))
    op.add_column("tasks", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tasks",
        sa.Column("completion_cycle", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tasks",
        sa.Column("last_completed_by_open_id", sa.String(length=128)),
    )
    op.add_column(
        "tasks", sa.Column("last_completed_by_name", sa.String(length=255))
    )
    op.add_column("tasks", sa.Column("last_completed_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_tasks_creator_created", "tasks", ["created_by_open_id", "created_at"]
    )
    op.create_index(
        "ix_tasks_chat_review_status",
        "tasks",
        ["chat_id", "review_status", "updated_at"],
    )
    op.execute(
        "CREATE TRIGGER ck_tasks_provenance_insert "
        "BEFORE INSERT ON tasks "
        "WHEN NEW.created_via NOT IN ('detected', 'management', 'system', 'unknown') "
        "OR NEW.creator_attribution_basis NOT IN "
        "('message_sender', 'explicit_assignment', 'unknown') "
        "OR (NEW.creator_attribution_confidence IS NOT NULL AND "
        "(NEW.creator_attribution_confidence < 0 OR "
        "NEW.creator_attribution_confidence > 1)) "
        "OR (NEW.created_by_open_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE open_id = NEW.created_by_open_id)) "
        "OR (NEW.reviewed_by_open_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE open_id = NEW.reviewed_by_open_id)) "
        "OR (NEW.last_completed_by_open_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE open_id = NEW.last_completed_by_open_id)) "
        "OR NEW.review_status NOT IN "
        "('none', 'pending', 'accepted', 'rework_required') "
        "OR NEW.completion_cycle < 0 "
        "BEGIN SELECT RAISE(ABORT, 'invalid task provenance fields'); END"
    )
    op.execute(
        "CREATE TRIGGER ck_tasks_provenance_update "
        "BEFORE UPDATE OF created_via, creator_attribution_basis, "
        "creator_attribution_confidence, review_status, completion_cycle ON tasks "
        "WHEN NEW.created_via NOT IN ('detected', 'management', 'system', 'unknown') "
        "OR NEW.creator_attribution_basis NOT IN "
        "('message_sender', 'explicit_assignment', 'unknown') "
        "OR (NEW.creator_attribution_confidence IS NOT NULL AND "
        "(NEW.creator_attribution_confidence < 0 OR "
        "NEW.creator_attribution_confidence > 1)) "
        "OR (NEW.created_by_open_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE open_id = NEW.created_by_open_id)) "
        "OR (NEW.reviewed_by_open_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE open_id = NEW.reviewed_by_open_id)) "
        "OR (NEW.last_completed_by_open_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE open_id = NEW.last_completed_by_open_id)) "
        "OR NEW.review_status NOT IN "
        "('none', 'pending', 'accepted', 'rework_required') "
        "OR NEW.completion_cycle < 0 "
        "BEGIN SELECT RAISE(ABORT, 'invalid task provenance fields'); END"
    )

    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.add_column(sa.Column("actor_name_snapshot", sa.String(length=255)))
        batch.add_column(sa.Column("source_message_id", sa.String(length=128)))
        batch.add_column(sa.Column("correlation_id", sa.String(length=128)))
        batch.add_column(sa.Column("idempotency_key", sa.String(length=128)))
        batch.add_column(sa.Column("from_review_status", sa.String(length=20)))
        batch.add_column(sa.Column("to_review_status", sa.String(length=20)))
        batch.add_column(sa.Column("reason", sa.Text()))
        batch.add_column(sa.Column("completion_cycle", sa.Integer()))
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.create_unique_constraint(
            "uq_task_lifecycle_events_idempotency", ["idempotency_key"]
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action", _LIFECYCLE_ACTIONS
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome", _LIFECYCLE_OUTCOME
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_from_review_status",
            "from_review_status IS NULL OR from_review_status IN "
            "('none', 'pending', 'accepted', 'rework_required')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_to_review_status",
            "to_review_status IS NULL OR to_review_status IN "
            "('none', 'pending', 'accepted', 'rework_required')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_completion_cycle",
            "completion_cycle IS NULL OR completion_cycle >= 0",
        )
        batch.create_index(
            "ix_task_lifecycle_events_correlation", ["correlation_id"]
        )

    op.create_table(
        "task_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("author_open_id", sa.String(length=128), nullable=False),
        sa.Column("author_name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("note_type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
        sa.Column("completion_cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "note_type IN ('progress', 'completion', 'delay', 'reopen', "
            "'general', 'correction')",
            name="ck_task_notes_note_type",
        ),
        sa.CheckConstraint(
            "length(trim(content)) > 0",
            name="ck_task_notes_content_nonempty",
        ),
        sa.CheckConstraint(
            "completion_cycle >= 0", name="ck_task_notes_completion_cycle"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["author_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_task_notes_idempotency"),
    )
    op.create_index(
        "ix_task_notes_task_created", "task_notes", ["task_id", "created_at"]
    )
    op.create_index(
        "ix_task_notes_chat_created", "task_notes", ["chat_id", "created_at"]
    )
    op.create_index(
        "ix_task_notes_author_created",
        "task_notes",
        ["author_open_id", "created_at"],
    )

    op.create_table(
        "task_completion_submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column(
            "submitted_by_open_id", sa.String(length=128), nullable=False
        ),
        sa.Column(
            "submitted_by_name_snapshot", sa.String(length=255), nullable=False
        ),
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
        sa.Column("content_snapshot", sa.Text(), nullable=False),
        sa.Column(
            "evidence_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewed_by_open_id", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "cycle >= 1", name="ck_task_completion_submissions_cycle"
        ),
        sa.CheckConstraint(
            "length(trim(content_snapshot)) > 0",
            name="ck_task_completion_submissions_content_nonempty",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'accepted', 'rework_required')",
            name="ck_task_completion_submissions_review_status",
        ),
        sa.CheckConstraint(
            "evidence_json IS NOT NULL",
            name="ck_task_completion_submissions_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.chat_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_open_id"], ["users.open_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "cycle", name="uq_task_completion_submissions_cycle"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_task_completion_submissions_idempotency",
        ),
    )
    op.create_index(
        "ix_task_completion_submissions_task_cycle",
        "task_completion_submissions",
        ["task_id", "cycle"],
    )
    op.create_index(
        "ix_task_completion_submissions_chat_submitted",
        "task_completion_submissions",
        ["chat_id", "submitted_at"],
    )
    op.execute("PRAGMA optimize")


def downgrade() -> None:
    # New actions cannot be represented by the previous lifecycle check.
    op.execute(
        "DELETE FROM task_lifecycle_evidence WHERE event_id IN "
        "(SELECT id FROM task_lifecycle_events "
        "WHERE action IN ('accept', 'reopen'))"
    )
    op.execute(
        "DELETE FROM task_lifecycle_events "
        "WHERE action IN ('accept', 'reopen')"
    )
    op.drop_index(
        "ix_task_completion_submissions_chat_submitted",
        table_name="task_completion_submissions",
    )
    op.drop_index(
        "ix_task_completion_submissions_task_cycle",
        table_name="task_completion_submissions",
    )
    op.drop_table("task_completion_submissions")
    op.drop_index("ix_task_notes_author_created", table_name="task_notes")
    op.drop_index("ix_task_notes_chat_created", table_name="task_notes")
    op.drop_index("ix_task_notes_task_created", table_name="task_notes")
    op.drop_table("task_notes")

    with op.batch_alter_table("task_lifecycle_events") as batch:
        batch.drop_index("ix_task_lifecycle_events_correlation")
        batch.drop_constraint(
            "ck_task_lifecycle_events_completion_cycle", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_to_review_status", type_="check"
        )
        batch.drop_constraint(
            "ck_task_lifecycle_events_from_review_status", type_="check"
        )
        batch.drop_constraint("ck_task_lifecycle_events_outcome", type_="check")
        batch.drop_constraint("ck_task_lifecycle_events_action", type_="check")
        batch.drop_constraint(
            "uq_task_lifecycle_events_idempotency", type_="unique"
        )
        batch.drop_column("completion_cycle")
        batch.drop_column("reason")
        batch.drop_column("to_review_status")
        batch.drop_column("from_review_status")
        batch.drop_column("idempotency_key")
        batch.drop_column("correlation_id")
        batch.drop_column("source_message_id")
        batch.drop_column("actor_name_snapshot")
        batch.create_check_constraint(
            "ck_task_lifecycle_events_action",
            "action IN ('confirm', 'complete', 'reschedule', 'cancel', 'rename', "
            "'reassign', 'invalidate', 'restore', 'merge')",
        )
        batch.create_check_constraint(
            "ck_task_lifecycle_events_outcome",
            "(action = 'confirm' AND previous_status = 'pending' "
            "AND new_status = 'todo') OR "
            "(action = 'complete' AND new_status = 'done') OR "
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
            "AND new_status = previous_status)",
        )

    op.execute("DROP TRIGGER IF EXISTS ck_tasks_provenance_update")
    op.execute("DROP TRIGGER IF EXISTS ck_tasks_provenance_insert")
    op.drop_index("ix_tasks_chat_review_status", table_name="tasks")
    op.drop_index("ix_tasks_creator_created", table_name="tasks")
    # SQLite's native DROP COLUMN preserves the child tables that reference
    # tasks; each column is nullable or has already been cleared above.
    for column in (
        "last_completed_at",
        "last_completed_by_name",
        "last_completed_by_open_id",
        "completion_cycle",
        "reviewed_at",
        "reviewed_by_name",
        "reviewed_by_open_id",
        "review_status",
        "creator_attribution_confidence",
        "creator_attribution_basis",
        "created_via",
        "created_by_name",
        "created_by_open_id",
    ):
        op.drop_column("tasks", column)
