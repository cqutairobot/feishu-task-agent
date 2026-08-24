# Phase 6D-1: shared tasks

## Business semantics

One shared deliverable has one task ID, one public code, one deadline, and an
ordered set of responsible members. For example:

```text
王政和王哈共同负责前端页面，提交同一份联合报告。
```

creates one `shared` candidate. In contrast:

```text
王政和王哈各自提交一份检查表。
```

creates two `single` candidates and two task codes. The model can select only
verified members in the current chat; every responsible name and Open ID is
locally grounded before any write.

## Persistence and compatibility

Migration `20260823_0014` creates `task_assignees` with unique task/member and
task/position constraints. Every existing task is backfilled using its former
owner as position zero. The original owner columns remain as a primary-member
compatibility snapshot while repositories treat the association table as the
canonical responsible-member set.

The same migration makes reminder rows recipient-scoped. Historical reminders
are backfilled to their original owner. A shared task gets four production or
test stages per member, without duplicating the task itself.

## Authorization and notifications

- Every responsible member sees the same task code in personal group and P2P
  lists.
- Every responsible member can complete, cancel, or reschedule the shared task
  through the existing private natural-language or card paths.
- A transition remains atomic for the one shared task and cancels or replans
  every member's reminder rows together.
- Other responsible members receive a private completion, cancellation, or
  reschedule notice.
- Administrators receive completion, cancellation, overdue, and reschedule
  notices. Reschedule notices contain both the old and new deadline.
- Missing-deadline prompts go once to every responsible member and once to the
  administrator at the existing one-/three-day policy.

Outgoing rows remain leased, retryable, idempotent, and private-only for task
notifications. Normal deadline reminders retain their existing final group
fallback policy if private delivery is definitively unavailable.

## Live acceptance messages

Send the following as two separate group messages and wait at least 70 seconds
after each:

```text
王政和王哈共同负责“Phase 6D-1 联合回归报告”，请在30分钟后提交同一份报告。
```

Expected: exactly one task, two responsible members, one public code, and eight
test reminder rows.

```text
王政和王哈各自完成一份“Phase 6D-1 个人检查表”，请在35分钟后提交。
```

Expected: two independent task codes, each with one responsible member and
four test reminder rows.
