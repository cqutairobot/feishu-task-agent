"""Provider-neutral task detection context and output contracts."""

from app.agent.context import (
    ContextMessage,
    ContextParticipant,
    TaskDetectionContext,
    TaskDetectionContextBuilder,
)
from app.agent.contracts import (
    TaskDetectionResult,
    TaskOutputError,
    parse_task_detection_json,
)

__all__ = [
    "ContextMessage",
    "ContextParticipant",
    "TaskDetectionContext",
    "TaskDetectionContextBuilder",
    "TaskDetectionResult",
    "TaskOutputError",
    "parse_task_detection_json",
]
