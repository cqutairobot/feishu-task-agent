"""Transactional conversion of audited detection runs into lifecycle tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import StrEnum
import json
import math
import unicodedata
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.context import (
    ContextMessage,
    ContextParticipant,
    TaskDetectionContext,
)
from app.agent.contracts import (
    TaskCandidate,
    TaskOutputError,
    parse_task_detection_batch_json,
)
from app.config import ReminderSettings
from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatSettings,
    DetectionJob,
    DetectionMaterialization,
    DetectionRun,
    DetectionRunFocusMessage,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskEvidence,
    TaskSource,
)
from app.reminders.repository import sync_task_reminders_in_session
from app.notifications.repository import (
    create_task_assignment_notifications_in_session,
)


class TaskStatus(StrEnum):
    PENDING = "pending"
    TODO = "todo"
    DONE = "done"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"


class TaskMaterializationError(RuntimeError):
    """Raised when an audited run cannot safely become task records."""


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: int
    chat_id: str
    owner_open_id: str
    owner_name: str
    title: str
    description: str
    deadline: datetime | None
    status: TaskStatus
    confidence: float
    created_at: datetime
    updated_at: datetime
    assignees: tuple["TaskAssigneeSnapshot", ...] = ()
    review_status: str = "none"
    completion_cycle: int = 0

    @property
    def public_code(self) -> str:
        from app.tasks.codes import format_task_code

        return format_task_code(self.task_id)

    @property
    def responsible_members(self) -> tuple["TaskAssigneeSnapshot", ...]:
        if self.assignees:
            return self.assignees
        return (
            TaskAssigneeSnapshot(
                open_id=self.owner_open_id,
                name=self.owner_name,
                position=0,
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskAssigneeSnapshot:
    open_id: str
    name: str
    position: int


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    detection_run_id: int
    already_materialized: bool
    candidate_count: int
    created_task_count: int
    reused_task_count: int
    task_ids: tuple[int, ...]
    materialized_at: datetime


@dataclass(frozen=True, slots=True)
class TaskListPage:
    chat_id: str
    total_count: int
    tasks: tuple[TaskSnapshot, ...]


@dataclass(frozen=True, slots=True)
class CrossChatTaskEntry:
    task: TaskSnapshot
    chat_name: str | None


@dataclass(frozen=True, slots=True)
class CrossChatTaskListPage:
    total_count: int
    entries: tuple[CrossChatTaskEntry, ...]


class TaskRepository:
    """Materialize exact successful runs with replay-safe provenance."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        auto_todo_confidence: float = 0.85,
        reminder_settings: ReminderSettings = ReminderSettings(),
    ) -> None:
        if (
            isinstance(auto_todo_confidence, bool)
            or not isinstance(auto_todo_confidence, (int, float))
            or not math.isfinite(float(auto_todo_confidence))
            or not 0 <= float(auto_todo_confidence) <= 1
        ):
            raise ValueError("auto_todo_confidence must be between 0 and 1")
        self._session_factory = session_factory
        self._auto_todo_confidence = float(auto_todo_confidence)
        self._reminder_settings = reminder_settings

    def materialize_run(
        self,
        detection_run_id: int,
        *,
        materialized_at: datetime,
    ) -> MaterializationResult:
        """Convert all candidates from one exact run in a single transaction."""

        detection_run_id, materialized_at = _materialization_input(
            detection_run_id, materialized_at
        )
        try:
            with session_scope(self._session_factory) as session:
                connection = session.connection()
                if connection.dialect.name == "sqlite":
                    # Serialize the read-before-write dedupe decision across
                    # explicit CLI processes.
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                return self._materialize_in_session(
                    session,
                    detection_run_id,
                    materialized_at=materialized_at,
                )
        except TaskMaterializationError:
            raise
        except SQLAlchemyError as exc:
            raise TaskMaterializationError(
                "database error while materializing detection run"
            ) from exc

    def materialize_run_in_session(
        self,
        session: Session,
        detection_run_id: int,
        *,
        materialized_at: datetime,
    ) -> MaterializationResult:
        """Materialize inside a caller-owned completion transaction."""

        detection_run_id, materialized_at = _materialization_input(
            detection_run_id, materialized_at
        )
        try:
            return self._materialize_in_session(
                session,
                detection_run_id,
                materialized_at=materialized_at,
            )
        except TaskMaterializationError:
            raise
        except SQLAlchemyError as exc:
            raise TaskMaterializationError(
                "database error while materializing detection run"
            ) from exc

    def _materialize_in_session(
        self,
        session: Session,
        detection_run_id: int,
        *,
        materialized_at: datetime,
    ) -> MaterializationResult:
        existing = session.get(DetectionMaterialization, detection_run_id)
        if existing is not None:
            return self._existing_result(session, existing)

        run = session.get(DetectionRun, detection_run_id)
        if run is None:
            raise TaskMaterializationError(
                f"detection run {detection_run_id} does not exist"
            )
        job = session.get(DetectionJob, run.job_id)
        if job is None:
            raise TaskMaterializationError(
                f"detection run {detection_run_id} has no job"
            )
        if run.status != "succeeded" or run.result_json is None:
            raise TaskMaterializationError(
                "only succeeded detection runs can be materialized"
            )
        if job.status != "completed":
            raise TaskMaterializationError(
                "the detection run's job is not completed"
            )

        context, messages_by_feishu_id = self._historical_context(
            session, job, run
        )
        chat_settings = session.get(ChatSettings, job.chat_id)
        auto_todo_confidence = (
            self._auto_todo_confidence
            if chat_settings is None
            else chat_settings.auto_todo_confidence
        )
        try:
            batch = parse_task_detection_batch_json(run.result_json, context)
        except TaskOutputError as exc:
            raise TaskMaterializationError(
                f"stored detection result is invalid: {exc}"
            ) from exc

        created = 0
        reused = 0
        task_ids: list[int] = []
        for candidate_index, candidate in enumerate(batch.candidates):
            evidence_messages = tuple(
                messages_by_feishu_id[message_id]
                for message_id in candidate.evidence_message_ids
            )
            task = self._find_existing_task(
                session,
                job.chat_id,
                candidate,
                evidence_messages,
            )
            if task is None:
                task = self._create_task(
                    session,
                    job.chat_id,
                    candidate,
                    materialized_at,
                    auto_todo_confidence=auto_todo_confidence,
                )
                created += 1
                create_task_assignment_notifications_in_session(
                    session,
                    task,
                    scheduled_for=materialized_at,
                    max_attempts=self._reminder_settings.max_attempts,
                    reason="created",
                )
            else:
                was_pending = task.status == TaskStatus.PENDING.value
                self._refine_existing_task(task, candidate)
                task.confidence = max(task.confidence, candidate.confidence)
                if (
                    task.status == TaskStatus.PENDING.value
                    and task.confidence >= auto_todo_confidence
                ):
                    task.status = TaskStatus.TODO.value
                task.updated_at = materialized_at
                reused += 1
                if was_pending and task.status == TaskStatus.TODO.value:
                    create_task_assignment_notifications_in_session(
                        session,
                        task,
                        scheduled_for=materialized_at,
                        max_attempts=self._reminder_settings.max_attempts,
                        reason="activated",
                    )

            self._add_evidence(
                session,
                task,
                evidence_messages,
                materialized_at,
            )
            session.add(
                TaskSource(
                    task_id=task.id,
                    detection_run_id=run.id,
                    candidate_index=candidate_index,
                    confidence=candidate.confidence,
                    created_at=materialized_at,
                )
            )
            sync_task_reminders_in_session(
                session,
                task,
                synced_at=materialized_at,
                settings=self._reminder_settings,
            )
            task_ids.append(task.id)

        audit = DetectionMaterialization(
            detection_run_id=run.id,
            candidate_count=len(batch.candidates),
            created_task_count=created,
            reused_task_count=reused,
            materialized_at=materialized_at,
        )
        session.add(audit)
        session.flush()
        return MaterializationResult(
            detection_run_id=run.id,
            already_materialized=False,
            candidate_count=len(batch.candidates),
            created_task_count=created,
            reused_task_count=reused,
            task_ids=tuple(task_ids),
            materialized_at=materialized_at,
        )

    def get_task(self, task_id: int) -> TaskSnapshot | None:
        with session_scope(self._session_factory) as session:
            task = session.get(Task, task_id)
            return None if task is None else _snapshot(task)

    def list_tasks(self, chat_id: str) -> tuple[TaskSnapshot, ...]:
        chat_id = chat_id.strip()
        if not chat_id:
            raise ValueError("chat_id must not be empty")
        with session_scope(self._session_factory) as session:
            tasks = session.scalars(
                select(Task)
                .where(Task.chat_id == chat_id)
                .order_by(Task.id)
            )
            return tuple(_snapshot(task) for task in tasks)

    def list_open_tasks(
        self,
        chat_id: str,
        *,
        owner_open_id: str | None = None,
        limit: int = 20,
    ) -> TaskListPage:
        """List unfinished tasks from one exact chat and no other chat."""

        chat_id = chat_id.strip()
        if not chat_id:
            raise ValueError("chat_id must not be empty")
        owner_open_id = _optional_open_id(owner_open_id)
        _validate_list_limit(limit)
        with session_scope(self._session_factory) as session:
            conditions = [
                Task.chat_id == chat_id,
                Task.status.in_(_open_statuses()),
            ]
            if owner_open_id is not None:
                conditions.append(
                    or_(
                        Task.owner_open_id == owner_open_id,
                        Task.assignees.any(
                            TaskAssignee.open_id == owner_open_id
                        ),
                    )
                )
            total_count = session.scalar(
                select(func.count(Task.id)).where(*conditions)
            ) or 0
            tasks = session.scalars(
                select(Task)
                .where(*conditions)
                .order_by(
                    Task.deadline.is_(None),
                    Task.deadline,
                    Task.id,
                )
                .limit(limit)
            )
            return TaskListPage(
                chat_id=chat_id,
                total_count=total_count,
                tasks=tuple(_snapshot(task) for task in tasks),
            )

    def list_open_tasks_across_chats(
        self,
        *,
        owner_open_id: str | None = None,
        chat_ids: frozenset[str] | None = None,
        limit: int = 20,
    ) -> CrossChatTaskListPage:
        """List open group tasks for one owner or an authorized administrator."""

        owner_open_id = _optional_open_id(owner_open_id)
        normalized_chat_ids = _optional_chat_ids(chat_ids)
        _validate_list_limit(limit)
        with session_scope(self._session_factory) as session:
            conditions = [
                Chat.chat_type == "group",
                Task.status.in_(_open_statuses()),
            ]
            if owner_open_id is not None:
                conditions.append(
                    or_(
                        Task.owner_open_id == owner_open_id,
                        Task.assignees.any(
                            TaskAssignee.open_id == owner_open_id
                        ),
                    )
                )
            if normalized_chat_ids is not None:
                conditions.append(Task.chat_id.in_(normalized_chat_ids))
            total_count = session.scalar(
                select(func.count(Task.id))
                .join(Chat, Chat.chat_id == Task.chat_id)
                .where(*conditions)
            ) or 0
            rows = session.execute(
                select(Task, Chat.name)
                .join(Chat, Chat.chat_id == Task.chat_id)
                .where(*conditions)
                .order_by(
                    Task.deadline.is_(None),
                    Task.deadline,
                    Task.chat_id,
                    Task.id,
                )
                .limit(limit)
            )
            return CrossChatTaskListPage(
                total_count=total_count,
                entries=tuple(
                    CrossChatTaskEntry(
                        task=_snapshot(task),
                        chat_name=chat_name,
                    )
                    for task, chat_name in rows
                ),
            )

    def list_lifecycle_targets(
        self, chat_id: str, *, limit: int = 50
    ) -> tuple[TaskSnapshot, ...]:
        """List only actionable tasks from one chat for lifecycle matching."""

        chat_id = chat_id.strip()
        if not chat_id:
            raise ValueError("chat_id must not be empty")
        _validate_list_limit(limit)
        with session_scope(self._session_factory) as session:
            tasks = session.scalars(
                select(Task)
                .where(
                    Task.chat_id == chat_id,
                    Task.status.in_(
                        (
                            TaskStatus.TODO.value,
                            TaskStatus.OVERDUE.value,
                        )
                    ),
                )
                .order_by(Task.updated_at.desc(), Task.id.desc())
                .limit(limit)
            )
            return tuple(_snapshot(task) for task in tasks)

    def find_lifecycle_target_across_chats(
        self,
        task_id: int,
        *,
        owner_open_id: str | None = None,
        chat_ids: frozenset[str] | None = None,
    ) -> CrossChatTaskEntry | None:
        """Resolve one exact actionable group task within an actor's scope."""

        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
        ):
            raise ValueError("task_id must be a positive integer")
        owner_open_id = _optional_open_id(owner_open_id)
        normalized_chat_ids = _optional_chat_ids(chat_ids)
        with session_scope(self._session_factory) as session:
            conditions = [
                Task.id == task_id,
                Chat.chat_type == "group",
                Task.status.in_(
                    (TaskStatus.TODO.value, TaskStatus.OVERDUE.value)
                ),
            ]
            if owner_open_id is not None:
                conditions.append(
                    or_(
                        Task.owner_open_id == owner_open_id,
                        Task.assignees.any(
                            TaskAssignee.open_id == owner_open_id
                        ),
                    )
                )
            if normalized_chat_ids is not None:
                conditions.append(Task.chat_id.in_(normalized_chat_ids))
            row = session.execute(
                select(Task, Chat.name)
                .join(Chat, Chat.chat_id == Task.chat_id)
                .where(*conditions)
            ).one_or_none()
            if row is None:
                return None
            task, chat_name = row
            return CrossChatTaskEntry(
                task=_snapshot(task),
                chat_name=chat_name,
            )

    def find_review_target_across_chats(
        self,
        task_id: int,
        *,
        chat_ids: frozenset[str] | None = None,
    ) -> CrossChatTaskEntry | None:
        """Resolve one completed task that can be accepted or reopened.

        Authorization is deliberately handled by the caller before any model
        request.  This query only establishes the exact task-code, group and
        review-state boundary used by the read-only review detector.
        """

        if (
            isinstance(task_id, bool)
            or not isinstance(task_id, int)
            or task_id < 1
        ):
            raise ValueError("task_id must be a positive integer")
        normalized_chat_ids = _optional_chat_ids(chat_ids)
        with session_scope(self._session_factory) as session:
            conditions = [
                Task.id == task_id,
                Chat.chat_type == "group",
                Task.status == TaskStatus.DONE.value,
                Task.review_status.in_(("pending", "accepted")),
                Task.completion_cycle >= 1,
            ]
            if normalized_chat_ids is not None:
                conditions.append(Task.chat_id.in_(normalized_chat_ids))
            row = session.execute(
                select(Task, Chat.name)
                .join(Chat, Chat.chat_id == Task.chat_id)
                .where(*conditions)
            ).one_or_none()
            if row is None:
                return None
            task, chat_name = row
            return CrossChatTaskEntry(
                task=_snapshot(task),
                chat_name=chat_name,
            )

    def evidence_message_ids(self, task_id: int) -> tuple[str, ...]:
        with session_scope(self._session_factory) as session:
            return tuple(
                session.scalars(
                    select(Message.message_id)
                    .join(
                        TaskEvidence,
                        TaskEvidence.message_db_id == Message.id,
                    )
                    .where(TaskEvidence.task_id == task_id)
                    .order_by(TaskEvidence.id)
                )
            )

    def source_candidates(self, task_id: int) -> tuple[tuple[int, int], ...]:
        with session_scope(self._session_factory) as session:
            rows = session.execute(
                select(
                    TaskSource.detection_run_id,
                    TaskSource.candidate_index,
                )
                .where(TaskSource.task_id == task_id)
                .order_by(
                    TaskSource.detection_run_id,
                    TaskSource.candidate_index,
                )
            )
            return tuple((row[0], row[1]) for row in rows)

    def _historical_context(
        self,
        session: Session,
        job: DetectionJob,
        run: DetectionRun,
    ) -> tuple[TaskDetectionContext, dict[str, Message]]:
        context_ids = _context_message_ids(
            run.context_message_ids_json,
            trigger_message_id=job.trigger_message_id,
        )
        messages = list(
            session.scalars(
                select(Message).where(
                    Message.chat_id == job.chat_id,
                    Message.message_id.in_(context_ids),
                )
            )
        )
        messages_by_id = {message.message_id: message for message in messages}
        missing = [item for item in context_ids if item not in messages_by_id]
        if missing:
            raise TaskMaterializationError(
                f"run context messages are missing from its chat: {missing}"
            )

        historical_names = _candidate_owner_names(run.result_json or "")
        participants: list[ContextParticipant] = []
        for open_id, name in sorted(historical_names.items()):
            observed = session.scalar(
                select(Message.id)
                .where(
                    Message.chat_id == job.chat_id,
                    Message.sender_open_id == open_id,
                )
                .limit(1)
            )
            if observed is None:
                # An administrator may have assigned a confirmed task name
                # to a directory member who has not spoken in this chat yet.
                # The detection context already grounds the candidate in that
                # alias; require current membership instead of a historical
                # message in this case. Members no longer in the chat still
                # fail closed below.
                active_membership = session.scalar(
                    select(ChatMembership.id)
                    .where(
                        ChatMembership.chat_id == job.chat_id,
                        ChatMembership.open_id == open_id,
                        ChatMembership.active.is_(True),
                    )
                    .limit(1)
                )
                if active_membership is not None:
                    participants.append(
                        ContextParticipant(open_id=open_id, name=name)
                    )
                    continue
                raise TaskMaterializationError(
                    f"candidate owner {open_id} was never observed in job chat"
                )
            participants.append(ContextParticipant(open_id=open_id, name=name))

        ordered_messages = tuple(
            ContextMessage(
                message_id=message.message_id,
                sender_open_id=message.sender_open_id,
                sender_name=(
                    message.sender_name_snapshot or message.sender_open_id
                ),
                content=(
                    message.text_content
                    or f"<{message.message_type} message>"
                ),
                created_at=message.message_created_at,
            )
            for message in (messages_by_id[item] for item in context_ids)
        )
        context = TaskDetectionContext(
            chat_id=job.chat_id,
            trigger_message_id=job.trigger_message_id,
            timezone="Asia/Shanghai",
            reference_time=ordered_messages[-1].created_at,
            participants=tuple(participants),
            messages=ordered_messages,
            focus_message_ids=tuple(
                session.scalars(
                    select(Message.message_id)
                    .join(
                        DetectionRunFocusMessage,
                        DetectionRunFocusMessage.message_db_id == Message.id,
                    )
                    .where(
                        DetectionRunFocusMessage.detection_run_id == run.id
                    )
                    .order_by(DetectionRunFocusMessage.position)
                )
            ),
        )
        return context, messages_by_id

    @staticmethod
    def _find_existing_task(
        session: Session,
        chat_id: str,
        candidate: TaskCandidate,
        evidence_messages: tuple[Message, ...],
    ) -> Task | None:
        deadline_conditions = [Task.deadline == candidate.deadline]
        if candidate.deadline is not None:
            # A later message may supply the deadline for an already-created
            # open task. This is a monotonic refinement, not a new assignment.
            deadline_conditions.append(Task.deadline.is_(None))
        possible = list(
            session.scalars(
                select(Task)
                .join(TaskEvidence, TaskEvidence.task_id == Task.id)
                .where(
                    Task.chat_id == chat_id,
                    or_(*deadline_conditions),
                    TaskEvidence.message_db_id.in_(
                        tuple(message.id for message in evidence_messages)
                    ),
                )
                .distinct()
                .order_by(Task.id)
            )
        )
        candidate_open_ids = {
            owner.open_id for owner in candidate.owners
        }
        matches = [
            task
            for task in possible
            if {
                assignee.open_id
                for assignee in _task_assignee_snapshots(task)
            }
            == candidate_open_ids
            and _candidate_matches_existing_task(task, candidate)
        ]
        if len(matches) > 1:
            raise TaskMaterializationError(
                "candidate ambiguously matches multiple existing tasks"
            )
        return None if not matches else matches[0]

    @staticmethod
    def _refine_existing_task(task: Task, candidate: TaskCandidate) -> None:
        """Apply only safe, monotonic details learned by a later run."""

        if (
            task.created_via == "detected"
            and task.created_by_open_id is None
            and candidate.publisher is not None
        ):
            task.created_by_open_id = candidate.publisher.open_id
            task.created_by_name = candidate.publisher.name
            task.creator_attribution_basis = (
                candidate.publisher_attribution_basis
            )
            task.creator_attribution_confidence = (
                candidate.publisher_attribution_confidence
            )

        if task.deadline is not None or candidate.deadline is None:
            return
        if task.status not in {
            TaskStatus.PENDING.value,
            TaskStatus.TODO.value,
        }:
            return
        task.deadline = candidate.deadline
        if candidate.confidence >= task.confidence:
            task.title = candidate.title
            task.normalized_title = _normalize_title(candidate.title)
            task.description = candidate.description

    def _create_task(
        self,
        session: Session,
        chat_id: str,
        candidate: TaskCandidate,
        created_at: datetime,
        auto_todo_confidence: float,
    ) -> Task:
        status = (
            TaskStatus.TODO
            if candidate.confidence >= auto_todo_confidence
            else TaskStatus.PENDING
        )
        task = Task(
            chat_id=chat_id,
            owner_open_id=candidate.owner.open_id,
            owner_name_snapshot=candidate.owner.name,
            created_by_open_id=(
                None
                if candidate.publisher is None
                else candidate.publisher.open_id
            ),
            created_by_name=(
                None if candidate.publisher is None else candidate.publisher.name
            ),
            created_via="detected",
            creator_attribution_basis=candidate.publisher_attribution_basis,
            creator_attribution_confidence=(
                candidate.publisher_attribution_confidence
            ),
            title=candidate.title,
            normalized_title=_normalize_title(candidate.title),
            description=candidate.description,
            deadline=candidate.deadline,
            status=status.value,
            confidence=candidate.confidence,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(task)
        session.flush()
        session.add_all(
            TaskAssignee(
                task_id=task.id,
                open_id=owner.open_id,
                name_snapshot=owner.name,
                position=position,
                created_at=created_at,
            )
            for position, owner in enumerate(candidate.owners)
        )
        session.flush()
        return task

    @staticmethod
    def _add_evidence(
        session: Session,
        task: Task,
        messages: tuple[Message, ...],
        created_at: datetime,
    ) -> None:
        existing_ids = set(
            session.scalars(
                select(TaskEvidence.message_db_id).where(
                    TaskEvidence.task_id == task.id
                )
            )
        )
        for message in messages:
            if message.id not in existing_ids:
                session.add(
                    TaskEvidence(
                        task_id=task.id,
                        message_db_id=message.id,
                        created_at=created_at,
                    )
                )
                existing_ids.add(message.id)

    @staticmethod
    def _existing_result(
        session: Session, audit: DetectionMaterialization
    ) -> MaterializationResult:
        task_ids = tuple(
            session.scalars(
                select(TaskSource.task_id)
                .where(
                    TaskSource.detection_run_id == audit.detection_run_id
                )
                .order_by(TaskSource.candidate_index)
            )
        )
        if len(task_ids) != audit.candidate_count:
            raise TaskMaterializationError(
                "materialization audit and candidate sources disagree"
            )
        return MaterializationResult(
            detection_run_id=audit.detection_run_id,
            already_materialized=True,
            candidate_count=audit.candidate_count,
            created_task_count=audit.created_task_count,
            reused_task_count=audit.reused_task_count,
            task_ids=task_ids,
            materialized_at=audit.materialized_at,
        )


def _context_message_ids(
    payload: str, *, trigger_message_id: str
) -> tuple[str, ...]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TaskMaterializationError("run context message IDs are invalid JSON") from exc
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise TaskMaterializationError(
            "run context message IDs must be a non-empty unique string array"
        )
    result = tuple(value)
    if result[-1] != trigger_message_id:
        raise TaskMaterializationError(
            "run context does not end at the job trigger message"
        )
    return result


def _materialization_input(
    detection_run_id: int, materialized_at: datetime
) -> tuple[int, datetime]:
    if (
        isinstance(detection_run_id, bool)
        or not isinstance(detection_run_id, int)
        or detection_run_id < 1
    ):
        raise TaskMaterializationError(
            "detection_run_id must be a positive integer"
        )
    return detection_run_id, _aware_utc(materialized_at, "materialized_at")


def _candidate_owner_names(payload: str) -> dict[str, str]:
    """Extract historical owner and publisher pairs before grounding.

    Stored detection results are replayed against a reconstructed context.
    Publisher attribution was added after the original result contract, so
    this helper accepts both old candidates (owner only) and new candidates
    that also carry a publisher.  The resulting participant set is still
    limited to identities explicitly present in the stored model output.
    """

    try:
        value: Any = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    candidates = value.get("candidates") if isinstance(value, dict) else None
    if not isinstance(candidates, list):
        return {}
    names: dict[str, str] = {}
    normalized_names: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_owners = [candidate.get("owner"), candidate.get("publisher")]
        co_owners = candidate.get("co_owners", [])
        if isinstance(co_owners, list):
            raw_owners.extend(co_owners)
        for owner in raw_owners:
            if not isinstance(owner, dict):
                continue
            open_id = owner.get("open_id")
            name = owner.get("name")
            if not isinstance(open_id, str) or not isinstance(name, str):
                continue
            cleaned_name = " ".join(
                unicodedata.normalize("NFKC", name).split()
            )
            previous = names.get(open_id)
            normalized_name = cleaned_name.casefold()
            if (
                previous is not None
                and normalized_names[open_id] != normalized_name
            ):
                raise TaskMaterializationError(
                    f"stored result has conflicting names for owner {open_id}"
                )
            names[open_id] = cleaned_name
            normalized_names[open_id] = normalized_name
    return names


def _normalize_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _candidate_matches_existing_task(
    task: Task, candidate: TaskCandidate
) -> bool:
    candidate_title = _normalize_title(candidate.title)
    is_deadline_refinement = (
        task.deadline is None and candidate.deadline is not None
    )
    if is_deadline_refinement and task.status not in {
        TaskStatus.PENDING.value,
        TaskStatus.TODO.value,
    }:
        return False
    if task.normalized_title == candidate_title:
        return True
    if not is_deadline_refinement:
        return False
    return _titles_are_likely_refinement(
        task.normalized_title,
        candidate_title,
    )


def _titles_are_likely_refinement(left: str, right: str) -> bool:
    """Conservatively recognize a richer title for one unfinished task.

    Shared evidence and an identical assignee set are already required by the
    caller. Requiring both sequence similarity and several shared trigrams
    avoids treating short generic phrases such as ``登录页`` and ``登录接口`` as
    the same task merely because a later candidate adds a deadline.
    """

    left_compact = "".join(left.split())
    right_compact = "".join(right.split())
    if left_compact == right_compact:
        return True
    if min(len(left_compact), len(right_compact)) < 5:
        return False
    similarity = SequenceMatcher(
        None,
        left_compact,
        right_compact,
        autojunk=False,
    ).ratio()
    if similarity < 0.65:
        return False
    left_trigrams = _character_ngrams(left_compact, size=3)
    right_trigrams = _character_ngrams(right_compact, size=3)
    return len(left_trigrams & right_trigrams) >= 3


def _character_ngrams(value: str, *, size: int) -> set[str]:
    return {
        value[index : index + size]
        for index in range(len(value) - size + 1)
    }


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskMaterializationError(f"{field} must include timezone information")
    return value.astimezone(timezone.utc)


def _open_statuses() -> tuple[str, ...]:
    return (
        TaskStatus.PENDING.value,
        TaskStatus.TODO.value,
        TaskStatus.OVERDUE.value,
    )


def _validate_list_limit(limit: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
    ):
        raise ValueError("limit must be between 1 and 100")


def _optional_open_id(open_id: str | None) -> str | None:
    if open_id is None:
        return None
    if not isinstance(open_id, str) or not open_id.strip():
        raise ValueError("owner_open_id must not be empty")
    return open_id.strip()


def _optional_chat_ids(
    chat_ids: frozenset[str] | None,
) -> frozenset[str] | None:
    if chat_ids is None:
        return None
    if not isinstance(chat_ids, frozenset) or any(
        not isinstance(chat_id, str) or not chat_id.strip()
        for chat_id in chat_ids
    ):
        raise ValueError("chat_ids must contain only non-empty strings")
    return frozenset(chat_id.strip() for chat_id in chat_ids)


def _snapshot(task: Task) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task.id,
        chat_id=task.chat_id,
        owner_open_id=task.owner_open_id,
        owner_name=task.owner_name_snapshot,
        title=task.title,
        description=task.description,
        deadline=task.deadline,
        status=TaskStatus(task.status),
        confidence=task.confidence,
        created_at=task.created_at,
        updated_at=task.updated_at,
        assignees=_task_assignee_snapshots(task),
        review_status=task.review_status,
        completion_cycle=task.completion_cycle,
    )


def _task_assignee_snapshots(
    task: Task,
) -> tuple[TaskAssigneeSnapshot, ...]:
    if task.assignees:
        return tuple(
            TaskAssigneeSnapshot(
                open_id=assignee.open_id,
                name=assignee.name_snapshot,
                position=assignee.position,
            )
            for assignee in task.assignees
        )
    return (
        TaskAssigneeSnapshot(
            open_id=task.owner_open_id,
            name=task.owner_name_snapshot,
            position=0,
        ),
    )
