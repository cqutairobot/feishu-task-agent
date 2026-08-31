"""Strict, locally grounded JSON contract for natural-language task notes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math

from app.lifecycle.context import LifecycleDetectionContext
from app.tasks.notes import TaskNoteType


NOTE_FIELDS = frozenset({"notes"})
NOTE_CANDIDATE_FIELDS = frozenset(
    {"task_id", "note_type", "content", "confidence", "evidence_message_ids"}
)
MAX_NOTE_CANDIDATES = 1
MAX_NOTE_CONTENT_LENGTH = 8_000


class TaskNoteOutputError(ValueError):
    """Raised when note model output is malformed or ungrounded."""


@dataclass(frozen=True, slots=True)
class TaskNoteCandidate:
    task_id: int
    note_type: TaskNoteType
    content: str
    confidence: float
    evidence_message_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "note_type": self.note_type.value,
            "content": self.content,
            "confidence": self.confidence,
            "evidence_message_ids": list(self.evidence_message_ids),
        }


@dataclass(frozen=True, slots=True)
class TaskNoteDetectionResult:
    notes: tuple[TaskNoteCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {"notes": [item.to_dict() for item in self.notes]}


def task_note_detection_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "maxItems": MAX_NOTE_CANDIDATES,
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer", "minimum": 1},
                        "note_type": {
                            "type": "string",
                            "enum": [item.value for item in TaskNoteType],
                        },
                        "content": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "evidence_message_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                    "required": sorted(NOTE_CANDIDATE_FIELDS),
                    "additionalProperties": False,
                },
            }
        },
        "required": sorted(NOTE_FIELDS),
        "additionalProperties": False,
    }


def parse_task_note_detection_json(
    payload: str,
    context: LifecycleDetectionContext,
) -> TaskNoteDetectionResult:
    try:
        data = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_constant(value),
        )
    except (json.JSONDecodeError, TaskNoteOutputError) as exc:
        raise TaskNoteOutputError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or frozenset(data) != NOTE_FIELDS:
        actual = set(data) if isinstance(data, dict) else set()
        raise TaskNoteOutputError(
            "output fields do not match contract; "
            f"missing={sorted(NOTE_FIELDS - actual)}, "
            f"extra={sorted(actual - NOTE_FIELDS)}"
        )
    raw_notes = data["notes"]
    if not isinstance(raw_notes, list):
        raise TaskNoteOutputError("notes must be an array")
    if len(raw_notes) > MAX_NOTE_CANDIDATES:
        raise TaskNoteOutputError("notes must contain at most one item")

    tasks = {item.task_id: item for item in context.tasks}
    message_ids = {
        message.message_id for message in context.conversation.messages
    }
    focus_ids = set(context.conversation.focus_message_ids)
    candidates: list[TaskNoteCandidate] = []
    for index, raw in enumerate(raw_notes):
        if not isinstance(raw, dict) or frozenset(raw) != NOTE_CANDIDATE_FIELDS:
            actual = set(raw) if isinstance(raw, dict) else set()
            raise TaskNoteOutputError(
                f"note[{index}] fields do not match contract; "
                f"missing={sorted(NOTE_CANDIDATE_FIELDS - actual)}, "
                f"extra={sorted(actual - NOTE_CANDIDATE_FIELDS)}"
            )
        task_id = _positive_int(raw["task_id"], f"note[{index}].task_id")
        if task_id not in tasks:
            raise TaskNoteOutputError(
                f"note[{index}].task_id is not an authorized open task"
            )
        try:
            note_type = TaskNoteType(str(raw["note_type"]).strip())
        except (TypeError, ValueError) as exc:
            raise TaskNoteOutputError(
                f"note[{index}].note_type is invalid"
            ) from exc
        content = raw["content"]
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content.strip()) > MAX_NOTE_CONTENT_LENGTH
        ):
            raise TaskNoteOutputError(
                f"note[{index}].content must contain 1-{MAX_NOTE_CONTENT_LENGTH} characters"
            )
        confidence = raw["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise TaskNoteOutputError(f"note[{index}].confidence is invalid")
        evidence = raw["evidence_message_ids"]
        if not isinstance(evidence, list) or not evidence:
            raise TaskNoteOutputError(
                f"note[{index}].evidence_message_ids must be non-empty"
            )
        evidence_ids: list[str] = []
        for message_id in evidence:
            if (
                not isinstance(message_id, str)
                or not message_id.strip()
                or message_id not in message_ids
            ):
                raise TaskNoteOutputError(
                    f"note[{index}] evidence contains an unknown message"
                )
            if message_id not in evidence_ids:
                evidence_ids.append(message_id)
        if not focus_ids.issubset(set(evidence_ids)):
            raise TaskNoteOutputError(
                f"note[{index}] evidence must include the trigger message"
            )
        candidates.append(
            TaskNoteCandidate(
                task_id=task_id,
                note_type=note_type,
                content=content.strip(),
                confidence=float(confidence),
                evidence_message_ids=tuple(evidence_ids),
            )
        )
    return TaskNoteDetectionResult(notes=tuple(candidates))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TaskNoteOutputError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise TaskNoteOutputError(f"non-finite JSON number: {value}")


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TaskNoteOutputError(f"{field} must be a positive integer")
    return value
