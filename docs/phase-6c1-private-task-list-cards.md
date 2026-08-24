# Phase 6C-1: private task-list cards

## Outcome

An exact `任务列表` command sent directly to the bot now prefers a Feishu
interactive card. Group task-list commands remain compact text replies so the
group is not filled with large cards.

The card is deliberately read-only in this step. Phase 6C-2 will add lifecycle
buttons and their callback verification after the card transport itself passes
live acceptance.

## Authorization boundary

Card rendering receives the same already-authorized repository result used by
the existing text formatter:

- an ordinary member receives only tasks whose owner Open ID matches the P2P
  sender, across configured groups;
- a task administrator receives all open tasks, but only from configured
  groups; and
- only `pending`, `todo`, and `overdue` tasks are displayed.

The card builder performs no database query and no LLM call. It cannot widen
the sender's scope. Titles, member names, and group names are length-bounded and
escaped before being inserted into Feishu markdown, preventing stored chat text
from becoming a card mention or markup instruction.

## Card contents

Each displayed task includes:

- stable public code such as `T-1A`;
- title;
- source group;
- owner in administrator views;
- Shanghai deadline; and
- current open status.

The source-group name is mutable. Each private list request refreshes only the
chat metadata for the distinct groups shown on that page, without fetching the
member list. If Feishu cannot return a current name, the card omits that line
instead of rendering a stale stored name. Replacement cards returned by task
buttons follow the same behavior.

The maximum remains 20 tasks. A hidden-item count is shown when necessary. An
empty result is still returned as an explicit empty-state card. Forwarding is
disabled in the card configuration.

## Transport and rollout

`FeishuMessageReplier.reply_card` sends `msg_type=interactive` with a stable
message-derived UUID. If Feishu rejects the card or the payload cannot be
serialized, the receiver automatically sends the pre-existing text reply with
a distinct stable UUID.

The feature is default-off:

```text
FEISHU_PRIVATE_TASK_CARDS_ENABLED=false
```

It has been enabled in the local live environment for acceptance. The listener
reports both the card gate and the text fallback at startup.

## Verification status

The full offline suite passes. Coverage includes personal/admin isolation, group
text preservation, empty cards, stable codes, Shanghai deadlines, stored-text
escaping, interactive serialization, transport rejection fallback, command
routing, and all previous identity, detection, reminder, and lifecycle paths.

No schema migration or task mutation is required.

The real P2P acceptance command `任务列表` was stored without creating a task
detection job. The listener reported `task_command: list / success /
reply=card`, proving that Feishu accepted the interactive payload and that text
fallback was not used. Because Task 1 was already complete, the real reply was
the expected personal empty-state card. Phase 6C-1 is complete.
