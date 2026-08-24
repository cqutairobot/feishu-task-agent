"""Chat-scoped settings with administrator authorization and audit history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.config import ReminderSettings
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatSettingEvent,
    ChatSettings,
    Task,
)
from app.reminders.repository import sync_task_reminders_in_session


DEFAULT_AUTO_TODO_CONFIDENCE = 0.85
DEFAULT_TASK_SCOPE = "broad"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_REMINDER_DUE_72H_OFFSET_HOURS = 72
DEFAULT_REMINDER_DUE_24H_OFFSET_HOURS = 24
DEFAULT_REMINDER_DUE_TODAY_HOUR = 9
DEFAULT_REMINDER_OVERDUE_GRACE_MINUTES = 1
DEFAULT_MISSING_DEADLINE_OWNER_DELAY_HOURS = 24
DEFAULT_MISSING_DEADLINE_ADMIN_DELAY_HOURS = 72
DEFAULT_ADMINISTRATOR_NOTIFICATION_MODE = "all"


class ChatSettingsError(ValueError):
    """Raised when chat settings are invalid or unauthorized."""


@dataclass(frozen=True, slots=True)
class ChatSettingsSnapshot:
    chat_id: str
    detection_enabled: bool
    auto_todo_confidence: float
    task_scope: str
    timezone: str
    reminder_due_72h_enabled: bool
    reminder_due_24h_enabled: bool
    reminder_due_today_enabled: bool
    reminder_overdue_enabled: bool
    reminder_due_72h_offset_hours: int
    reminder_due_24h_offset_hours: int
    reminder_due_today_hour: int
    reminder_overdue_grace_minutes: int
    missing_deadline_owner_enabled: bool
    missing_deadline_admin_enabled: bool
    missing_deadline_owner_delay_hours: int
    missing_deadline_admin_delay_hours: int
    administrator_notification_mode: str
    administrator_notification_open_ids: tuple[str, ...]
    updated_by_open_id: str | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatSettingEventSnapshot:
    event_id: int
    chat_id: str
    actor_open_id: str
    changed_fields: dict[str, object]
    created_at: datetime


class ChatSettingsRepository:
    """Read and update settings without crossing a chat authorization boundary."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        reminder_settings: ReminderSettings = ReminderSettings(),
    ) -> None:
        self._session_factory = session_factory
        self._reminder_settings = reminder_settings

    def get(self, chat_id: str) -> ChatSettingsSnapshot:
        chat_id = _required_chat_id(chat_id)
        with session_scope(self._session_factory) as session:
            _require_group(session, chat_id)
            return _snapshot(session.get(ChatSettings, chat_id), chat_id)

    def get_for_administrator(
        self, actor_open_id: str, chat_id: str
    ) -> ChatSettingsSnapshot:
        actor_open_id = _required(actor_open_id, "actor_open_id")
        chat_id = _required_chat_id(chat_id)
        with session_scope(self._session_factory) as session:
            _require_group(session, chat_id)
            _require_administrator(session, actor_open_id, chat_id)
            return _snapshot(session.get(ChatSettings, chat_id), chat_id)

    def update_for_administrator(
        self,
        actor_open_id: str,
        chat_id: str,
        *,
        detection_enabled: bool | None = None,
        auto_todo_confidence: float | None = None,
        task_scope: str | None = None,
        reminder_due_72h_enabled: bool | None = None,
        reminder_due_24h_enabled: bool | None = None,
        reminder_due_today_enabled: bool | None = None,
        reminder_overdue_enabled: bool | None = None,
        reminder_due_72h_offset_hours: int | None = None,
        reminder_due_24h_offset_hours: int | None = None,
        reminder_due_today_hour: int | None = None,
        reminder_overdue_grace_minutes: int | None = None,
        missing_deadline_owner_enabled: bool | None = None,
        missing_deadline_admin_enabled: bool | None = None,
        missing_deadline_owner_delay_hours: int | None = None,
        missing_deadline_admin_delay_hours: int | None = None,
        administrator_notification_mode: str | None = None,
        administrator_notification_open_ids: tuple[str, ...] | None = None,
        updated_at: datetime | None = None,
    ) -> ChatSettingsSnapshot:
        actor_open_id = _required(actor_open_id, "actor_open_id")
        chat_id = _required_chat_id(chat_id)
        reminder_updates = {
            "reminder_due_72h_enabled": reminder_due_72h_enabled,
            "reminder_due_24h_enabled": reminder_due_24h_enabled,
            "reminder_due_today_enabled": reminder_due_today_enabled,
            "reminder_overdue_enabled": reminder_overdue_enabled,
        }
        timing_updates = {
            "reminder_due_72h_offset_hours": reminder_due_72h_offset_hours,
            "reminder_due_24h_offset_hours": reminder_due_24h_offset_hours,
            "reminder_due_today_hour": reminder_due_today_hour,
            "reminder_overdue_grace_minutes": reminder_overdue_grace_minutes,
        }
        missing_deadline_switch_updates = {
            "missing_deadline_owner_enabled": missing_deadline_owner_enabled,
            "missing_deadline_admin_enabled": missing_deadline_admin_enabled,
        }
        missing_deadline_timing_updates = {
            "missing_deadline_owner_delay_hours": (
                missing_deadline_owner_delay_hours
            ),
            "missing_deadline_admin_delay_hours": (
                missing_deadline_admin_delay_hours
            ),
        }
        if (
            detection_enabled is None
            and auto_todo_confidence is None
            and task_scope is None
            and all(value is None for value in reminder_updates.values())
            and all(value is None for value in timing_updates.values())
            and all(
                value is None
                for value in missing_deadline_switch_updates.values()
            )
            and all(
                value is None
                for value in missing_deadline_timing_updates.values()
            )
            and administrator_notification_mode is None
            and administrator_notification_open_ids is None
        ):
            raise ChatSettingsError("at least one setting must be provided")
        if detection_enabled is not None and not isinstance(detection_enabled, bool):
            raise ChatSettingsError("detection_enabled must be boolean")
        if auto_todo_confidence is not None:
            if (
                isinstance(auto_todo_confidence, bool)
                or not isinstance(auto_todo_confidence, (int, float))
                or not math.isfinite(float(auto_todo_confidence))
                or not 0 <= float(auto_todo_confidence) <= 1
            ):
                raise ChatSettingsError(
                    "auto_todo_confidence must be between 0 and 1"
                )
        if task_scope is not None and task_scope not in {"broad", "work_only"}:
            raise ChatSettingsError("task_scope must be broad or work_only")
        for field, value in reminder_updates.items():
            if value is not None and not isinstance(value, bool):
                raise ChatSettingsError(f"{field} must be boolean")
        for field, value in missing_deadline_switch_updates.items():
            if value is not None and not isinstance(value, bool):
                raise ChatSettingsError(f"{field} must be boolean")
        _validate_integer_setting(
            "reminder_due_72h_offset_hours",
            reminder_due_72h_offset_hours,
            minimum=2,
            maximum=720,
        )
        _validate_integer_setting(
            "reminder_due_24h_offset_hours",
            reminder_due_24h_offset_hours,
            minimum=1,
            maximum=719,
        )
        _validate_integer_setting(
            "reminder_due_today_hour",
            reminder_due_today_hour,
            minimum=0,
            maximum=23,
        )
        _validate_integer_setting(
            "reminder_overdue_grace_minutes",
            reminder_overdue_grace_minutes,
            minimum=0,
            maximum=1_440,
        )
        _validate_integer_setting(
            "missing_deadline_owner_delay_hours",
            missing_deadline_owner_delay_hours,
            minimum=1,
            maximum=720,
        )
        _validate_integer_setting(
            "missing_deadline_admin_delay_hours",
            missing_deadline_admin_delay_hours,
            minimum=2,
            maximum=2_160,
        )
        if (
            administrator_notification_mode is not None
            and administrator_notification_mode not in {"all", "selected"}
        ):
            raise ChatSettingsError(
                "administrator_notification_mode must be all or selected"
            )
        normalized_notification_open_ids = None
        if administrator_notification_open_ids is not None:
            if not isinstance(administrator_notification_open_ids, tuple):
                raise ChatSettingsError(
                    "administrator_notification_open_ids must be a tuple"
                )
            normalized_notification_open_ids = tuple(
                _required(open_id, "administrator notification Open ID")
                for open_id in administrator_notification_open_ids
            )
            if any(
                len(open_id) > 128
                for open_id in normalized_notification_open_ids
            ):
                raise ChatSettingsError(
                    "administrator notification Open ID is too long"
                )
            if len(normalized_notification_open_ids) > 100:
                raise ChatSettingsError(
                    "administrator notification recipients are too numerous"
                )
            if len(set(normalized_notification_open_ids)) != len(
                normalized_notification_open_ids
            ):
                raise ChatSettingsError(
                    "administrator notification recipients must be unique"
                )
        changed_at = _aware_utc(updated_at or datetime.now(timezone.utc))
        with session_scope(self._session_factory) as session:
            _require_group(session, chat_id)
            _require_administrator(session, actor_open_id, chat_id)
            settings = session.get(ChatSettings, chat_id)
            if settings is None:
                settings = ChatSettings(
                    chat_id=chat_id,
                    detection_enabled=True,
                    auto_todo_confidence=DEFAULT_AUTO_TODO_CONFIDENCE,
                    task_scope=DEFAULT_TASK_SCOPE,
                    timezone=DEFAULT_TIMEZONE,
                    created_at=changed_at,
                    updated_at=changed_at,
                )
                session.add(settings)
                session.flush()
            before = _snapshot(settings, chat_id)
            if detection_enabled is not None:
                settings.detection_enabled = detection_enabled
            if auto_todo_confidence is not None:
                settings.auto_todo_confidence = float(auto_todo_confidence)
            if task_scope is not None:
                settings.task_scope = task_scope
            for field, value in reminder_updates.items():
                if value is not None:
                    setattr(settings, field, value)
            for field, value in timing_updates.items():
                if value is not None:
                    setattr(settings, field, value)
            for field, value in missing_deadline_switch_updates.items():
                if value is not None:
                    setattr(settings, field, value)
            for field, value in missing_deadline_timing_updates.items():
                if value is not None:
                    setattr(settings, field, value)
            next_notification_mode = (
                settings.administrator_notification_mode
                if administrator_notification_mode is None
                else administrator_notification_mode
            )
            next_notification_open_ids = (
                ()
                if (
                    administrator_notification_mode == "all"
                    and normalized_notification_open_ids is None
                )
                else _administrator_notification_open_ids(settings)
                if normalized_notification_open_ids is None
                else normalized_notification_open_ids
            )
            if next_notification_mode == "all":
                if next_notification_open_ids:
                    raise ChatSettingsError(
                        "all administrator notification mode cannot select recipients"
                    )
            else:
                if not next_notification_open_ids:
                    raise ChatSettingsError(
                        "selected administrator notification mode requires recipients"
                    )
                current_administrators = frozenset(
                    session.scalars(
                        select(ChatAdministrator.open_id).where(
                            ChatAdministrator.chat_id == chat_id
                        )
                    )
                )
                if not set(next_notification_open_ids).issubset(
                    current_administrators
                ):
                    raise ChatSettingsError(
                        "administrator notification recipients must be current administrators"
                    )
            settings.administrator_notification_mode = next_notification_mode
            settings.administrator_notification_open_ids_json = json.dumps(
                list(next_notification_open_ids),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if (
                settings.reminder_due_72h_offset_hours
                <= settings.reminder_due_24h_offset_hours
            ):
                raise ChatSettingsError(
                    "first reminder offset must exceed second reminder offset"
                )
            if (
                settings.missing_deadline_admin_delay_hours
                <= settings.missing_deadline_owner_delay_hours
            ):
                raise ChatSettingsError(
                    "administrator escalation must occur after owner reminder"
                )
            settings.updated_by_open_id = actor_open_id
            settings.updated_at = changed_at
            session.flush()
            after = _snapshot(settings, chat_id)
            changed = {
                "before": _audited_values(before),
                "after": _audited_values(after),
            }
            session.add(
                ChatSettingEvent(
                    chat_id=chat_id,
                    actor_open_id=actor_open_id,
                    changed_fields_json=json.dumps(
                        changed, ensure_ascii=False, sort_keys=True
                    ),
                    created_at=changed_at,
                )
            )
            for task in session.scalars(
                select(Task).where(
                    Task.chat_id == chat_id,
                    Task.status.in_(("todo", "overdue")),
                )
            ):
                sync_task_reminders_in_session(
                    session,
                    task,
                    synced_at=changed_at,
                    settings=self._reminder_settings,
                )
            return after

    def list_events_for_administrator(
        self, actor_open_id: str, chat_id: str, *, limit: int = 20
    ) -> tuple[ChatSettingEventSnapshot, ...]:
        actor_open_id = _required(actor_open_id, "actor_open_id")
        chat_id = _required_chat_id(chat_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ChatSettingsError("limit must be between 1 and 100")
        with session_scope(self._session_factory) as session:
            _require_group(session, chat_id)
            _require_administrator(session, actor_open_id, chat_id)
            events = session.scalars(
                select(ChatSettingEvent)
                .where(ChatSettingEvent.chat_id == chat_id)
                .order_by(ChatSettingEvent.created_at.desc(), ChatSettingEvent.id.desc())
                .limit(limit)
            )
            return tuple(
                ChatSettingEventSnapshot(
                    event_id=event.id,
                    chat_id=event.chat_id,
                    actor_open_id=event.actor_open_id,
                    changed_fields=json.loads(event.changed_fields_json),
                    created_at=event.created_at,
                )
                for event in events
            )

    def detection_enabled(self, chat_id: str) -> bool:
        chat_id = _required_chat_id(chat_id)
        with session_scope(self._session_factory) as session:
            settings = session.get(ChatSettings, chat_id)
            return True if settings is None else settings.detection_enabled

    def auto_todo_confidence(self, chat_id: str) -> float:
        chat_id = _required_chat_id(chat_id)
        with session_scope(self._session_factory) as session:
            settings = session.get(ChatSettings, chat_id)
            return (
                DEFAULT_AUTO_TODO_CONFIDENCE
                if settings is None
                else settings.auto_todo_confidence
            )

    def task_scope(self, chat_id: str) -> str:
        chat_id = _required_chat_id(chat_id)
        with session_scope(self._session_factory) as session:
            settings = session.get(ChatSettings, chat_id)
            return DEFAULT_TASK_SCOPE if settings is None else settings.task_scope


def _snapshot(settings: ChatSettings | None, chat_id: str) -> ChatSettingsSnapshot:
    if settings is None:
        return ChatSettingsSnapshot(
            chat_id=chat_id,
            detection_enabled=True,
            auto_todo_confidence=DEFAULT_AUTO_TODO_CONFIDENCE,
            task_scope=DEFAULT_TASK_SCOPE,
            timezone=DEFAULT_TIMEZONE,
            reminder_due_72h_enabled=True,
            reminder_due_24h_enabled=True,
            reminder_due_today_enabled=True,
            reminder_overdue_enabled=True,
            reminder_due_72h_offset_hours=(
                DEFAULT_REMINDER_DUE_72H_OFFSET_HOURS
            ),
            reminder_due_24h_offset_hours=(
                DEFAULT_REMINDER_DUE_24H_OFFSET_HOURS
            ),
            reminder_due_today_hour=DEFAULT_REMINDER_DUE_TODAY_HOUR,
            reminder_overdue_grace_minutes=(
                DEFAULT_REMINDER_OVERDUE_GRACE_MINUTES
            ),
            missing_deadline_owner_enabled=True,
            missing_deadline_admin_enabled=True,
            missing_deadline_owner_delay_hours=(
                DEFAULT_MISSING_DEADLINE_OWNER_DELAY_HOURS
            ),
            missing_deadline_admin_delay_hours=(
                DEFAULT_MISSING_DEADLINE_ADMIN_DELAY_HOURS
            ),
            administrator_notification_mode=(
                DEFAULT_ADMINISTRATOR_NOTIFICATION_MODE
            ),
            administrator_notification_open_ids=(),
            updated_by_open_id=None,
            updated_at=None,
        )
    return ChatSettingsSnapshot(
        chat_id=chat_id,
        detection_enabled=settings.detection_enabled,
        auto_todo_confidence=settings.auto_todo_confidence,
        task_scope=settings.task_scope,
        timezone=settings.timezone,
        reminder_due_72h_enabled=settings.reminder_due_72h_enabled,
        reminder_due_24h_enabled=settings.reminder_due_24h_enabled,
        reminder_due_today_enabled=settings.reminder_due_today_enabled,
        reminder_overdue_enabled=settings.reminder_overdue_enabled,
        reminder_due_72h_offset_hours=(
            settings.reminder_due_72h_offset_hours
        ),
        reminder_due_24h_offset_hours=(
            settings.reminder_due_24h_offset_hours
        ),
        reminder_due_today_hour=settings.reminder_due_today_hour,
        reminder_overdue_grace_minutes=(
            settings.reminder_overdue_grace_minutes
        ),
        missing_deadline_owner_enabled=(
            settings.missing_deadline_owner_enabled
        ),
        missing_deadline_admin_enabled=(
            settings.missing_deadline_admin_enabled
        ),
        missing_deadline_owner_delay_hours=(
            settings.missing_deadline_owner_delay_hours
        ),
        missing_deadline_admin_delay_hours=(
            settings.missing_deadline_admin_delay_hours
        ),
        administrator_notification_mode=(
            settings.administrator_notification_mode
        ),
        administrator_notification_open_ids=(
            _administrator_notification_open_ids(settings)
        ),
        updated_by_open_id=settings.updated_by_open_id,
        updated_at=settings.updated_at,
    )


def _audited_values(settings: ChatSettingsSnapshot) -> dict[str, object]:
    return {
        "detection_enabled": settings.detection_enabled,
        "auto_todo_confidence": settings.auto_todo_confidence,
        "task_scope": settings.task_scope,
        "reminder_due_72h_enabled": settings.reminder_due_72h_enabled,
        "reminder_due_24h_enabled": settings.reminder_due_24h_enabled,
        "reminder_due_today_enabled": settings.reminder_due_today_enabled,
        "reminder_overdue_enabled": settings.reminder_overdue_enabled,
        "reminder_due_72h_offset_hours": (
            settings.reminder_due_72h_offset_hours
        ),
        "reminder_due_24h_offset_hours": (
            settings.reminder_due_24h_offset_hours
        ),
        "reminder_due_today_hour": settings.reminder_due_today_hour,
        "reminder_overdue_grace_minutes": (
            settings.reminder_overdue_grace_minutes
        ),
        "missing_deadline_owner_enabled": (
            settings.missing_deadline_owner_enabled
        ),
        "missing_deadline_admin_enabled": (
            settings.missing_deadline_admin_enabled
        ),
        "missing_deadline_owner_delay_hours": (
            settings.missing_deadline_owner_delay_hours
        ),
        "missing_deadline_admin_delay_hours": (
            settings.missing_deadline_admin_delay_hours
        ),
        "administrator_notification_mode": (
            settings.administrator_notification_mode
        ),
        "administrator_notification_open_ids": list(
            settings.administrator_notification_open_ids
        ),
    }


def _administrator_notification_open_ids(
    settings: ChatSettings,
) -> tuple[str, ...]:
    try:
        raw = json.loads(settings.administrator_notification_open_ids_json)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        return ()
    return tuple(dict.fromkeys(item.strip() for item in raw))


def _validate_integer_setting(
    field: str,
    value: int | None,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ChatSettingsError(
            f"{field} must be an integer between {minimum} and {maximum}"
        )


def _require_group(session: Session, chat_id: str) -> Chat:
    chat = session.get(Chat, chat_id)
    if chat is None or chat.chat_type != "group" or not chat.enabled:
        raise ChatSettingsError("chat must be an enabled group")
    return chat


def _require_administrator(session: Session, actor_open_id: str, chat_id: str) -> None:
    if session.scalar(
        select(ChatAdministrator.id).where(
            ChatAdministrator.chat_id == chat_id,
            ChatAdministrator.open_id == actor_open_id,
        )
    ) is None:
        raise ChatSettingsError("actor must be an administrator of this group")


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChatSettingsError(f"{name} must not be empty")
    return value.strip()


def _required_chat_id(value: str) -> str:
    value = _required(value, "chat_id")
    if len(value) > 128:
        raise ChatSettingsError("chat_id is too long")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChatSettingsError("updated_at must be timezone-aware")
    return value.astimezone(timezone.utc)
