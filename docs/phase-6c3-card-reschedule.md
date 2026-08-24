# Phase 6C-3: card deadline rescheduling

## Scope

Actionable `todo` and `overdue` tasks in a private task-list card now include a
Feishu `picker_datetime` control in addition to the existing completion and
cancellation buttons. The picker starts at the current Shanghai deadline when
one exists, shows a confirmation dialog, and submits the selected instant only
through the signed `card.action.trigger` callback.

The interaction is deterministic and does not call an LLM. Natural-language
private commands such as `T-1A 延期到下周三` continue to use the separately
authorized lifecycle model path.

## Callback validation

The callback processor accepts `reschedule` only when all of the following are
true:

- the callback component tag is exactly `picker_datetime`;
- the action value contains only the existing versioned command, task code,
  and action fields;
- the picker option is a valid datetime carrying an offset, or a valid naive
  datetime accompanied by a recognized callback timezone;
- the signed operator is the task owner or a configured administrator;
- the task belongs to a configured group and is still `todo` or `overdue`; and
- the selected instant is in the future and differs from the stored deadline.

Malformed, stale, unauthorized, same-deadline, and past-deadline callbacks do
not write. They receive the same generic warning and fresh actor-scoped card as
other rejected card actions.

## Atomic lifecycle and reminders

No database migration is required. Migration `20260823_0012` already allows
`reschedule` card-action events and stores the before/after deadline in the
unified lifecycle audit.

Within one SQLite transaction, a successful picker callback:

1. changes an overdue task back to `todo` when necessary;
2. writes the new UTC-normalized deadline;
3. records the signed actor, callback/card/chat IDs, authorization role, old
   and new deadlines, and model-free confidence `1.0` audit;
4. cancels unsent reminders for the old deadline with
   `task_deadline_changed`; and
5. creates the four deadline-versioned reminder stages for the new deadline.

Sent reminders remain immutable audit history. Callback replay is idempotent
only when the task, actor, action, card, chat, and requested deadline all match
the original event.

## Verification

All 318 offline tests pass, covering picker rendering, SDK callback
normalization, offset and named-timezone parsing, malformed values, permission
and state rechecks, changed/future deadline enforcement, atomic rollback,
deadline-bound callback idempotency, lifecycle audit, and reminder replanning.

The live database was backed up to
`data/feishu_task_agent.db.pre-phase6c3-20260823`. Both source and backup pass
SQLite integrity checks, and the schema remains at `20260823_0012 (head)`.

## Live acceptance

The real group assignment created Task 4 (`T-4V`) for 王政 with initial
deadline `2026-09-02 18:00 Asia/Shanghai` and four scheduled reminders. A
configured task administrator then requested a fresh private card and used the
date-time picker to select `2026-09-04 20:00`.

The listener received `task_card_action: success` and Feishu accepted the
actor-scoped replacement card. Task 4 remained `todo` with the new deadline.
All four zero-attempt reminders tied to the old deadline were cancelled with
`task_deadline_changed`; four new reminders were scheduled for the new
deadline. Lifecycle event 4 records `reschedule`, authorization role
`administrator`, the exact old/new deadlines and callback/card/chat IDs,
`trigger_source=card_action`, confidence `1.0`, null model metadata, and zero
message-evidence rows. Both private `任务列表` messages created zero detection
jobs, and final database integrity is `ok`. Phase 6C-3 is complete.
