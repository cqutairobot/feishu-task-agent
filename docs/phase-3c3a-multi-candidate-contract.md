# Phase 3C-3A: zero-to-many task candidates

Phase 3C-2 uses a configurable twenty-second sliding window to reduce repeated
model calls during a short burst of messages. That window is a scheduling
mechanism only. It is not evidence that messages belong to one task.

Before enabling the background Worker, Phase 3C-3A adds a separate automatic
detection contract that can return zero, one, or several independent tasks:

```json
{
  "candidates": [
    {
      "assignment_mode": "single",
      "confidence": 0.96,
      "co_owners": [],
      "owner": {
        "name": "王政",
        "open_id": "ou_xxx"
      },
      "title": "补充 baseline 实验",
      "description": "完成 ResNet50 baseline 实验",
      "deadline": "2026-08-27T23:59:59+08:00",
      "evidence_message_ids": ["om_xxx", "om_yyy"]
    }
  ]
}
```

`{"candidates": []}` means that no explicit task has formed. A candidate with
`assignment_mode=shared` has one primary `owner` plus one or more ordered
`co_owners` and represents one shared deliverable. `single` requires an empty
`co_owners` array. Wording such as “each person submits one copy” produces
separate single candidates, which may cite the same source message.

## Local safety checks

The model-facing JSON Schema and the local parser both enforce closed objects;
unknown or missing fields are rejected. Local validation additionally rejects:

- more than ten candidates in one response;
- non-finite or out-of-range confidence values;
- empty titles, descriptions, owners, or evidence arrays;
- duplicate responsible Open IDs or an assignment mode inconsistent with
  `co_owners`;
- owner Open IDs absent from the current chat-isolated context;
- owner names that do not match the confirmed name for that Open ID;
- evidence IDs absent from the current context;
- deadlines without a timezone offset; and
- duplicate candidates with the same owner, normalized title, and evidence.

The model may not create an identity mapping. It can only select a member from
`known_participants`, which is built from stored group participants and verified
per-chat name bindings.

## Scheduling versus semantics

The current layers have different responsibilities:

1. `chat_id` is a hard isolation boundary. No context or candidate can cross it.
2. The twenty-second window batches nearby triggers to reduce duplicate calls.
3. The rolling context gives the model enough prior messages to resolve phrases
   such as “我来做” and “周四前完成”.
4. The batch prompt separates different work and per-person deliverables while
   keeping one shared deliverable in one candidate.
5. Exact evidence IDs make each candidate auditable.
6. Phase 4 will compare candidates with persisted tasks before task creation or
   update, providing lifecycle-level deduplication.

Thread identifiers such as `root_id` and `parent_id` can later be added as strong
context-selection signals. They should improve relevance but must not replace
chat isolation or evidence validation.

## Verification

Run all local tests:

```bash
python -m unittest discover -s tests -v
```

Run a real provider call using only a built-in fictional conversation that
contains two assignments:

```bash
python -m app llm-check --batch-probe
```

The existing Phase 3B commands retain their original seven-field single-result
contract. Phase 3C-3A does not start a Worker, claim queued jobs, create tasks,
or send reminders.
