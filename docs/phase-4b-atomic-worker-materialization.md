# Phase 4B: atomic Worker materialization

## Outcome

New successful Worker runs now create formal tasks automatically. The following
changes occur in one database transaction:

1. the active `detection_runs` row becomes `succeeded` and stores the validated
   candidate JSON and token audit;
2. its `detection_jobs` row becomes `completed`;
3. candidates are created or deterministically reused as `tasks`;
4. evidence and source links are written; and
5. `detection_materializations` records the whole-run conversion.

If task materialization fails after making partial writes, the transaction rolls
all of them back. The same run remains active long enough to be audited as
`task_materialization_error`, and the job follows the existing bounded retry or
dead-letter policy. A process crash before commit likewise cannot leave a
successful run without its task records; the lease-expiry recovery path handles
the still-running attempt.

## Clear assignments do not require a reply

A message such as:

```text
王政，请在8月30日18:00前完成 Phase 4B 自动物化验收记录。
```

already contains the verified owner, work item, and deadline. It forms a task
without “收到”, “好的”, or another acknowledgement from 王政. A reply may add
context but is not a creation prerequisite. Detection instructions also require
the model to exclude pure acknowledgements from evidence when the assignment
message is sufficient.

An offline end-to-end Worker regression supplies only the assignment in its
context and focus set. It verifies that one task is created for the bound 王政
Open ID with only that assignment message as evidence.

## Batch focus versus semantic grouping

The twenty-second debounce window remains a collection optimization, not a
decision that all nearby messages describe one task. The model still receives a
larger chat-only context to resolve owners, pronouns, work, and relative dates.

Phase 4B adds `focus_message_ids` to context version `1.1`. These are the
messages whose database `received_at` is at or after the claimed job's stable
`created_at`, restricted to the selected context window. Local contract
validation rejects every candidate whose evidence does not intersect that focus
set. Older context can explain a new message but cannot independently produce a
new candidate.

Migration `20260822_0007` adds
`detection_run_focus_messages`, an ordered link from each new run to the exact
stored messages in its focus set. It is a child table, so the migration does not
rebuild or mutate historical `detection_runs`. Existing run 1 intentionally has
no focus rows and is not automatically backfilled or materialized.

## Worker output

Successful `worker --once` output now includes:

```json
{
  "status": "completed",
  "job_id": 9,
  "run_id": 2,
  "attempt": 1,
  "candidate_count": 1,
  "created_task_count": 1,
  "reused_task_count": 0,
  "task_ids": [1],
  "error_code": null,
  "retry_at": null
}
```

The same fields are emitted by continuous mode for each processed job.

## Controlled live acceptance

Feishu delivered three messages into job 9 after a WebSocket reconnect: the
original explicit assignment, 王政's acknowledgement, and the repeated explicit
assignment. Run 2 processed only job 9 and produced one candidate. The atomic
completion created Task 1 with:

- owner `王政`, linked to the verified group Open ID;
- title `完成 Phase 4B 自动物化验收记录`;
- deadline `2026-08-30T18:00:00+08:00`;
- status `todo` at confidence `0.95`; and
- source run 2, candidate index 0.

The live model included all three focus messages as evidence. This confirms the
online automatic materialization path, but the stronger “no acknowledgement
required” condition is established by the single-message offline regression.
The prompt was tightened afterward so future clear assignments should omit pure
acknowledgements from their evidence.

After acceptance, run 1 remains succeeded with its original 6,790-token audit
and has neither a materialization record nor task sources. SQLite foreign-key
and integrity checks pass. No listener or Worker is left running.
