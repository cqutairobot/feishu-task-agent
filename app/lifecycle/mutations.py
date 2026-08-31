"""Authorized, audited, and atomic task lifecycle mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import unicodedata

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import ReminderSettings
from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatMemberAlias,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskCompletionSubmission,
    TaskEvidence,
    TaskLifecycleEvent,
    TaskLifecycleEvidence,
    TaskNote,
    TaskNotification,
    TaskSource,
    User,
)
from app.agent.contracts import MAX_TASK_ASSIGNEES, TaskOwner
from app.identity.aliases import normalize_alias
from app.lifecycle.contracts import LifecycleAction, LifecycleCandidate
from app.lifecycle.review_contracts import ReviewAction, ReviewActionCandidate
from app.reminders.repository import sync_task_reminders_in_session
from app.notifications.repository import (
    create_task_assignment_notifications_in_session,
)
from app.tasks.codes import TaskCodeError, format_task_code, parse_task_code
from app.tasks.notes import MAX_TASK_NOTE_CONTENT_LENGTH
from app.tasks.repository import TaskStatus


class LifecycleMutationError(RuntimeError):
    """Raised when a lifecycle candidate cannot be safely applied."""


class LifecycleAuthorizationRole(StrEnum):
    OWNER = "owner"
    ADMINISTRATOR = "administrator"


@dataclass(frozen=True, slots=True)
class LifecycleMutationResult:
    event_id: int
    task_id: int
    task_code: str
    action: LifecycleAction
    authorization_role: LifecycleAuthorizationRole
    previous_status: TaskStatus
    new_status: TaskStatus
    deadline_before: datetime | None
    deadline_after: datetime | None
    reminders_created: int
    reminders_cancelled: int
    already_applied: bool
    applied_at: datetime
    title_before: str | None = None
    title_after: str | None = None
    assignees_before: tuple[TaskOwner, ...] = ()
    assignees_after: tuple[TaskOwner, ...] = ()
    merge_target_task_id: int | None = None


@dataclass(frozen=True, slots=True)
class LifecycleModelAudit:
    provider: str
    model: str
    response_format: str
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class _LifecycleAudit:
    actor_name: str
    source_message_id: str | None
    correlation_id: str
    idempotency_key: str
    from_review_status: str
    to_review_status: str
    completion_cycle: int
    completion_note_id: int | None


class LifecycleMutationService:
    """Apply one already-validated candidate with local authorization checks."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        reminder_settings: ReminderSettings = ReminderSettings(),
        administrator_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        minimum_confidence: float = 0.9,
    ) -> None:
        if any(
            not isinstance(item, str) or not item.strip()
            for item in administrator_open_ids
        ):
            raise ValueError("administrator Open IDs must not be empty")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in allowed_chat_ids
        ):
            raise ValueError("allowed chat IDs must not be empty")
        if (
            isinstance(minimum_confidence, bool)
            or not isinstance(minimum_confidence, (int, float))
            or not math.isfinite(float(minimum_confidence))
            or not 0 <= float(minimum_confidence) <= 1
        ):
            raise ValueError("minimum_confidence must be between 0 and 1")
        self._session_factory = session_factory
        self._reminder_settings = reminder_settings
        self._administrator_open_ids = frozenset(
            item.strip() for item in administrator_open_ids
        )
        self._allowed_chat_ids = frozenset(
            item.strip() for item in allowed_chat_ids
        )
        self._minimum_confidence = float(minimum_confidence)

    def apply_candidate(
        self,
        candidate: LifecycleCandidate,
        *,
        actor_open_id: str,
        trigger_message_id: str,
        applied_at: datetime,
        task_code: str | None = None,
        model_audit: LifecycleModelAudit | None = None,
    ) -> LifecycleMutationResult:
        candidate = _validate_candidate(candidate, self._minimum_confidence)
        actor_open_id = _required_text(actor_open_id, "actor_open_id", 128)
        trigger_message_id = _required_text(
            trigger_message_id, "trigger_message_id", 128
        )
        applied_at = _aware_utc(applied_at, "applied_at")
        model_audit = _validate_model_audit(model_audit)
        expected_task_id = _optional_task_code(task_code)
        if expected_task_id is not None and expected_task_id != candidate.task_id:
            raise LifecycleMutationError(
                "task code does not match the lifecycle candidate task_id"
            )

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, candidate.task_id)
            if task is None:
                raise LifecycleMutationError(
                    f"task {candidate.task_id} does not exist"
                )
            if not _chat_is_admitted(
                session, task.chat_id, self._allowed_chat_ids
            ):
                raise LifecycleMutationError(
                    "task belongs to a chat outside the configured allowlist"
                )

            trigger = _unique_message(session, trigger_message_id)
            trigger_chat = session.get(Chat, trigger.chat_id)
            if trigger_chat is None:
                raise LifecycleMutationError("trigger message chat does not exist")
            _validate_trigger(
                trigger,
                trigger_chat,
                task,
                actor_open_id=actor_open_id,
                applied_at=applied_at,
            )
            authorization = self._authorize(
                session,
                task,
                actor_open_id,
                action=candidate.action,
            )
            evidence = _load_evidence(
                session,
                candidate.evidence_message_ids,
                trigger=trigger,
            )

            existing = session.scalar(
                select(TaskLifecycleEvent).where(
                    TaskLifecycleEvent.task_id == task.id,
                    TaskLifecycleEvent.trigger_message_db_id == trigger.id,
                )
            )
            if existing is not None:
                _validate_replay(
                    existing,
                    candidate,
                    actor_open_id=actor_open_id,
                    authorization=authorization,
                    evidence=evidence,
                )
                return _result(existing, already_applied=True)

            previous_status = _actionable_status(task.status)
            deadline_before = task.deadline
            title_before = (
                task.title if candidate.action is LifecycleAction.RENAME else None
            )
            assignees_before = (
                _task_assignees(task)
                if candidate.action is LifecycleAction.REASSIGN
                else ()
            )
            new_status, deadline_after = _apply_transition(
                session,
                task,
                candidate,
                applied_at=applied_at,
            )
            lifecycle_audit = _record_lifecycle_audit(
                session,
                task,
                action=candidate.action,
                actor_open_id=actor_open_id,
                applied_at=applied_at,
                source_message_id=trigger.message_id,
                content_snapshot=trigger.text_content,
                evidence_message_ids=candidate.evidence_message_ids,
                correlation_id=(
                    model_audit.request_id
                    if model_audit is not None and model_audit.request_id
                    else trigger.event_id
                ),
                idempotency_key=_lifecycle_idempotency_key(
                    "message", task.id, trigger.id
                ),
                completion_note_source=trigger,
                model_audit=model_audit,
                confidence=candidate.confidence,
            )
            title_after = (
                task.title if candidate.action is LifecycleAction.RENAME else None
            )
            assignees_after = (
                _task_assignees(task)
                if candidate.action is LifecycleAction.REASSIGN
                else ()
            )
            event = TaskLifecycleEvent(
                task_id=task.id,
                actor_open_id=actor_open_id,
                actor_name_snapshot=lifecycle_audit.actor_name,
                trigger_message_db_id=trigger.id,
                source_message_id=lifecycle_audit.source_message_id,
                correlation_id=lifecycle_audit.correlation_id,
                idempotency_key=lifecycle_audit.idempotency_key,
                action=candidate.action.value,
                authorization_role=authorization.value,
                task_code_snapshot=format_task_code(task.id),
                previous_status=previous_status.value,
                new_status=new_status.value,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
                title_before=title_before,
                title_after=title_after,
                assignees_before_json=_owners_json_or_none(assignees_before),
                assignees_after_json=_owners_json_or_none(assignees_after),
                from_review_status=lifecycle_audit.from_review_status,
                to_review_status=lifecycle_audit.to_review_status,
                completion_cycle=lifecycle_audit.completion_cycle,
                confidence=candidate.confidence,
                provider=(None if model_audit is None else model_audit.provider),
                model=(None if model_audit is None else model_audit.model),
                response_format=(
                    None if model_audit is None else model_audit.response_format
                ),
                model_request_id=(
                    None if model_audit is None else model_audit.request_id
                ),
                prompt_tokens=(
                    None if model_audit is None else model_audit.prompt_tokens
                ),
                completion_tokens=(
                    None
                    if model_audit is None
                    else model_audit.completion_tokens
                ),
                total_tokens=(
                    None if model_audit is None else model_audit.total_tokens
                ),
                applied_at=applied_at,
                created_at=applied_at,
            )
            session.add(event)
            session.flush()
            session.add_all(
                TaskLifecycleEvidence(
                    event_id=event.id,
                    message_db_id=message.id,
                    position=position,
                )
                for position, message in enumerate(evidence)
            )
            reminder_result = sync_task_reminders_in_session(
                session,
                task,
                synced_at=applied_at,
                settings=self._reminder_settings,
            )
            session.flush()
            return LifecycleMutationResult(
                event_id=event.id,
                task_id=task.id,
                task_code=event.task_code_snapshot,
                action=candidate.action,
                authorization_role=authorization,
                previous_status=previous_status,
                new_status=TaskStatus(task.status),
                deadline_before=deadline_before,
                deadline_after=task.deadline,
                title_before=title_before,
                title_after=title_after,
                assignees_before=assignees_before,
                assignees_after=assignees_after,
                reminders_created=reminder_result.created,
                reminders_cancelled=reminder_result.cancelled,
                already_applied=False,
                applied_at=applied_at,
            )

    def apply_private_review_action(
        self,
        candidate: ReviewActionCandidate,
        *,
        actor_open_id: str,
        trigger_message_id: str,
        task_code: str | None = None,
        applied_at: datetime,
        model_audit: LifecycleModelAudit | None = None,
    ) -> LifecycleMutationResult:
        """Atomically apply one explicitly confirmed private review action.

        This path is intentionally separate from ordinary owner lifecycle
        updates. It requires an administrator, a completed reviewable task and
        a P2P trigger message from that same administrator. The trigger message
        is recorded as a normal message-source lifecycle event so the complete
        audit trail remains tied to the human confirmation.
        """

        candidate = _validate_review_candidate(candidate, self._minimum_confidence)
        actor_open_id = _required_text(actor_open_id, "actor_open_id", 128)
        trigger_message_id = _required_text(
            trigger_message_id, "trigger_message_id", 128
        )
        applied_at = _aware_utc(applied_at, "applied_at")
        model_audit = _validate_model_audit(model_audit)
        expected_task_id = _optional_task_code(task_code)
        if expected_task_id is not None and expected_task_id != candidate.task_id:
            raise LifecycleMutationError(
                "task code does not match the review candidate task_id"
            )

        action = LifecycleAction(candidate.action.value)
        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, candidate.task_id)
            if task is None:
                raise LifecycleMutationError(
                    f"task {candidate.task_id} does not exist"
                )
            if not _chat_is_admitted(
                session, task.chat_id, self._allowed_chat_ids
            ):
                raise LifecycleMutationError(
                    "task belongs to a chat outside the configured allowlist"
                )
            if not self._is_administrator(session, task.chat_id, actor_open_id):
                raise LifecycleMutationError(
                    "private review actions require an authorized administrator"
                )
            trigger = _unique_message(session, trigger_message_id)
            trigger_chat = session.get(Chat, trigger.chat_id)
            if trigger_chat is None:
                raise LifecycleMutationError("trigger message chat does not exist")
            _validate_trigger(
                trigger,
                trigger_chat,
                task,
                actor_open_id=actor_open_id,
                applied_at=applied_at,
            )
            if trigger_chat.chat_type != "p2p":
                raise LifecycleMutationError(
                    "private review trigger must come from a P2P chat"
                )
            evidence = _load_evidence(
                session,
                candidate.evidence_message_ids,
                trigger=trigger,
            )
            existing = session.scalar(
                select(TaskLifecycleEvent).where(
                    TaskLifecycleEvent.task_id == task.id,
                    TaskLifecycleEvent.trigger_message_db_id == trigger.id,
                )
            )
            if existing is not None:
                _validate_private_review_replay(
                    existing,
                    candidate=candidate,
                    actor_open_id=actor_open_id,
                    evidence=evidence,
                )
                return _result(existing, already_applied=True)

            previous_status = _management_previous_status(task, action)
            deadline_before = task.deadline
            lifecycle_candidate = LifecycleCandidate(
                action=action,
                confidence=candidate.confidence,
                task_id=task.id,
                new_deadline=None,
                evidence_message_ids=candidate.evidence_message_ids,
            )
            new_status, deadline_after = _apply_transition(
                session,
                task,
                lifecycle_candidate,
                applied_at=applied_at,
            )
            correlation_id = (
                model_audit.request_id
                if model_audit is not None and model_audit.request_id
                else trigger.event_id
            )
            idempotency_key = _lifecycle_idempotency_key(
                "private_review", task.id, trigger.id
            )
            lifecycle_audit = _record_lifecycle_audit(
                session,
                task,
                action=action,
                actor_open_id=actor_open_id,
                applied_at=applied_at,
                source_message_id=trigger.message_id,
                source_chat_id=trigger.chat_id,
                content_snapshot=trigger.text_content,
                evidence_message_ids=tuple(
                    message.message_id for message in evidence
                ),
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                model_audit=model_audit,
                confidence=candidate.confidence,
                reason=candidate.reason,
            )
            event = TaskLifecycleEvent(
                task_id=task.id,
                actor_open_id=actor_open_id,
                actor_name_snapshot=lifecycle_audit.actor_name,
                trigger_source="message",
                trigger_message_db_id=trigger.id,
                source_message_id=lifecycle_audit.source_message_id,
                correlation_id=lifecycle_audit.correlation_id,
                idempotency_key=lifecycle_audit.idempotency_key,
                action=action.value,
                authorization_role=LifecycleAuthorizationRole.ADMINISTRATOR.value,
                task_code_snapshot=format_task_code(task.id),
                previous_status=previous_status.value,
                new_status=new_status.value,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
                title_before=None,
                title_after=None,
                assignees_before_json=None,
                assignees_after_json=None,
                from_review_status=lifecycle_audit.from_review_status,
                to_review_status=lifecycle_audit.to_review_status,
                reason=candidate.reason,
                completion_cycle=lifecycle_audit.completion_cycle,
                confidence=candidate.confidence,
                provider=(None if model_audit is None else model_audit.provider),
                model=(None if model_audit is None else model_audit.model),
                response_format=(
                    None if model_audit is None else model_audit.response_format
                ),
                model_request_id=(
                    None if model_audit is None else model_audit.request_id
                ),
                prompt_tokens=(
                    None if model_audit is None else model_audit.prompt_tokens
                ),
                completion_tokens=(
                    None
                    if model_audit is None
                    else model_audit.completion_tokens
                ),
                total_tokens=(
                    None if model_audit is None else model_audit.total_tokens
                ),
                applied_at=applied_at,
                created_at=applied_at,
            )
            session.add(event)
            session.flush()
            session.add_all(
                TaskLifecycleEvidence(
                    event_id=event.id,
                    message_db_id=message.id,
                    position=position,
                )
                for position, message in enumerate(evidence)
            )
            reminder_result = sync_task_reminders_in_session(
                session,
                task,
                synced_at=applied_at,
                settings=self._reminder_settings,
            )
            session.flush()
            return LifecycleMutationResult(
                event_id=event.id,
                task_id=task.id,
                task_code=event.task_code_snapshot,
                action=action,
                authorization_role=LifecycleAuthorizationRole.ADMINISTRATOR,
                previous_status=previous_status,
                new_status=TaskStatus(task.status),
                deadline_before=deadline_before,
                deadline_after=task.deadline,
                title_before=None,
                title_after=None,
                assignees_before=(),
                assignees_after=(),
                reminders_created=reminder_result.created,
                reminders_cancelled=reminder_result.cancelled,
                already_applied=False,
                applied_at=applied_at,
            )

    def apply_card_action(
        self,
        action: LifecycleAction,
        *,
        actor_open_id: str,
        callback_id: str,
        card_message_id: str,
        card_chat_id: str,
        task_code: str,
        applied_at: datetime,
        new_deadline: datetime | None = None,
    ) -> LifecycleMutationResult:
        """Apply one explicit, signed Feishu card action without an LLM."""

        if action not in {
            LifecycleAction.COMPLETE,
            LifecycleAction.CANCEL,
            LifecycleAction.RESCHEDULE,
        }:
            raise LifecycleMutationError(
                "this card action is not supported in the current phase"
            )
        if action is LifecycleAction.RESCHEDULE:
            if (
                not isinstance(new_deadline, datetime)
                or new_deadline.tzinfo is None
            ):
                raise LifecycleMutationError(
                    "card reschedule requires a timezone-aware new deadline"
                )
            new_deadline = new_deadline.astimezone(timezone.utc)
        elif new_deadline is not None:
            raise LifecycleMutationError(
                f"card {action.value} must not include a new deadline"
            )
        actor_open_id = _required_text(actor_open_id, "actor_open_id", 128)
        callback_id = _required_text(callback_id, "callback_id", 128)
        card_message_id = _required_text(
            card_message_id, "card_message_id", 128
        )
        card_chat_id = _required_text(card_chat_id, "card_chat_id", 128)
        applied_at = _aware_utc(applied_at, "applied_at")
        task_id = _optional_task_code(task_code)
        if task_id is None:
            raise LifecycleMutationError("task_code must not be empty")

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, task_id)
            if task is None:
                raise LifecycleMutationError(
                    f"task {task_id} does not exist"
                )
            if not _chat_is_admitted(
                session, task.chat_id, self._allowed_chat_ids
            ):
                raise LifecycleMutationError(
                    "task belongs to a chat outside the configured allowlist"
                )
            authorization = self._authorize(
                session,
                task,
                actor_open_id,
                action=action,
            )

            existing = session.scalar(
                select(TaskLifecycleEvent).where(
                    TaskLifecycleEvent.trigger_card_action_id == callback_id
                )
            )
            if existing is not None:
                _validate_card_replay(
                    existing,
                    task_id=task_id,
                    action=action,
                    actor_open_id=actor_open_id,
                    authorization=authorization,
                    card_message_id=card_message_id,
                    card_chat_id=card_chat_id,
                    new_deadline=new_deadline,
                )
                return _result(existing, already_applied=True)

            previous_status = _actionable_status(task.status)
            deadline_before = task.deadline
            candidate = LifecycleCandidate(
                action=action,
                confidence=1.0,
                task_id=task.id,
                new_deadline=new_deadline,
                evidence_message_ids=(),
            )
            new_status, deadline_after = _apply_transition(
                session,
                task,
                candidate,
                applied_at=applied_at,
            )
            lifecycle_audit = _record_lifecycle_audit(
                session,
                task,
                action=action,
                actor_open_id=actor_open_id,
                applied_at=applied_at,
                source_message_id=card_message_id,
                content_snapshot="通过任务卡片标记完成",
                evidence_message_ids=(card_message_id,),
                correlation_id=callback_id,
                idempotency_key=_lifecycle_idempotency_key(
                    "card_action", callback_id
                ),
            )
            event = TaskLifecycleEvent(
                task_id=task.id,
                actor_open_id=actor_open_id,
                actor_name_snapshot=lifecycle_audit.actor_name,
                trigger_source="card_action",
                trigger_message_db_id=None,
                trigger_card_action_id=callback_id,
                trigger_card_message_id=card_message_id,
                trigger_card_chat_id=card_chat_id,
                source_message_id=lifecycle_audit.source_message_id,
                correlation_id=lifecycle_audit.correlation_id,
                idempotency_key=lifecycle_audit.idempotency_key,
                action=action.value,
                authorization_role=authorization.value,
                task_code_snapshot=format_task_code(task.id),
                previous_status=previous_status.value,
                new_status=new_status.value,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
                title_before=None,
                title_after=None,
                assignees_before_json=None,
                assignees_after_json=None,
                from_review_status=lifecycle_audit.from_review_status,
                to_review_status=lifecycle_audit.to_review_status,
                completion_cycle=lifecycle_audit.completion_cycle,
                confidence=1.0,
                applied_at=applied_at,
                created_at=applied_at,
            )
            session.add(event)
            session.flush()
            reminder_result = sync_task_reminders_in_session(
                session,
                task,
                synced_at=applied_at,
                settings=self._reminder_settings,
            )
            session.flush()
            return LifecycleMutationResult(
                event_id=event.id,
                task_id=task.id,
                task_code=event.task_code_snapshot,
                action=action,
                authorization_role=authorization,
                previous_status=previous_status,
                new_status=TaskStatus(task.status),
                deadline_before=deadline_before,
                deadline_after=task.deadline,
                title_before=None,
                title_after=None,
                assignees_before=(),
                assignees_after=(),
                reminders_created=reminder_result.created,
                reminders_cancelled=reminder_result.cancelled,
                already_applied=False,
                applied_at=applied_at,
            )

    def apply_management_action(
        self,
        action: LifecycleAction,
        *,
        actor_open_id: str,
        request_id: str,
        chat_id: str,
        task_id: int,
        applied_at: datetime,
        new_deadline: datetime | None = None,
        new_title: str | None = None,
        new_owner_open_ids: tuple[str, ...] = (),
        merge_target_task_id: int | None = None,
        reason: str | None = None,
    ) -> LifecycleMutationResult:
        """Apply an explicit administrator action from the management page.

        The browser supplies only an idempotency key and requested value. The
        authenticated actor and exact group authorization are rechecked inside
        the same transaction that updates the task and reminder plan.
        """

        if action not in {
            LifecycleAction.CONFIRM,
            LifecycleAction.COMPLETE,
            LifecycleAction.ACCEPT,
            LifecycleAction.REOPEN,
            LifecycleAction.CANCEL,
            LifecycleAction.INVALIDATE,
            LifecycleAction.RESCHEDULE,
            LifecycleAction.RENAME,
            LifecycleAction.REASSIGN,
            LifecycleAction.RESTORE,
            LifecycleAction.MERGE,
        }:
            raise LifecycleMutationError(
                "this management action is not supported in the current phase"
            )
        normalized_reason = None
        if action is LifecycleAction.REOPEN:
            normalized_reason = _management_reason(reason)
        elif reason is not None:
            raise LifecycleMutationError(
                f"management {action.value} does not accept a reason"
            )
        if action is LifecycleAction.RESCHEDULE:
            if (
                not isinstance(new_deadline, datetime)
                or new_deadline.tzinfo is None
            ):
                raise LifecycleMutationError(
                    "management reschedule requires a timezone-aware new deadline"
                )
            if new_title is not None or new_owner_open_ids:
                raise LifecycleMutationError(
                    "management reschedule accepts only a new deadline"
                )
            new_deadline = new_deadline.astimezone(timezone.utc)
        elif action is LifecycleAction.RENAME:
            if (
                not isinstance(new_title, str)
                or not new_title.strip()
                or len(new_title.strip()) > 200
            ):
                raise LifecycleMutationError(
                    "management rename requires a title containing 1 to 200 characters"
                )
            new_title = " ".join(
                unicodedata.normalize("NFKC", new_title).split()
            )
            if not new_title or len(new_title) > 200:
                raise LifecycleMutationError(
                    "management rename requires a title containing 1 to 200 characters"
                )
            if new_deadline is not None or new_owner_open_ids:
                raise LifecycleMutationError(
                    "management rename accepts only a new title"
                )
        elif action is LifecycleAction.REASSIGN:
            if (
                not isinstance(new_owner_open_ids, tuple)
                or not 1 <= len(new_owner_open_ids) <= MAX_TASK_ASSIGNEES
                or any(
                    not isinstance(open_id, str) or not open_id.strip()
                    for open_id in new_owner_open_ids
                )
                or len(set(new_owner_open_ids)) != len(new_owner_open_ids)
            ):
                raise LifecycleMutationError(
                    "management reassign requires 1 to 20 unique member Open IDs"
                )
            new_owner_open_ids = tuple(
                open_id.strip() for open_id in new_owner_open_ids
            )
            if len(set(new_owner_open_ids)) != len(new_owner_open_ids):
                raise LifecycleMutationError(
                    "management reassign requires unique member Open IDs"
                )
            if new_deadline is not None or new_title is not None:
                raise LifecycleMutationError(
                    "management reassign accepts only responsible members"
                )
        elif action is LifecycleAction.MERGE:
            if (
                isinstance(new_owner_open_ids, bool)
                or new_owner_open_ids
                or new_deadline is not None
                or new_title is not None
            ):
                raise LifecycleMutationError(
                    "management merge accepts only a target task"
                )
        else:
            if (
                new_deadline is not None
                or new_title is not None
                or new_owner_open_ids
            ):
                raise LifecycleMutationError(
                    f"management {action.value} accepts no task values"
                )
        actor_open_id = _required_text(actor_open_id, "actor_open_id", 128)
        request_id = _required_text(request_id, "request_id", 128)
        chat_id = _required_text(chat_id, "chat_id", 128)
        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
        ):
            raise LifecycleMutationError("task_id must be positive")
        if action is LifecycleAction.MERGE:
            if (
                isinstance(merge_target_task_id, bool)
                or not isinstance(merge_target_task_id, int)
                or merge_target_task_id < 1
            ):
                raise LifecycleMutationError(
                    "management merge requires a positive target task ID"
                )
        elif merge_target_task_id is not None:
            raise LifecycleMutationError(
                f"management {action.value} does not accept a merge target"
            )
        applied_at = _aware_utc(applied_at, "applied_at")

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = session.get(Task, task_id)
            if task is None or task.chat_id != chat_id:
                raise LifecycleMutationError(
                    "task does not exist in the requested chat"
                )
            if not _chat_is_admitted(
                session, task.chat_id, self._allowed_chat_ids
            ):
                raise LifecycleMutationError(
                    "task belongs to a chat outside the configured allowlist"
                )
            if not self._is_administrator(session, chat_id, actor_open_id):
                raise LifecycleMutationError(
                    "management actions require an authorized administrator"
                )
            authorization = LifecycleAuthorizationRole.ADMINISTRATOR

            existing = session.scalar(
                select(TaskLifecycleEvent).where(
                    TaskLifecycleEvent.trigger_management_request_id
                    == request_id
                )
            )
            if existing is not None:
                _validate_management_replay(
                    existing,
                    task_id=task_id,
                    action=action,
                    actor_open_id=actor_open_id,
                    new_deadline=new_deadline,
                    new_title=new_title,
                    new_owner_open_ids=new_owner_open_ids,
                    merge_target_task_id=merge_target_task_id,
                    reason=normalized_reason,
                )
                return _result(existing, already_applied=True)

            if (
                task.merged_into_task_id is not None
                and action is not LifecycleAction.MERGE
            ):
                raise LifecycleMutationError(
                    "merged tasks cannot receive another lifecycle action"
                )
            previous_status = _management_previous_status(task, action)
            merge_target = None
            if action is LifecycleAction.MERGE:
                assert merge_target_task_id is not None
                if merge_target_task_id == task.id:
                    raise LifecycleMutationError(
                        "a task cannot be merged into itself"
                    )
                merge_target = session.get(Task, merge_target_task_id)
                if merge_target is None or merge_target.chat_id != chat_id:
                    raise LifecycleMutationError(
                        "merge target does not exist in the requested chat"
                    )
                if merge_target.merged_into_task_id is not None:
                    raise LifecycleMutationError(
                        "merge target is already merged into another task"
                    )
                if merge_target.status == TaskStatus.CANCELLED.value:
                    raise LifecycleMutationError(
                        "cancelled tasks cannot be merge targets"
                    )
                if task.merged_into_task_id is not None:
                    raise LifecycleMutationError(
                        "task is already merged into another task"
                    )
            deadline_before = task.deadline
            title_before = (
                task.title if action is LifecycleAction.RENAME else None
            )
            assignees_before = (
                _task_assignees(task)
                if action is LifecycleAction.REASSIGN
                else ()
            )
            new_owners = (
                _management_owners(
                    session,
                    chat_id=chat_id,
                    open_ids=new_owner_open_ids,
                )
                if action is LifecycleAction.REASSIGN
                else ()
            )
            candidate = LifecycleCandidate(
                action=action,
                confidence=1.0,
                task_id=task.id,
                new_deadline=new_deadline,
                evidence_message_ids=(),
                new_title=new_title,
                new_owners=new_owners,
            )
            new_status, deadline_after = _apply_transition(
                session,
                task,
                candidate,
                applied_at=applied_at,
                merge_target=merge_target,
            )
            lifecycle_audit = _record_lifecycle_audit(
                session,
                task,
                action=action,
                actor_open_id=actor_open_id,
                applied_at=applied_at,
                source_message_id=None,
                content_snapshot="管理员在管理后台标记任务完成",
                evidence_message_ids=(),
                correlation_id=request_id,
                idempotency_key=_lifecycle_idempotency_key(
                    "management_page", request_id
                ),
                reason=normalized_reason,
            )
            if action is LifecycleAction.MERGE:
                assert merge_target is not None
                _merge_task_provenance(session, task, merge_target)
                _cancel_unsent_task_notifications(
                    session,
                    task,
                    cancelled_at=applied_at,
                    reason="task_merged",
                )
            title_after = (
                task.title if action is LifecycleAction.RENAME else None
            )
            assignees_after = (
                _task_assignees(task)
                if action is LifecycleAction.REASSIGN
                else ()
            )
            event = TaskLifecycleEvent(
                task_id=task.id,
                actor_open_id=actor_open_id,
                actor_name_snapshot=lifecycle_audit.actor_name,
                trigger_source="management_page",
                trigger_message_db_id=None,
                trigger_card_action_id=None,
                trigger_card_message_id=None,
                trigger_card_chat_id=None,
                trigger_management_request_id=request_id,
                source_message_id=lifecycle_audit.source_message_id,
                correlation_id=lifecycle_audit.correlation_id,
                idempotency_key=lifecycle_audit.idempotency_key,
                action=action.value,
                authorization_role=authorization.value,
                task_code_snapshot=format_task_code(task.id),
                previous_status=previous_status.value,
                new_status=new_status.value,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
                title_before=title_before,
                title_after=title_after,
                assignees_before_json=_owners_json_or_none(assignees_before),
                assignees_after_json=_owners_json_or_none(assignees_after),
                from_review_status=lifecycle_audit.from_review_status,
                to_review_status=lifecycle_audit.to_review_status,
                reason=normalized_reason,
                completion_cycle=lifecycle_audit.completion_cycle,
                merge_target_task_id=merge_target_task_id,
                confidence=1.0,
                applied_at=applied_at,
                created_at=applied_at,
            )
            session.add(event)
            session.flush()
            reminder_result = sync_task_reminders_in_session(
                session,
                task,
                synced_at=applied_at,
                settings=self._reminder_settings,
            )
            if action is LifecycleAction.CONFIRM:
                create_task_assignment_notifications_in_session(
                    session,
                    task,
                    scheduled_for=applied_at,
                    max_attempts=self._reminder_settings.max_attempts,
                    reason="activated",
                )
            session.flush()
            return LifecycleMutationResult(
                event_id=event.id,
                task_id=task.id,
                task_code=event.task_code_snapshot,
                action=action,
                authorization_role=authorization,
                previous_status=previous_status,
                new_status=TaskStatus(task.status),
                deadline_before=deadline_before,
                deadline_after=task.deadline,
                title_before=title_before,
                title_after=title_after,
                assignees_before=assignees_before,
                assignees_after=assignees_after,
                reminders_created=reminder_result.created,
                reminders_cancelled=reminder_result.cancelled,
                already_applied=False,
                applied_at=applied_at,
                merge_target_task_id=merge_target_task_id,
            )

    def _authorize(
        self,
        session: Session,
        task: Task,
        actor_open_id: str,
        *,
        action: LifecycleAction,
    ) -> LifecycleAuthorizationRole:
        if action in {
            LifecycleAction.RENAME,
            LifecycleAction.REASSIGN,
            LifecycleAction.INVALIDATE,
            LifecycleAction.REOPEN,
        }:
            if self._is_administrator(session, task.chat_id, actor_open_id):
                return LifecycleAuthorizationRole.ADMINISTRATOR
            raise LifecycleMutationError(
                "task corrections require an authorized administrator"
            )
        is_responsible = actor_open_id == task.owner_open_id or session.scalar(
            select(TaskAssignee.id)
            .where(
                TaskAssignee.task_id == task.id,
                TaskAssignee.open_id == actor_open_id,
            )
            .limit(1)
        ) is not None
        if is_responsible:
            return LifecycleAuthorizationRole.OWNER
        if self._is_administrator(session, task.chat_id, actor_open_id):
            return LifecycleAuthorizationRole.ADMINISTRATOR
        raise LifecycleMutationError(
            "actor is neither the task owner nor an authorized administrator"
        )

    def _is_administrator(
        self, session: Session, chat_id: str, actor_open_id: str
    ) -> bool:
        if actor_open_id in self._administrator_open_ids:
            return True
        return session.scalar(
            select(ChatAdministrator.id)
            .where(
                ChatAdministrator.chat_id == chat_id,
                ChatAdministrator.open_id == actor_open_id,
            )
            .limit(1)
        ) is not None


def _validate_candidate(
    candidate: LifecycleCandidate, minimum_confidence: float
) -> LifecycleCandidate:
    if not isinstance(candidate, LifecycleCandidate):
        raise LifecycleMutationError("candidate must be a LifecycleCandidate")
    if not isinstance(candidate.action, LifecycleAction):
        raise LifecycleMutationError("candidate action is invalid")
    if candidate.action is LifecycleAction.CONFIRM:
        raise LifecycleMutationError(
            "pending confirmation is available only to administrators"
        )
    if candidate.action is LifecycleAction.MERGE:
        raise LifecycleMutationError(
            "task merging is available only from the management page"
        )
    if candidate.action is LifecycleAction.REOPEN:
        raise LifecycleMutationError(
            "task reopening is available only from the management page"
        )
    if (
        isinstance(candidate.task_id, bool)
        or not isinstance(candidate.task_id, int)
        or candidate.task_id < 1
    ):
        raise LifecycleMutationError("candidate task_id must be positive")
    if (
        isinstance(candidate.confidence, bool)
        or not isinstance(candidate.confidence, (int, float))
        or not math.isfinite(float(candidate.confidence))
        or not 0 <= float(candidate.confidence) <= 1
    ):
        raise LifecycleMutationError("candidate confidence is invalid")
    if candidate.confidence < minimum_confidence:
        raise LifecycleMutationError(
            "candidate confidence is below the mutation threshold"
        )
    if not isinstance(candidate.evidence_message_ids, tuple) or not (
        candidate.evidence_message_ids
    ):
        raise LifecycleMutationError("candidate evidence must be non-empty")
    if any(
        not isinstance(message_id, str) or not message_id.strip()
        for message_id in candidate.evidence_message_ids
    ):
        raise LifecycleMutationError("candidate evidence IDs must not be empty")
    if len(set(candidate.evidence_message_ids)) != len(
        candidate.evidence_message_ids
    ):
        raise LifecycleMutationError("candidate evidence must be unique")
    if candidate.action is LifecycleAction.RESCHEDULE:
        if (
            not isinstance(candidate.new_deadline, datetime)
            or candidate.new_deadline.tzinfo is None
        ):
            raise LifecycleMutationError(
                "reschedule requires a timezone-aware new deadline"
            )
    elif candidate.new_deadline is not None:
        raise LifecycleMutationError(
            f"{candidate.action.value} must not include a new deadline"
        )
    if candidate.action is LifecycleAction.RENAME:
        if (
            not isinstance(candidate.new_title, str)
            or not candidate.new_title.strip()
            or len(candidate.new_title.strip()) > 200
        ):
            raise LifecycleMutationError(
                "rename requires a new title containing 1 to 200 characters"
            )
    elif candidate.new_title is not None:
        raise LifecycleMutationError(
            f"{candidate.action.value} must not include a new title"
        )
    if candidate.action is LifecycleAction.REASSIGN:
        if (
            not isinstance(candidate.new_owners, tuple)
            or not candidate.new_owners
            or len(candidate.new_owners) > MAX_TASK_ASSIGNEES
            or any(not isinstance(owner, TaskOwner) for owner in candidate.new_owners)
        ):
            raise LifecycleMutationError(
                "reassign requires between 1 and 20 valid new owners"
            )
        owner_ids = tuple(owner.open_id for owner in candidate.new_owners)
        if any(not owner_id.strip() for owner_id in owner_ids):
            raise LifecycleMutationError("new owner Open IDs must not be empty")
        if len(set(owner_ids)) != len(owner_ids):
            raise LifecycleMutationError("new owners must be unique")
    elif candidate.new_owners:
        raise LifecycleMutationError(
            f"{candidate.action.value} must not include new owners"
        )
    return candidate


def _validate_review_candidate(
    candidate: ReviewActionCandidate, minimum_confidence: float
) -> ReviewActionCandidate:
    """Validate the narrow candidate accepted by the private review gate."""

    if not isinstance(candidate, ReviewActionCandidate):
        raise LifecycleMutationError(
            "candidate must be a ReviewActionCandidate"
        )
    if not isinstance(candidate.action, ReviewAction):
        raise LifecycleMutationError("review candidate action is invalid")
    if (
        isinstance(candidate.task_id, bool)
        or not isinstance(candidate.task_id, int)
        or candidate.task_id < 1
    ):
        raise LifecycleMutationError("review candidate task_id must be positive")
    if (
        isinstance(candidate.confidence, bool)
        or not isinstance(candidate.confidence, (int, float))
        or not math.isfinite(float(candidate.confidence))
        or not 0 <= float(candidate.confidence) <= 1
    ):
        raise LifecycleMutationError("review candidate confidence is invalid")
    if candidate.confidence < minimum_confidence:
        raise LifecycleMutationError(
            "review candidate confidence is below the mutation threshold"
        )
    if not isinstance(candidate.evidence_message_ids, tuple) or not candidate.evidence_message_ids:
        raise LifecycleMutationError("review candidate evidence must be non-empty")
    if any(
        not isinstance(message_id, str) or not message_id.strip()
        for message_id in candidate.evidence_message_ids
    ):
        raise LifecycleMutationError("review evidence IDs must not be empty")
    if len(set(candidate.evidence_message_ids)) != len(
        candidate.evidence_message_ids
    ):
        raise LifecycleMutationError("review evidence must be unique")

    normalized_reason: str | None = None
    if candidate.action is ReviewAction.ACCEPT:
        if candidate.reason is not None:
            raise LifecycleMutationError("accept review must not include a reason")
    else:
        if not isinstance(candidate.reason, str):
            raise LifecycleMutationError("reopen review requires a reason")
        normalized_reason = " ".join(
            unicodedata.normalize("NFC", candidate.reason).split()
        )
        if not normalized_reason or len(normalized_reason) > 2_000:
            raise LifecycleMutationError(
                "reopen review reason must contain 1 to 2000 characters"
            )
    return ReviewActionCandidate(
        action=candidate.action,
        confidence=float(candidate.confidence),
        task_id=candidate.task_id,
        reason=normalized_reason,
        evidence_message_ids=tuple(
            message_id.strip() for message_id in candidate.evidence_message_ids
        ),
    )


def _validate_trigger(
    trigger: Message,
    trigger_chat: Chat,
    task: Task,
    *,
    actor_open_id: str,
    applied_at: datetime,
) -> None:
    if trigger.sender_open_id != actor_open_id or trigger.is_from_bot:
        raise LifecycleMutationError(
            "actor must be the human sender of the trigger message"
        )
    if trigger.message_type != "text":
        raise LifecycleMutationError("trigger message must be text")
    if trigger.received_at > applied_at:
        raise LifecycleMutationError("applied_at cannot precede message receipt")
    if trigger_chat.chat_type == "group":
        if trigger.chat_id != task.chat_id:
            raise LifecycleMutationError("group lifecycle updates cannot cross chats")
    elif trigger_chat.chat_type != "p2p":
        raise LifecycleMutationError("unsupported lifecycle trigger chat type")


def _load_evidence(
    session: Session,
    message_ids: tuple[str, ...],
    *,
    trigger: Message,
) -> tuple[Message, ...]:
    if trigger.message_id not in message_ids:
        raise LifecycleMutationError("evidence must include the trigger message")
    rows = tuple(
        session.scalars(
            select(Message).where(
                Message.tenant_key == trigger.tenant_key,
                Message.message_id.in_(message_ids),
            )
        )
    )
    by_id = {message.message_id: message for message in rows}
    if set(by_id) != set(message_ids):
        raise LifecycleMutationError("lifecycle evidence message does not exist")
    evidence = tuple(by_id[message_id] for message_id in message_ids)
    if any(message.chat_id != trigger.chat_id for message in evidence):
        raise LifecycleMutationError("lifecycle evidence crosses chat boundaries")
    if any(
        message.message_created_at > trigger.message_created_at
        for message in evidence
    ):
        raise LifecycleMutationError("lifecycle evidence occurs after the trigger")
    return evidence


def _unique_message(session: Session, message_id: str) -> Message:
    rows = tuple(
        session.scalars(select(Message).where(Message.message_id == message_id))
    )
    if len(rows) != 1:
        raise LifecycleMutationError(
            "trigger message does not exist or is ambiguous"
        )
    return rows[0]


def _actionable_status(value: str) -> TaskStatus:
    status = TaskStatus(value)
    if status not in {TaskStatus.TODO, TaskStatus.OVERDUE}:
        raise LifecycleMutationError(
            f"task status {status.value} is not actionable"
        )
    return status


def _management_previous_status(
    task: Task, action: LifecycleAction
) -> TaskStatus:
    status = TaskStatus(task.status)
    if action is LifecycleAction.CONFIRM:
        if status is not TaskStatus.PENDING:
            raise LifecycleMutationError(
                "only pending tasks can be confirmed"
            )
        return status
    if action is LifecycleAction.INVALIDATE and status is TaskStatus.PENDING:
        return status
    if action is LifecycleAction.ACCEPT:
        if (
            status is not TaskStatus.DONE
            or task.review_status != "pending"
            or task.completion_cycle < 1
        ):
            raise LifecycleMutationError(
                "only completed tasks awaiting review can be accepted"
            )
        return status
    if action is LifecycleAction.REOPEN:
        if (
            status is not TaskStatus.DONE
            or task.review_status not in {"pending", "accepted"}
            or task.completion_cycle < 1
        ):
            raise LifecycleMutationError(
                "only completed tasks with a review result can be reopened"
            )
        return status
    if action is LifecycleAction.RESTORE:
        if status not in {TaskStatus.DONE, TaskStatus.CANCELLED}:
            raise LifecycleMutationError(
                "only completed or cancelled tasks can be restored"
            )
        return status
    if action is LifecycleAction.MERGE:
        if status not in {
            TaskStatus.PENDING,
            TaskStatus.TODO,
            TaskStatus.OVERDUE,
            TaskStatus.DONE,
        }:
            raise LifecycleMutationError(
                "only open or completed tasks can be merged"
            )
        return status
    return _actionable_status(task.status)


def _apply_transition(
    session: Session,
    task: Task,
    candidate: LifecycleCandidate,
    *,
    applied_at: datetime,
    merge_target: Task | None = None,
) -> tuple[TaskStatus, datetime | None]:
    if candidate.action is LifecycleAction.CONFIRM:
        task.status = TaskStatus.TODO.value
        task.completed_at = None
        task.cancelled_at = None
    elif candidate.action is LifecycleAction.COMPLETE:
        task.status = TaskStatus.DONE.value
        task.completed_at = applied_at
        task.cancelled_at = None
    elif candidate.action is LifecycleAction.ACCEPT:
        # Acceptance is a quality gate over the current completion submission;
        # the task remains completed while its review state advances.
        pass
    elif candidate.action is LifecycleAction.REOPEN:
        task.status = (
            TaskStatus.OVERDUE.value
            if task.deadline is not None and task.deadline <= applied_at
            else TaskStatus.TODO.value
        )
        task.completed_at = None
        task.cancelled_at = None
    elif candidate.action in {
        LifecycleAction.CANCEL,
        LifecycleAction.INVALIDATE,
    }:
        task.status = TaskStatus.CANCELLED.value
        task.completed_at = None
        task.cancelled_at = applied_at
    elif candidate.action is LifecycleAction.RESTORE:
        task.status = (
            TaskStatus.OVERDUE.value
            if task.deadline is not None and task.deadline <= applied_at
            else TaskStatus.TODO.value
        )
        task.completed_at = None
        task.cancelled_at = None
    elif candidate.action is LifecycleAction.MERGE:
        if merge_target is None:
            raise LifecycleMutationError("merge target is required")
        task.status = TaskStatus.CANCELLED.value
        task.completed_at = None
        task.cancelled_at = applied_at
        task.merged_into_task_id = merge_target.id
        task.merged_at = applied_at
    elif candidate.action is LifecycleAction.RESCHEDULE:
        assert candidate.new_deadline is not None
        new_deadline = candidate.new_deadline.astimezone(timezone.utc)
        if new_deadline <= applied_at:
            raise LifecycleMutationError(
                "reschedule deadline must still be in the future when applied"
            )
        if task.deadline == new_deadline:
            raise LifecycleMutationError("reschedule must change the deadline")
        task.deadline = new_deadline
        task.status = TaskStatus.TODO.value
        task.completed_at = None
        task.cancelled_at = None
    elif candidate.action is LifecycleAction.RENAME:
        assert candidate.new_title is not None
        new_title = " ".join(
            unicodedata.normalize("NFKC", candidate.new_title).split()
        )
        if _normalize_title(new_title) == task.normalized_title:
            raise LifecycleMutationError("rename must change the task title")
        task.title = new_title
        task.normalized_title = _normalize_title(new_title)
    elif candidate.action is LifecycleAction.REASSIGN:
        _apply_reassignment(session, task, candidate.new_owners, applied_at)
    else:
        raise LifecycleMutationError("unsupported lifecycle transition")
    task.updated_at = applied_at
    return TaskStatus(task.status), task.deadline


def _record_lifecycle_audit(
    session: Session,
    task: Task,
    *,
    action: LifecycleAction,
    actor_open_id: str,
    applied_at: datetime,
    source_message_id: str | None,
    content_snapshot: str | None,
    evidence_message_ids: tuple[str, ...],
    correlation_id: str,
    idempotency_key: str,
    source_chat_id: str | None = None,
    completion_note_source: Message | None = None,
    model_audit: LifecycleModelAudit | None = None,
    confidence: float | None = None,
    reason: str | None = None,
) -> _LifecycleAudit:
    """Resolve actor/source audit and persist completion details when needed."""

    actor_name = _actor_name(session, task, actor_open_id)
    from_review_status = task.review_status
    completion_cycle = task.completion_cycle
    completion_note_id = None
    if action is LifecycleAction.COMPLETE:
        completion_cycle += 1
        cleaned_content = (
            content_snapshot.strip()
            if isinstance(content_snapshot, str) and content_snapshot.strip()
            else "通过飞书消息标记任务完成"
        )

        task.review_status = "pending"
        task.reviewed_by_open_id = None
        task.reviewed_by_name = None
        task.reviewed_at = None
        task.completion_cycle = completion_cycle
        task.last_completed_by_open_id = actor_open_id
        task.last_completed_by_name = actor_name
        task.last_completed_at = applied_at

        if completion_note_source is not None:
            completion_note = TaskNote(
                task_id=task.id,
                chat_id=task.chat_id,
                author_open_id=actor_open_id,
                author_name_snapshot=actor_name,
                note_type="completion",
                content=_completion_note_content(cleaned_content),
                source_message_id=completion_note_source.message_id,
                source_chat_id=completion_note_source.chat_id,
                completion_cycle=completion_cycle,
                idempotency_key=f"{idempotency_key}:completion-note",
                confidence=confidence,
                provider=(
                    None if model_audit is None else model_audit.provider
                ),
                model=None if model_audit is None else model_audit.model,
                response_format=(
                    None
                    if model_audit is None
                    else model_audit.response_format
                ),
                model_request_id=(
                    None if model_audit is None else model_audit.request_id
                ),
                prompt_tokens=(
                    None if model_audit is None else model_audit.prompt_tokens
                ),
                completion_tokens=(
                    None
                    if model_audit is None
                    else model_audit.completion_tokens
                ),
                total_tokens=(
                    None if model_audit is None else model_audit.total_tokens
                ),
                created_at=applied_at,
            )
            session.add(completion_note)
            session.flush()
            completion_note_id = completion_note.id

        session.add(
            TaskCompletionSubmission(
                task_id=task.id,
                chat_id=task.chat_id,
                cycle=completion_cycle,
                submitted_by_open_id=actor_open_id,
                submitted_by_name_snapshot=actor_name,
                source_message_id=source_message_id,
                completion_note_id=completion_note_id,
                content_snapshot=cleaned_content,
                evidence_json=json.dumps(
                    list(evidence_message_ids),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                submitted_at=applied_at,
                review_status="pending",
                idempotency_key=idempotency_key,
            )
        )
    elif action is LifecycleAction.ACCEPT:
        submission = session.scalar(
            select(TaskCompletionSubmission).where(
                TaskCompletionSubmission.task_id == task.id,
                TaskCompletionSubmission.cycle == task.completion_cycle,
            )
        )
        if submission is None or submission.review_status != "pending":
            raise LifecycleMutationError(
                "current completion submission is not awaiting review"
            )
        task.review_status = "accepted"
        task.reviewed_by_open_id = actor_open_id
        task.reviewed_by_name = actor_name
        task.reviewed_at = applied_at
        submission.review_status = "accepted"
        submission.reviewed_by_open_id = actor_open_id
        submission.reviewed_at = applied_at
        submission.review_reason = None
    elif action is LifecycleAction.REOPEN:
        normalized_reason = _management_reason(reason)
        submission = session.scalar(
            select(TaskCompletionSubmission).where(
                TaskCompletionSubmission.task_id == task.id,
                TaskCompletionSubmission.cycle == task.completion_cycle,
            )
        )
        if submission is None or submission.review_status not in {
            "pending",
            "accepted",
        }:
            raise LifecycleMutationError(
                "current completion submission cannot be reopened"
            )
        task.review_status = "rework_required"
        task.reviewed_by_open_id = actor_open_id
        task.reviewed_by_name = actor_name
        task.reviewed_at = applied_at
        submission.review_status = "rework_required"
        submission.reviewed_by_open_id = actor_open_id
        submission.reviewed_at = applied_at
        submission.review_reason = normalized_reason
        session.add(
            TaskNote(
                task_id=task.id,
                chat_id=task.chat_id,
                author_open_id=actor_open_id,
                author_name_snapshot=actor_name,
                note_type="reopen",
                content=_completion_note_content(normalized_reason),
                source_message_id=source_message_id,
                source_chat_id=source_chat_id or task.chat_id,
                completion_cycle=task.completion_cycle,
                idempotency_key=f"{idempotency_key}:reopen-note",
                created_at=applied_at,
            )
        )
        session.flush()
    return _LifecycleAudit(
        actor_name=actor_name,
        source_message_id=source_message_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        from_review_status=from_review_status,
        to_review_status=task.review_status,
        completion_cycle=completion_cycle,
        completion_note_id=completion_note_id,
    )


def _completion_note_content(value: str) -> str:
    """Bound the display note while the submission keeps the full raw text."""

    if len(value) <= MAX_TASK_NOTE_CONTENT_LENGTH:
        return value
    return value[: MAX_TASK_NOTE_CONTENT_LENGTH - 1] + "…"


def _actor_name(session: Session, task: Task, actor_open_id: str) -> str:
    for owner in _task_assignees(task):
        if owner.open_id == actor_open_id:
            return owner.name
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


def _lifecycle_idempotency_key(source: str, *parts: object) -> str:
    payload = "\x1f".join((source, *(str(part) for part in parts)))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"lifecycle:{source}:{digest}"


def _validate_replay(
    event: TaskLifecycleEvent,
    candidate: LifecycleCandidate,
    *,
    actor_open_id: str,
    authorization: LifecycleAuthorizationRole,
    evidence: tuple[Message, ...],
) -> None:
    requested_deadline = (
        None
        if candidate.new_deadline is None
        else candidate.new_deadline.astimezone(timezone.utc)
    )
    event_deadline = (
        event.deadline_after
        if candidate.action is LifecycleAction.RESCHEDULE
        else None
    )
    stored_evidence = tuple(
        link.message.message_id for link in event.evidence_links
    )
    if (
        event.actor_open_id != actor_open_id
        or event.authorization_role != authorization.value
        or event.action != candidate.action.value
        or event.confidence != candidate.confidence
        or event_deadline != requested_deadline
        or event.title_after != candidate.new_title
        or _owners_from_json(event.assignees_after_json) != candidate.new_owners
        or stored_evidence != tuple(item.message_id for item in evidence)
    ):
        raise LifecycleMutationError(
            "trigger message was already used for a different lifecycle update"
        )


def _validate_private_review_replay(
    event: TaskLifecycleEvent,
    *,
    candidate: ReviewActionCandidate,
    actor_open_id: str,
    evidence: tuple[Message, ...],
) -> None:
    """Ensure a duplicate delivery cannot reinterpret a review decision."""

    stored_evidence = tuple(
        link.message.message_id for link in event.evidence_links
    )
    if (
        event.trigger_source != "message"
        or event.task_id != candidate.task_id
        or event.actor_open_id != actor_open_id
        or event.authorization_role
        != LifecycleAuthorizationRole.ADMINISTRATOR.value
        or event.action != candidate.action.value
        or event.confidence != candidate.confidence
        or event.reason != candidate.reason
        or stored_evidence != tuple(item.message_id for item in evidence)
    ):
        raise LifecycleMutationError(
            "trigger message was already used for a different private review action"
        )


def _validate_card_replay(
    event: TaskLifecycleEvent,
    *,
    task_id: int,
    action: LifecycleAction,
    actor_open_id: str,
    authorization: LifecycleAuthorizationRole,
    card_message_id: str,
    card_chat_id: str,
    new_deadline: datetime | None,
) -> None:
    requested_deadline = (
        None
        if new_deadline is None
        else new_deadline.astimezone(timezone.utc)
    )
    event_deadline = (
        event.deadline_after
        if action is LifecycleAction.RESCHEDULE
        else None
    )
    if (
        event.trigger_source != "card_action"
        or event.task_id != task_id
        or event.actor_open_id != actor_open_id
        or event.authorization_role != authorization.value
        or event.action != action.value
        or event.trigger_card_message_id != card_message_id
        or event.trigger_card_chat_id != card_chat_id
        or event.confidence != 1.0
        or event_deadline != requested_deadline
    ):
        raise LifecycleMutationError(
            "card callback was already used for a different lifecycle update"
        )


def _validate_management_replay(
    event: TaskLifecycleEvent,
    *,
    task_id: int,
    action: LifecycleAction,
    actor_open_id: str,
    new_deadline: datetime | None,
    new_title: str | None,
    new_owner_open_ids: tuple[str, ...],
    merge_target_task_id: int | None,
    reason: str | None,
) -> None:
    requested_deadline = (
        None
        if new_deadline is None
        else new_deadline.astimezone(timezone.utc)
    )
    event_deadline = (
        event.deadline_after
        if action is LifecycleAction.RESCHEDULE
        else None
    )
    event_title = (
        event.title_after if action is LifecycleAction.RENAME else None
    )
    event_owner_open_ids = (
        tuple(
            owner.open_id
            for owner in _owners_from_json(event.assignees_after_json)
        )
        if action is LifecycleAction.REASSIGN
        else ()
    )
    event_merge_target = (
        event.merge_target_task_id
        if action is LifecycleAction.MERGE
        else None
    )
    if (
        event.trigger_source != "management_page"
        or event.task_id != task_id
        or event.actor_open_id != actor_open_id
        or event.authorization_role
        != LifecycleAuthorizationRole.ADMINISTRATOR.value
        or event.action != action.value
        or event.confidence != 1.0
        or event_deadline != requested_deadline
        or event_title != new_title
        or event_owner_open_ids != new_owner_open_ids
        or event_merge_target != merge_target_task_id
        or event.reason != reason
    ):
        raise LifecycleMutationError(
            "management request was already used for a different lifecycle update"
        )


def _result(
    event: TaskLifecycleEvent, *, already_applied: bool
) -> LifecycleMutationResult:
    return LifecycleMutationResult(
        event_id=event.id,
        task_id=event.task_id,
        task_code=event.task_code_snapshot,
        action=LifecycleAction(event.action),
        authorization_role=LifecycleAuthorizationRole(
            event.authorization_role
        ),
        previous_status=TaskStatus(event.previous_status),
        new_status=TaskStatus(event.new_status),
        deadline_before=event.deadline_before,
        deadline_after=event.deadline_after,
        title_before=event.title_before,
        title_after=event.title_after,
        assignees_before=_owners_from_json(event.assignees_before_json),
        assignees_after=_owners_from_json(event.assignees_after_json),
        merge_target_task_id=event.merge_target_task_id,
        reminders_created=0,
        reminders_cancelled=0,
        already_applied=already_applied,
        applied_at=event.applied_at,
    )


def _task_assignees(task: Task) -> tuple[TaskOwner, ...]:
    if task.assignees:
        return tuple(
            TaskOwner(name=item.name_snapshot, open_id=item.open_id)
            for item in task.assignees
        )
    return (
        TaskOwner(
            name=task.owner_name_snapshot,
            open_id=task.owner_open_id,
        ),
    )


def _merge_task_provenance(
    session: Session, source: Task, target: Task
) -> None:
    """Move duplicate evidence and detection provenance into the retained task."""

    target_message_ids = set(
        session.scalars(
            select(TaskEvidence.message_db_id).where(
                TaskEvidence.task_id == target.id
            )
        )
    )
    source_evidence = tuple(
        session.scalars(
            select(TaskEvidence)
            .where(TaskEvidence.task_id == source.id)
            .order_by(TaskEvidence.id)
        )
    )
    for evidence in source_evidence:
        if evidence.message_db_id in target_message_ids:
            session.delete(evidence)
        else:
            evidence.task_id = target.id
            target_message_ids.add(evidence.message_db_id)

    target_source_keys = {
        (item.detection_run_id, item.candidate_index)
        for item in session.scalars(
            select(TaskSource).where(TaskSource.task_id == target.id)
        )
    }
    source_sources = tuple(
        session.scalars(
            select(TaskSource)
            .where(TaskSource.task_id == source.id)
            .order_by(TaskSource.id)
        )
    )
    for provenance in source_sources:
        key = (provenance.detection_run_id, provenance.candidate_index)
        if key in target_source_keys:
            session.delete(provenance)
        else:
            provenance.task_id = target.id
            target_source_keys.add(key)

    target.updated_at = max(target.updated_at, source.updated_at)


def _cancel_unsent_task_notifications(
    session: Session,
    task: Task,
    *,
    cancelled_at: datetime,
    reason: str,
) -> int:
    cancelled = 0
    notifications = session.scalars(
        select(TaskNotification).where(
            TaskNotification.task_id == task.id,
            TaskNotification.status.in_(("scheduled", "leased")),
        )
    )
    for notification in notifications:
        notification.status = "cancelled"
        notification.worker_id = None
        notification.leased_at = None
        notification.lease_expires_at = None
        notification.cancelled_at = cancelled_at
        notification.cancel_reason = reason
        notification.updated_at = cancelled_at
        cancelled += 1
    return cancelled


def _management_owners(
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
            raise LifecycleMutationError(
                "new owner must be an active member with a bound task name"
            )
        owners.append(TaskOwner(name=binding.alias, open_id=open_id))
    return tuple(owners)


def _apply_reassignment(
    session: Session,
    task: Task,
    requested: tuple[TaskOwner, ...],
    applied_at: datetime,
) -> None:
    grounded: list[TaskOwner] = []
    for owner in requested:
        if session.get(User, owner.open_id) is None:
            raise LifecycleMutationError(
                f"new owner does not exist: {owner.open_id}"
            )
        binding = session.scalar(
            select(ChatMemberAlias).where(
                ChatMemberAlias.chat_id == task.chat_id,
                ChatMemberAlias.open_id == owner.open_id,
            )
        )
        if binding is None or normalize_alias(binding.alias) != normalize_alias(
            owner.name
        ):
            raise LifecycleMutationError(
                "new owner is not a currently verified member of the task chat"
            )
        grounded.append(TaskOwner(name=binding.alias, open_id=binding.open_id))
    if tuple(owner.open_id for owner in grounded) == tuple(
        owner.open_id for owner in _task_assignees(task)
    ):
        raise LifecycleMutationError("reassign must change responsible members")

    session.execute(
        delete(TaskAssignee).where(TaskAssignee.task_id == task.id)
    )
    session.flush()
    session.expire(task, ["assignees"])
    session.add_all(
        TaskAssignee(
            task_id=task.id,
            open_id=owner.open_id,
            name_snapshot=owner.name,
            position=position,
            created_at=applied_at,
        )
        for position, owner in enumerate(grounded)
    )
    primary = grounded[0]
    task.owner_open_id = primary.open_id
    task.owner_name_snapshot = primary.name
    session.flush()
    session.expire(task, ["assignees"])


def _owners_json_or_none(owners: tuple[TaskOwner, ...]) -> str | None:
    if not owners:
        return None
    return json.dumps(
        [
            {"name": owner.name, "open_id": owner.open_id}
            for owner in owners
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _owners_from_json(payload: str | None) -> tuple[TaskOwner, ...]:
    if payload is None:
        return ()
    try:
        raw = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LifecycleMutationError("stored assignee audit JSON is invalid") from exc
    if not isinstance(raw, list):
        raise LifecycleMutationError("stored assignee audit JSON is invalid")
    result: list[TaskOwner] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "open_id"}:
            raise LifecycleMutationError("stored assignee audit JSON is invalid")
        name = item["name"]
        open_id = item["open_id"]
        if not isinstance(name, str) or not isinstance(open_id, str):
            raise LifecycleMutationError("stored assignee audit JSON is invalid")
        result.append(TaskOwner(name=name, open_id=open_id))
    return tuple(result)


def _normalize_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _optional_task_code(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return parse_task_code(value)
    except TaskCodeError as exc:
        raise LifecycleMutationError(str(exc)) from exc


def _chat_is_admitted(
    session: Session,
    chat_id: str,
    configured_chat_ids: frozenset[str],
) -> bool:
    if not configured_chat_ids or chat_id in configured_chat_ids:
        return True
    return session.scalar(
        select(ChatAdministrator.id)
        .join(Chat, Chat.chat_id == ChatAdministrator.chat_id)
        .where(
            ChatAdministrator.chat_id == chat_id,
            Chat.chat_type == "group",
            Chat.enabled.is_(True),
        )
        .limit(1)
    ) is not None


def _validate_model_audit(
    value: LifecycleModelAudit | None,
) -> LifecycleModelAudit | None:
    if value is None:
        return None
    if not isinstance(value, LifecycleModelAudit):
        raise LifecycleMutationError(
            "model_audit must be a LifecycleModelAudit"
        )
    provider = _required_text(value.provider, "model provider", 32)
    model = _required_text(value.model, "model", 128)
    response_format = _required_text(
        value.response_format, "response_format", 32
    )
    request_id = value.request_id
    if request_id is not None:
        request_id = _required_text(request_id, "model request_id", 128)
    token_values = (
        value.prompt_tokens,
        value.completion_tokens,
        value.total_tokens,
    )
    if any(
        token is not None
        and (
            isinstance(token, bool)
            or not isinstance(token, int)
            or token < 0
        )
        for token in token_values
    ):
        raise LifecycleMutationError("model token counts must be non-negative")
    return LifecycleModelAudit(
        provider=provider,
        model=model,
        response_format=response_format,
        request_id=request_id,
        prompt_tokens=value.prompt_tokens,
        completion_tokens=value.completion_tokens,
        total_tokens=value.total_tokens,
    )


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleMutationError(f"{field} must not be empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise LifecycleMutationError(f"{field} is too long")
    return cleaned


def _management_reason(value: str | None) -> str:
    if not isinstance(value, str):
        raise LifecycleMutationError("management reopen requires a reason")
    # Preserve the administrator's original punctuation in the immutable audit
    # trail. NFC combines equivalent Unicode sequences without the compatibility
    # folding performed by NFKC (for example, turning `，` into `,`).
    cleaned = " ".join(unicodedata.normalize("NFC", value).split())
    if not cleaned or len(cleaned) > 2_000:
        raise LifecycleMutationError(
            "management reopen reason must contain 1 to 2000 characters"
        )
    return cleaned


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LifecycleMutationError(
            f"{field} must include timezone information"
        )
    return value.astimezone(timezone.utc)
