# Phase 6A: read-only lifecycle detection

## Safety boundary

Phase 6A recognizes explicit updates to existing tasks but does not modify any
task, deadline, status, reminder, or audit row. Database mutation is deferred to
Phase 6B, after the matching contract has passed both offline and real-chat
acceptance.

The model receives a same-chat conversation window plus at most 50 actionable
tasks from that exact chat. Only `todo` and `overdue` tasks are eligible;
low-confidence `pending`, terminal `done` and `cancelled`, and every task from a
different chat are absent from the choice set.

## Strict output

```json
{
  "updates": [
    {
      "action": "complete",
      "confidence": 0.97,
      "task_id": 1,
      "new_deadline": null,
      "evidence_message_ids": ["om_xxx"]
    }
  ]
}
```

Allowed actions are:

- `complete`: an explicit statement that the task is finished;
- `reschedule`: an explicit new deadline later than the conversation reference
  time; and
- `cancel`: an explicit statement that the task is no longer required.

Questions such as “完成了吗”, progress statements such as “快完成了”, and vague
phrases such as “先别急” produce no update. A reschedule without a concrete new
date produces no update. If multiple similar tasks cannot be uniquely matched,
the output must also be empty.

Local validation rejects invented or cross-chat task IDs, evidence outside the
conversation, evidence that omits the trigger/focus message, unsupported
actions, duplicate updates to the same task, non-finite confidence, naive or
past reschedule timestamps, and extra JSON fields.

## Read-only command

```bash
python -m app lifecycle-detect \
  --chat-id oc_xxx \
  --message-id om_xxx \
  --limit 30
```

Standard output contains only the structured update candidates. Model and token
diagnostics go to standard error and explicitly report `read_only=true`.

## Verification status

The offline suite passed 253 tests. A real same-chat completion statement for
Task 1 was processed by this command only. The model selected Task 1 with action
`complete`, confidence `0.98`, and the exact focus message as evidence. A
post-run database check confirmed Task 1 remained `todo` and all four reminder
rows remained `scheduled` with zero attempts. Mutation and authorization policy
remain Phase 6B work.
