# Phase 4C: personal and administrator task lists

## Outcome

The bot supports exact unfinished-task queries in a group:

```text
@任务机器人 任务列表
@任务机器人 本群任务
@任务机器人 查看任务
```

Ordinary members see only their own tasks from that group. A configured task
administrator sees all owners' tasks from that group.

The same member may privately message the bot with plain `任务列表`. An ordinary
member receives their own tasks across configured groups, with each item labeled
by its source group. A task administrator receives all unfinished tasks across
those configured groups. This lets routine queries happen without interrupting
the group.

Every query is deterministic and does not call the task-detection model. The
query message is stored for audit but excluded from the detection queue.

## Authorization and isolation boundaries

For an ordinary group member, the repository query enforces all three:

```text
tasks.chat_id = command_message.chat_id
EXISTS task_assignees WHERE open_id = command_message.sender_open_id
tasks.status IN (pending, todo, overdue)
```

Neither the owner nor the group ID can come from command text or an LLM. A task
in group B therefore cannot appear in group A's reply, and one member cannot
request another member's tasks by typing their name.

A private query cannot use its P2P `chat_id` because tasks belong to groups. It
filters by the sender Open ID and restricts results to
`FEISHU_ALLOWED_CHAT_IDS` when that allowlist is configured. Each result carries
its original group ID and display name.

Task-list administration is separate from identity-binding administration. The
temporary comma-separated setting `FEISHU_TASK_ADMIN_OPEN_IDS` grants the wider
view:

- in a group, an administrator sees all unfinished tasks from that group only;
- in a private chat, an administrator sees all unfinished tasks across the
  configured allowed groups.

For live acceptance, 莉莉 is the temporary administrator because that was the
account that sent the initial task-list query. The privilege is fixed to the
sender Open ID, not inferred from the displayed or bound name. Phase 7 will
replace this environment setting with chat-scoped administrator management in
the web page.

## Reply rules

- Show only `pending`, `todo`, and `overdue`.
- Hide `done` and `cancelled`.
- Sort tasks with deadlines first by ascending deadline, then put tasks without
  a deadline last.
- Display deadlines in `Asia/Shanghai` time.
- Show all responsible names in administrator views. Personal views omit the
  redundant line for single-owner tasks but show the full member set for a
  shared task.
- Label every private result with its source group.
- Return no more than 20 tasks in one bot reply and state how many additional
  tasks were omitted.
- Reply explicitly when the current scope has no unfinished tasks.

Group commands require a leading bot mention and an exact command. Private
commands require exact text but no mention. Neither path uses fuzzy intent
recognition, which prevents ordinary discussion from triggering a task dump.
An exact bare task-list phrase sent in a group remains silent without the bot
mention, but is still treated as a reserved command for queue suppression so it
cannot become LLM detection work.

## Local verification

An operator can still inspect all unfinished tasks from one exact group without
sending a Feishu reply:

```bash
python -m app task-list --chat-id oc_xxx --limit 20
```

The JSON includes each task's ID, title, ordered responsible-member snapshots,
primary compatibility owner, Shanghai
deadline, status, and confidence. `--limit` must be between 1 and 100; bot
replies enforce the stricter maximum of 20.

Phase 4C adds no database migration. It reads the chat-isolated `tasks` records
introduced in Phase 4A.

## Verification status

The full offline suite passes 198 tests, including:

- cross-group and cross-owner isolation in the same database;
- group-personal and group-administrator authorization;
- private-personal aggregation and allowed-group-bounded private administration;
- filtering completed and cancelled tasks;
- deadline ordering and Shanghai time rendering;
- empty-list and reply-truncation behavior;
- direct messages bypassing only the group-chat allowlist check;
- command routing without detection enqueueing; and
- exact-chat operator CLI routing.

The real database contains Task 1 for the configured group:
`完成 Phase 4B 自动物化验收记录`, owned by 王政, due at
`2026-08-30 18:00 +08:00`. Live Feishu logs confirm successful replies for:

- 莉莉's administrator view in the group;
- 莉莉's administrator view in a private chat;
- 王哈's personal view in the group and in a private chat; and
- 王政's personal view in the group.

A bare group phrase exposed a queue-suppression edge case during acceptance.
Job 10 was cancelled with zero attempts before any Worker ran, and no detection
run was created for it. Exact bare task-list phrases are now suppressed from
future detection while remaining silent unless the bot is mentioned.

Final real-database counts are 33 messages, 10 detection jobs including the
cancelled audit row, 2 historical detection runs, and 1 task. Foreign-key and
SQLite integrity checks pass. No listener or Worker is left running.
