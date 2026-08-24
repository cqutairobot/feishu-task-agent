"""Pure Phase 5 reminder schedule calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.config import ReminderSettings


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class ReminderKind(StrEnum):
    DUE_72H = "due_72h"
    DUE_24H = "due_24h"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"


@dataclass(frozen=True, slots=True)
class ReminderMoment:
    kind: ReminderKind
    scheduled_for: datetime


def reminder_moments(
    deadline: datetime,
    settings: ReminderSettings = ReminderSettings(),
    *,
    due_72h_offset_hours: int = 72,
    due_24h_offset_hours: int = 24,
    due_day_hour: int | None = None,
    overdue_grace_minutes: int | None = None,
) -> tuple[ReminderMoment, ...]:
    """Return the four deterministic UTC stages for one exact deadline."""

    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise ValueError("deadline must include timezone information")
    _validate_policy_integer(
        "due_72h_offset_hours", due_72h_offset_hours, minimum=2, maximum=720
    )
    _validate_policy_integer(
        "due_24h_offset_hours", due_24h_offset_hours, minimum=1, maximum=719
    )
    if due_72h_offset_hours <= due_24h_offset_hours:
        raise ValueError("first reminder offset must exceed second reminder offset")
    due_day_hour = settings.due_day_hour if due_day_hour is None else due_day_hour
    overdue_grace_minutes = (
        settings.overdue_grace_minutes
        if overdue_grace_minutes is None
        else overdue_grace_minutes
    )
    _validate_policy_integer("due_day_hour", due_day_hour, minimum=0, maximum=23)
    _validate_policy_integer(
        "overdue_grace_minutes",
        overdue_grace_minutes,
        minimum=0,
        maximum=1_440,
    )
    deadline_utc = deadline.astimezone(timezone.utc)
    if settings.test_mode:
        return (
            ReminderMoment(
                ReminderKind.DUE_72H,
                deadline_utc - timedelta(minutes=6),
            ),
            ReminderMoment(
                ReminderKind.DUE_24H,
                deadline_utc - timedelta(minutes=4),
            ),
            ReminderMoment(
                ReminderKind.DUE_TODAY,
                deadline_utc - timedelta(minutes=2),
            ),
            ReminderMoment(
                ReminderKind.OVERDUE,
                deadline_utc + timedelta(minutes=1),
            ),
        )
    deadline_local = deadline_utc.astimezone(SHANGHAI_TZ)
    due_today_local = datetime.combine(
        deadline_local.date(),
        time(hour=due_day_hour),
        tzinfo=SHANGHAI_TZ,
    )
    if due_today_local > deadline_local:
        due_today_local = deadline_local
    return (
        ReminderMoment(
            ReminderKind.DUE_72H,
            deadline_utc - timedelta(hours=due_72h_offset_hours),
        ),
        ReminderMoment(
            ReminderKind.DUE_24H,
            deadline_utc - timedelta(hours=due_24h_offset_hours),
        ),
        ReminderMoment(
            ReminderKind.DUE_TODAY,
            due_today_local.astimezone(timezone.utc),
        ),
        ReminderMoment(
            ReminderKind.OVERDUE,
            deadline_utc
            + timedelta(minutes=overdue_grace_minutes),
        ),
    )


def _validate_policy_integer(
    field: str,
    value: int,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
