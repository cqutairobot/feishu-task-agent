"""Strict, locally grounded JSON contract for lifecycle update candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import math
from typing import Any

from app.agent.context import SHANGHAI_TZ
from app.agent.contracts import MAX_TASK_ASSIGNEES, TaskOwner
from app.identity.aliases import normalize_alias
from app.lifecycle.context import LifecycleDetectionContext


LIFECYCLE_FIELDS = frozenset({"updates"})
CANDIDATE_FIELDS = frozenset(
    {
        "action",
        "confidence",
        "task_id",
        "new_deadline",
        "new_title",
        "new_owners",
        "evidence_message_ids",
    }
)
MAX_LIFECYCLE_CANDIDATES = 10


class LifecycleOutputError(ValueError):
    """Raised when lifecycle model output is malformed or ungrounded."""


class LifecycleAction(StrEnum):
    CONFIRM = "confirm"
    COMPLETE = "complete"
    ACCEPT = "accept"
    REOPEN = "reopen"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    RENAME = "rename"
    REASSIGN = "reassign"
    INVALIDATE = "invalidate"
    RESTORE = "restore"
    MERGE = "merge"


MODEL_LIFECYCLE_ACTIONS = tuple(
    action
    for action in LifecycleAction
    if action
    not in {
        LifecycleAction.CONFIRM,
        LifecycleAction.ACCEPT,
        LifecycleAction.REOPEN,
        LifecycleAction.RESTORE,
        LifecycleAction.MERGE,
    }
)


@dataclass(frozen=True, slots=True)
class LifecycleCandidate:
    action: LifecycleAction
    confidence: float
    task_id: int
    new_deadline: datetime | None
    evidence_message_ids: tuple[str, ...]
    new_title: str | None = None
    new_owners: tuple[TaskOwner, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "confidence": self.confidence,
            "task_id": self.task_id,
            "new_deadline": (
                None
                if self.new_deadline is None
                else self.new_deadline.astimezone(SHANGHAI_TZ).isoformat()
            ),
            "new_title": self.new_title,
            "new_owners": [
                {"name": owner.name, "open_id": owner.open_id}
                for owner in self.new_owners
            ],
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True, slots=True)
class LifecycleDetectionResult:
    updates: tuple[LifecycleCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {"updates": [item.to_dict() for item in self.updates]}


def lifecycle_detection_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "updates": {
                "type": "array",
                "maxItems": MAX_LIFECYCLE_CANDIDATES,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [item.value for item in MODEL_LIFECYCLE_ACTIONS],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "task_id": {"type": "integer", "minimum": 1},
                        "new_deadline": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "new_title": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "new_owners": {
                            "type": "array",
                            "maxItems": MAX_TASK_ASSIGNEES,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "open_id": {"type": "string"},
                                },
                                "required": ["name", "open_id"],
                                "additionalProperties": False,
                            },
                        },
                        "evidence_message_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                    "required": sorted(CANDIDATE_FIELDS),
                    "additionalProperties": False,
                },
            }
        },
        "required": sorted(LIFECYCLE_FIELDS),
        "additionalProperties": False,
    }


def parse_lifecycle_detection_json(
    payload: str,
    context: LifecycleDetectionContext,
) -> LifecycleDetectionResult:
    try:
        data = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_constant(value),
        )
    except (json.JSONDecodeError, LifecycleOutputError) as exc:
        raise LifecycleOutputError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or frozenset(data) != LIFECYCLE_FIELDS:
        actual = set(data) if isinstance(data, dict) else set()
        raise LifecycleOutputError(
            "output fields do not match contract; "
            f"missing={sorted(LIFECYCLE_FIELDS - actual)}, "
            f"extra={sorted(actual - LIFECYCLE_FIELDS)}"
        )
    raw_updates = data["updates"]
    if not isinstance(raw_updates, list):
        raise LifecycleOutputError("updates must be an array")
    if len(raw_updates) > MAX_LIFECYCLE_CANDIDATES:
        raise LifecycleOutputError(
            f"updates must contain at most {MAX_LIFECYCLE_CANDIDATES} items"
        )

    tasks = {task.task_id: task for task in context.tasks}
    message_ids = {
        message.message_id for message in context.conversation.messages
    }
    focus_ids = set(context.conversation.focus_message_ids)
    seen_tasks: set[int] = set()
    updates: list[LifecycleCandidate] = []
    for index, raw in enumerate(raw_updates):
        if not isinstance(raw, dict) or frozenset(raw) != CANDIDATE_FIELDS:
            actual = set(raw) if isinstance(raw, dict) else set()
            raise LifecycleOutputError(
                f"update[{index}] fields do not match contract; "
                f"missing={sorted(CANDIDATE_FIELDS - actual)}, "
                f"extra={sorted(actual - CANDIDATE_FIELDS)}"
            )
        task_id = _task_id(raw["task_id"])
        if task_id not in tasks:
            raise LifecycleOutputError(
                f"update[{index}].task_id is not an open task in this chat"
            )
        if task_id in seen_tasks:
            raise LifecycleOutputError(
                f"duplicate lifecycle update for task_id {task_id}"
            )
        seen_tasks.add(task_id)
        action = _action(raw["action"])
        deadline = _deadline(raw["new_deadline"])
        new_title = _optional_title(raw["new_title"])
        new_owners = _owners(
            raw["new_owners"],
            context=context,
            update_index=index,
        )
        if action is LifecycleAction.RESCHEDULE:
            if deadline is None:
                raise LifecycleOutputError(
                    "reschedule requires a non-null new_deadline"
                )
            if deadline <= context.reference_time:
                raise LifecycleOutputError(
                    "reschedule new_deadline must be after reference_time"
                )
            if tasks[task_id].deadline == deadline:
                raise LifecycleOutputError(
                    "reschedule new_deadline must change the current deadline"
                )
        elif deadline is not None:
            raise LifecycleOutputError(
                f"{action.value} requires new_deadline to be null"
            )
        if action is LifecycleAction.RENAME:
            if new_title is None:
                raise LifecycleOutputError("rename requires a non-null new_title")
            if _normalize_title(new_title) == _normalize_title(tasks[task_id].title):
                raise LifecycleOutputError("rename new_title must change the title")
        elif new_title is not None:
            raise LifecycleOutputError(
                f"{action.value} requires new_title to be null"
            )
        if action is LifecycleAction.REASSIGN:
            if not new_owners:
                raise LifecycleOutputError("reassign requires at least one new owner")
            current_owner_ids = tuple(
                open_id
                for _name, open_id in (
                    tasks[task_id].assignees
                    or ((tasks[task_id].owner_name, tasks[task_id].owner_open_id),)
                )
            )
            if tuple(owner.open_id for owner in new_owners) == current_owner_ids:
                raise LifecycleOutputError("reassign must change the responsible members")
        elif new_owners:
            raise LifecycleOutputError(
                f"{action.value} requires new_owners to be empty"
            )
        evidence = _evidence(raw["evidence_message_ids"])
        unknown = set(evidence) - message_ids
        if unknown:
            raise LifecycleOutputError(
                f"evidence is outside the current context: {sorted(unknown)}"
            )
        if focus_ids and not set(evidence).intersection(focus_ids):
            raise LifecycleOutputError(
                "lifecycle evidence must include the trigger/focus message"
            )
        updates.append(
            LifecycleCandidate(
                action=action,
                confidence=_confidence(raw["confidence"]),
                task_id=task_id,
                new_deadline=deadline,
                evidence_message_ids=evidence,
                new_title=new_title,
                new_owners=new_owners,
            )
        )
    return LifecycleDetectionResult(updates=tuple(updates))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleOutputError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LifecycleOutputError(f"non-finite number is not valid: {value}")


def _action(value: Any) -> LifecycleAction:
    if not isinstance(value, str):
        raise LifecycleOutputError("action must be a string")
    try:
        action = LifecycleAction(value)
    except ValueError as exc:
        raise LifecycleOutputError("action is not supported") from exc
    # The JSON schema is only the first line of defence. JSON-object fallback
    # providers can return any string, so enforce the exact model allowlist
    # locally as well.  Review actions use their own read-only contract and
    # must never fall through to the mutation-oriented lifecycle pipeline.
    if action not in MODEL_LIFECYCLE_ACTIONS:
        raise LifecycleOutputError("action is not supported")
    return action


def _task_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LifecycleOutputError("task_id must be a positive integer")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LifecycleOutputError("confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise LifecycleOutputError("confidence must be between 0 and 1")
    return result


def _deadline(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LifecycleOutputError(
            "new_deadline must be an ISO 8601 string or null"
        )
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleOutputError(
            "new_deadline must be valid ISO 8601"
        ) from exc
    if result.tzinfo is None:
        raise LifecycleOutputError("new_deadline must include timezone offset")
    return result.astimezone(SHANGHAI_TZ)


def _evidence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LifecycleOutputError(
            "evidence_message_ids must be a non-empty array"
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LifecycleOutputError(
                "evidence message IDs must be non-empty strings"
            )
        if item in result:
            raise LifecycleOutputError(
                f"duplicate evidence message ID: {item}"
            )
        result.append(item)
    return tuple(result)


def _optional_title(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LifecycleOutputError("new_title must be a string or null")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 200:
        raise LifecycleOutputError("new_title must contain 1 to 200 characters")
    return cleaned


def _owners(
    value: Any,
    *,
    context: LifecycleDetectionContext,
    update_index: int,
) -> tuple[TaskOwner, ...]:
    if not isinstance(value, list):
        raise LifecycleOutputError("new_owners must be an array")
    if len(value) > MAX_TASK_ASSIGNEES:
        raise LifecycleOutputError(
            f"new_owners must contain at most {MAX_TASK_ASSIGNEES} members"
        )
    eligible = {
        participant.open_id: participant
        for participant in context.eligible_owners
    }
    result: list[TaskOwner] = []
    seen: set[str] = set()
    for owner_index, raw_owner in enumerate(value):
        if not isinstance(raw_owner, dict) or frozenset(raw_owner) != {
            "name",
            "open_id",
        }:
            raise LifecycleOutputError(
                f"update[{update_index}].new_owners[{owner_index}] is malformed"
            )
        name = raw_owner["name"]
        open_id = raw_owner["open_id"]
        if not isinstance(name, str) or not name.strip():
            raise LifecycleOutputError("new owner name must not be empty")
        if not isinstance(open_id, str) or not open_id.strip():
            raise LifecycleOutputError("new owner open_id must not be empty")
        participant = eligible.get(open_id)
        if participant is None or normalize_alias(name) not in {
            normalize_alias(accepted)
            for accepted in participant.accepted_names
        }:
            raise LifecycleOutputError(
                f"new owner is not grounded in the task source chat: {name!r}"
            )
        if open_id in seen:
            raise LifecycleOutputError(f"duplicate new owner open_id: {open_id}")
        seen.add(open_id)
        result.append(TaskOwner(name=participant.name, open_id=open_id))
    return tuple(result)


def _normalize_title(value: str) -> str:
    return " ".join(value.split()).casefold()
