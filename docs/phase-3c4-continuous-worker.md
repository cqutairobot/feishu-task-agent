# Phase 3C-4: controlled continuous Worker

## Scope

Phase 3C-4 adds two operational controls needed before unattended detection:

1. queued jobs can be cancelled atomically with a stored reason and timestamp;
2. the Worker can poll continuously until a clean Ctrl+C shutdown.

The Worker still stores validated candidates only in `detection_runs`. It does
not create lifecycle-managed tasks or send reminders.

## Auditable cancellation

Migration `20260822_0005` adds the terminal `cancelled` state plus
`cancelled_at` and `cancel_reason`. Only queued jobs may be cancelled. Running,
completed, and dead jobs are rejected.

Cancel one or several exact jobs in one transaction:

```bash
python -m app queue cancel \
  --job-id 1 \
  --job-id 2 \
  --reason "historical acceptance jobs"
```

If any requested ID is missing or not cancellable, none of the requested jobs
change. Repeating the same cancellation is idempotent and preserves the original
timestamp and reason.

## Continuous polling

Start an explicit long-running Worker process:

```bash
python -m app worker --forever
```

The Worker immediately processes ready jobs. When none are ready, it sleeps for
`DETECTION_WORKER_POLL_SECONDS` before polling again. Idle polls do not call the
model and do not print repetitive output.

Ctrl+C between jobs exits cleanly. If interruption occurs during a model call,
the active attempt is first audited as `worker_interrupted` and returned to the
retry queue before the process exits.

The polling setting defaults to:

```dotenv
DETECTION_WORKER_POLL_SECONDS=2
```

It accepts values from 0.1 through 60 seconds.

## Migration audit preservation

SQLite implements an altered check constraint by rebuilding `detection_jobs`.
Because `detection_runs.job_id` uses `ON DELETE CASCADE`, a naive table rebuild
can delete child audit rows. This issue was detected during real-database
acceptance when the run count unexpectedly changed from one to zero.

The migration was corrected to preserve all `detection_runs` rows before the
parent rebuild and restore them afterward. A regression test now seeds a
successful run and verifies that both `0004 -> 0005` and `0005 -> 0004` retain
the same run ID, job ID, status, token count, and result JSON.

The real database was restored from the pre-migration SQLite snapshot, migrated
again with the corrected code, and rechecked. The successful Phase 3C-3C run is
present with its original 6,790-token audit and candidate JSON.

Recovery artifacts are retained locally:

- `data/feishu_task_agent.db.pre-phase3c4-20260822`: clean pre-migration snapshot;
- `data/feishu_task_agent.db.failed-phase3c4-20260822`: captured failed state.

## Real database acceptance

After recovery and corrected migration:

- jobs 1 through 7 are `cancelled`, each with the shared cleanup reason;
- job 8 remains `completed` with attempt 1;
- run 1 remains `succeeded` with its result JSON and token usage;
- queued and running job counts are both zero;
- continuous polling starts without a model call and stops cleanly on Ctrl+C;
- SQLite integrity check returns `ok`; and
- all 155 automated tests pass.

No listener or Worker process is left running after acceptance.
