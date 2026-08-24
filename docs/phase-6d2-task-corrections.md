# Phase 6D-2: audited task corrections

## Business rules

Task corrections are private, task-code-anchored operations. The configured
task administrator can use natural language to correct an actionable task:

- `T-AB 标题改为“提交最终联合回归报告”` uses `rename`;
- `T-AB 负责人改为王哈` uses `reassign` with one member;
- `T-AB 改为王政和王哈共同负责` uses `reassign` with two members;
- `T-AB 延期到 8 月 30 日 18:00` keeps using `reschedule`;
- `T-AB 是机器人误识别的，不是任务，撤销` uses `invalidate`.

`cancel` remains a real business cancellation. `invalidate` records an AI
false positive and changes the task to `cancelled` without deleting its source,
evidence, code, or audit history. Ordinary responsible members may still
complete, cancel, and reschedule their own tasks, but cannot rename, reassign,
or invalidate them.

## Grounding and transaction safety

The LLM receives only one pre-authorized task and the verified alias directory
from that task's source group. A reassignment may select at most 20 members and
must copy each name and Open ID from that directory. Before commit, local code
rechecks the task code, administrator identity, group allowlist, task state,
trigger message, confidence, exact alias/Open-ID binding, and changed value.

Migration `20260823_0015` extends lifecycle events with `rename`, `reassign`,
and `invalidate`, plus old/new title and ordered responsible-member snapshots.
The task update, lifecycle evidence, compatibility owner fields, assignee rows,
and reminder cancellation/replanning commit in one SQLite transaction. Duplicate
delivery of the same trigger is replay-safe.

## Notifications

- Rename: every current responsible member is privately told the corrected
  title.
- Reassignment: added, removed, and retained responsible members receive
  different private messages.
- Invalidation: all former responsible members are told that the false-positive
  task was withdrawn and will no longer remind them.
- Other configured administrators receive an audit notification; the acting
  administrator does not receive a duplicate message.

Existing missing-deadline prompts for a removed member are cancelled. Deadline
reminders are replanned for the complete new responsible-member set.

## Live acceptance

Create one unique test task in the group, wait for its task code, then use the
administrator's private chat to run these as separate operations:

```text
T-XX 标题改为“Phase 6D-2 纠错后的标题”
T-XX 负责人改为王哈
```

Verify the title, personal visibility, reminder recipient, member notices, and
event snapshots after each operation. Create a second unique test task and send:

```text
T-YY 是机器人误识别创建的，这不是任务，请撤销
```

Expected: `T-YY` becomes `cancelled`, disappears from unfinished task lists,
keeps its audit/evidence rows, cancels all unsent reminders, and privately
notifies its responsible members.
