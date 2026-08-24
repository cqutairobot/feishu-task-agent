"""Strict provider-neutral JSON contract for task detection results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from enum import StrEnum
from typing import Any

from app.agent.context import SHANGHAI_TZ, TaskDetectionContext
from app.identity.aliases import normalize_alias


EXPECTED_FIELDS = frozenset(
    {
        "is_task",
        "confidence",
        "owner",
        "title",
        "description",
        "deadline",
        "evidence_message_ids",
    }
)

BATCH_EXPECTED_FIELDS = frozenset({"candidates"})
CANDIDATE_EXPECTED_FIELDS = frozenset(
    {
        "assignment_mode",
        "confidence",
        "co_owners",
        "owner",
        "title",
        "description",
        "deadline",
        "evidence_message_ids",
    }
)
MAX_TASK_CANDIDATES = 10
MAX_TASK_ASSIGNEES = 20


class TaskOutputError(ValueError):
    """Raised when model output is malformed, ungrounded, or unsafe."""


@dataclass(frozen=True, slots=True)
class TaskOwner:
    name: str
    open_id: str


class TaskAssignmentMode(StrEnum):
    SINGLE = "single"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class TaskDetectionResult:
    is_task: bool
    confidence: float
    owner: TaskOwner | None
    title: str | None
    description: str | None
    deadline: datetime | None
    evidence_message_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "is_task": self.is_task,
            "confidence": self.confidence,
            "owner": (
                None
                if self.owner is None
                else {"name": self.owner.name, "open_id": self.owner.open_id}
            ),
            "title": self.title,
            "description": self.description,
            "deadline": (
                None
                if self.deadline is None
                else self.deadline.astimezone(SHANGHAI_TZ).isoformat()
            ),
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    confidence: float
    owner: TaskOwner
    title: str
    description: str
    deadline: datetime | None
    evidence_message_ids: tuple[str, ...]
    assignment_mode: TaskAssignmentMode = TaskAssignmentMode.SINGLE
    co_owners: tuple[TaskOwner, ...] = ()

    @property
    def owners(self) -> tuple[TaskOwner, ...]:
        return (self.owner, *self.co_owners)

    def to_dict(self) -> dict[str, object]:
        return {
            "assignment_mode": self.assignment_mode.value,
            "confidence": self.confidence,
            "co_owners": [
                {"name": owner.name, "open_id": owner.open_id}
                for owner in self.co_owners
            ],
            "owner": {
                "name": self.owner.name,
                "open_id": self.owner.open_id,
            },
            "title": self.title,
            "description": self.description,
            "deadline": (
                None
                if self.deadline is None
                else self.deadline.astimezone(SHANGHAI_TZ).isoformat()
            ),
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True, slots=True)
class TaskDetectionBatchResult:
    candidates: tuple[TaskCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates]
        }


def task_detection_json_schema() -> dict[str, object]:
    """Return the provider-facing schema; semantic checks remain local."""

    nullable_string = {
        "anyOf": [
            {"type": "string"},
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "properties": {
            "is_task": {"type": "boolean"},
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "owner": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "open_id": {"type": "string"},
                        },
                        "required": ["name", "open_id"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                ]
            },
            "title": nullable_string,
            "description": nullable_string,
            "deadline": nullable_string,
            "evidence_message_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": sorted(EXPECTED_FIELDS),
        "additionalProperties": False,
    }


def task_detection_batch_json_schema() -> dict[str, object]:
    """Return the strict schema for zero or more independent task candidates."""

    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "maxItems": MAX_TASK_CANDIDATES,
                "items": {
                    "type": "object",
                    "properties": {
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "assignment_mode": {
                            "type": "string",
                            "enum": ["single", "shared"],
                        },
                        "owner": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "open_id": {"type": "string"},
                            },
                            "required": ["name", "open_id"],
                            "additionalProperties": False,
                        },
                        "co_owners": {
                            "type": "array",
                            "maxItems": MAX_TASK_ASSIGNEES - 1,
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
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "deadline": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "evidence_message_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                    "required": sorted(CANDIDATE_EXPECTED_FIELDS),
                    "additionalProperties": False,
                },
            }
        },
        "required": sorted(BATCH_EXPECTED_FIELDS),
        "additionalProperties": False,
    }


def parse_task_detection_json(
    payload: str, context: TaskDetectionContext
) -> TaskDetectionResult:
    """Parse strict JSON and reject IDs or names absent from the input context."""

    try:
        data = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_constant(value),
        )
    except (json.JSONDecodeError, TaskOutputError) as exc:
        raise TaskOutputError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskOutputError("top-level output must be an object")
    if frozenset(data) != EXPECTED_FIELDS:
        missing = sorted(EXPECTED_FIELDS - set(data))
        extra = sorted(set(data) - EXPECTED_FIELDS)
        raise TaskOutputError(
            f"output fields do not match contract; missing={missing}, extra={extra}"
        )

    is_task = data["is_task"]
    if not isinstance(is_task, bool):
        raise TaskOutputError("is_task must be a boolean")
    confidence = _confidence(data["confidence"])
    evidence = _evidence(data["evidence_message_ids"])

    if not is_task:
        if any(
            data[field] is not None
            for field in ("owner", "title", "description", "deadline")
        ) or evidence:
            raise TaskOutputError(
                "non-task output must use null task fields and empty evidence"
            )
        return TaskDetectionResult(
            is_task=False,
            confidence=confidence,
            owner=None,
            title=None,
            description=None,
            deadline=None,
            evidence_message_ids=(),
        )

    owner = _owner(data["owner"])
    title = _required_text(data["title"], "title", maximum=200)
    description = _required_text(
        data["description"], "description", maximum=2_000
    )
    deadline = _deadline(data["deadline"])
    if not evidence:
        raise TaskOutputError("task evidence_message_ids must not be empty")

    result = TaskDetectionResult(
        is_task=True,
        confidence=confidence,
        owner=owner,
        title=title,
        description=description,
        deadline=deadline,
        evidence_message_ids=evidence,
    )
    _validate_grounding(result, context)
    return result


def parse_task_detection_batch_json(
    payload: str, context: TaskDetectionContext
) -> TaskDetectionBatchResult:
    """Parse and ground a zero-to-many task-candidate response."""

    try:
        data = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_constant(value),
        )
    except (json.JSONDecodeError, TaskOutputError) as exc:
        raise TaskOutputError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TaskOutputError("top-level output must be an object")
    if frozenset(data) != BATCH_EXPECTED_FIELDS:
        missing = sorted(BATCH_EXPECTED_FIELDS - set(data))
        extra = sorted(set(data) - BATCH_EXPECTED_FIELDS)
        raise TaskOutputError(
            "batch output fields do not match contract; "
            f"missing={missing}, extra={extra}"
        )

    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list):
        raise TaskOutputError("candidates must be an array")
    if len(raw_candidates) > MAX_TASK_CANDIDATES:
        raise TaskOutputError(
            f"candidates must contain at most {MAX_TASK_CANDIDATES} items"
        )

    candidates: list[TaskCandidate] = []
    fingerprints: set[tuple[str, str, tuple[str, ...]]] = set()
    for index, value in enumerate(raw_candidates):
        if not isinstance(value, dict) or frozenset(value) != CANDIDATE_EXPECTED_FIELDS:
            actual = set(value) if isinstance(value, dict) else set()
            missing = sorted(CANDIDATE_EXPECTED_FIELDS - actual)
            extra = sorted(actual - CANDIDATE_EXPECTED_FIELDS)
            raise TaskOutputError(
                f"candidate[{index}] fields do not match contract; "
                f"missing={missing}, extra={extra}"
            )

        evidence = _evidence(value["evidence_message_ids"])
        if not evidence:
            raise TaskOutputError(
                f"candidate[{index}] evidence_message_ids must not be empty"
            )
        candidate = TaskCandidate(
            confidence=_confidence(value["confidence"]),
            owner=_owner(value["owner"]),
            title=_required_text(value["title"], "title", maximum=200),
            description=_required_text(
                value["description"], "description", maximum=2_000
            ),
            deadline=_deadline(value["deadline"]),
            evidence_message_ids=evidence,
            assignment_mode=_assignment_mode(value["assignment_mode"]),
            co_owners=_co_owners(value["co_owners"]),
        )
        _validate_assignment(candidate, index=index)
        _validate_grounding(candidate, context)
        fingerprint = (
            ",".join(sorted(owner.open_id for owner in candidate.owners)),
            " ".join(candidate.title.split()).casefold(),
            tuple(sorted(candidate.evidence_message_ids)),
        )
        if fingerprint in fingerprints:
            raise TaskOutputError(f"duplicate task candidate at index {index}")
        fingerprints.add(fingerprint)
        candidates.append(candidate)

    return TaskDetectionBatchResult(candidates=tuple(candidates))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskOutputError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise TaskOutputError(f"non-finite number is not valid: {value}")


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskOutputError("confidence must be a number")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise TaskOutputError("confidence must be between 0 and 1")
    return confidence


def _owner(value: Any) -> TaskOwner:
    if not isinstance(value, dict) or set(value) != {"name", "open_id"}:
        raise TaskOutputError("owner must contain exactly name and open_id")
    return TaskOwner(
        name=_required_text(value["name"], "owner.name", maximum=255),
        open_id=_required_text(
            value["open_id"], "owner.open_id", maximum=128
        ),
    )


def _co_owners(value: Any) -> tuple[TaskOwner, ...]:
    if not isinstance(value, list):
        raise TaskOutputError("co_owners must be an array")
    if len(value) > MAX_TASK_ASSIGNEES - 1:
        raise TaskOutputError(
            f"co_owners must contain at most {MAX_TASK_ASSIGNEES - 1} items"
        )
    return tuple(_owner(item) for item in value)


def _assignment_mode(value: Any) -> TaskAssignmentMode:
    try:
        return TaskAssignmentMode(value)
    except (TypeError, ValueError) as exc:
        raise TaskOutputError(
            "assignment_mode must be single or shared"
        ) from exc


def _validate_assignment(candidate: TaskCandidate, *, index: int) -> None:
    owners = candidate.owners
    open_ids = [owner.open_id for owner in owners]
    if len(open_ids) != len(set(open_ids)):
        raise TaskOutputError(
            f"candidate[{index}] contains a duplicate responsible member"
        )
    if candidate.assignment_mode is TaskAssignmentMode.SINGLE:
        if candidate.co_owners:
            raise TaskOutputError(
                f"candidate[{index}] single assignment cannot have co_owners"
            )
    elif not candidate.co_owners:
        raise TaskOutputError(
            f"candidate[{index}] shared assignment requires co_owners"
        )


def _required_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskOutputError(f"{field} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise TaskOutputError(f"{field} must be at most {maximum} characters")
    return cleaned


def _deadline(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskOutputError("deadline must be an ISO 8601 string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskOutputError("deadline must be valid ISO 8601") from exc
    if parsed.tzinfo is None:
        raise TaskOutputError("deadline must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def _evidence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TaskOutputError("evidence_message_ids must be an array")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise TaskOutputError("evidence message IDs must be non-empty strings")
        if item in result:
            raise TaskOutputError(f"duplicate evidence message ID: {item}")
        result.append(item)
    return tuple(result)


def _validate_grounding(
    result: TaskDetectionResult | TaskCandidate,
    context: TaskDetectionContext,
) -> None:
    known_messages = {message.message_id for message in context.messages}
    unknown_evidence = set(result.evidence_message_ids) - known_messages
    if unknown_evidence:
        raise TaskOutputError(
            f"evidence is outside the current context: {sorted(unknown_evidence)}"
        )

    if (
        context.focus_message_ids
        and not set(result.evidence_message_ids).intersection(
            context.focus_message_ids
        )
    ):
        raise TaskOutputError(
            "task evidence must include at least one batch-focus message"
        )

    assert result.owner is not None
    owners = result.owners if isinstance(result, TaskCandidate) else (result.owner,)
    for owner in owners:
        participant = next(
            (
                item
                for item in context.participants
                if item.open_id == owner.open_id
            ),
            None,
        )
        if participant is None:
            raise TaskOutputError("owner.open_id is not a known participant")
        accepted_names = {
            normalize_alias(name) for name in participant.accepted_names
        }
        if normalize_alias(owner.name) not in accepted_names:
            raise TaskOutputError(
                "owner.name does not match the confirmed names for owner.open_id"
            )
