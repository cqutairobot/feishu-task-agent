"""Append-only, authorized task notes with stable idempotency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatMemberAlias,
    ChatMembership,
    Message,
    Task,
    TaskAssignee,
    TaskNote,
    User,
)
from app.tasks.codes import format_task_code


MAX_TASK_NOTE_CONTENT_LENGTH = 8_000


class TaskNoteType(StrEnum):
    PROGRESS = "progress"
    BLOCKER = "blocker"
    COMPLETION = "completion"
    DELAY = "delay"
    REOPEN = "reopen"
    GENERAL = "general"
    CORRECTION = "correction"


class TaskNoteError(RuntimeError):
    """Base error for a task-note mutation that cannot be applied."""


class TaskNoteAccessDenied(TaskNoteError):
    """Raised without leaking whether an unauthorized task exists."""


class TaskNoteConflict(TaskNoteError):
    """Raised when the task or an idempotent replay conflicts."""


@dataclass(frozen=True, slots=True)
class TaskNoteModelAudit:
    """Minimal provider metadata retained for a model-originated note."""

    provider: str
    model: str
    response_format: str
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class TaskNoteResult:
    note_id: int
    task_id: int
    task_code: str
    author_open_id: str
    author_name: str
    note_type: TaskNoteType
    content: str
    source_message_id: str | None
    source_chat_id: str | None
    completion_cycle: int
    idempotency_key: str
    confidence: float | None
    provider: str | None
    model: str | None
    response_format: str | None
    model_request_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    already_created: bool
    created_at: datetime


class TaskNoteService:
    """Append immutable notes after checking exact-group authorization."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append(
        self,
        *,
        actor_open_id: str,
        chat_id: str,
        task_id: int,
        note_type: TaskNoteType | str,
        content: str,
        idempotency_key: str,
        created_at: datetime,
        source_message_id: str | None = None,
        source_chat_id: str | None = None,
        model_audit: TaskNoteModelAudit | None = None,
    ) -> TaskNoteResult:
        actor_open_id = _required_text(actor_open_id, "actor_open_id", 128)
        chat_id = _required_text(chat_id, "chat_id", 128)
        task_id = _positive_integer(task_id, "task_id")
        note_type = _note_type(note_type)
        content = _required_text(
            content,
            "content",
            MAX_TASK_NOTE_CONTENT_LENGTH,
        )
        idempotency_key = _required_text(
            idempotency_key, "idempotency_key", 128
        )
        created_at = _aware_utc(created_at, "created_at")
        if source_message_id is not None:
            source_message_id = _required_text(
                source_message_id, "source_message_id", 128
            )
            source_chat_id = _required_text(
                source_chat_id or chat_id, "source_chat_id", 128
            )
        elif source_chat_id is not None:
            source_chat_id = _required_text(
                source_chat_id, "source_chat_id", 128
            )
        model_audit = _validate_model_audit(model_audit)

        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")

            existing = session.scalar(
                select(TaskNote).where(
                    TaskNote.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                _validate_replay(
                    existing,
                    actor_open_id=actor_open_id,
                    chat_id=chat_id,
                    task_id=task_id,
                    note_type=note_type,
                    content=content,
                    source_message_id=source_message_id,
                    source_chat_id=source_chat_id,
                )
                return _result(existing, already_created=True)

            chat = session.get(Chat, chat_id)
            task = session.get(Task, task_id)
            if (
                chat is None
                or chat.chat_type != "group"
                or not chat.enabled
                or task is None
                or task.chat_id != chat_id
            ):
                raise TaskNoteConflict("task is unavailable in this chat")
            if task.merged_into_task_id is not None:
                raise TaskNoteConflict("merged tasks cannot receive new notes")

            is_administrator, is_assignee = _authorization(
                session,
                task,
                actor_open_id,
            )
            if not is_administrator and not is_assignee:
                raise TaskNoteAccessDenied(
                    "actor is not authorized to write this task note"
                )
            if note_type in {
                TaskNoteType.REOPEN,
                TaskNoteType.CORRECTION,
            } and not is_administrator:
                raise TaskNoteAccessDenied(
                    "this task note type requires an administrator"
                )

            if source_message_id is not None:
                _validate_source_message(
                    session,
                    source_message_id,
                    chat_id=source_chat_id or chat_id,
                    actor_open_id=actor_open_id,
                )

            note = TaskNote(
                task_id=task.id,
                chat_id=chat_id,
                author_open_id=actor_open_id,
                author_name_snapshot=_author_name(
                    session, chat_id, actor_open_id
                ),
                note_type=note_type.value,
                content=content,
                source_message_id=source_message_id,
                source_chat_id=source_chat_id,
                completion_cycle=task.completion_cycle,
                idempotency_key=idempotency_key,
                confidence=(
                    None if model_audit is None else model_audit.confidence
                ),
                provider=(None if model_audit is None else model_audit.provider),
                model=(None if model_audit is None else model_audit.model),
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
                created_at=created_at,
            )
            session.add(note)
            session.flush()
            return _result(note, already_created=False)


def build_task_note_idempotency_key(source: str, token: str) -> str:
    """Build a bounded key without storing a potentially long raw token."""

    source = _required_text(source, "source", 24)
    token = _required_text(token, "token", 512)
    digest = hashlib.sha256(
        f"{source}\0{token}".encode("utf-8")
    ).hexdigest()
    return f"task-note:{source}:{digest}"


def _authorization(
    session: Session,
    task: Task,
    actor_open_id: str,
) -> tuple[bool, bool]:
    active_member = session.scalar(
        select(ChatMembership.id)
        .where(
            ChatMembership.chat_id == task.chat_id,
            ChatMembership.open_id == actor_open_id,
            ChatMembership.active.is_(True),
        )
        .limit(1)
    )
    if active_member is None:
        raise TaskNoteAccessDenied(
            "actor is not a current member of this task chat"
        )
    is_administrator = session.scalar(
        select(ChatAdministrator.id)
        .where(
            ChatAdministrator.chat_id == task.chat_id,
            ChatAdministrator.open_id == actor_open_id,
        )
        .limit(1)
    ) is not None
    is_assignee = actor_open_id == task.owner_open_id or session.scalar(
        select(TaskAssignee.id)
        .where(
            TaskAssignee.task_id == task.id,
            TaskAssignee.open_id == actor_open_id,
        )
        .limit(1)
    ) is not None
    return is_administrator, is_assignee


def _validate_source_message(
    session: Session,
    source_message_id: str,
    *,
    chat_id: str,
    actor_open_id: str,
) -> None:
    message = session.scalar(
        select(Message).where(Message.message_id == source_message_id)
    )
    if (
        message is None
        or message.chat_id != chat_id
        or message.sender_open_id != actor_open_id
    ):
        raise TaskNoteConflict(
            "source message does not belong to this actor and task chat"
        )


def _author_name(session: Session, chat_id: str, open_id: str) -> str:
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
    name = session.scalar(select(User.name).where(User.open_id == open_id))
    return name or open_id


def _validate_replay(
    existing: TaskNote,
    *,
    actor_open_id: str,
    chat_id: str,
    task_id: int,
    note_type: TaskNoteType,
    content: str,
    source_message_id: str | None,
    source_chat_id: str | None,
) -> None:
    if (
        existing.author_open_id != actor_open_id
        or existing.chat_id != chat_id
        or existing.task_id != task_id
        or existing.note_type != note_type.value
        or existing.content != content
        or existing.source_message_id != source_message_id
        or existing.source_chat_id != source_chat_id
    ):
        raise TaskNoteConflict(
            "idempotency key was already used for a different task note"
        )


def _result(note: TaskNote, *, already_created: bool) -> TaskNoteResult:
    return TaskNoteResult(
        note_id=note.id,
        task_id=note.task_id,
        task_code=format_task_code(note.task_id),
        author_open_id=note.author_open_id,
        author_name=note.author_name_snapshot,
        note_type=TaskNoteType(note.note_type),
        content=note.content,
        source_message_id=note.source_message_id,
        source_chat_id=note.source_chat_id,
        completion_cycle=note.completion_cycle,
        idempotency_key=note.idempotency_key or "",
        confidence=note.confidence,
        provider=note.provider,
        model=note.model,
        response_format=note.response_format,
        model_request_id=note.model_request_id,
        prompt_tokens=note.prompt_tokens,
        completion_tokens=note.completion_tokens,
        total_tokens=note.total_tokens,
        already_created=already_created,
        created_at=note.created_at,
    )


def _note_type(value: TaskNoteType | str) -> TaskNoteType:
    if isinstance(value, TaskNoteType):
        return value
    if not isinstance(value, str):
        raise ValueError("note_type must be a string")
    try:
        return TaskNoteType(value.strip())
    except ValueError as exc:
        raise ValueError("note_type is invalid") from exc


def _positive_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{field} must contain 1-{maximum} characters")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_model_audit(
    value: TaskNoteModelAudit | None,
) -> TaskNoteModelAudit | None:
    if value is None:
        return None
    if not isinstance(value, TaskNoteModelAudit):
        raise ValueError("model_audit must be a TaskNoteModelAudit")
    # Keep validation limits aligned with the persisted column sizes.  This
    # prevents an otherwise valid-looking audit object from failing later at
    # the database layer (and also bounds request identifiers retained from a
    # provider response).
    _required_text(value.provider, "provider", 32)
    _required_text(value.model, "model", 128)
    _required_text(value.response_format, "response_format", 32)
    if value.request_id is not None:
        _required_text(value.request_id, "request_id", 128)
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = getattr(value, field)
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item < 0
        ):
            raise ValueError(f"{field} must be a non-negative integer")
    if value.confidence is not None:
        if (
            isinstance(value.confidence, bool)
            or not isinstance(value.confidence, (int, float))
            or not 0 <= value.confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")
    return value
