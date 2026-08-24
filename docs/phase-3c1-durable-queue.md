# Phase 3C-1: durable detection queue

## Scope

This subphase creates the persistence boundary needed before automatic model
calls. It does not yet enqueue new Feishu messages, run a background Worker,
create task records, or send group messages.

## Tables

### `detection_jobs`

One row represents one chat-scoped trigger message. The unique constraint on
`(chat_id, trigger_message_id)` makes repeated event delivery and repeated
enqueue attempts idempotent.

The job states are:

```text
queued -> running -> completed
   ^          |
   |          +-> queued (retry available later)
   |          +-> dead   (attempts exhausted)
   +----------+          (expired lease recovery)
```

Important fields include `available_at`, `priority`, `attempt_count`,
`max_attempts`, `worker_id`, and `lease_expires_at`. Only a human text message
already stored in the same chat can become a trigger.

### `detection_runs`

Each claimed attempt has one immutable attempt number. A run records:

- provider, model, and final response format;
- ordered context message IDs and a SHA-256 context fingerprint;
- request ID, token usage, and latency;
- the locally validated result JSON on success;
- a bounded, classified error code and message on failure.

The run never duplicates raw group messages. Its message IDs point back to the
existing chat-isolated message store.

## Lease and retry rules

- Claiming uses a conditional `UPDATE ... RETURNING`, so two workers cannot both
  acquire the same queued job.
- The default lease is 300 seconds; a live worker can extend it with a heartbeat.
- A transition is accepted only from the worker that owns the current attempt
  and only while its lease is active.
- A failed attempt returns to `queued` with an explicit retry delay.
- When `max_attempts` is reached, the job becomes `dead`.
- A crashed worker's expired running attempt is marked failed and the job is
  either reclaimed or moved to `dead`.

Completion and its run metadata are committed in the same SQLite transaction.
This prevents a successful run from being recorded without also completing its
job.

## Acceptance

Phase 3C-1 is accepted when:

1. migration `20260822_0004` upgrades and downgrades temporary databases;
2. duplicate enqueue returns the existing job;
3. scheduled jobs are invisible before `available_at`;
4. concurrent claimers produce at most one lease for one job;
5. expired leases are recoverable;
6. retries stop at `max_attempts`;
7. cross-chat context message IDs are rejected;
8. successful and failed attempt metadata survives a database restart;
9. all prior-phase tests continue to pass.
