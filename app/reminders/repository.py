"""Transactional, idempotent reminder planning and cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import ReminderSettings
from app.database.engine import session_scope
from app.database.models import Chat, ChatSettings, Message, Task, TaskReminder
from app.reminders.schedule import ReminderKind, reminder_moments


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    LEASED = "leased"
    SENT = "sent"
    CANCELLED = "cancelled"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class ReminderSnapshot:
    reminder_id: int
    task_id: int
    kind: ReminderKind
    deadline_snapshot: datetime
    scheduled_for: datetime
    available_at: datetime
    status: ReminderStatus
    attempt_count: int
    max_attempts: int
    sent_at: datetime | None
    feishu_message_id: str | None
    delivery_receive_id_type: str | None
    delivery_receive_id: str | None
    last_error_code: str | None
    last_error_message: str | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    recipient_open_id: str
    recipient_name: str


@dataclass(frozen=True, slots=True)
class ReminderSyncResult:
    tasks_scanned: int
    task_statuses_changed: int
    reminders_created: int
    reminders_cancelled: int
    active_reminders: int
    synced_at: datetime


@dataclass(frozen=True, slots=True)
class ReminderLease:
    reminder_id: int
    task_id: int
    kind: ReminderKind
    deadline: datetime
    scheduled_for: datetime
    attempt: int
    max_attempts: int
    worker_id: str
    lease_expires_at: datetime
    chat_id: str
    owner_open_id: str
    owner_name: str
    title: str
    task_status: str
    owner_private_chat_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReminderFailureResult:
    reminder_id: int
    status: ReminderStatus
    retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class _TaskSyncResult:
    status_changed: bool
    created: int
    cancelled: int


class ReminderRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: ReminderSettings = ReminderSettings(),
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    def sync_all(
        self, *, synced_at: datetime | None = None
    ) -> ReminderSyncResult:
        synced_at = _aware_utc(
            synced_at or datetime.now(timezone.utc), "synced_at"
        )
        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            task_count, changed, created, cancelled = (
                _synchronize_all_in_session(
                    session,
                    synced_at=synced_at,
                    settings=self._settings,
                )
            )
            session.flush()
            active = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.status.in_(
                        (
                            ReminderStatus.SCHEDULED.value,
                            ReminderStatus.LEASED.value,
                        )
                    )
                )
            ) or 0
            return ReminderSyncResult(
                tasks_scanned=task_count,
                task_statuses_changed=changed,
                reminders_created=created,
                reminders_cancelled=cancelled,
                active_reminders=active,
                synced_at=synced_at,
            )

    def sync_task(
        self,
        task_id: int,
        *,
        synced_at: datetime | None = None,
    ) -> ReminderSyncResult:
        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
        ):
            raise ValueError("task_id must be a positive integer")
        synced_at = _aware_utc(
            synced_at or datetime.now(timezone.utc), "synced_at"
        )
        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} does not exist")
            result = sync_task_reminders_in_session(
                session,
                task,
                synced_at=synced_at,
                settings=self._settings,
            )
            session.flush()
            active = session.scalar(
                select(func.count(TaskReminder.id)).where(
                    TaskReminder.task_id == task_id,
                    TaskReminder.status.in_(
                        (
                            ReminderStatus.SCHEDULED.value,
                            ReminderStatus.LEASED.value,
                        )
                    ),
                )
            ) or 0
            return ReminderSyncResult(
                tasks_scanned=1,
                task_statuses_changed=int(result.status_changed),
                reminders_created=result.created,
                reminders_cancelled=result.cancelled,
                active_reminders=active,
                synced_at=synced_at,
            )

    def list_for_task(self, task_id: int) -> tuple[ReminderSnapshot, ...]:
        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
        ):
            raise ValueError("task_id must be a positive integer")
        with session_scope(self._session_factory) as session:
            reminders = session.scalars(
                select(TaskReminder)
                .where(TaskReminder.task_id == task_id)
                .order_by(TaskReminder.scheduled_for, TaskReminder.id)
            )
            return tuple(_snapshot(reminder) for reminder in reminders)

    def find_private_chat_id(self, owner_open_id: str) -> str | None:
        """Return the newest known bot P2P chat for one exact user."""

        owner_open_id = _required_text(
            owner_open_id, "owner_open_id", maximum=128
        )
        with session_scope(self._session_factory) as session:
            return _find_private_chat_id_in_session(session, owner_open_id)

    def claim_due(
        self,
        worker_id: str,
        *,
        claimed_at: datetime,
        lease_duration: timedelta = timedelta(minutes=2),
        reminder_id: int | None = None,
    ) -> ReminderLease | None:
        worker_id = _required_text(worker_id, "worker_id", maximum=128)
        claimed_at = _aware_utc(claimed_at, "claimed_at")
        if lease_duration < timedelta(seconds=10) or lease_duration > timedelta(
            hours=1
        ):
            raise ValueError(
                "lease_duration must be between 10 seconds and 1 hour"
            )
        if reminder_id is not None and (
            isinstance(reminder_id, bool)
            or not isinstance(reminder_id, int)
            or reminder_id < 1
        ):
            raise ValueError("reminder_id must be a positive integer")

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            _synchronize_all_in_session(
                session,
                synced_at=claimed_at,
                settings=self._settings,
            )
            _recover_expired_leases(session, recovered_at=claimed_at)
            _cancel_superseded_due(session, cancelled_at=claimed_at)
            conditions = [
                TaskReminder.status == ReminderStatus.SCHEDULED.value,
                TaskReminder.scheduled_for <= claimed_at,
                TaskReminder.available_at <= claimed_at,
                Task.status.in_(("todo", "overdue")),
                Task.deadline == TaskReminder.deadline_snapshot,
            ]
            if reminder_id is not None:
                conditions.append(TaskReminder.id == reminder_id)
            row = session.execute(
                select(TaskReminder, Task)
                .join(Task, Task.id == TaskReminder.task_id)
                .where(*conditions)
                .order_by(TaskReminder.scheduled_for, TaskReminder.id)
                .limit(1)
            ).one_or_none()
            if row is None:
                return None
            reminder, task = row
            reminder.status = ReminderStatus.LEASED.value
            reminder.attempt_count += 1
            reminder.worker_id = worker_id
            reminder.leased_at = claimed_at
            reminder.lease_expires_at = claimed_at + lease_duration
            reminder.updated_at = claimed_at
            session.flush()
            return ReminderLease(
                reminder_id=reminder.id,
                task_id=task.id,
                kind=ReminderKind(reminder.kind),
                deadline=reminder.deadline_snapshot,
                scheduled_for=reminder.scheduled_for,
                attempt=reminder.attempt_count,
                max_attempts=reminder.max_attempts,
                worker_id=worker_id,
                lease_expires_at=reminder.lease_expires_at,
                chat_id=task.chat_id,
                owner_open_id=reminder.recipient_open_id,
                owner_name=reminder.recipient_name_snapshot,
                title=task.title,
                task_status=task.status,
                owner_private_chat_id=_find_private_chat_id_in_session(
                    session, reminder.recipient_open_id
                ),
            )

    def mark_sent(
        self,
        lease: ReminderLease,
        *,
        feishu_message_id: str,
        receive_id_type: str,
        receive_id: str,
        sent_at: datetime,
        private_error_code: str | None = None,
        private_error_message: str | None = None,
    ) -> ReminderSnapshot:
        sent_at = _aware_utc(sent_at, "sent_at")
        feishu_message_id = _required_text(
            feishu_message_id, "feishu_message_id", maximum=128
        )
        if receive_id_type not in {"open_id", "chat_id"}:
            raise ValueError("receive_id_type must be open_id or chat_id")
        receive_id = _required_text(receive_id, "receive_id", maximum=128)
        private_error_code = _optional_text(
            private_error_code, "private_error_code", maximum=64
        )
        private_error_message = _optional_text(
            private_error_message, "private_error_message", maximum=2_000
        )
        with session_scope(self._session_factory) as session:
            reminder = _require_owned_lease(session, lease, active_at=sent_at)
            reminder.status = ReminderStatus.SENT.value
            reminder.worker_id = None
            reminder.leased_at = None
            reminder.lease_expires_at = None
            reminder.sent_at = sent_at
            reminder.feishu_message_id = feishu_message_id
            reminder.delivery_receive_id_type = receive_id_type
            reminder.delivery_receive_id = receive_id
            reminder.last_error_code = private_error_code
            reminder.last_error_message = private_error_message
            reminder.updated_at = sent_at
            session.flush()
            return _snapshot(reminder)

    def fail(
        self,
        lease: ReminderLease,
        *,
        error_code: str,
        error_message: str,
        failed_at: datetime,
        retry_delay: timedelta,
    ) -> ReminderFailureResult:
        failed_at = _aware_utc(failed_at, "failed_at")
        error_code = _required_text(error_code, "error_code", maximum=64)
        error_message = _required_text(
            error_message, "error_message", maximum=2_000
        )
        if retry_delay < timedelta(0) or retry_delay > timedelta(days=1):
            raise ValueError("retry_delay must be between zero and one day")
        with session_scope(self._session_factory) as session:
            reminder = _require_owned_lease(
                session, lease, active_at=failed_at
            )
            reminder.worker_id = None
            reminder.leased_at = None
            reminder.lease_expires_at = None
            reminder.last_error_code = error_code
            reminder.last_error_message = error_message
            reminder.updated_at = failed_at
            if reminder.attempt_count >= reminder.max_attempts:
                reminder.status = ReminderStatus.DEAD.value
                retry_at = None
            else:
                reminder.status = ReminderStatus.SCHEDULED.value
                reminder.available_at = failed_at + retry_delay
                retry_at = reminder.available_at
            session.flush()
            return ReminderFailureResult(
                reminder_id=reminder.id,
                status=ReminderStatus(reminder.status),
                retry_at=retry_at,
            )


def _synchronize_all_in_session(
    session: Session,
    *,
    synced_at: datetime,
    settings: ReminderSettings,
) -> tuple[int, int, int, int]:
    tasks = tuple(session.scalars(select(Task).order_by(Task.id)))
    changed = 0
    created = 0
    cancelled = 0
    for task in tasks:
        result = sync_task_reminders_in_session(
            session,
            task,
            synced_at=synced_at,
            settings=settings,
        )
        changed += int(result.status_changed)
        created += result.created
        cancelled += result.cancelled
    session.flush()
    return len(tasks), changed, created, cancelled


def _find_private_chat_id_in_session(
    session: Session, owner_open_id: str
) -> str | None:
    return session.scalar(
        select(Message.chat_id)
        .join(Chat, Chat.chat_id == Message.chat_id)
        .where(
            Message.sender_open_id == owner_open_id,
            Chat.chat_type == "p2p",
        )
        .order_by(Message.message_created_at.desc(), Message.id.desc())
        .limit(1)
    )


def _recover_expired_leases(
    session: Session, *, recovered_at: datetime
) -> None:
    expired = tuple(
        session.scalars(
            select(TaskReminder).where(
                TaskReminder.status == ReminderStatus.LEASED.value,
                TaskReminder.lease_expires_at <= recovered_at,
            )
        )
    )
    for reminder in expired:
        reminder.worker_id = None
        reminder.leased_at = None
        reminder.lease_expires_at = None
        reminder.last_error_code = "lease_expired"
        reminder.last_error_message = (
            "previous reminder worker lease expired before completion"
        )
        reminder.updated_at = recovered_at
        if reminder.attempt_count >= reminder.max_attempts:
            reminder.status = ReminderStatus.DEAD.value
        else:
            reminder.status = ReminderStatus.SCHEDULED.value
            reminder.available_at = recovered_at


def _cancel_superseded_due(
    session: Session, *, cancelled_at: datetime
) -> None:
    due = tuple(
        session.scalars(
            select(TaskReminder)
            .where(
                TaskReminder.status == ReminderStatus.SCHEDULED.value,
                TaskReminder.scheduled_for <= cancelled_at,
            )
            .order_by(TaskReminder.task_id, TaskReminder.scheduled_for)
        )
    )
    grouped: dict[tuple[int, str, datetime], list[TaskReminder]] = {}
    for reminder in due:
        grouped.setdefault(
            (
                reminder.task_id,
                reminder.recipient_open_id,
                reminder.deadline_snapshot,
            ),
            [],
        ).append(reminder)
    priority = {
        ReminderKind.DUE_72H.value: 1,
        ReminderKind.DUE_24H.value: 2,
        ReminderKind.DUE_TODAY.value: 3,
        ReminderKind.OVERDUE.value: 4,
    }
    for reminders in grouped.values():
        winner = max(reminders, key=lambda item: priority[item.kind])
        for reminder in reminders:
            if reminder.id == winner.id:
                continue
            reminder.status = ReminderStatus.CANCELLED.value
            reminder.cancelled_at = cancelled_at
            reminder.cancel_reason = f"superseded_by_{winner.kind}"
            reminder.updated_at = cancelled_at


def _require_owned_lease(
    session: Session,
    lease: ReminderLease,
    *,
    active_at: datetime,
) -> TaskReminder:
    # Expiry makes a lease eligible for recovery by another claimant; it does
    # not by itself revoke ownership. The worker may still finish safely until
    # a new transaction actually recovers/re-leases the row. worker_id and the
    # monotonically increasing attempt number reject the old worker afterward.
    _aware_utc(active_at, "active_at")
    reminder = session.get(TaskReminder, lease.reminder_id)
    if (
        reminder is None
        or reminder.status != ReminderStatus.LEASED.value
        or reminder.worker_id != lease.worker_id
        or reminder.attempt_count != lease.attempt
        or reminder.lease_expires_at is None
    ):
        raise ValueError("reminder lease is no longer active or owned")
    return reminder


def sync_task_reminders_in_session(
    session: Session,
    task: Task,
    *,
    synced_at: datetime,
    settings: ReminderSettings,
) -> _TaskSyncResult:
    """Synchronize one task inside its caller's transaction."""

    synced_at = _aware_utc(synced_at, "synced_at")
    status_changed = False
    if (
        task.status == "todo"
        and task.deadline is not None
        and task.deadline <= synced_at
    ):
        task.status = "overdue"
        task.updated_at = synced_at
        status_changed = True

    existing = tuple(
        session.scalars(
            select(TaskReminder)
            .where(TaskReminder.task_id == task.id)
            .order_by(TaskReminder.id)
        )
    )
    chat_settings = session.get(ChatSettings, task.chat_id)
    desired = _desired_schedule(task, settings, chat_settings)
    assignees = _responsible_members(task)
    desired_by_key = {
        (assignee[0], kind.value, task.deadline): scheduled_for
        for assignee in assignees
        for kind, scheduled_for in desired
    }
    cancelled = 0
    for reminder in existing:
        if reminder.status not in {
            ReminderStatus.SCHEDULED.value,
            ReminderStatus.LEASED.value,
        }:
            continue
        key = (
            reminder.recipient_open_id,
            reminder.kind,
            reminder.deadline_snapshot,
        )
        expected_time = desired_by_key.get(key)
        if expected_time is not None:
            if reminder.scheduled_for != expected_time:
                reminder.status = ReminderStatus.SCHEDULED.value
                reminder.worker_id = None
                reminder.leased_at = None
                reminder.lease_expires_at = None
                reminder.scheduled_for = expected_time
                reminder.available_at = expected_time
                reminder.updated_at = synced_at
            continue
        reminder.status = ReminderStatus.CANCELLED.value
        reminder.worker_id = None
        reminder.leased_at = None
        reminder.lease_expires_at = None
        reminder.cancelled_at = synced_at
        reminder.cancel_reason = _cancellation_reason(
            task, reminder, chat_settings
        )
        reminder.updated_at = synced_at
        cancelled += 1

    existing_by_unique_key = {
        (
            reminder.recipient_open_id,
            reminder.kind,
            reminder.deadline_snapshot,
        ): reminder
        for reminder in existing
    }
    created = 0
    for recipient_open_id, recipient_name in assignees:
        for kind, scheduled_for in desired:
            unique_key = (
                recipient_open_id,
                kind.value,
                task.deadline,
            )
            existing_reminder = existing_by_unique_key.get(unique_key)
            if existing_reminder is not None:
                if (
                    existing_reminder.status
                    == ReminderStatus.CANCELLED.value
                    and not _was_superseded_by_more_urgent_stage(
                        existing_reminder
                    )
                ):
                    existing_reminder.recipient_name_snapshot = recipient_name
                    existing_reminder.scheduled_for = scheduled_for
                    existing_reminder.available_at = scheduled_for
                    existing_reminder.status = ReminderStatus.SCHEDULED.value
                    existing_reminder.attempt_count = 0
                    existing_reminder.max_attempts = settings.max_attempts
                    existing_reminder.worker_id = None
                    existing_reminder.leased_at = None
                    existing_reminder.lease_expires_at = None
                    existing_reminder.cancelled_at = None
                    existing_reminder.cancel_reason = None
                    existing_reminder.last_error_code = None
                    existing_reminder.last_error_message = None
                    existing_reminder.updated_at = synced_at
                    created += 1
                continue
            new_reminder = TaskReminder(
                task_id=task.id,
                recipient_open_id=recipient_open_id,
                recipient_name_snapshot=recipient_name,
                kind=kind.value,
                deadline_snapshot=task.deadline,
                scheduled_for=scheduled_for,
                available_at=scheduled_for,
                status=ReminderStatus.SCHEDULED.value,
                attempt_count=0,
                max_attempts=settings.max_attempts,
                created_at=synced_at,
                updated_at=synced_at,
            )
            session.add(new_reminder)
            existing_by_unique_key[unique_key] = new_reminder
            created += 1
    return _TaskSyncResult(
        status_changed=status_changed,
        created=created,
        cancelled=cancelled,
    )


def _was_superseded_by_more_urgent_stage(
    reminder: TaskReminder,
) -> bool:
    """Keep a collapsed reminder stage terminal for the same plan.

    ``claim_due`` cancels stale due stages before leasing the most urgent one.
    The next global synchronization must not resurrect those stale stages after
    the winner has been sent, otherwise the worker delivers every missed stage
    one by one.
    """

    return bool(
        reminder.cancel_reason
        and reminder.cancel_reason.startswith("superseded_by_")
    )


def _desired_schedule(
    task: Task,
    settings: ReminderSettings,
    chat_settings: ChatSettings | None,
) -> tuple[tuple[ReminderKind, datetime], ...]:
    if task.deadline is None:
        return ()
    enabled = _enabled_reminder_kinds(chat_settings)
    moments = tuple(
        moment
        for moment in reminder_moments(
            task.deadline,
            settings,
            due_72h_offset_hours=(
                72
                if chat_settings is None
                else chat_settings.reminder_due_72h_offset_hours
            ),
            due_24h_offset_hours=(
                24
                if chat_settings is None
                else chat_settings.reminder_due_24h_offset_hours
            ),
            due_day_hour=(
                settings.due_day_hour
                if chat_settings is None
                else chat_settings.reminder_due_today_hour
            ),
            overdue_grace_minutes=(
                settings.overdue_grace_minutes
                if chat_settings is None
                else chat_settings.reminder_overdue_grace_minutes
            ),
        )
        if moment.kind in enabled
    )
    if task.status == "todo":
        return tuple((moment.kind, moment.scheduled_for) for moment in moments)
    if task.status == "overdue":
        return tuple(
            (moment.kind, moment.scheduled_for)
            for moment in moments
            if moment.kind is ReminderKind.OVERDUE
        )
    return ()


def _cancellation_reason(
    task: Task,
    reminder: TaskReminder,
    chat_settings: ChatSettings | None,
) -> str:
    if task.deadline is None:
        return "task_has_no_deadline"
    if reminder.deadline_snapshot != task.deadline:
        return "task_deadline_changed"
    if task.status == "pending":
        return "task_pending_confirmation"
    if task.status == "done":
        return "task_done"
    if task.status == "cancelled":
        return "task_cancelled"
    if ReminderKind(reminder.kind) not in _enabled_reminder_kinds(chat_settings):
        return "reminder_stage_disabled"
    if task.status == "overdue":
        return "task_overdue"
    return "reminder_plan_replaced"


def _enabled_reminder_kinds(
    settings: ChatSettings | None,
) -> frozenset[ReminderKind]:
    if settings is None:
        return frozenset(ReminderKind)
    enabled: set[ReminderKind] = set()
    if settings.reminder_due_72h_enabled:
        enabled.add(ReminderKind.DUE_72H)
    if settings.reminder_due_24h_enabled:
        enabled.add(ReminderKind.DUE_24H)
    if settings.reminder_due_today_enabled:
        enabled.add(ReminderKind.DUE_TODAY)
    if settings.reminder_overdue_enabled:
        enabled.add(ReminderKind.OVERDUE)
    return frozenset(enabled)


def _responsible_members(task: Task) -> tuple[tuple[str, str], ...]:
    if task.assignees:
        return tuple(
            (assignee.open_id, assignee.name_snapshot)
            for assignee in task.assignees
        )
    return ((task.owner_open_id, task.owner_name_snapshot),)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must include timezone information")
    return value.astimezone(timezone.utc)


def _required_text(value: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return value


def _optional_text(
    value: str | None, field: str, *, maximum: int
) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum=maximum)


def _snapshot(reminder: TaskReminder) -> ReminderSnapshot:
    return ReminderSnapshot(
        reminder_id=reminder.id,
        task_id=reminder.task_id,
        kind=ReminderKind(reminder.kind),
        deadline_snapshot=reminder.deadline_snapshot,
        scheduled_for=reminder.scheduled_for,
        available_at=reminder.available_at,
        status=ReminderStatus(reminder.status),
        attempt_count=reminder.attempt_count,
        max_attempts=reminder.max_attempts,
        sent_at=reminder.sent_at,
        feishu_message_id=reminder.feishu_message_id,
        delivery_receive_id_type=reminder.delivery_receive_id_type,
        delivery_receive_id=reminder.delivery_receive_id,
        last_error_code=reminder.last_error_code,
        last_error_message=reminder.last_error_message,
        cancelled_at=reminder.cancelled_at,
        cancel_reason=reminder.cancel_reason,
        recipient_open_id=reminder.recipient_open_id,
        recipient_name=reminder.recipient_name_snapshot,
    )
