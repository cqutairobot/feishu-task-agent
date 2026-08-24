"""Authorization-first, read-only management application API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatAdministratorEvent,
    ChatMemberAlias,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskEvidence,
    TaskLifecycleEvent,
    TaskLifecycleEvidence,
    TaskNotification,
    TaskReminder,
    User,
)
from app.management.access import ChatAdministratorRepository
from app.tasks.codes import TaskCodeError, format_task_code, parse_task_code


TASK_STATUSES = frozenset(
    {"pending", "todo", "overdue", "done", "cancelled"}
)
OPEN_TASK_STATUSES = frozenset({"pending", "todo", "overdue"})


class ManagementAccessDenied(PermissionError):
    """Raised without resource details when an actor lacks group access."""


class ManagementQueryError(ValueError):
    """Raised when a read filter or requested resource is invalid."""


@dataclass(frozen=True, slots=True)
class ManagementChatSnapshot:
    chat_id: str
    chat_name: str | None
    administrator_count: int
    open_task_count: int


@dataclass(frozen=True, slots=True)
class ManagementDashboardSnapshot:
    chat_id: str
    chat_name: str | None
    member_count: int
    administrator_count: int
    total_task_count: int
    pending_count: int
    todo_count: int
    overdue_count: int
    done_count: int
    cancelled_count: int
    open_without_deadline_count: int
    due_next_7_days_count: int


@dataclass(frozen=True, slots=True)
class ManagementMemberSnapshot:
    open_id: str
    name: str
    feishu_name: str
    task_alias: str | None
    is_owner: bool
    is_administrator: bool
    last_synced_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementAdministratorEventSnapshot:
    event_id: int
    action: str
    source: str
    target_open_id: str
    target_name: str
    actor_open_id: str | None
    actor_name: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementAssigneeSnapshot:
    open_id: str
    name: str
    position: int


@dataclass(frozen=True, slots=True)
class ManagementTaskSnapshot:
    task_id: int
    task_code: str
    chat_id: str
    title: str
    description: str
    status: str
    merged_into_task_id: int | None
    merged_into_task_code: str | None
    deadline: datetime | None
    confidence: float
    creation_source: str
    assignees: tuple[ManagementAssigneeSnapshot, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementTaskPage:
    chat_id: str
    total_count: int
    total_pages: int
    page: int
    limit: int
    offset: int
    tasks: tuple[ManagementTaskSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ManagementEvidenceSnapshot:
    message_id: str
    sender_open_id: str
    sender_name: str | None
    content: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementLifecycleSnapshot:
    event_id: int
    action: str
    actor_open_id: str
    authorization_role: str
    previous_status: str
    new_status: str
    deadline_before: datetime | None
    deadline_after: datetime | None
    title_before: str | None
    title_after: str | None
    confidence: float
    provider: str | None
    model: str | None
    evidence_message_ids: tuple[str, ...]
    applied_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementDeliverySnapshot:
    delivery_type: str
    kind: str
    recipient_open_id: str
    status: str
    scheduled_for: datetime
    sent_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class ManagementTaskDetail:
    task: ManagementTaskSnapshot
    evidence: tuple[ManagementEvidenceSnapshot, ...]
    lifecycle: tuple[ManagementLifecycleSnapshot, ...]
    deliveries: tuple[ManagementDeliverySnapshot, ...]


class ManagementReadApi:
    """Read model for a future web adapter; every group read authorizes first."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_chats(self, actor_open_id: str) -> tuple[ManagementChatSnapshot, ...]:
        actor_open_id = _required(actor_open_id, "actor_open_id", 128)
        with session_scope(self._session_factory) as session:
            chats = session.scalars(
                select(Chat)
                .join(
                    ChatAdministrator,
                    ChatAdministrator.chat_id == Chat.chat_id,
                )
                .where(
                    ChatAdministrator.open_id == actor_open_id,
                    Chat.chat_type == "group",
                    Chat.enabled.is_(True),
                )
                .order_by(Chat.name, Chat.chat_id)
            )
            return tuple(self._chat_snapshot(session, chat) for chat in chats)

    def dashboard(
        self,
        actor_open_id: str,
        chat_id: str,
        *,
        now: datetime | None = None,
    ) -> ManagementDashboardSnapshot:
        actor_open_id, chat_id = _scope(actor_open_id, chat_id)
        now = _aware_utc(now or datetime.now(timezone.utc), "now")
        with session_scope(self._session_factory) as session:
            chat = _require_access(session, actor_open_id, chat_id)
            status_counts = dict(
                session.execute(
                    select(Task.status, func.count(Task.id))
                    .where(Task.chat_id == chat_id)
                    .group_by(Task.status)
                ).all()
            )
            no_deadline = session.scalar(
                select(func.count(Task.id)).where(
                    Task.chat_id == chat_id,
                    Task.status.in_(OPEN_TASK_STATUSES),
                    Task.deadline.is_(None),
                )
            ) or 0
            due_next_week = session.scalar(
                select(func.count(Task.id)).where(
                    Task.chat_id == chat_id,
                    Task.status.in_(OPEN_TASK_STATUSES),
                    Task.deadline >= now,
                    Task.deadline <= now + timedelta(days=7),
                )
            ) or 0
            member_count = session.scalar(
                select(func.count(ChatMembership.id)).where(
                    ChatMembership.chat_id == chat_id,
                    ChatMembership.active.is_(True),
                )
            ) or 0
            admin_count = session.scalar(
                select(func.count(ChatAdministrator.id)).where(
                    ChatAdministrator.chat_id == chat_id
                )
            ) or 0
            return ManagementDashboardSnapshot(
                chat_id=chat_id,
                chat_name=chat.name,
                member_count=member_count,
                administrator_count=admin_count,
                total_task_count=sum(status_counts.values()),
                pending_count=status_counts.get("pending", 0),
                todo_count=status_counts.get("todo", 0),
                overdue_count=status_counts.get("overdue", 0),
                done_count=status_counts.get("done", 0),
                cancelled_count=status_counts.get("cancelled", 0),
                open_without_deadline_count=no_deadline,
                due_next_7_days_count=due_next_week,
            )

    def list_members(
        self, actor_open_id: str, chat_id: str
    ) -> tuple[ManagementMemberSnapshot, ...]:
        actor_open_id, chat_id = _scope(actor_open_id, chat_id)
        with session_scope(self._session_factory) as session:
            _require_access(session, actor_open_id, chat_id)
            rows = session.execute(
                select(
                    ChatMembership,
                    ChatMemberAlias.alias,
                    ChatAdministrator.id,
                )
                .outerjoin(
                    ChatMemberAlias,
                    (ChatMemberAlias.chat_id == ChatMembership.chat_id)
                    & (ChatMemberAlias.open_id == ChatMembership.open_id),
                )
                .outerjoin(
                    ChatAdministrator,
                    (ChatAdministrator.chat_id == ChatMembership.chat_id)
                    & (ChatAdministrator.open_id == ChatMembership.open_id),
                )
                .where(
                    ChatMembership.chat_id == chat_id,
                    ChatMembership.active.is_(True),
                )
            )
            snapshots = tuple(
                ManagementMemberSnapshot(
                    open_id=membership.open_id,
                    name=alias or membership.display_name_snapshot,
                    feishu_name=membership.display_name_snapshot,
                    task_alias=alias,
                    is_owner=membership.is_owner,
                    is_administrator=administrator_id is not None,
                    last_synced_at=membership.last_synced_at,
                )
                for membership, alias, administrator_id in rows
            )
            return tuple(
                sorted(
                    snapshots,
                    key=lambda item: (
                        not item.is_owner,
                        not item.is_administrator,
                        item.name,
                        item.open_id,
                    ),
                )
            )

    def list_administrator_events(
        self,
        actor_open_id: str,
        chat_id: str,
        *,
        limit: int = 100,
    ) -> tuple[ManagementAdministratorEventSnapshot, ...]:
        actor_open_id, chat_id = _scope(actor_open_id, chat_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ManagementQueryError("limit must be between 1 and 200")
        with session_scope(self._session_factory) as session:
            _require_access(session, actor_open_id, chat_id)
            events = tuple(
                session.scalars(
                    select(ChatAdministratorEvent)
                    .where(ChatAdministratorEvent.chat_id == chat_id)
                    .order_by(
                        ChatAdministratorEvent.created_at.desc(),
                        ChatAdministratorEvent.id.desc(),
                    )
                    .limit(limit)
                )
            )
            return tuple(
                ManagementAdministratorEventSnapshot(
                    event_id=event.id,
                    action=event.action,
                    source=event.source,
                    target_open_id=event.target_open_id,
                    target_name=_management_name(
                        session, chat_id, event.target_open_id
                    ),
                    actor_open_id=event.actor_open_id,
                    actor_name=(
                        _management_name(
                            session, chat_id, event.actor_open_id
                        )
                        if event.actor_open_id is not None
                        else None
                    ),
                    created_at=event.created_at,
                )
                for event in events
            )

    def list_tasks(
        self,
        actor_open_id: str,
        chat_id: str,
        *,
        statuses: Iterable[str] = (),
        owner_open_id: str | None = None,
        query: str | None = None,
        missing_deadline: bool | None = None,
        deadline_before: datetime | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> ManagementTaskPage:
        actor_open_id, chat_id = _scope(actor_open_id, chat_id)
        normalized_statuses = _statuses(statuses)
        owner_open_id = _optional(owner_open_id, "owner_open_id", 128)
        query = _optional(query, "query", 100)
        if missing_deadline not in {None, True, False}:
            raise ManagementQueryError("missing_deadline must be boolean or null")
        if deadline_before is not None:
            deadline_before = _aware_utc(deadline_before, "deadline_before")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ManagementQueryError("limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ManagementQueryError("offset must be zero or greater")

        with session_scope(self._session_factory) as session:
            _require_access(session, actor_open_id, chat_id)
            conditions = [Task.chat_id == chat_id]
            if normalized_statuses:
                conditions.append(Task.status.in_(normalized_statuses))
            if owner_open_id is not None:
                conditions.append(
                    or_(
                        Task.owner_open_id == owner_open_id,
                        Task.assignees.any(TaskAssignee.open_id == owner_open_id),
                    )
                )
            if query is not None:
                try:
                    task_code_id = parse_task_code(query)
                except TaskCodeError:
                    task_code_id = None
                if task_code_id is not None:
                    # The chat predicate remains in force, so a valid code from
                    # another group returns an empty result without disclosure.
                    conditions.append(Task.id == task_code_id)
                else:
                    conditions.append(
                        or_(
                            func.lower(Task.title).contains(
                                query.lower(), autoescape=True
                            ),
                            func.lower(Task.description).contains(
                                query.lower(), autoescape=True
                            ),
                        )
                    )
            if missing_deadline is True:
                conditions.append(Task.deadline.is_(None))
            elif missing_deadline is False:
                conditions.append(Task.deadline.is_not(None))
            if deadline_before is not None:
                conditions.append(Task.deadline <= deadline_before)

            total_count = session.scalar(
                select(func.count(Task.id)).where(*conditions)
            ) or 0
            tasks = session.scalars(
                select(Task)
                .where(*conditions)
                .order_by(Task.deadline.is_(None), Task.deadline, Task.id)
                .limit(limit)
                .offset(offset)
            )
            return ManagementTaskPage(
                chat_id=chat_id,
                total_count=total_count,
                total_pages=(total_count + limit - 1) // limit,
                page=(offset // limit) + 1,
                limit=limit,
                offset=offset,
                tasks=tuple(_task_snapshot(task) for task in tasks),
            )

    def task_detail(
        self, actor_open_id: str, chat_id: str, task_id: int
    ) -> ManagementTaskDetail:
        actor_open_id, chat_id = _scope(actor_open_id, chat_id)
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
            raise ManagementQueryError("task_id must be a positive integer")
        with session_scope(self._session_factory) as session:
            _require_access(session, actor_open_id, chat_id)
            task = session.scalar(
                select(Task).where(Task.id == task_id, Task.chat_id == chat_id)
            )
            if task is None:
                raise ManagementQueryError("task does not exist in this chat")
            return ManagementTaskDetail(
                task=_task_snapshot(task),
                evidence=_task_evidence(session, task.id),
                lifecycle=_task_lifecycle(session, task.id),
                deliveries=_task_deliveries(session, task.id),
            )

    @staticmethod
    def _chat_snapshot(
        session: Session, chat: Chat
    ) -> ManagementChatSnapshot:
        admin_count = session.scalar(
            select(func.count(ChatAdministrator.id)).where(
                ChatAdministrator.chat_id == chat.chat_id
            )
        ) or 0
        open_count = session.scalar(
            select(func.count(Task.id)).where(
                Task.chat_id == chat.chat_id,
                Task.status.in_(OPEN_TASK_STATUSES),
            )
        ) or 0
        return ManagementChatSnapshot(
            chat_id=chat.chat_id,
            chat_name=chat.name,
            administrator_count=admin_count,
            open_task_count=open_count,
        )


def _require_access(session: Session, actor_open_id: str, chat_id: str) -> Chat:
    if not ChatAdministratorRepository.is_administrator_in_session(
        session, chat_id, actor_open_id
    ):
        raise ManagementAccessDenied("not authorized for this chat")
    chat = session.get(Chat, chat_id)
    if chat is None or chat.chat_type != "group" or not chat.enabled:
        raise ManagementAccessDenied("not authorized for this chat")
    return chat


def _management_name(session: Session, chat_id: str, open_id: str) -> str:
    alias = session.scalar(
        select(ChatMemberAlias.alias).where(
            ChatMemberAlias.chat_id == chat_id,
            ChatMemberAlias.open_id == open_id,
        )
    )
    if alias:
        return alias
    membership_name = session.scalar(
        select(ChatMembership.display_name_snapshot).where(
            ChatMembership.chat_id == chat_id,
            ChatMembership.open_id == open_id,
        )
    )
    if membership_name:
        return membership_name
    user_name = session.scalar(select(User.name).where(User.open_id == open_id))
    return user_name or open_id


def _task_snapshot(task: Task) -> ManagementTaskSnapshot:
    assignees = tuple(
        ManagementAssigneeSnapshot(
            open_id=item.open_id,
            name=item.name_snapshot,
            position=item.position,
        )
        for item in task.assignees
    )
    if not assignees:
        assignees = (
            ManagementAssigneeSnapshot(
                open_id=task.owner_open_id,
                name=task.owner_name_snapshot,
                position=0,
            ),
        )
    return ManagementTaskSnapshot(
        task_id=task.id,
        task_code=format_task_code(task.id),
        chat_id=task.chat_id,
        title=task.title,
        description=task.description,
        status="merged" if task.merged_into_task_id is not None else task.status,
        merged_into_task_id=task.merged_into_task_id,
        merged_into_task_code=(
            format_task_code(task.merged_into_task_id)
            if task.merged_into_task_id is not None
            else None
        ),
        deadline=task.deadline,
        confidence=task.confidence,
        creation_source=(
            "management_page"
            if task.creation_event is not None
            else "model_detection"
        ),
        assignees=assignees,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _task_evidence(
    session: Session, task_id: int
) -> tuple[ManagementEvidenceSnapshot, ...]:
    rows = session.scalars(
        select(Message)
        .join(TaskEvidence, TaskEvidence.message_db_id == Message.id)
        .where(TaskEvidence.task_id == task_id)
        .order_by(TaskEvidence.id)
    )
    return tuple(
        ManagementEvidenceSnapshot(
            message_id=message.message_id,
            sender_open_id=message.sender_open_id,
            sender_name=message.sender_name_snapshot,
            content=message.text_content,
            created_at=message.message_created_at,
        )
        for message in rows
    )


def _task_lifecycle(
    session: Session, task_id: int
) -> tuple[ManagementLifecycleSnapshot, ...]:
    events = session.scalars(
        select(TaskLifecycleEvent)
        .where(TaskLifecycleEvent.task_id == task_id)
        .order_by(TaskLifecycleEvent.applied_at, TaskLifecycleEvent.id)
    )
    snapshots: list[ManagementLifecycleSnapshot] = []
    for event in events:
        evidence_ids = tuple(
            session.scalars(
                select(Message.message_id)
                .join(
                    TaskLifecycleEvidence,
                    TaskLifecycleEvidence.message_db_id == Message.id,
                )
                .where(TaskLifecycleEvidence.event_id == event.id)
                .order_by(TaskLifecycleEvidence.position)
            )
        )
        snapshots.append(
            ManagementLifecycleSnapshot(
                event_id=event.id,
                action=event.action,
                actor_open_id=event.actor_open_id,
                authorization_role=event.authorization_role,
                previous_status=event.previous_status,
                new_status=event.new_status,
                deadline_before=event.deadline_before,
                deadline_after=event.deadline_after,
                title_before=event.title_before,
                title_after=event.title_after,
                confidence=event.confidence,
                provider=event.provider,
                model=event.model,
                evidence_message_ids=evidence_ids,
                applied_at=event.applied_at,
            )
        )
    return tuple(snapshots)


def _task_deliveries(
    session: Session, task_id: int
) -> tuple[ManagementDeliverySnapshot, ...]:
    reminders = session.scalars(
        select(TaskReminder)
        .where(TaskReminder.task_id == task_id)
        .order_by(TaskReminder.scheduled_for, TaskReminder.id)
    )
    notifications = session.scalars(
        select(TaskNotification)
        .where(TaskNotification.task_id == task_id)
        .order_by(TaskNotification.scheduled_for, TaskNotification.id)
    )
    result = [
        ManagementDeliverySnapshot(
            delivery_type="reminder",
            kind=item.kind,
            recipient_open_id=item.recipient_open_id,
            status=item.status,
            scheduled_for=item.scheduled_for,
            sent_at=item.sent_at,
            cancelled_at=item.cancelled_at,
            cancel_reason=item.cancel_reason,
            last_error_code=item.last_error_code,
        )
        for item in reminders
    ]
    result.extend(
        ManagementDeliverySnapshot(
            delivery_type="notification",
            kind=item.kind,
            recipient_open_id=item.recipient_open_id,
            status=item.status,
            scheduled_for=item.scheduled_for,
            sent_at=item.sent_at,
            cancelled_at=item.cancelled_at,
            cancel_reason=item.cancel_reason,
            last_error_code=item.last_error_code,
        )
        for item in notifications
    )
    return tuple(
        sorted(result, key=lambda item: (item.scheduled_for, item.delivery_type))
    )


def _scope(actor_open_id: str, chat_id: str) -> tuple[str, str]:
    return (
        _required(actor_open_id, "actor_open_id", 128),
        _required(chat_id, "chat_id", 128),
    )


def _statuses(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    normalized = tuple(dict.fromkeys(str(value).strip() for value in values))
    if any(not value or value not in TASK_STATUSES for value in normalized):
        raise ManagementQueryError("task status filter is invalid")
    return normalized


def _required(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagementQueryError(f"{name} must not be empty")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ManagementQueryError(f"{name} is too long")
    return normalized


def _optional(value: str | None, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required(value, name, maximum)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ManagementQueryError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)
