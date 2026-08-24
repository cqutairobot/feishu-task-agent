# Phase 3C-3B: durable detection Worker

## Scope

This subphase connects the durable detection queue to the zero-to-many model
contract. It processes at most one job per explicit command. It does not yet run
continuously, create `tasks` rows, update task lifecycle state, or send messages.

For one claimed job the Worker performs:

1. atomically acquire a time-bounded lease;
2. build a rolling context that ends at the trigger and contains only that chat;
3. create a running `detection_runs` audit record;
4. call the model with the Phase 3C-3A batch contract;
5. locally validate every candidate, owner, deadline, and evidence ID;
6. atomically store the validated result and complete the job; or
7. record a classified failure and schedule exponential retry.

An empty candidate array is a successful detection result. It means the group
context did not contain an explicit task.

## Explicit safe CLI

The command requires an explicit execution mode:

```bash
python -m app worker --once
```

This claims the oldest ready job after applying queue priority. For controlled
acceptance, target one exact job instead:

```bash
python -m app worker --once --job-id 8
```

Targeted mode never falls back to another job. If job 8 does not exist, is not
yet available, is already running, or has reached a terminal state, the command
returns `idle` and makes no model request.

Successful output contains only operational metadata, not raw group messages:

```json
{
  "status": "completed",
  "job_id": 8,
  "run_id": 1,
  "attempt": 1,
  "candidate_count": 2,
  "error_code": null,
  "retry_at": null
}
```

## Lease and failure behavior

The configured lease is automatically increased when necessary to cover the
model timeout and all configured provider retries. This avoids losing ownership
during a valid long model request while keeping crash recovery bounded.

Expected model-service failures use `model_provider_error`. Context construction
failures use `context_error`. Every failed attempt gets an audit row and returns
to `queued` after exponential delay. Delay is capped at one hour. When
`max_attempts` is exhausted, the job becomes `dead`.

The relevant settings and defaults are:

```dotenv
DETECTION_WORKER_CONTEXT_LIMIT=30
DETECTION_WORKER_LEASE_SECONDS=300
DETECTION_WORKER_RETRY_BASE_SECONDS=30
```

## Safety boundaries

- Queue claims use conditional SQLite updates, so concurrent Workers cannot own
  the same attempt.
- `--job-id` allows a newly created acceptance job to be processed without
  consuming historical test jobs.
- Context construction and run creation both enforce `chat_id` isolation.
- A run stores ordered context message IDs and a fingerprint, not another copy
  of raw messages.
- Only locally validated batch JSON can complete a job.
- This phase stores candidates only inside `detection_runs.result_json`; Phase 4
  will deduplicate and materialize them into lifecycle-managed `tasks` rows.

## Verification

```bash
python -m unittest tests.test_detection_worker -v
python -m unittest tests.test_worker_cli -v
python -m unittest discover -s tests -v
```

Phase 3C-3B is accepted when success, empty result, provider failure, retry
exhaustion, targeted claim, lease recovery, and cross-chat isolation tests pass,
and the real database's existing jobs remain untouched until a deliberate live
acceptance run.
