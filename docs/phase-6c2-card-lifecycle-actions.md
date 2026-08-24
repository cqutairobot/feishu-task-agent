# Phase 6C-2: card lifecycle actions

## Scope

Actionable `todo` and `overdue` tasks in private task-list cards now carry two
buttons:

- `完成`; and
- `取消任务`, with a Feishu confirmation dialog.

`pending` tasks remain read-only. Deadline selection is intentionally deferred
to Phase 6C-3 because it introduces form values and timezone validation.

## Trust boundary

Button values contain only a command version, action name, and checksum-backed
public task code. They contain no trusted owner or administrator identity.

The signed `card.action.trigger` callback supplies the operator Open ID. Before
writing, the server re-parses the task code and rechecks that:

- the task exists and is still `todo` or `overdue`;
- the task belongs to a configured group;
- the operator is its owner or a configured task administrator; and
- the requested action is exactly `complete` or `cancel`.

Every callback returns a newly queried, operator-scoped replacement card. An
ordinary member therefore still sees only their own tasks; an administrator is
still restricted to configured groups. Malformed, forged, unauthorized, or
stale actions receive a generic no-change toast and a safe refreshed list.

## Atomic audit and idempotency

Migration `20260823_0012` extends the unified lifecycle audit with two honest
trigger types:

- `message`, retaining its required stored message and evidence; and
- `card_action`, retaining the callback event ID, original card message ID,
  and private chat ID without manufacturing message evidence.

The callback event ID is globally unique in the lifecycle table. Exact Feishu
redelivery returns the existing result. Reusing the same callback for a
different task, actor, action, card, or chat is rejected.

Task state, lifecycle audit, completion/cancellation timestamp, and reminder
cancellation remain one SQLite transaction. A reminder synchronization failure
rolls the entire action back.

## Rollout and verification

The independent default-off gate is:

```text
FEISHU_TASK_CARD_ACTIONS_ENABLED=false
```

It requires private card transport to be enabled. The local live environment
now has both gates enabled.

All 312 offline tests pass. The real database was backed up to
`data/feishu_task_agent.db.pre-phase6c2-20260823`, upgraded to
`20260823_0012`, and passed SQLite integrity checks. The existing message-based
Task 1 event, completed task state, and four cancelled reminders were preserved.
The listener is connected with card callback registration active. A new open
task is required for live button acceptance.

Two live tasks were then created successfully as `T-3C` and `T-2T`, and their
private administrator card displayed both buttons. The first click was stopped
by the Feishu client with “该应用尚未配置卡片交互功能”; no callback reached the
listener. Post-click verification showed both tasks still `todo`, all eight
reminders still `scheduled`, zero card-action lifecycle events, and database
integrity `ok`.

This is an application-console callback subscription requirement, not an API
permission or local-code failure. Under the application's `事件与回调` page,
`回调配置` must use long-connection delivery and subscribe to `卡片回传交互`
(`card.action.trigger`). After the platform configuration is saved (and a new
app version published if the console requests it), send a fresh private
`任务列表` before retrying the button.

After `卡片回传交互` was configured for long-connection delivery, a fresh
administrator card completed `T-3C` successfully. The listener received
`task_card_action: success`; Task 3 changed from `todo` to `done`, its four
zero-attempt reminders were cancelled with `task_done`, and the replacement
card was accepted by Feishu. Lifecycle event 2 records the administrator Open
ID, exact callback/card/chat IDs, `trigger_source=card_action`, confidence
`1.0`, null model metadata, and zero message-evidence rows. Task 2 and its four
scheduled reminders were untouched, and database integrity remained `ok`.

The task owner then requested a fresh personal card and confirmed cancellation
of `T-2T`. The listener again received `task_card_action: success`; Task 2
changed from `todo` to `cancelled`, its four zero-attempt reminders were
cancelled with `task_cancelled`, and lifecycle event 3 recorded authorization
role `owner`. Both private list commands were excluded from task detection,
both card events have no message-evidence rows or model metadata, and final
database integrity is `ok`. Phase 6C-2 is complete.
