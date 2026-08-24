# Phase 3B: OpenAI-compatible task detection

## Scope

Phase 3B performs one explicit LLM call for a stored trigger message. It does
not yet call the model for every incoming message, persist tasks, or send group
notifications. Those mutations remain outside this phase.

The detector receives only the context produced by Phase 3A:

- messages from one `chat_id`;
- messages no later than the selected trigger;
- at most the configured context limit and character budget;
- the current one-to-one verified member names for that chat.

## Configuration

Keep these values in `.env`:

```dotenv
TASK_LLM_API_KEY=
TASK_LLM_BASE_URL=https://example.test/compatible-mode/v1
TASK_LLM_MODEL=qwen3.7-plus
TASK_LLM_TIMEOUT_SECONDS=60
TASK_LLM_MAX_RETRIES=2
```

The API key is passed only in the HTTP `Authorization` header. It is not printed
by the commands or included in model error messages.

## Commands

Model discovery only:

```bash
python -m app llm-check
```

Model discovery plus a built-in fictional conversation:

```bash
python -m app llm-check --probe
```

One stored conversation:

```bash
python -m app task-detect \
  --chat-id oc_xxx \
  --message-id om_xxx \
  --limit 30
```

`task-detect` prints the strict seven-field result to standard output. Model,
response format, request ID, and token count are written separately to standard
error so the JSON can be piped safely to another process.

## Structured-output strategy

The first request uses Chat Completions with `response_format.type` set to
`json_schema`, a strict schema, and no additional fields. If a compatible
provider explicitly rejects that feature with HTTP 400 or 422, the detector
retries once with `json_object`.

Provider-side formatting is only the first boundary. Every successful response
must also pass the local Phase 3A contract:

- exactly seven top-level fields;
- owner Open ID must exist in the current context;
- owner name must match that Open ID's current verified group name;
- evidence IDs must exist in the selected message window;
- deadlines must be valid ISO 8601 values with a timezone;
- non-task results must contain null task fields and no evidence.

Invalid output is rejected and is never silently repaired or persisted.

## Acceptance

The built-in fictional probe is accepted when it:

1. reaches the configured model;
2. returns `json_schema` or the documented `json_object` fallback;
3. assigns the fictional volunteer by the exact known Open ID;
4. converts “周四之前” using the supplied reference time and Asia/Shanghai;
5. cites only the three fictional message IDs;
6. passes local contract validation.

The real-chat acceptance uses the same command, but requires explicit approval
to send that chat's selected messages and participant identities to the
configured external model endpoint.
