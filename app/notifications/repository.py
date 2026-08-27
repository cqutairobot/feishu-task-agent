"""Durable, idempotent private task-notification queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import ReminderSettings
from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatMemberAlias,
    ChatSettings,
    Message,
    Task,
    TaskLifecycleEvent,
    TaskNotification,
    TaskNotificationDeferredLifecycleEvent,
    TaskNotificationState,
    User,
)
from app.tasks.codes import format_task_code


class TaskNotificationKind(StrEnum):
    TASK_CREATED_ASSIGNEE = "task_created_assignee"
    MISSING_DEADLINE_OWNER = "missing_deadline_owner"
    MISSING_DEADLINE_ADMIN = "missing_deadline_admin"
    TASK_DONE_ADMIN = "task_done_admin"
    TASK_CANCELLED_ADMIN = "task_cancelled_admin"
    TASK_OVERDUE_ADMIN = "task_overdue_admin"
    TASK_RESCHEDULED_ADMIN = "task_rescheduled_admin"
    TASK_DONE_COASSIGNEE = "task_done_coassignee"
    TASK_CANCELLED_COASSIGNEE = "task_cancelled_coassignee"
    TASK_RESCHEDULED_COASSIGNEE = "task_rescheduled_coassignee"
    TASK_RENAMED_ASSIGNEE = "task_renamed_assignee"
    TASK_ASSIGNEE_ADDED = "task_assignee_added"
    TASK_ASSIGNEE_REMOVED = "task_assignee_removed"
    TASK_ASSIGNEES_CHANGED = "task_assignees_changed"
    TASK_INVALIDATED_ASSIGNEE = "task_invalidated_assignee"
    TASK_RENAMED_ADMIN = "task_renamed_admin"
    TASK_REASSIGNED_ADMIN = "task_reassigned_admin"
    TASK_INVALIDATED_ADMIN = "task_invalidated_admin"
    TASK_RESTORED_COASSIGNEE = "task_restored_coassignee"
    TASK_RESTORED_ADMIN = "task_restored_admin"


class TaskNotificationStatus(StrEnum):
    SCHEDULED = "scheduled"
    LEASED = "leased"
    SENT = "sent"
    CANCELLED = "cancelled"
    DEAD = "dead"


ADMINISTRATOR_NOTIFICATION_KINDS = frozenset(
    {
        TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
        TaskNotificationKind.TASK_DONE_ADMIN.value,
        TaskNotificationKind.TASK_CANCELLED_ADMIN.value,
        TaskNotificationKind.TASK_OVERDUE_ADMIN.value,
        TaskNotificationKind.TASK_RESCHEDULED_ADMIN.value,
        TaskNotificationKind.TASK_RENAMED_ADMIN.value,
        TaskNotificationKind.TASK_REASSIGNED_ADMIN.value,
        TaskNotificationKind.TASK_INVALIDATED_ADMIN.value,
        TaskNotificationKind.TASK_RESTORED_ADMIN.value,
    }
)


@dataclass(frozen=True, slots=True)
class TaskNotificationLease:
    notification_id: int
    task_id: int
    kind: TaskNotificationKind
    recipient_open_id: str
    recipient_private_chat_id: str | None
    task_code: str
    owner_open_id: str
    owner_name: str
    title: str
    status_snapshot: str
    deadline: datetime | None
    deadline_before: datetime | None
    task_created_at: datetime
    scheduled_for: datetime
    attempt: int
    max_attempts: int
    worker_id: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class TaskNotificationSyncResult:
    created: int
    cancelled: int


@dataclass(frozen=True, slots=True)
class TaskNotificationFailureResult:
    notification_id: int
    status: TaskNotificationStatus
    retry_at: datetime | None


class TaskNotificationRepository:
    """Plan, lease, and audit private task notifications."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        administrator_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        settings: ReminderSettings = ReminderSettings(),
    ) -> None:
        if any(not item.strip() for item in administrator_open_ids):
            raise ValueError("administrator Open IDs must not be empty")
        if any(not item.strip() for item in allowed_chat_ids):
            raise ValueError("allowed chat IDs must not be empty")
        self._session_factory = session_factory
        self._administrator_open_ids = frozenset(
            item.strip() for item in administrator_open_ids
        )
        self._allowed_chat_ids = frozenset(
            item.strip() for item in allowed_chat_ids
        )
        self._settings = settings

    def sync_all(
        self, *, synced_at: datetime | None = None
    ) -> TaskNotificationSyncResult:
        synced_at = _aware_utc(
            synced_at or datetime.now(timezone.utc), "synced_at"
        )
        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            return _sync_all_in_session(
                session,
                synced_at=synced_at,
                administrator_open_ids=self._administrator_open_ids,
                allowed_chat_ids=self._allowed_chat_ids,
                settings=self._settings,
            )

    def claim_due(
        self,
        worker_id: str,
        *,
        claimed_at: datetime,
        lease_duration: timedelta = timedelta(minutes=2),
        notification_id: int | None = None,
    ) -> TaskNotificationLease | None:
        worker_id = _required_text(worker_id, "worker_id", maximum=128)
        claimed_at = _aware_utc(claimed_at, "claimed_at")
        if lease_duration < timedelta(seconds=10) or lease_duration > timedelta(
            hours=1
        ):
            raise ValueError(
                "lease_duration must be between 10 seconds and 1 hour"
            )
        if notification_id is not None and (
            isinstance(notification_id, bool)
            or not isinstance(notification_id, int)
            or notification_id < 1
        ):
            raise ValueError("notification_id must be a positive integer")

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            _sync_all_in_session(
                session,
                synced_at=claimed_at,
                administrator_open_ids=self._administrator_open_ids,
                allowed_chat_ids=self._allowed_chat_ids,
                settings=self._settings,
            )
            _recover_expired_leases(session, recovered_at=claimed_at)
            conditions = [
                TaskNotification.status
                == TaskNotificationStatus.SCHEDULED.value,
                TaskNotification.scheduled_for <= claimed_at,
                TaskNotification.available_at <= claimed_at,
            ]
            if notification_id is not None:
                conditions.append(TaskNotification.id == notification_id)
            notification = session.scalar(
                select(TaskNotification)
                .where(*conditions)
                .order_by(
                    TaskNotification.scheduled_for,
                    TaskNotification.id,
                )
                .limit(1)
            )
            if notification is None:
                return None
            notification.status = TaskNotificationStatus.LEASED.value
            notification.attempt_count += 1
            notification.worker_id = worker_id
            notification.leased_at = claimed_at
            notification.lease_expires_at = claimed_at + lease_duration
            notification.updated_at = claimed_at
            session.flush()
            task = session.get(Task, notification.task_id)
            if task is None:
                raise RuntimeError(
                    f"task {notification.task_id} disappeared while claiming "
                    "its notification"
                )
            return TaskNotificationLease(
                notification_id=notification.id,
                task_id=notification.task_id,
                kind=TaskNotificationKind(notification.kind),
                recipient_open_id=notification.recipient_open_id,
                recipient_private_chat_id=_find_private_chat_id_in_session(
                    session, notification.recipient_open_id
                ),
                task_code=notification.task_code_snapshot,
                owner_open_id=notification.owner_open_id_snapshot,
                owner_name=notification.owner_name_snapshot,
                title=notification.title_snapshot,
                status_snapshot=notification.status_snapshot,
                deadline=notification.deadline_snapshot,
                deadline_before=notification.deadline_before_snapshot,
                task_created_at=task.created_at,
                scheduled_for=notification.scheduled_for,
                attempt=notification.attempt_count,
                max_attempts=notification.max_attempts,
                worker_id=worker_id,
                lease_expires_at=notification.lease_expires_at,
            )

    def mark_sent(
        self,
        lease: TaskNotificationLease,
        *,
        feishu_message_id: str,
        receive_id_type: str,
        receive_id: str,
        sent_at: datetime,
    ) -> None:
        sent_at = _aware_utc(sent_at, "sent_at")
        feishu_message_id = _required_text(
            feishu_message_id, "feishu_message_id", maximum=128
        )
        if receive_id_type not in {"open_id", "chat_id"}:
            raise ValueError("receive_id_type must be open_id or chat_id")
        receive_id = _required_text(
            receive_id, "receive_id", maximum=128
        )
        with session_scope(self._session_factory) as session:
            notification = _require_owned_lease(
                session, lease, active_at=sent_at
            )
            notification.status = TaskNotificationStatus.SENT.value
            notification.worker_id = None
            notification.leased_at = None
            notification.lease_expires_at = None
            notification.sent_at = sent_at
            notification.feishu_message_id = feishu_message_id
            notification.delivery_receive_id_type = receive_id_type
            notification.delivery_receive_id = receive_id
            notification.last_error_code = None
            notification.last_error_message = None
            notification.updated_at = sent_at

    def fail(
        self,
        lease: TaskNotificationLease,
        *,
        error_code: str,
        error_message: str,
        failed_at: datetime,
        retry_delay: timedelta,
    ) -> TaskNotificationFailureResult:
        failed_at = _aware_utc(failed_at, "failed_at")
        error_code = _required_text(
            error_code, "error_code", maximum=64
        )
        error_message = _required_text(
            error_message, "error_message", maximum=2_000
        )
        if retry_delay < timedelta(0) or retry_delay > timedelta(days=1):
            raise ValueError("retry_delay must be between zero and one day")
        with session_scope(self._session_factory) as session:
            notification = _require_owned_lease(
                session, lease, active_at=failed_at
            )
            notification.worker_id = None
            notification.leased_at = None
            notification.lease_expires_at = None
            notification.last_error_code = error_code
            notification.last_error_message = error_message
            notification.updated_at = failed_at
            if notification.attempt_count >= notification.max_attempts:
                notification.status = TaskNotificationStatus.DEAD.value
                retry_at = None
            else:
                notification.status = TaskNotificationStatus.SCHEDULED.value
                notification.available_at = failed_at + retry_delay
                retry_at = notification.available_at
            return TaskNotificationFailureResult(
                notification_id=notification.id,
                status=TaskNotificationStatus(notification.status),
                retry_at=retry_at,
            )


def create_task_assignment_notifications_in_session(
    session: Session,
    task: Task,
    *,
    scheduled_for: datetime,
    max_attempts: int,
    reason: str,
) -> int:
    """Atomically queue one private assignment notice per responsible member."""

    scheduled_for = _aware_utc(scheduled_for, "scheduled_for")
    if reason not in {"created", "activated"}:
        raise ValueError("assignment notification reason is invalid")
    if task.status not in {"todo", "overdue"}:
        return 0
    responsible_members = _responsible_members(task)
    responsible_names = "、".join(
        name for _open_id, name in responsible_members
    )
    existing_keys = {
        (
            item.task_id,
            item.kind,
            item.recipient_open_id,
            item.dedupe_key,
        )
        for item in session.scalars(
            select(TaskNotification).where(TaskNotification.task_id == task.id)
        )
    }
    created = 0
    for open_id, _name in responsible_members:
        created += _ensure_notification(
            session,
            existing_keys,
            task=task,
            kind=TaskNotificationKind.TASK_CREATED_ASSIGNEE,
            recipient_open_id=open_id,
            dedupe_key=f"assignment:{reason}",
            scheduled_for=scheduled_for,
            status_snapshot=task.status,
            deadline_snapshot=task.deadline,
            max_attempts=max_attempts,
            created_at=scheduled_for,
            owner_open_id_snapshot=task.owner_open_id,
            owner_name_snapshot=responsible_names,
        )
    return created


def _sync_all_in_session(
    session: Session,
    *,
    synced_at: datetime,
    administrator_open_ids: frozenset[str],
    allowed_chat_ids: frozenset[str],
    settings: ReminderSettings,
) -> TaskNotificationSyncResult:
    admitted_chat_ids = allowed_chat_ids
    if allowed_chat_ids:
        admitted_chat_ids = allowed_chat_ids | frozenset(
            session.scalars(
                select(ChatAdministrator.chat_id)
                .join(Chat, Chat.chat_id == ChatAdministrator.chat_id)
                .where(
                    Chat.chat_type == "group",
                    Chat.enabled.is_(True),
                )
                .distinct()
            )
        )
    tasks = tuple(session.scalars(select(Task).order_by(Task.id)))
    tasks_by_id = {
        task.id: task
        for task in tasks
        if not admitted_chat_ids or task.chat_id in admitted_chat_ids
    }
    existing = tuple(
        session.scalars(select(TaskNotification).order_by(TaskNotification.id))
    )
    existing_keys = {
        (
            item.task_id,
            item.kind,
            item.recipient_open_id,
            item.dedupe_key,
        )
        for item in existing
    }
    created = 0

    for task in tasks_by_id.values():
        chat_settings = session.get(ChatSettings, task.chat_id)
        owner_enabled, admin_enabled, owner_delay, admin_delay = (
            _missing_deadline_policy(chat_settings, settings)
        )
        task_administrator_open_ids = _notification_administrator_open_ids(
            session, task.chat_id, administrator_open_ids
        )
        responsible_members = _responsible_members(task)
        responsible_open_ids = {
            open_id for open_id, _name in responsible_members
        }
        responsible_names = "、".join(
            name for _open_id, name in responsible_members
        )
        if task.status == "todo" and task.deadline is None:
            if owner_enabled:
                for owner_open_id, owner_name in responsible_members:
                    created += _ensure_notification(
                        session,
                        existing_keys,
                        task=task,
                        kind=TaskNotificationKind.MISSING_DEADLINE_OWNER,
                        recipient_open_id=owner_open_id,
                        dedupe_key=f"created:{task.created_at.isoformat()}",
                        scheduled_for=task.created_at + owner_delay,
                        status_snapshot=task.status,
                        deadline_snapshot=None,
                        max_attempts=settings.max_attempts,
                        created_at=synced_at,
                        owner_open_id_snapshot=owner_open_id,
                        owner_name_snapshot=owner_name,
                    )
            if admin_enabled:
                for administrator in sorted(task_administrator_open_ids):
                    if administrator in responsible_open_ids:
                        continue
                    created += _ensure_notification(
                        session,
                        existing_keys,
                        task=task,
                        kind=TaskNotificationKind.MISSING_DEADLINE_ADMIN,
                        recipient_open_id=administrator,
                        dedupe_key=f"created:{task.created_at.isoformat()}",
                        scheduled_for=task.created_at + admin_delay,
                        status_snapshot=task.status,
                        deadline_snapshot=None,
                        max_attempts=settings.max_attempts,
                        created_at=synced_at,
                        owner_name_snapshot=responsible_names,
                    )
        if task.status == "overdue" and task.deadline is not None:
            for administrator in sorted(task_administrator_open_ids):
                if administrator in responsible_open_ids:
                    continue
                created += _ensure_notification(
                    session,
                    existing_keys,
                    task=task,
                    kind=TaskNotificationKind.TASK_OVERDUE_ADMIN,
                    recipient_open_id=administrator,
                    dedupe_key=f"deadline:{task.deadline.isoformat()}",
                    scheduled_for=synced_at,
                    status_snapshot=task.status,
                    deadline_snapshot=task.deadline,
                    max_attempts=settings.max_attempts,
                    created_at=synced_at,
                    owner_name_snapshot=responsible_names,
                )

    state = session.get(TaskNotificationState, 1)
    if state is None:
        state = TaskNotificationState(
            id=1,
            last_lifecycle_event_id=0,
            updated_at=synced_at,
        )
        session.add(state)
        session.flush()
    tracked_actions = (
        "complete",
        "cancel",
        "reschedule",
        "rename",
        "reassign",
        "invalidate",
        "restore",
        "merge",
    )
    new_events = tuple(
        session.scalars(
            select(TaskLifecycleEvent)
            .where(
                TaskLifecycleEvent.id > state.last_lifecycle_event_id,
                TaskLifecycleEvent.action.in_(tracked_actions),
            )
            .order_by(TaskLifecycleEvent.id)
        )
    )
    deferred_rows = tuple(
        session.scalars(
            select(TaskNotificationDeferredLifecycleEvent).order_by(
                TaskNotificationDeferredLifecycleEvent.event_id
            )
        )
    )
    deferred_by_id = {item.event_id: item for item in deferred_rows}
    events_by_id = {event.id: event for event in new_events}
    for deferred in deferred_rows:
        event = session.get(TaskLifecycleEvent, deferred.event_id)
        if event is not None and event.action in tracked_actions:
            events_by_id[event.id] = event
        elif event is None:
            session.delete(deferred)
    events = tuple(events_by_id[event_id] for event_id in sorted(events_by_id))
    for event in events:
        task = tasks_by_id.get(event.task_id)
        if task is None:
            if event.id not in deferred_by_id:
                deferred = TaskNotificationDeferredLifecycleEvent(
                    event_id=event.id,
                    deferred_at=synced_at,
                )
                session.add(deferred)
                deferred_by_id[event.id] = deferred
            continue
        if event.action == "merge":
            # Merging cancels the duplicate task's unsent deliveries in the
            # lifecycle transaction; it does not create another status notice.
            deferred = deferred_by_id.pop(event.id, None)
            if deferred is not None:
                session.delete(deferred)
            continue
        task_administrator_open_ids = _notification_administrator_open_ids(
            session, task.chat_id, administrator_open_ids
        )
        responsible_members = _responsible_members(task)
        responsible_names = "、".join(
            name for _open_id, name in responsible_members
        )
        actor_name = _actor_name(session, task, event.actor_open_id)
        if event.action in {"complete", "cancel", "reschedule"}:
            admin_kind = {
                "complete": TaskNotificationKind.TASK_DONE_ADMIN,
                "cancel": TaskNotificationKind.TASK_CANCELLED_ADMIN,
                "reschedule": TaskNotificationKind.TASK_RESCHEDULED_ADMIN,
            }[event.action]
            member_kind = {
                "complete": TaskNotificationKind.TASK_DONE_COASSIGNEE,
                "cancel": TaskNotificationKind.TASK_CANCELLED_COASSIGNEE,
                "reschedule": TaskNotificationKind.TASK_RESCHEDULED_COASSIGNEE,
            }[event.action]
            member_recipients = {
                open_id: member_kind for open_id, _name in responsible_members
            }
        elif event.action == "rename":
            admin_kind = TaskNotificationKind.TASK_RENAMED_ADMIN
            member_recipients = {
                open_id: TaskNotificationKind.TASK_RENAMED_ASSIGNEE
                for open_id, _name in responsible_members
            }
        elif event.action == "invalidate":
            admin_kind = TaskNotificationKind.TASK_INVALIDATED_ADMIN
            member_recipients = {
                open_id: TaskNotificationKind.TASK_INVALIDATED_ASSIGNEE
                for open_id, _name in responsible_members
            }
        elif event.action == "restore":
            admin_kind = TaskNotificationKind.TASK_RESTORED_ADMIN
            member_recipients = {
                open_id: TaskNotificationKind.TASK_RESTORED_COASSIGNEE
                for open_id, _name in responsible_members
            }
        else:
            admin_kind = TaskNotificationKind.TASK_REASSIGNED_ADMIN
            before = dict(_event_assignees(event.assignees_before_json))
            after = dict(_event_assignees(event.assignees_after_json))
            member_recipients = {}
            for open_id in before.keys() | after.keys():
                if open_id not in before:
                    kind = TaskNotificationKind.TASK_ASSIGNEE_ADDED
                elif open_id not in after:
                    kind = TaskNotificationKind.TASK_ASSIGNEE_REMOVED
                else:
                    kind = TaskNotificationKind.TASK_ASSIGNEES_CHANGED
                member_recipients[open_id] = kind
        for open_id, member_kind in member_recipients.items():
            if open_id == event.actor_open_id:
                continue
            created += _ensure_notification(
                session,
                existing_keys,
                task=task,
                kind=member_kind,
                recipient_open_id=open_id,
                dedupe_key=f"event:{event.id}",
                scheduled_for=event.applied_at,
                status_snapshot=event.new_status,
                deadline_snapshot=event.deadline_after,
                deadline_before_snapshot=event.deadline_before,
                max_attempts=settings.max_attempts,
                created_at=synced_at,
                source_lifecycle_event_id=event.id,
                owner_name_snapshot=(
                    responsible_names
                    if event.action == "restore"
                    else actor_name
                ),
            )
        for administrator in sorted(task_administrator_open_ids):
            if administrator == event.actor_open_id:
                continue
            if administrator in member_recipients:
                continue
            created += _ensure_notification(
                session,
                existing_keys,
                task=task,
                kind=admin_kind,
                recipient_open_id=administrator,
                dedupe_key=f"event:{event.id}",
                scheduled_for=event.applied_at,
                status_snapshot=event.new_status,
                deadline_snapshot=event.deadline_after,
                deadline_before_snapshot=event.deadline_before,
                max_attempts=settings.max_attempts,
                created_at=synced_at,
                source_lifecycle_event_id=event.id,
                owner_name_snapshot=responsible_names,
            )
        deferred = deferred_by_id.pop(event.id, None)
        if deferred is not None:
            session.delete(deferred)
    newest_event_id = session.scalar(
        select(TaskLifecycleEvent.id)
        .where(TaskLifecycleEvent.id > state.last_lifecycle_event_id)
        .order_by(TaskLifecycleEvent.id.desc())
        .limit(1)
    )
    if newest_event_id is not None:
        state.last_lifecycle_event_id = newest_event_id
        state.updated_at = synced_at

    cancelled = 0
    for notification in existing:
        if notification.kind not in {
            TaskNotificationKind.MISSING_DEADLINE_OWNER.value,
            TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
        }:
            continue
        active = notification.status in {
            TaskNotificationStatus.SCHEDULED.value,
            TaskNotificationStatus.LEASED.value,
        }
        reactivatable = (
            notification.status == TaskNotificationStatus.CANCELLED.value
            and notification.cancel_reason
            in {
                "notification_stage_disabled",
                "administrator_notification_policy_changed",
            }
        )
        if not active and not reactivatable:
            continue
        task = tasks_by_id.get(notification.task_id)
        stage_disabled = False
        desired_for: datetime | None = None
        if task is not None and task.status == "todo" and task.deadline is None:
            chat_settings = session.get(ChatSettings, task.chat_id)
            owner_enabled, admin_enabled, owner_delay, admin_delay = (
                _missing_deadline_policy(chat_settings, settings)
            )
            responsible_open_ids = {
                open_id for open_id, _name in _responsible_members(task)
            }
            if notification.kind == TaskNotificationKind.MISSING_DEADLINE_OWNER.value:
                stage_disabled = not owner_enabled
                if owner_enabled and notification.recipient_open_id in responsible_open_ids:
                    desired_for = task.created_at + owner_delay
            else:
                stage_disabled = not admin_enabled
                administrators = _notification_administrator_open_ids(
                    session, task.chat_id, administrator_open_ids
                )
                if (
                    admin_enabled
                    and notification.recipient_open_id in administrators
                    and notification.recipient_open_id not in responsible_open_ids
                ):
                    desired_for = task.created_at + admin_delay
        if desired_for is not None:
            notification.scheduled_for = desired_for
            notification.available_at = desired_for
            notification.updated_at = synced_at
            if reactivatable:
                notification.status = TaskNotificationStatus.SCHEDULED.value
                notification.attempt_count = 0
                notification.worker_id = None
                notification.leased_at = None
                notification.lease_expires_at = None
                notification.cancelled_at = None
                notification.cancel_reason = None
                notification.last_error_code = None
                notification.last_error_message = None
                created += 1
            elif notification.status == TaskNotificationStatus.LEASED.value:
                notification.status = TaskNotificationStatus.SCHEDULED.value
                notification.worker_id = None
                notification.leased_at = None
                notification.lease_expires_at = None
                continue
            continue
        if not active:
            continue
        administrator_policy_removed = (
            notification.kind
            == TaskNotificationKind.MISSING_DEADLINE_ADMIN.value
            and task is not None
            and notification.recipient_open_id
            in _chat_administrator_open_ids(
                session, task.chat_id, administrator_open_ids
            )
        )
        notification.status = TaskNotificationStatus.CANCELLED.value
        notification.worker_id = None
        notification.leased_at = None
        notification.lease_expires_at = None
        notification.cancelled_at = synced_at
        if task is None:
            notification.cancel_reason = "task_outside_allowlist"
        elif task.deadline is not None:
            notification.cancel_reason = "task_deadline_set"
        elif task.status != "todo":
            notification.cancel_reason = f"task_{task.status}"
        elif stage_disabled:
            notification.cancel_reason = "notification_stage_disabled"
        elif administrator_policy_removed:
            notification.cancel_reason = (
                "administrator_notification_policy_changed"
            )
        else:
            notification.cancel_reason = "recipient_no_longer_authorized"
        notification.updated_at = synced_at
        cancelled += 1
    for notification in existing:
        if notification.kind != TaskNotificationKind.TASK_OVERDUE_ADMIN.value:
            continue
        if notification.status not in {
            TaskNotificationStatus.SCHEDULED.value,
            TaskNotificationStatus.LEASED.value,
        }:
            continue
        task = tasks_by_id.get(notification.task_id)
        if (
            task is not None
            and task.status == "overdue"
            and task.deadline == notification.deadline_snapshot
            and notification.recipient_open_id
            in _notification_administrator_open_ids(
                session, task.chat_id, administrator_open_ids
            )
        ):
            continue
        administrator_policy_removed = (
            task is not None
            and task.status == "overdue"
            and task.deadline == notification.deadline_snapshot
            and notification.recipient_open_id
            in _chat_administrator_open_ids(
                session, task.chat_id, administrator_open_ids
            )
        )
        notification.status = TaskNotificationStatus.CANCELLED.value
        notification.worker_id = None
        notification.leased_at = None
        notification.lease_expires_at = None
        notification.cancelled_at = synced_at
        notification.cancel_reason = (
            "administrator_notification_policy_changed"
            if administrator_policy_removed
            else "task_no_longer_overdue"
        )
        notification.updated_at = synced_at
        cancelled += 1
    for notification in existing:
        if notification.kind not in ADMINISTRATOR_NOTIFICATION_KINDS or (
            notification.kind
            in {
                TaskNotificationKind.MISSING_DEADLINE_ADMIN.value,
                TaskNotificationKind.TASK_OVERDUE_ADMIN.value,
            }
        ):
            continue
        if notification.status not in {
            TaskNotificationStatus.SCHEDULED.value,
            TaskNotificationStatus.LEASED.value,
        }:
            continue
        task = tasks_by_id.get(notification.task_id)
        if task is not None and notification.recipient_open_id in (
            _notification_administrator_open_ids(
                session, task.chat_id, administrator_open_ids
            )
        ):
            continue
        notification.status = TaskNotificationStatus.CANCELLED.value
        notification.worker_id = None
        notification.leased_at = None
        notification.lease_expires_at = None
        notification.cancelled_at = synced_at
        notification.cancel_reason = (
            "administrator_notification_policy_changed"
            if task is not None
            else "task_outside_allowlist"
        )
        notification.updated_at = synced_at
        cancelled += 1
    session.flush()
    return TaskNotificationSyncResult(created=created, cancelled=cancelled)


def _chat_administrator_open_ids(
    session: Session,
    chat_id: str,
    legacy_open_ids: frozenset[str],
) -> frozenset[str]:
    persisted = session.scalars(
        select(ChatAdministrator.open_id).where(
            ChatAdministrator.chat_id == chat_id
        )
    )
    return frozenset(legacy_open_ids).union(persisted)


def _notification_administrator_open_ids(
    session: Session,
    chat_id: str,
    legacy_open_ids: frozenset[str],
) -> frozenset[str]:
    """Return this chat's effective recipients for administrator alerts."""

    administrators = _chat_administrator_open_ids(
        session, chat_id, legacy_open_ids
    )
    settings = session.get(ChatSettings, chat_id)
    if (
        settings is None
        or settings.administrator_notification_mode != "selected"
    ):
        return administrators
    try:
        raw = json.loads(settings.administrator_notification_open_ids_json)
    except (TypeError, json.JSONDecodeError):
        return administrators
    if not isinstance(raw, list):
        return administrators
    selected = frozenset(
        item.strip()
        for item in raw
        if isinstance(item, str) and item.strip()
    )
    effective = administrators.intersection(selected)
    # An administrator may leave after being selected. Falling back to the
    # remaining administrators avoids silently dropping critical alerts.
    return effective or administrators


def _missing_deadline_policy(
    chat_settings: ChatSettings | None,
    settings: ReminderSettings,
) -> tuple[bool, bool, timedelta, timedelta]:
    owner_enabled = (
        True
        if chat_settings is None
        else chat_settings.missing_deadline_owner_enabled
    )
    admin_enabled = (
        True
        if chat_settings is None
        else chat_settings.missing_deadline_admin_enabled
    )
    if settings.test_mode:
        return (
            owner_enabled,
            admin_enabled,
            timedelta(minutes=2),
            timedelta(minutes=4),
        )
    owner_hours = (
        24
        if chat_settings is None
        else chat_settings.missing_deadline_owner_delay_hours
    )
    admin_hours = (
        72
        if chat_settings is None
        else chat_settings.missing_deadline_admin_delay_hours
    )
    return (
        owner_enabled,
        admin_enabled,
        timedelta(hours=owner_hours),
        timedelta(hours=admin_hours),
    )


def _ensure_notification(
    session: Session,
    existing_keys: set[tuple[int, str, str, str]],
    *,
    task: Task,
    kind: TaskNotificationKind,
    recipient_open_id: str,
    dedupe_key: str,
    scheduled_for: datetime,
    status_snapshot: str,
    deadline_snapshot: datetime | None,
    max_attempts: int,
    created_at: datetime,
    source_lifecycle_event_id: int | None = None,
    owner_open_id_snapshot: str | None = None,
    owner_name_snapshot: str | None = None,
    deadline_before_snapshot: datetime | None = None,
) -> int:
    key = (task.id, kind.value, recipient_open_id, dedupe_key)
    if key in existing_keys:
        existing = session.scalar(
            select(TaskNotification).where(
                TaskNotification.task_id == task.id,
                TaskNotification.kind == kind.value,
                TaskNotification.recipient_open_id == recipient_open_id,
                TaskNotification.dedupe_key == dedupe_key,
            )
        )
        if (
            existing is not None
            and existing.status == TaskNotificationStatus.CANCELLED.value
            and existing.cancel_reason
            == "administrator_notification_policy_changed"
        ):
            existing.status = TaskNotificationStatus.SCHEDULED.value
            existing.scheduled_for = scheduled_for
            existing.available_at = scheduled_for
            existing.attempt_count = 0
            existing.worker_id = None
            existing.leased_at = None
            existing.lease_expires_at = None
            existing.cancelled_at = None
            existing.cancel_reason = None
            existing.last_error_code = None
            existing.last_error_message = None
            existing.updated_at = created_at
            return 1
        return 0
    session.add(
        TaskNotification(
            task_id=task.id,
            source_lifecycle_event_id=source_lifecycle_event_id,
            kind=kind.value,
            recipient_open_id=recipient_open_id,
            dedupe_key=dedupe_key,
            task_code_snapshot=format_task_code(task.id),
            owner_open_id_snapshot=(
                owner_open_id_snapshot or task.owner_open_id
            ),
            owner_name_snapshot=(
                owner_name_snapshot or task.owner_name_snapshot
            ),
            title_snapshot=task.title,
            status_snapshot=status_snapshot,
            deadline_snapshot=deadline_snapshot,
            deadline_before_snapshot=deadline_before_snapshot,
            scheduled_for=scheduled_for,
            available_at=scheduled_for,
            status=TaskNotificationStatus.SCHEDULED.value,
            attempt_count=0,
            max_attempts=max_attempts,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    existing_keys.add(key)
    return 1


def _find_private_chat_id_in_session(
    session: Session, recipient_open_id: str
) -> str | None:
    return session.scalar(
        select(Message.chat_id)
        .join(Chat, Chat.chat_id == Message.chat_id)
        .where(
            Message.sender_open_id == recipient_open_id,
            Chat.chat_type == "p2p",
        )
        .order_by(Message.message_created_at.desc(), Message.id.desc())
        .limit(1)
    )


def _responsible_members(task: Task) -> tuple[tuple[str, str], ...]:
    if task.assignees:
        return tuple(
            (assignee.open_id, assignee.name_snapshot)
            for assignee in task.assignees
        )
    return ((task.owner_open_id, task.owner_name_snapshot),)


def _actor_name(session: Session, task: Task, actor_open_id: str) -> str:
    for open_id, name in _responsible_members(task):
        if open_id == actor_open_id:
            return name
    alias = session.scalar(
        select(ChatMemberAlias.alias).where(
            ChatMemberAlias.chat_id == task.chat_id,
            ChatMemberAlias.open_id == actor_open_id,
        )
    )
    if alias:
        return alias
    user = session.get(User, actor_open_id)
    if user is not None and user.name:
        return user.name
    return actor_open_id


def _event_assignees(payload: str | None) -> tuple[tuple[str, str], ...]:
    if payload is None:
        return ()
    try:
        raw = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    result: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return ()
        open_id = item.get("open_id")
        name = item.get("name")
        if not isinstance(open_id, str) or not isinstance(name, str):
            return ()
        result.append((open_id, name))
    return tuple(result)


def _recover_expired_leases(
    session: Session, *, recovered_at: datetime
) -> None:
    expired = tuple(
        session.scalars(
            select(TaskNotification).where(
                TaskNotification.status == TaskNotificationStatus.LEASED.value,
                TaskNotification.lease_expires_at <= recovered_at,
            )
        )
    )
    for notification in expired:
        notification.worker_id = None
        notification.leased_at = None
        notification.lease_expires_at = None
        notification.last_error_code = "lease_expired"
        notification.last_error_message = (
            "previous notification worker lease expired before completion"
        )
        notification.updated_at = recovered_at
        if notification.attempt_count >= notification.max_attempts:
            notification.status = TaskNotificationStatus.DEAD.value
        else:
            notification.status = TaskNotificationStatus.SCHEDULED.value
            notification.available_at = recovered_at


def _require_owned_lease(
    session: Session,
    lease: TaskNotificationLease,
    *,
    active_at: datetime,
) -> TaskNotification:
    # Expiry only makes a lease eligible for recovery by another claimant; it
    # does not revoke ownership on its own. worker_id plus the monotonically
    # increasing attempt number rejects an old worker after actual recovery.
    _aware_utc(active_at, "active_at")
    notification = session.get(TaskNotification, lease.notification_id)
    if (
        notification is None
        or notification.status != TaskNotificationStatus.LEASED.value
        or notification.worker_id != lease.worker_id
        or notification.attempt_count != lease.attempt
        or notification.lease_expires_at is None
    ):
        raise ValueError("notification lease is no longer active or owned")
    return notification


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
