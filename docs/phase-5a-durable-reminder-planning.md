# Phase 5A: durable reminder planning

## Outcome

Phase 5A creates a persistent reminder plan but deliberately sends no Feishu
messages. It separates deadline calculation and audit correctness from the
external delivery behavior that will be added in Phase 5B.

Each reminder row belongs to one task and one exact deadline snapshot. The
unique key is:

```text
(task_id, reminder kind, deadline_snapshot)
```

This makes repeated synchronization idempotent and lets a deadline extension
keep the old plan as cancelled audit rows while creating a new plan for the new
deadline.

## Schedule policy

For a confirmed `todo` task with a deadline, the four stages are:

1. `due_72h`: deadline minus 72 hours;
2. `due_24h`: deadline minus 24 hours;
3. `due_today`: 09:00 in `Asia/Shanghai` on the deadline date, capped at the
   deadline so it can never be scheduled afterward; and
4. `overdue`: deadline plus one minute.

The due-day hour, overdue grace, and future delivery retry limit are configured
by `REMINDER_DUE_DAY_HOUR`, `REMINDER_OVERDUE_GRACE_MINUTES`, and
`REMINDER_MAX_ATTEMPTS`.

The planner applies these lifecycle rules:

- `pending`: no automatic reminders because the detection is not confirmed;
- `todo` with deadline: full four-stage plan;
- `todo` whose deadline has passed: atomically becomes `overdue` and keeps only
  the overdue stage;
- `overdue`: only the overdue stage remains active;
- `done`, `cancelled`, or no deadline: every unsent stage is cancelled;
- deadline change: old unsent stages are cancelled as
  `task_deadline_changed`, then four new deadline-versioned rows are created;
- a sent row is immutable audit history and is never cancelled retroactively;
  and
- changing the schedule policy updates unsent rows in place instead of creating
  duplicates.

## Durable table

Migration `20260823_0008` adds `task_reminders` with schedule and deadline
snapshots, state, retry counters, lease fields, delivery audit fields, error
fields, and cancellation audit fields. Phase 5A uses `scheduled` and
`cancelled`; the predeclared `leased`, `sent`, and `dead` states support the
crash-safe delivery Worker in Phase 5B.

New task materialization calls the reminder planner within the same transaction
that completes the detection run and writes the task. A failure therefore
cannot leave a task without its required plan or leave reminder rows for a
rolled-back task.

## Commands

```bash
python -m app reminder sync
python -m app reminder sync --task-id 1
python -m app reminder list --task-id 1
```

`sync` is the backfill and lifecycle reconciliation operation. `list` prints all
active and historical reminder rows for one task in Shanghai time. Neither
command connects to Feishu or sends a message.

## Verification

The full offline suite passes 213 tests. Coverage includes:

- exact four-stage Shanghai-time calculations;
- early deadlines that occur before the configured due-day hour;
- repeat synchronization without duplicate rows;
- low-confidence pending suppression;
- terminal-state cancellation while preserving sent audit rows;
- deadline change cancellation and new-version planning;
- overdue status transition and obsolete-stage suppression;
- schedule-policy changes without duplicate rows;
- materialization-time planning and transaction rollback;
- migration constraints and downgrade preservation; and
- CLI routing and Shanghai-time JSON output.

Before migrating the real database, a recoverable backup was created at
`data/feishu_task_agent.db.pre-phase5a-20260823`. The real database upgraded
from `20260822_0007` to `20260823_0008` and Task 1 received:

- `due_72h`: 2026-08-27 18:00 +08:00;
- `due_24h`: 2026-08-29 18:00 +08:00;
- `due_today`: 2026-08-30 09:00 +08:00; and
- `overdue`: 2026-08-30 18:01 +08:00.

A second synchronization created and cancelled zero rows, leaving four active
scheduled reminders with zero attempts. Foreign-key and integrity checks pass.
No listener, detection Worker, or reminder delivery Worker is running.
