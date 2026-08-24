# Phase 6B-3: private lifecycle commands

## Interaction

An ordinary member can privately send one natural-language operation anchored
by one public task code:

```text
1A 已完成
把 T-1A 延期到下周三下午 6 点
取消 1A
```

The full `T-` form is always accepted. The shorthand form is accepted when its
checksum character is alphabetic. A message without a valid task code is not a
lifecycle command. One message currently modifies at most one task.

## Pre-model boundary

Before any private context is sent to the model, the program:

- validates the task-code checksum;
- resolves exactly one actionable `todo` or `overdue` group task;
- restricts an ordinary sender to their own Open ID;
- allows a configured administrator to select another member's task;
- applies the configured group allowlist; and
- supplies only the selected task to the model, with its source group identity.

This prevents the model from seeing or selecting unrelated tasks. The model
receives recent human messages from the same P2P conversation, the actor Open
ID, and that single pre-authorized task. Questions, vague progress, and results
that disagree with the task code do not write anything.

## Write and reply

A valid model candidate enters `LifecycleMutationService`, which rechecks the
sender, evidence, task code, permissions, state, confidence, and deadline inside
the transaction. On success, the bot privately replies with the task code,
title, source group, resulting status, and the new deadline when applicable.
Provider, model, structured-output mode, request ID, and token counts are saved
in the lifecycle event; credentials are never stored there.

## Feature gate and read-only probe

Live writes are independently disabled by default:

```dotenv
LIFECYCLE_PRIVATE_WRITES_ENABLED=false
LIFECYCLE_PRIVATE_CONTEXT_LIMIT=20
LIFECYCLE_MUTATION_MIN_CONFIDENCE=0.90
```

Before enabling writes, one stored owner-authored P2P message can be tested with:

```bash
python -m app private-lifecycle-detect \
  --message-id om_xxx \
  --task-code T-1A \
  --limit 20
```

The command prints `read_only=true` diagnostics and never calls the mutation
service.

## Verification status

All 284 offline tests pass. Migration `20260823_0011` was applied after creating
`data/feishu_task_agent.db.pre-phase6b3-20260823`. Database integrity is `ok`;
Task 1 remains `todo`, all four reminders remain `scheduled` with zero attempts,
and the lifecycle event count remains zero. The listener has been restarted
with the private-write gate still disabled.

The authorized real P2P probe used the compact message `1A已完成` (without a
space). The configured `qwen3.7-plus` model returned `complete` for Task 1 with
confidence `0.95` and cited only the exact trigger message. Diagnostics reported
`scope=private_owner_task` and `read_only=true`. A post-probe check confirmed
database integrity `ok`, Task 1 still `todo`, all four reminders still
`scheduled` with zero attempts, zero lifecycle audit/evidence rows, and no new
task-detection job for the private message.

After separate write authorization, the gate was enabled and the same compact
command was sent again as a new Feishu message. The complete live path succeeded:

- the model selected Task 1 with `complete` and confidence `0.95`;
- Task 1 transitioned from `todo` to `done`;
- all four unsent reminders became `cancelled` with reason `task_done` and zero
  delivery attempts;
- lifecycle event 1 recorded owner authorization, `T-1A`, before/after state,
  the unchanged deadline, `qwen3.7-plus`, `json_schema`, request ID, and token
  usage;
- the evidence table references only the exact P2P trigger message; and
- no ordinary task-detection job was created.

Feishu redelivered the same event once after success. Message ingestion marked
the second delivery `duplicate`, so neither the model nor the mutation ran a
second time. Final database integrity remained `ok`. The authorized private
lifecycle feature remains enabled for future actionable tasks.
