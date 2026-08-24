# Phase 3C-3C: live targeted Worker acceptance

## Live input

On 2026-08-22, two ordinary group messages assigned separate work to the
verified names 王政 and 王哈. They arrived approximately nine seconds apart:

- 王政: prepare the frontend acceptance checklist by 2026-08-27 18:00;
- 王哈: summarize API test results by 2026-08-28 18:00.

The first message created job 8. The second message arrived inside the
twenty-second sliding window, reused job 8, and moved its trigger to the second
message.

## Targeted execution

Only the new job was executed:

```bash
python -m app worker --once --job-id 8
```

The Worker completed attempt 1 with JSON Schema output and two validated
candidates. Each candidate had:

- the correct verified name and corresponding Open ID;
- a separate title and description;
- the expected `+08:00` deadline; and
- only its own assignment message as evidence.

This confirms that the twenty-second batch does not merge two semantic tasks.

## Audit and isolation checks

- job 8 transitioned to `completed` with one successful run;
- the run stored a 64-character context fingerprint, ordered message IDs,
  provider request ID, token usage, latency, and validated candidate JSON;
- jobs 1 through 7 remained `queued` with `attempt_count=0`;
- no `tasks` rows or reminder messages were created; and
- SQLite `PRAGMA integrity_check` returned `ok`.

The listener was stopped after acceptance. The Feishu WebSocket SDK emits noisy
pending-task messages when interrupted on Python 3.14, but the listener command
still exits successfully and the database is already committed safely.

## Remaining boundary

Phase 3C now has a verified single-job execution path. Continuous Worker polling
is still deliberately disabled. Before enabling unattended execution, the old
acceptance jobs need an explicit retention decision, and Worker shutdown/polling
behavior should be tested independently of the listener process.
