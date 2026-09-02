"""Strict provider-neutral contract for natural-language task queries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from typing import Any


QUERY_FIELDS = frozenset(
    {"is_task_query", "scope", "target_name", "status", "confidence"}
)
MAX_TARGET_NAME_LENGTH = 200


class TaskQueryOutputError(ValueError):
    """Raised when model output is malformed or semantically unsafe."""


class TaskQueryScope(StrEnum):
    """The subject whose tasks a natural-language query requests."""

    NONE = "none"
    SELF = "self"
    PERSON = "person"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class TaskQueryIntent:
    """A validated, read-only task query intent."""

    is_query: bool
    scope: TaskQueryScope
    target_name: str | None
    status: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "is_task_query": self.is_query,
            "scope": self.scope.value,
            "target_name": self.target_name,
            "status": self.status,
            "confidence": self.confidence,
        }


def task_query_json_schema() -> dict[str, object]:
    """Return the strict provider-facing schema for query classification."""

    return {
        "type": "object",
        "properties": {
            "is_task_query": {"type": "boolean"},
            "scope": {
                "type": "string",
                "enum": [scope.value for scope in TaskQueryScope],
            },
            "target_name": {
                "anyOf": [
                    {"type": "string", "maxLength": MAX_TARGET_NAME_LENGTH},
                    {"type": "null"},
                ]
            },
            "status": {"type": "string", "enum": ["open"]},
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": sorted(QUERY_FIELDS),
        "additionalProperties": False,
    }


def parse_task_query_json(payload: str) -> TaskQueryIntent:
    """Parse and semantically validate one model query classification."""

    try:
        data = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: _reject_constant(value),
        )
    except (json.JSONDecodeError, TaskQueryOutputError) as exc:
        raise TaskQueryOutputError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict) or frozenset(data) != QUERY_FIELDS:
        actual = set(data) if isinstance(data, dict) else set()
        raise TaskQueryOutputError(
            "output fields do not match contract; "
            f"missing={sorted(QUERY_FIELDS - actual)}, "
            f"extra={sorted(actual - QUERY_FIELDS)}"
        )

    is_query = data["is_task_query"]
    if not isinstance(is_query, bool):
        raise TaskQueryOutputError("is_task_query must be a boolean")
    scope = _scope(data["scope"])
    target_name = _target_name(data["target_name"])
    status = data["status"]
    if status != "open":
        raise TaskQueryOutputError("status must be open")

    if not is_query:
        if scope is not TaskQueryScope.NONE:
            raise TaskQueryOutputError(
                "non-query intent must use scope=none"
            )
        if target_name is not None:
            raise TaskQueryOutputError(
                "non-query intent must not contain target_name"
            )
    elif scope is TaskQueryScope.NONE:
        raise TaskQueryOutputError("query intent must specify a scope")
    elif scope is TaskQueryScope.PERSON and target_name is None:
        raise TaskQueryOutputError(
            "person query intent requires target_name"
        )
    elif scope is not TaskQueryScope.PERSON and target_name is not None:
        raise TaskQueryOutputError(
            "only person query intent may contain target_name"
        )

    return TaskQueryIntent(
        is_query=is_query,
        scope=scope,
        target_name=target_name,
        status=status,
        confidence=_confidence(data["confidence"]),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskQueryOutputError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise TaskQueryOutputError(f"non-finite number is not valid: {value}")


def _scope(value: Any) -> TaskQueryScope:
    if not isinstance(value, str):
        raise TaskQueryOutputError("scope must be a string")
    try:
        return TaskQueryScope(value)
    except ValueError as exc:
        raise TaskQueryOutputError("scope is not supported") from exc


def _target_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskQueryOutputError("target_name must be a string or null")
    value = value.strip()
    if not value or len(value) > MAX_TARGET_NAME_LENGTH:
        raise TaskQueryOutputError(
            f"target_name must contain 1-{MAX_TARGET_NAME_LENGTH} characters"
        )
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskQueryOutputError("confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise TaskQueryOutputError("confidence must be between 0 and 1")
    return result
