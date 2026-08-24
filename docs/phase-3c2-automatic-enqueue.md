# Phase 3C-2: atomic automatic enqueue

## Scope

This subphase connects Feishu message ingestion to the durable queue. It does
not run the AI Worker, call the model, create tasks, or send reminders.

## Transaction boundary

For an eligible event, one SQLite transaction now performs:

1. chat upsert;
2. sender upsert;
3. idempotent message insert;
4. detection job creation or debounce update;
5. commit.

If job creation or coalescing fails, the chat, user, and message changes roll
back with it. A repeated Feishu event is recognized before queue mutation and
does not extend the debounce window.

## Eligibility

A message enters the detection queue only when it is:

- a newly inserted event;
- a group message;
- text;
- sent by a human;
- not a recognized identity binding or identity query command.

The message receiver parses identity commands without applying them, suppresses
their detection scheduling, commits the message, and only then performs the
existing binding/query operation and bot reply.

## Twenty-second sliding wait window

The first eligible message creates a queued job with `available_at` set to twenty
seconds after receipt. Another eligible message in the same chat before that
time reuses the job, selects the chronologically newest trigger, and moves
`available_at` to twenty seconds after the new receipt. The value is configurable
with `DETECTION_DEBOUNCE_SECONDS`.

If the existing job is already available, running, retried, completed, or dead,
the new message creates another job. Out-of-order event delivery never moves a
job's trigger backwards in conversation time.

## Acceptance

Automated acceptance verifies:

1. message and job creation are atomic;
2. an injected queue failure rolls back message, chat, and user writes;
3. messages inside the window coalesce to one job;
4. messages outside the window create separate jobs;
5. duplicate delivery does not change the queued job;
6. out-of-order delivery keeps the latest chronological trigger;
7. private, non-text, bot, and identity-command messages are excluded;
8. all earlier phase tests still pass.

Live acceptance uses ordinary, non-task test messages. The listener should print
`detection_queue: created` for the first message and
`detection_queue: coalesced` for another message sent within twenty seconds. No
model request is made because the Worker is not running.

## Batching is not task identity

The twenty-second value only waits for a short conversation burst and reduces
duplicate model calls. It never asserts that every message in the window belongs
to one task.

Semantic attribution uses a hybrid boundary:

1. `chat_id` is the mandatory isolation boundary;
2. `root_id` and `parent_id` provide strong reply/thread signals when present;
3. verified member names constrain possible owners;
4. the rolling context may extend beyond the twenty-second batch;
5. the model must select exact `evidence_message_ids` for each task candidate;
6. Phase 4 will deduplicate candidates against existing tasks and evidence.

Before automatic model execution, the result contract will be extended from one
optional task to a list of zero or more candidates. This prevents two unrelated
assignments posted within twenty seconds—or two assignments in one message—from
being collapsed into a single task.
