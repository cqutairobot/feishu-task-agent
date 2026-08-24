# Phase 6B-2: atomic lifecycle writes

## Scope

This phase supplies the trusted write boundary between read-only LLM output and
the task database. It does not call the model and is not connected to the live
Feishu receiver. Real messages therefore remain read-only until the next
integration phase.

## Authorization and grounding

Before changing a task, the service independently verifies all of the following:

- the candidate is a strict `LifecycleCandidate` with confidence at least
  `0.90`;
- the optional public task code has a valid checksum and resolves to the same
  internal task ID;
- the trigger is one unique, stored, human-authored text message and its sender
  Open ID equals the claimed actor;
- every evidence message exists in the same tenant and conversation, occurs no
  later than the trigger, and the evidence includes the trigger;
- an ordinary actor owns the task, or the actor is a configured administrator;
- the task belongs to the configured chat allowlist; and
- a group trigger belongs to the task's exact source group. A P2P trigger may
  address an authorized task from another configured group.

Only `todo` and `overdue` are actionable. `pending`, `done`, and `cancelled`
cannot be silently promoted or reopened by this service.

## Transitions

| Action | Required input | Result |
|---|---|---|
| `complete` | no new deadline | status `done`, `completed_at` set |
| `cancel` | no new deadline | status `cancelled`, `cancelled_at` set |
| `reschedule` | changed, timezone-aware future deadline | status `todo`, new deadline |

Completion and cancellation cancel all unsent reminder rows. Rescheduling
cancels reminders tied to the old deadline and creates the four deterministic
stages for the new deadline. Sent reminders remain immutable audit history.

## Atomic audit

Migration `20260823_0010` adds:

- `task_lifecycle_events`, containing actor, authorization role, trigger,
  action, task-code snapshot, before/after state, before/after deadline,
  confidence, and application time; and
- `task_lifecycle_evidence`, containing the ordered, foreign-keyed message set.

The task mutation, reminder synchronization, lifecycle event, and evidence rows
commit in one SQLite transaction protected with `BEGIN IMMEDIATE`. Any failure,
including a reminder-planning failure, rolls back all four parts.

The unique `(task_id, trigger_message_db_id)` key makes exact replay
idempotent. Reusing the same trigger for different candidate content is rejected
as a conflicting replay.

Migration `20260823_0011` extends successful events with the model provider,
model name, response format, request ID, and prompt/completion/total token
counts. API credentials are not stored.

## Verification

All 269 offline tests pass. Coverage includes owner and administrator paths,
private cross-group targeting, group cross-chat rejection, allowlist and task
code checks, low-confidence and non-actionable-state rejection, evidence
isolation, completion/cancellation reminder cleanup, overdue rescheduling,
idempotent replay, conflicting replay, and injected transactional rollback.

Before migrating the real database, a recoverable SQLite backup was created at
`data/feishu_task_agent.db.pre-phase6b2-20260823`. After migration, integrity
was `ok`, Task 1 remained `todo`, its four reminders remained `scheduled` with
zero attempts, and both lifecycle audit tables contained zero rows.
