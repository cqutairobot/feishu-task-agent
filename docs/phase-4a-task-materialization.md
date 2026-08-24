# Phase 4A: transactional task materialization

## Scope

Phase 4A establishes the durable task records needed by later lifecycle and
reminder phases. It does not automatically convert Worker output, change task
state from chat messages, or send reminders.

An operator can convert one exact successful `detection_runs.id`:

```bash
python -m app task-materialize --run-id 7
```

The existing real run 1 is intentionally not materialized during this phase.

## Tables

Migration `20260822_0006` adds four tables:

- `tasks`: the chat-scoped task, primary responsible Open ID compatibility
  snapshot, title,
  description, deadline, state, and strongest observed confidence;
- `task_evidence`: links a task to the exact stored Feishu messages supporting
  it;
- `task_sources`: links each model candidate index to its resulting task and
  detection run;
- `detection_materializations`: records that a whole run, including an empty
  candidate batch, was converted successfully.

Phase 6D-1 later adds `task_assignees` as the canonical ordered responsible
member set and backfills the original owner as position zero.

Task states are `pending`, `todo`, `done`, `cancelled`, and `overdue`. Phase 4A
creates only `pending` and `todo`. Candidates at or above
`TASK_AUTO_TODO_CONFIDENCE` become `todo`; the default threshold is `0.85`.
When a later matching source raises a pending task above the threshold, it is
promoted to `todo`. Terminal states are never reopened by materialization.

## Validation and transaction boundary

Before writing any task, the materializer checks all of the following:

1. the requested run exists and has status `succeeded`;
2. its queue job has status `completed`;
3. the stored context message ID list is non-empty, unique, belongs to the job
   chat, and ends at the job trigger;
4. every candidate owner has sent a stored message in that same chat;
5. the stored candidate JSON still satisfies the strict Phase 3C-3A contract;
6. every evidence ID belongs to the run's original context.

The historical owner name stored in the successful result is used for this
validation and the task snapshot. A later alias rename therefore does not make
an older valid run impossible to materialize. The Open ID remains the stable
identity used for a future Feishu mention.

All candidates, evidence links, source links, and the run-level audit are
written in one database transaction. If any candidate fails, none of them are
created. SQLite materializations take an immediate write transaction so two
local processes cannot make the same read-before-write deduplication decision
concurrently.

## Replay and cross-run deduplication

Repeating materialization for the same run returns the original task IDs and
counts without creating new rows. Empty candidate batches are also audited, so
they are replay-safe.

A candidate from a different run reuses an existing task only when all of these
values match:

- `chat_id`;
- exact responsible Open-ID set;
- Unicode- and whitespace-normalized, case-folded title;
- exact deadline, including both being absent; and
- at least one evidence message.

Requiring evidence overlap is the semantic guardrail. The same person can have
the same recurring task title and no deadline multiple times; separate evidence
creates separate tasks. A different deadline also creates a separate task in
Phase 4A, leaving explicit deadline-change reconciliation for the lifecycle
agent phase.

Tasks never merge across chats, even when owner, title, and deadline are equal.
Future group task queries will filter by `tasks.chat_id`.

## Acceptance

Automated coverage verifies:

- high- and low-confidence state selection;
- whole-run atomicity for invalid candidates;
- same-run replay, including an empty batch;
- evidence-aware reuse across runs;
- periodic same-title task separation;
- different-deadline separation;
- strict cross-chat isolation;
- pending-to-todo promotion;
- historical owner-name handling after an alias rename;
- rejection of owners never observed in the job chat;
- rejection of failed runs; and
- migration downgrade preservation of pre-Phase-4 messages and run audits.

The real database was backed up to
`data/feishu_task_agent.db.pre-phase4a-20260822`, upgraded from `0005` to
`0006`, and checked afterward. It retains 23 messages, 8 jobs, and the one
successful 6,790-token detection run. All four new tables contain zero rows,
run 1 has no materialization audit, the foreign-key check is empty, and SQLite
reports `ok` for its integrity check.

No listener or Worker is left running after acceptance.
