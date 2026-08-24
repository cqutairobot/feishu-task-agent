# Phase 6B-1: stable task codes

## Purpose

Natural-language lifecycle commands need an unambiguous reference when one
person owns several tasks. Every persisted task therefore exposes a stable
public code, for example `T-1A`. The code is global rather than per chat, so the
same code can safely appear in a private cross-group task list.

## Encoding

The code has three parts:

- the fixed `T-` prefix distinguishes task references from ordinary numbers;
- the immutable positive database task ID encoded in Base36 guarantees global
  uniqueness; and
- one checksum character detects common typing or copying errors.

Task ID 1 intentionally encodes as `T-1A`. The encoder is deterministic and
requires no new database column, migration, or mutable mapping table. Deleting
or closing a task never causes its code to be reused.

The parser normalizes letter case and full-width characters. It accepts the
display form `T-1A`, compact `T1A`, and conversational shorthand `1A`, but only
when the checksum is valid.

## Surfaces

- Group and direct-message task lists display `[T-…]` before every title.
- `python -m app task-list` includes `task_code` beside the internal `id`.
- Phase 6 lifecycle model context includes both `task_code` and `task_id`; the
  model must match the code against the exact current-chat task choice.

This subphase does not change task status, deadline, reminders, or audit rows.
The next Phase 6B step will parse private natural-language lifecycle commands,
authorize the sender, validate the state transition locally, and only then
perform an audited transaction.

Offline verification passes all 258 tests, including representative round
trips, full-width input, checksum rejection, 10,000-code uniqueness, task-list
rendering, CLI output, and Phase 6A context compatibility. The real Task 1
read-only CLI result is `T-1A`.
