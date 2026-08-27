"""Atomic, idempotent persistence for normalized Feishu messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from collections.abc import Mapping
import json

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatAdministratorEvent,
    ChatMemberAlias,
    ChatMembership,
    DetectionJob,
    DetectionRun,
    DetectionMaterialization,
    Message,
    ManagementSession,
    Task,
    TaskReminder,
    User,
)
from app.feishu.messages import IncomingMessage


class SaveStatus(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"


class DetectionEnqueueStatus(StrEnum):
    CREATED = "created"
    COALESCED = "coalesced"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DetectionEnqueueResult:
    status: DetectionEnqueueStatus
    job_id: int | None
    trigger_message_id: str | None
    available_at: datetime | None


@dataclass(frozen=True, slots=True)
class SaveResult:
    status: SaveStatus
    message_id: str
    detection: DetectionEnqueueResult

    @property
    def inserted(self) -> bool:
        return self.status is SaveStatus.INSERTED


@dataclass(frozen=True, slots=True)
class StoreCounts:
    chats: int
    users: int
    messages: int
    aliases: int
    detection_jobs: int
    detection_runs: int
    tasks: int
    task_materializations: int
    task_reminders: int


@dataclass(frozen=True, slots=True)
class DirectoryUpdateResult:
    chats_updated: int
    users_updated: int
    message_snapshots_updated: int
    memberships_created: int = 0
    memberships_updated: int = 0
    memberships_deactivated: int = 0
    aliases_released: int = 0
    administrators_revoked: tuple[str, ...] = ()
    management_sessions_revoked: int = 0


@dataclass(frozen=True, slots=True)
class ConversationLine:
    message_id: str
    sender_open_id: str
    sender_name: str
    content: str
    created_at: datetime
    received_at: datetime
    is_from_bot: bool
    mentions: tuple["ConversationMention", ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationMention:
    """The exact Feishu user mapping for one inline mention."""

    key: str
    open_id: str
    name: str | None


class MessageLookupError(LookupError):
    """Raised when an anchored conversation cannot resolve its trigger."""


class TenantIsolationError(RuntimeError):
    """Raised when a second Feishu tenant targets a single-tenant database."""


class MessageRepository:
    """Persist a message and its chat/user references in one transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save(
        self,
        incoming: IncomingMessage,
        *,
        enqueue_detection: bool = False,
        debounce_seconds: int = 5,
    ) -> SaveResult:
        if not 0 <= debounce_seconds <= 60:
            raise ValueError("debounce_seconds must be between 0 and 60")
        with session_scope(self._session_factory) as session:
            _require_database_tenant(session, incoming.tenant_key)
            self._upsert_chat(session, incoming)
            self._upsert_user(session, incoming)
            result = session.execute(
                sqlite_insert(Message)
                .values(
                    tenant_key=incoming.tenant_key,
                    event_id=incoming.event_id,
                    message_id=incoming.message_id,
                    chat_id=incoming.chat_id,
                    sender_open_id=incoming.sender_open_id,
                    sender_name_snapshot=incoming.sender_name,
                    message_type=incoming.message_type,
                    text_content=incoming.text,
                    raw_content=incoming.raw_content,
                    raw_event_json=incoming.raw_event_json,
                    root_id=incoming.root_id,
                    parent_id=incoming.parent_id,
                    message_created_at=incoming.created_at,
                    received_at=incoming.received_at,
                    is_from_bot=incoming.sender_type == "bot",
                )
                .on_conflict_do_nothing()
            )
            inserted = result.rowcount == 1
            if inserted and enqueue_detection:
                detection = self._enqueue_detection(
                    session,
                    incoming,
                    message_database_id=result.inserted_primary_key[0],
                    debounce_seconds=debounce_seconds,
                )
            else:
                detection = DetectionEnqueueResult(
                    status=DetectionEnqueueStatus.SKIPPED,
                    job_id=None,
                    trigger_message_id=None,
                    available_at=None,
                )

        return SaveResult(
            status=SaveStatus.INSERTED if inserted else SaveStatus.DUPLICATE,
            message_id=incoming.message_id,
            detection=detection,
        )

    def list_recent(self, chat_id: str, *, limit: int = 50) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with session_scope(self._session_factory) as session:
            newest_first = list(
                session.scalars(
                    select(Message)
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.message_created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            return list(reversed(newest_first))

    def count(self) -> int:
        with session_scope(self._session_factory) as session:
            return session.scalar(select(func.count(Message.id))) or 0

    def counts(self) -> StoreCounts:
        with session_scope(self._session_factory) as session:
            return StoreCounts(
                chats=session.scalar(select(func.count(Chat.chat_id))) or 0,
                users=session.scalar(select(func.count(User.open_id))) or 0,
                messages=session.scalar(select(func.count(Message.id))) or 0,
                aliases=session.scalar(select(func.count(ChatMemberAlias.id))) or 0,
                detection_jobs=(
                    session.scalar(select(func.count(DetectionJob.id))) or 0
                ),
                detection_runs=(
                    session.scalar(select(func.count(DetectionRun.id))) or 0
                ),
                tasks=session.scalar(select(func.count(Task.id))) or 0,
                task_materializations=(
                    session.scalar(
                        select(
                            func.count(
                                DetectionMaterialization.detection_run_id
                            )
                        )
                    )
                    or 0
                ),
                task_reminders=(
                    session.scalar(select(func.count(TaskReminder.id))) or 0
                ),
            )

    def apply_directory_snapshot(
        self,
        chat_id: str,
        chat_name: str,
        member_names: Mapping[str, str],
        *,
        owner_open_id: str | None = None,
        member_tenant_keys: Mapping[str, str | None] | None = None,
        authoritative_members: bool = False,
        chat_type: str | None = None,
        tenant_key: str | None = None,
        updated_at: datetime,
    ) -> DirectoryUpdateResult:
        """Persist mutable directory data and an authoritative member set."""

        if updated_at.tzinfo is None:
            raise ValueError("updated_at must include timezone information")
        if authoritative_members and not member_names:
            raise ValueError("authoritative member snapshot cannot be empty")
        if owner_open_id is not None and owner_open_id not in member_names:
            raise ValueError("chat owner must be present in the member snapshot")
        member_tenant_keys = member_tenant_keys or {}

        with session_scope(self._session_factory) as session:
            chat = session.get(Chat, chat_id)
            if chat is None:
                if not tenant_key or not chat_type:
                    raise ValueError(
                        "tenant_key and chat_type are required for a new chat"
                    )
                _require_database_tenant(session, tenant_key)
                chat = Chat(
                    chat_id=chat_id,
                    tenant_key=tenant_key,
                    name=chat_name,
                    chat_type=chat_type,
                    enabled=True,
                    created_at=updated_at,
                    updated_at=updated_at,
                )
                session.add(chat)
                session.flush()
            else:
                if tenant_key is not None and tenant_key != chat.tenant_key:
                    raise TenantIsolationError(
                        "directory snapshot tenant does not match its chat"
                    )
                chat.name = chat_name
                chat.updated_at = updated_at
            if (
                authoritative_members
                and chat.chat_type == "group"
                and owner_open_id is None
            ):
                raise ValueError("group directory snapshot has no owner")

            users_updated = 0
            snapshots_updated = 0
            for open_id, name in member_names.items():
                user = session.get(User, open_id)
                if user is None:
                    user_tenant_key = member_tenant_keys.get(open_id) or (
                        tenant_key or chat.tenant_key
                    )
                    user = User(
                        open_id=open_id,
                        union_id=None,
                        name=name,
                        tenant_key=user_tenant_key,
                        last_seen_at=updated_at,
                        created_at=updated_at,
                        updated_at=updated_at,
                    )
                    session.add(user)
                else:
                    user.name = name
                    user.updated_at = updated_at
                users_updated += 1
                snapshot_result = session.execute(
                    update(Message)
                    .where(
                        Message.sender_open_id == open_id,
                        Message.sender_name_snapshot.is_(None),
                    )
                    .values(sender_name_snapshot=name)
                )
                snapshots_updated += snapshot_result.rowcount

            memberships_created = 0
            memberships_updated = 0
            memberships_deactivated = 0
            aliases_released = 0
            administrators_revoked: tuple[str, ...] = ()
            management_sessions_revoked = 0
            if authoritative_members:
                session.flush()
                existing = {
                    item.open_id: item
                    for item in session.scalars(
                        select(ChatMembership).where(
                            ChatMembership.chat_id == chat_id
                        )
                    )
                }
                for open_id, name in member_names.items():
                    membership = existing.pop(open_id, None)
                    if membership is None:
                        session.add(
                            ChatMembership(
                                chat_id=chat_id,
                                open_id=open_id,
                                display_name_snapshot=name,
                                active=True,
                                is_owner=open_id == owner_open_id,
                                first_synced_at=updated_at,
                                last_synced_at=updated_at,
                                left_at=None,
                            )
                        )
                        memberships_created += 1
                    else:
                        membership.display_name_snapshot = name
                        membership.active = True
                        membership.is_owner = open_id == owner_open_id
                        membership.last_synced_at = updated_at
                        membership.left_at = None
                        memberships_updated += 1
                for membership in existing.values():
                    if membership.active:
                        memberships_deactivated += 1
                    membership.active = False
                    membership.is_owner = False
                    membership.last_synced_at = updated_at
                    membership.left_at = updated_at

                current_open_ids = frozenset(member_names)
                released = session.execute(
                    delete(ChatMemberAlias).where(
                        ChatMemberAlias.chat_id == chat_id,
                        ChatMemberAlias.open_id.not_in(current_open_ids),
                    )
                )
                aliases_released = released.rowcount
                departed_administrators = tuple(
                    item
                    for item in session.scalars(
                        select(ChatAdministrator).where(
                            ChatAdministrator.chat_id == chat_id
                        )
                    )
                    if item.open_id not in current_open_ids
                )
                administrators_revoked = tuple(
                    item.open_id for item in departed_administrators
                )
                for administrator in departed_administrators:
                    session.add(
                        ChatAdministratorEvent(
                            chat_id=chat_id,
                            target_open_id=administrator.open_id,
                            actor_open_id=None,
                            action="revoke",
                            source="membership_sync",
                            created_at=updated_at,
                        )
                    )
                    session.delete(administrator)
                if departed_administrators:
                    session.flush()
                    for open_id in administrators_revoked:
                        still_administrator = session.scalar(
                            select(ChatAdministrator.id)
                            .where(ChatAdministrator.open_id == open_id)
                            .limit(1)
                        )
                        if still_administrator is None:
                            result = session.execute(
                                update(ManagementSession)
                                .where(
                                    ManagementSession.actor_open_id == open_id,
                                    ManagementSession.revoked_at.is_(None),
                                )
                                .values(revoked_at=updated_at)
                            )
                            management_sessions_revoked += result.rowcount

        return DirectoryUpdateResult(
            chats_updated=1,
            users_updated=users_updated,
            message_snapshots_updated=snapshots_updated,
            memberships_created=memberships_created,
            memberships_updated=memberships_updated,
            memberships_deactivated=memberships_deactivated,
            aliases_released=aliases_released,
            administrators_revoked=administrators_revoked,
            management_sessions_revoked=management_sessions_revoked,
        )

    def conversation(self, chat_id: str, *, limit: int = 50) -> list[ConversationLine]:
        """Return recent messages oldest-first with the best available name."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        display_name = func.coalesce(
            ChatMemberAlias.alias,
            Message.sender_name_snapshot,
            User.name,
            Message.sender_open_id,
        ).label("display_name")
        with session_scope(self._session_factory) as session:
            rows = list(
                session.execute(
                    select(Message, display_name)
                    .outerjoin(User, User.open_id == Message.sender_open_id)
                    .outerjoin(
                        ChatMemberAlias,
                        and_(
                            ChatMemberAlias.chat_id == Message.chat_id,
                            ChatMemberAlias.open_id == Message.sender_open_id,
                        ),
                    )
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.message_created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            lines = [
                ConversationLine(
                    message_id=message.message_id,
                    sender_open_id=message.sender_open_id,
                    sender_name=name,
                    content=message.text_content or f"<{message.message_type} message>",
                    created_at=message.message_created_at,
                    received_at=message.received_at,
                    is_from_bot=message.is_from_bot,
                    mentions=_conversation_mentions(message.raw_event_json),
                )
                for message, name in rows
            ]
        return list(reversed(lines))

    def conversation_through(
        self,
        chat_id: str,
        trigger_message_id: str,
        *,
        limit: int = 30,
        include_bots: bool = False,
    ) -> list[ConversationLine]:
        """Return a stable chat-only window ending at one trigger message."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        display_name = func.coalesce(
            ChatMemberAlias.alias,
            Message.sender_name_snapshot,
            User.name,
            Message.sender_open_id,
        ).label("display_name")
        with session_scope(self._session_factory) as session:
            trigger = session.scalar(
                select(Message).where(
                    Message.chat_id == chat_id,
                    Message.message_id == trigger_message_id,
                )
            )
            if trigger is None:
                raise MessageLookupError(
                    f"message {trigger_message_id} was not found in chat {chat_id}"
                )

            conditions = [
                Message.chat_id == chat_id,
                or_(
                    Message.message_created_at < trigger.message_created_at,
                    and_(
                        Message.message_created_at == trigger.message_created_at,
                        Message.id <= trigger.id,
                    ),
                ),
            ]
            if not include_bots:
                conditions.append(Message.is_from_bot.is_(False))

            rows = list(
                session.execute(
                    select(Message, display_name)
                    .outerjoin(User, User.open_id == Message.sender_open_id)
                    .outerjoin(
                        ChatMemberAlias,
                        and_(
                            ChatMemberAlias.chat_id == Message.chat_id,
                            ChatMemberAlias.open_id == Message.sender_open_id,
                        ),
                    )
                    .where(*conditions)
                    .order_by(Message.message_created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            )
            lines = [
                ConversationLine(
                    message_id=message.message_id,
                    sender_open_id=message.sender_open_id,
                    sender_name=name,
                    content=message.text_content
                    or f"<{message.message_type} message>",
                    created_at=message.message_created_at,
                    received_at=message.received_at,
                    is_from_bot=message.is_from_bot,
                    mentions=_conversation_mentions(message.raw_event_json),
                )
                for message, name in rows
            ]
        return list(reversed(lines))

    @staticmethod
    def _upsert_chat(session: Session, incoming: IncomingMessage) -> None:
        statement = sqlite_insert(Chat).values(
            chat_id=incoming.chat_id,
            tenant_key=incoming.tenant_key,
            name=None,
            chat_type=incoming.chat_type,
            enabled=True,
            created_at=incoming.received_at,
            updated_at=incoming.received_at,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Chat.chat_id],
                set_={
                    "tenant_key": statement.excluded.tenant_key,
                    "chat_type": statement.excluded.chat_type,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    @staticmethod
    def _upsert_user(session: Session, incoming: IncomingMessage) -> None:
        statement = sqlite_insert(User).values(
            open_id=incoming.sender_open_id,
            union_id=incoming.sender_union_id,
            name=incoming.sender_name,
            tenant_key=incoming.tenant_key,
            last_seen_at=incoming.created_at,
            created_at=incoming.received_at,
            updated_at=incoming.received_at,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[User.open_id],
                set_={
                    "union_id": func.coalesce(
                        statement.excluded.union_id, User.union_id
                    ),
                    "name": func.coalesce(statement.excluded.name, User.name),
                    "tenant_key": statement.excluded.tenant_key,
                    "last_seen_at": func.max(
                        User.last_seen_at, statement.excluded.last_seen_at
                    ),
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    @staticmethod
    def _enqueue_detection(
        session: Session,
        incoming: IncomingMessage,
        *,
        message_database_id: int,
        debounce_seconds: int,
    ) -> DetectionEnqueueResult:
        available_at = incoming.received_at + timedelta(
            seconds=debounce_seconds
        )
        pending = session.scalar(
            select(DetectionJob)
            .where(
                DetectionJob.chat_id == incoming.chat_id,
                DetectionJob.status == "queued",
                DetectionJob.attempt_count == 0,
                DetectionJob.available_at > incoming.received_at,
            )
            .order_by(DetectionJob.available_at.desc(), DetectionJob.id.desc())
            .limit(1)
        )
        if pending is not None:
            current_trigger = session.execute(
                select(Message.message_created_at, Message.id).where(
                    Message.chat_id == incoming.chat_id,
                    Message.message_id == pending.trigger_message_id,
                )
            ).one()
            if (incoming.created_at, message_database_id) >= (
                current_trigger.message_created_at,
                current_trigger.id,
            ):
                pending.trigger_message_id = incoming.message_id
            pending.available_at = available_at
            pending.updated_at = incoming.received_at
            session.flush()
            return DetectionEnqueueResult(
                status=DetectionEnqueueStatus.COALESCED,
                job_id=pending.id,
                trigger_message_id=pending.trigger_message_id,
                available_at=pending.available_at,
            )

        job = DetectionJob(
            chat_id=incoming.chat_id,
            trigger_message_id=incoming.message_id,
            status="queued",
            priority=0,
            attempt_count=0,
            max_attempts=3,
            available_at=available_at,
            created_at=incoming.received_at,
            updated_at=incoming.received_at,
        )
        session.add(job)
        session.flush()
        return DetectionEnqueueResult(
            status=DetectionEnqueueStatus.CREATED,
            job_id=job.id,
            trigger_message_id=job.trigger_message_id,
            available_at=job.available_at,
        )


def _require_database_tenant(session: Session, tenant_key: str) -> None:
    """Enforce the product's one-Feishu-tenant-per-database boundary.

    Member tenant keys are intentionally not considered here: a group can
    legitimately include external contacts. The chat tenant identifies the
    installation that owns the event and is the isolation boundary used by
    this deployment.
    """

    existing_tenant = session.scalar(
        select(Chat.tenant_key).distinct().order_by(Chat.tenant_key).limit(1)
    )
    if existing_tenant is not None and existing_tenant != tenant_key:
        raise TenantIsolationError(
            "this database is already bound to another Feishu tenant"
        )


def _conversation_mentions(raw_event_json: str) -> tuple[ConversationMention, ...]:
    """Recover exact mention targets from the sanitized Feishu event."""

    try:
        payload = json.loads(raw_event_json)
    except (TypeError, json.JSONDecodeError):
        return ()
    event = payload.get("event")
    message = event.get("message") if isinstance(event, dict) else None
    raw_mentions = message.get("mentions") if isinstance(message, dict) else None
    if not isinstance(raw_mentions, list):
        return ()
    mentions: list[ConversationMention] = []
    for item in raw_mentions:
        if not isinstance(item, dict):
            continue
        mention_id = item.get("id")
        key = item.get("key")
        open_id = mention_id.get("open_id") if isinstance(mention_id, dict) else None
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(open_id, str) or not open_id.strip():
            continue
        name = item.get("name")
        mentions.append(
            ConversationMention(
                key=key,
                open_id=open_id,
                name=name if isinstance(name, str) and name.strip() else None,
            )
        )
    return tuple(mentions)
