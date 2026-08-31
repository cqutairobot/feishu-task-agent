"""Stable identities and keys for lifecycle transitions made by the system."""

from __future__ import annotations

from datetime import datetime, timezone


SYSTEM_REMINDER_ACTOR_OPEN_ID = "__system_reminder_worker__"
SYSTEM_REMINDER_ACTOR_NAME = "系统（截止时间检查）"
SYSTEM_ACTOR_TENANT_KEY = "__system__"


def overdue_transition_key(task_id: int, deadline: datetime) -> str:
    """Return one readable, stable idempotency key per deadline version."""

    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise ValueError("task_id must be a positive integer")
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("deadline must include timezone information")
    utc_deadline = deadline.astimezone(timezone.utc)
    return (
        f"system:overdue:{task_id}:"
        f"{utc_deadline.isoformat(timespec='microseconds')}"
    )
