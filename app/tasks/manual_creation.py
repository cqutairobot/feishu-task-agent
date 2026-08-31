"""Authorized, audited, and atomic management-page task creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.contracts import MAX_TASK_ASSIGNEES, TaskOwner
from app.config import ReminderSettings
from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatMemberAlias,
    ChatMembership,
    Task,
    TaskAssignee,
    TaskCreationEvent,
    User,
)
from app.notifications.repository import (
    create_task_assignment_notifications_in_session,
)
from app.reminders.repository import sync_task_reminders_in_session
from app.tasks.codes import format_task_code
from app.tasks.repository import TaskStatus


MAX_MANUAL_DESCRIPTION_LENGTH = 2_000


class ManagementTaskCreationError(RuntimeError):
    """Raised when a management task cannot be created safely."""


@dataclass(frozen=True, slots=True)
class ManagementTaskCreationResult:
    task_id: int
    task_code: str
    reminder_count: int
    notification_count: int
    already_created: bool
    created_at: datetime


class ManagementTaskCreationService:
    """Create one exact-group task without fabricating model provenance."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        reminder_settings: ReminderSettings = ReminderSettings(),
    ) -> None:
        self._session_factory = session_factory
        self._reminder_settings = reminder_settings

    def create(
        self,
        *,
        actor_open_id: str,
        request_id: str,
        chat_id: str,
        title: str,
        description: str,
        deadline: datetime | None,
        owner_open_ids: tuple[str, ...],
        created_at: datetime,
    ) -> ManagementTaskCreationResult:
        actor_open_id = _required_text(actor_open_id, "actor_open_id", 128)
        request_id = _required_text(request_id, "request_id", 128)
        chat_id = _required_text(chat_id, "chat_id", 128)
        title = _normalized_title(title)
        description = _description(description)
        owner_open_ids = _owner_ids(owner_open_ids)
        created_at = _aware_utc(created_at, "created_at")
        if deadline is not None:
            deadline = _aware_utc(deadline, "deadline")
            if deadline <= created_at:
                raise ManagementTaskCreationError(
                    "manual task deadline must be in the future"
                )

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            chat = session.get(Chat, chat_id)
            if chat is None or chat.chat_type != "group" or not chat.enabled:
                raise ManagementTaskCreationError(
                    "manual task chat is unavailable"
                )
            if session.scalar(
                select(ChatAdministrator.id)
                .where(
                    ChatAdministrator.chat_id == chat_id,
                    ChatAdministrator.open_id == actor_open_id,
                )
                .limit(1)
            ) is None:
                raise ManagementTaskCreationError(
                    "manual task creation requires an authorized administrator"
                )

            existing = session.scalar(
                select(TaskCreationEvent).where(
                    TaskCreationEvent.request_id == request_id
                )
            )
            if existing is not None:
                _validate_replay(
                    existing,
                    actor_open_id=actor_open_id,
                    chat_id=chat_id,
                    title=title,
                    description=description,
                    deadline=deadline,
                    owner_open_ids=owner_open_ids,
                )
                return ManagementTaskCreationResult(
                    task_id=existing.task_id,
                    task_code=format_task_code(existing.task_id),
                    reminder_count=0,
                    notification_count=0,
                    already_created=True,
                    created_at=existing.created_at,
                )

            owners = _verified_owners(
                session,
                chat_id=chat_id,
                open_ids=owner_open_ids,
            )
            primary = owners[0]
            creator_name = _creator_name(
                session,
                chat_id=chat_id,
                open_id=actor_open_id,
            )
            task = Task(
                chat_id=chat_id,
                owner_open_id=primary.open_id,
                owner_name_snapshot=primary.name,
                created_by_open_id=actor_open_id,
                created_by_name=creator_name,
                created_via="management",
                creator_attribution_basis="explicit_assignment",
                creator_attribution_confidence=1.0,
                title=title,
                normalized_title=title.casefold(),
                description=description,
                deadline=deadline,
                status=TaskStatus.TODO.value,
                confidence=1.0,
                created_at=created_at,
                updated_at=created_at,
            )
            task.assignees = [
                TaskAssignee(
                    open_id=owner.open_id,
                    name_snapshot=owner.name,
                    position=position,
                    created_at=created_at,
                )
                for position, owner in enumerate(owners)
            ]
            session.add(task)
            session.flush()
            session.add(
                TaskCreationEvent(
                    task_id=task.id,
                    actor_open_id=actor_open_id,
                    request_id=request_id,
                    source="management_page",
                    title_snapshot=title,
                    description_snapshot=description,
                    deadline_snapshot=deadline,
                    assignees_json=_owners_json(owners),
                    created_at=created_at,
                )
            )
            notification_count = (
                create_task_assignment_notifications_in_session(
                    session,
                    task,
                    scheduled_for=created_at,
                    max_attempts=self._reminder_settings.max_attempts,
                    reason="created",
                )
            )
            reminder_result = sync_task_reminders_in_session(
                session,
                task,
                synced_at=created_at,
                settings=self._reminder_settings,
            )
            session.flush()
            return ManagementTaskCreationResult(
                task_id=task.id,
                task_code=format_task_code(task.id),
                reminder_count=reminder_result.created,
                notification_count=notification_count,
                already_created=False,
                created_at=created_at,
            )


def _verified_owners(
    session: Session,
    *,
    chat_id: str,
    open_ids: tuple[str, ...],
) -> tuple[TaskOwner, ...]:
    owners: list[TaskOwner] = []
    for open_id in open_ids:
        membership = session.scalar(
            select(ChatMembership.id)
            .where(
                ChatMembership.chat_id == chat_id,
                ChatMembership.open_id == open_id,
                ChatMembership.active.is_(True),
            )
            .limit(1)
        )
        binding = session.scalar(
            select(ChatMemberAlias).where(
                ChatMemberAlias.chat_id == chat_id,
                ChatMemberAlias.open_id == open_id,
            )
        )
        if membership is None or binding is None:
            raise ManagementTaskCreationError(
                "manual task owner must be an active member with a bound task name"
            )
        owners.append(TaskOwner(name=binding.alias, open_id=open_id))
    return tuple(owners)


def _creator_name(
    session: Session,
    *,
    chat_id: str,
    open_id: str,
) -> str | None:
    """Snapshot the administrator's group task name, then Feishu name."""

    alias = session.scalar(
        select(ChatMemberAlias.alias).where(
            ChatMemberAlias.chat_id == chat_id,
            ChatMemberAlias.open_id == open_id,
        )
    )
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    name = session.scalar(select(User.name).where(User.open_id == open_id))
    return name.strip() if isinstance(name, str) and name.strip() else None


def _validate_replay(
    event: TaskCreationEvent,
    *,
    actor_open_id: str,
    chat_id: str,
    title: str,
    description: str,
    deadline: datetime | None,
    owner_open_ids: tuple[str, ...],
) -> None:
    stored_owner_ids = tuple(
        item["open_id"] for item in json.loads(event.assignees_json)
    )
    if (
        event.source != "management_page"
        or event.actor_open_id != actor_open_id
        or event.task.chat_id != chat_id
        or event.title_snapshot != title
        or event.description_snapshot != description
        or event.deadline_snapshot != deadline
        or stored_owner_ids != owner_open_ids
    ):
        raise ManagementTaskCreationError(
            "manual task request was already used for different values"
        )


def _owners_json(owners: tuple[TaskOwner, ...]) -> str:
    return json.dumps(
        [
            {"name": owner.name, "open_id": owner.open_id}
            for owner in owners
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _owner_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or not 1 <= len(value) <= MAX_TASK_ASSIGNEES
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ManagementTaskCreationError(
            "manual task requires 1 to 20 responsible members"
        )
    result = tuple(item.strip() for item in value)
    if len(set(result)) != len(result):
        raise ManagementTaskCreationError(
            "manual task responsible members must be unique"
        )
    return result


def _normalized_title(value: str) -> str:
    if not isinstance(value, str):
        raise ManagementTaskCreationError("manual task title must be text")
    result = " ".join(unicodedata.normalize("NFKC", value).split())
    if not result or len(result) > 200:
        raise ManagementTaskCreationError(
            "manual task title must contain 1 to 200 characters"
        )
    return result


def _description(value: str) -> str:
    if not isinstance(value, str):
        raise ManagementTaskCreationError(
            "manual task description must be text"
        )
    result = unicodedata.normalize("NFKC", value).strip()
    if len(result) > MAX_MANUAL_DESCRIPTION_LENGTH:
        raise ManagementTaskCreationError(
            "manual task description is too long"
        )
    return result


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagementTaskCreationError(f"{field} must not be empty")
    result = value.strip()
    if len(result) > maximum:
        raise ManagementTaskCreationError(f"{field} is too long")
    return result


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ManagementTaskCreationError(
            f"{field} must include timezone information"
        )
    return value.astimezone(timezone.utc)
