# Phase 5B: private-first reminder delivery

## Outcome

Phase 5B turns the Phase 5A schedule into a crash-recoverable outgoing queue.
For each due row, the Worker first uses the newest known P2P `chat_id` created
when that member privately messaged the bot. If no P2P conversation is known,
it addresses the member by verified Open ID. If private delivery is
definitively rejected, it sends the same reminder to the task's source group
and uses the Open ID in a real `@` tag. A group in which the bot is installed
can therefore still deliver the reminder when private chat is unavailable.

Delivery never searches by display name. The task already stores the Open ID
resolved from that chat's one-to-one verified name binding, so two users with
similar names or the same name in different groups cannot be mixed.

## State machine and recovery

```text
scheduled -> leased -> sent
                    -> scheduled (retry later)
                    -> dead (maximum attempts reached)
scheduled/leased -> cancelled (task or deadline changed)
```

Claiming uses a SQLite `BEGIN IMMEDIATE` transaction. A claim increments the
attempt counter and records the Worker ID, lease start, and lease expiry. A
second Worker cannot claim the same active row. If a Worker crashes, a later
claim recovers the expired lease; if another Worker has already recovered it,
the old Worker's ID and attempt number can no longer complete the row.

Failed attempts use exponential delays derived from
`REMINDER_WORKER_RETRY_BASE_SECONDS`, capped at one hour. After
`REMINDER_MAX_ATTEMPTS` the row becomes `dead`. Error codes and bounded error
messages remain on the audit row.

The Feishu request UUID is deterministic for the reminder, deadline, and
delivery channel. If Feishu accepts a request but the local process stops before
recording it, a retry reuses that UUID instead of intentionally creating a new
message. Results that do not prove non-delivery (`230049`, `230101`, or a
transport exception) are not immediately copied into the group: the Worker
first retries the private channel with the same UUID and permits group fallback
only on the final configured attempt.

## Delivery and audit policy

Private success through an established P2P chat records:

```text
status = sent
delivery_receive_id_type = chat_id
delivery_receive_id = the owner's P2P chat_id
feishu_message_id = returned message ID
```

If no known P2P chat exists and Open-ID delivery succeeds, the receive type is
`open_id` and the receive ID is the reminder row's responsible-member Open ID.

Private rejection followed by group success records:

```text
status = sent
delivery_receive_id_type = chat_id
delivery_receive_id = task.chat_id
feishu_message_id = returned group message ID
last_error_code/message = private failure details
```

The group message contains `<at user_id="OWNER_OPEN_ID">OWNER_NAME</at>`.
The name is only presentation text; Feishu targets the Open ID.

For a shared task, each responsible member has a separate durable row for each
stage. Private delivery, retry, deduplication, and any final group fallback are
therefore independently audited per recipient.

When the process has been offline long enough for multiple stages of the same
task and deadline to be due, it cancels the older stages as
`superseded_by_<kind>` and sends only the most urgent stage in this order:

```text
overdue > due_today > due_24h > due_72h
```

This avoids sending several stale reminders immediately after recovery.

## Commands and configuration

Run one due attempt:

```bash
python -m app reminder-worker --once
```

Target one exact row without taking another ready row:

```bash
python -m app reminder-worker --once --reminder-id 1
```

Run continuously in a process separate from the Feishu listener and detection
Worker:

```bash
python -m app reminder-worker --forever
```

Send a delivery-only probe for one task. The probe has an independent UUID and
does not claim, send, cancel, or increment any formal reminder row:

```bash
python -m app reminder probe --task-id 1
```

Defaults:

```dotenv
REMINDER_WORKER_LEASE_SECONDS=120
REMINDER_WORKER_RETRY_BASE_SECONDS=30
REMINDER_WORKER_POLL_SECONDS=5
```

## Verification

Migration `20260823_0009` adds the final receive type and receive ID to each
sent audit row. During upgrade it safely attributes any legacy sent record to
its source chat before enforcing the new invariant.

Offline coverage verifies exclusive claims, expired-lease recovery, stale
Worker rejection, successful delivery audit, private-to-group fallback with a
real Open-ID `@`, deterministic UUID reuse, transport failures, retry timing,
terminal `dead` state, missed-stage consolidation, task completion cancellation,
targeted claims, CLI routing, schema constraints, and legacy migration.

The real database upgraded to `20260823_0009`; foreign-key and integrity checks
pass. Re-synchronizing Task 1 created and cancelled zero rows, and its four
formal reminders remained `scheduled` with zero attempts. A recoverable backup
was created at
`data/feishu_task_agent.db.pre-phase5b-acceptance-20260823` before the external
probe.

The first probe for Task 1 received private error `230101` and succeeded through
the source-group fallback. A later private-only diagnostic showed that Feishu
can report `Sending messages to users is temporarily unavailable` even when the
message subsequently arrives, so immediate fallback could duplicate a reminder.
The sender was tightened accordingly.

After the owner sent a direct `任务列表` command, the incoming event established
P2P chat `oc_example_private_chat`. Direct delivery to this chat and
the final formal probe both received explicit success responses. The final
probe result was:

```text
message_id: om_example_message
receive_id_type: chat_id
receive_id: oc_example_private_chat
private_error_code: null
formal_reminder_plan_changed: false
```

The reminder and detection Workers are not left running by this acceptance
step. The Feishu Listener was restarted for the following Phase 6A live test.
