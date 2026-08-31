"""Strict local contract for high-risk review-action intent detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from typing import Any
import unicodedata

from app.lifecycle.review_context import ReviewDetectionContext


REVIEW_OUTPUT_FIELDS = frozenset({"intents"})
REVIEW_INTENT_FIELDS = frozenset(
    {"action", "confidence", "task_id", "reason", "evidence_message_ids"}
)
MAX_REOPEN_REASON_LENGTH = 2_000


class ReviewOutputError(ValueError):
    """Raised when review model output is malformed or ungrounded."""


class ReviewAction(StrEnum):
    ACCEPT = "accept"
    REOPEN = "reopen"


@dataclass(frozen=True, slots=True)
class ReviewActionCandidate:
    action: ReviewAction
    confidence: float
    task_id: int
    reason: str | None
    evidence_message_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "confidence": self.confidence,
            "task_id": self.task_id,
            "reason": self.reason,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True, slots=True)
class ReviewDetectionResult:
    intents: tuple[ReviewActionCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {"intents": [intent.to_dict() for intent in self.intents]}


def review_detection_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [action.value for action in ReviewAction],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "task_id": {"type": "integer", "minimum": 1},
                        "reason": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": MAX_REOPEN_REASON_LENGTH,
                                },
                                {"type": "null"},
                            ]
                        },
                        "evidence_message_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                    "required": sorted(REVIEW_INTENT_FIELDS),
                    "additionalProperties": False,
                },
            }
        },
        "required": sorted(REVIEW_OUTPUT_FIELDS),
        "additionalProperties": False,
    }


def parse_review_detection_json(
    payload: str,
    context: ReviewDetectionContext,
) -> ReviewDetectionResult:
    try:
        data = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_constant(value),
        )
    except (json.JSONDecodeError, ReviewOutputError) as exc:
        raise ReviewOutputError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or frozenset(data) != REVIEW_OUTPUT_FIELDS:
        actual = set(data) if isinstance(data, dict) else set()
        raise ReviewOutputError(
            "output fields do not match contract; "
            f"missing={sorted(REVIEW_OUTPUT_FIELDS - actual)}, "
            f"extra={sorted(actual - REVIEW_OUTPUT_FIELDS)}"
        )
    raw_intents = data["intents"]
    if not isinstance(raw_intents, list):
        raise ReviewOutputError("intents must be an array")
    if len(raw_intents) > 1:
        raise ReviewOutputError("intents must contain at most one item")
    if not raw_intents:
        return ReviewDetectionResult(intents=())

    raw = raw_intents[0]
    if not isinstance(raw, dict) or frozenset(raw) != REVIEW_INTENT_FIELDS:
        actual = set(raw) if isinstance(raw, dict) else set()
        raise ReviewOutputError(
            "intent[0] fields do not match contract; "
            f"missing={sorted(REVIEW_INTENT_FIELDS - actual)}, "
            f"extra={sorted(actual - REVIEW_INTENT_FIELDS)}"
        )

    task_id = _task_id(raw["task_id"])
    tasks = {task.task_id: task for task in context.tasks}
    task = tasks.get(task_id)
    if task is None:
        raise ReviewOutputError("intent task_id is not the authorized review task")
    action = _action(raw["action"])
    reason = _reason(raw["reason"])
    if action is ReviewAction.ACCEPT:
        if task.review_status != "pending":
            raise ReviewOutputError("accept requires pending review status")
        if reason is not None:
            raise ReviewOutputError("accept requires reason to be null")
    else:
        if task.review_status not in {"pending", "accepted"}:
            raise ReviewOutputError(
                "reopen requires pending or accepted review status"
            )
        if reason is None:
            raise ReviewOutputError("reopen requires a non-empty reason")

    evidence = _evidence(raw["evidence_message_ids"])
    message_ids = {
        message.message_id for message in context.conversation.messages
    }
    unknown = set(evidence) - message_ids
    if unknown:
        raise ReviewOutputError(
            f"evidence is outside the current context: {sorted(unknown)}"
        )
    focus_ids = set(context.conversation.focus_message_ids)
    if focus_ids and not set(evidence).intersection(focus_ids):
        raise ReviewOutputError(
            "review evidence must include the trigger/focus message"
        )
    return ReviewDetectionResult(
        intents=(
            ReviewActionCandidate(
                action=action,
                confidence=_confidence(raw["confidence"]),
                task_id=task_id,
                reason=reason,
                evidence_message_ids=evidence,
            ),
        )
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewOutputError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ReviewOutputError(f"non-finite number is not valid: {value}")


def _action(value: Any) -> ReviewAction:
    if not isinstance(value, str):
        raise ReviewOutputError("action must be a string")
    try:
        return ReviewAction(value)
    except ValueError as exc:
        raise ReviewOutputError("action is not supported") from exc


def _task_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewOutputError("task_id must be a positive integer")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewOutputError("confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ReviewOutputError("confidence must be between 0 and 1")
    return result


def _reason(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewOutputError("reason must be a string or null")
    result = " ".join(unicodedata.normalize("NFC", value).split())
    if not result or len(result) > MAX_REOPEN_REASON_LENGTH:
        raise ReviewOutputError(
            f"reason must contain 1 to {MAX_REOPEN_REASON_LENGTH} characters"
        )
    return result


def _evidence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ReviewOutputError(
            "evidence_message_ids must be a non-empty array"
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ReviewOutputError(
                "evidence message IDs must be non-empty strings"
            )
        if item in result:
            raise ReviewOutputError(f"duplicate evidence message ID: {item}")
        result.append(item)
    return tuple(result)
