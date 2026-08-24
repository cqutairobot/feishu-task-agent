# Phase 7C-1: authoritative chat membership synchronization

Phase 7C-1 separates Feishu chat membership from task-name aliases.

## Trust model

- `chat_memberships` is populated only from a successful full Feishu chat and
  member-directory fetch.
- The chat owner comes from `im.v1.chat.get` with `user_id_type=open_id`.
- A returned owner must also occur in the complete paginated member list.
- Group snapshots without a valid owner fail closed and do not partially
  update the database.
- `chat_member_aliases` remains dedicated to understanding task assignee names
  in natural-language conversations. It no longer proves group membership.

## Synchronization behavior

One authoritative refresh atomically:

1. creates or refreshes the chat snapshot;
2. creates or refreshes every returned Feishu user;
3. activates every current chat member and records the current owner;
4. deactivates members absent from the new complete member list;
5. preserves the last display name and departure timestamp for audit.

A lightweight chat-name refresh does not alter membership state.

## Administrator prerequisite

New administrator grants require an active `chat_memberships` row in the exact
group. A task alias is optional. Existing administrator rows were preserved by
this migration. Automatic revocation after departure is implemented by the
completed Phase 7C flow documented in `phase-7c-group-administration.md`.

## Acceptance

- migration revision: `20260823_0018`
- full automated suite: 390 tests
- live allowed-group sync: four active members and exactly one owner
- existing production-like data preserved: one administrator and 17 tasks
- SQLite integrity check: `ok`
