"""Verified mapping between names used in a chat and Feishu Open IDs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatMemberAlias,
    ChatMembership,
    Message,
    User,
)


class AliasError(ValueError):
    """Raised when an alias operation is invalid or cannot be completed safely."""


class AliasConflictError(AliasError):
    """Raised when a name is already mapped to another user in the same chat."""


@dataclass(frozen=True, slots=True)
class AliasBinding:
    chat_id: str
    open_id: str
    alias: str
    source: str
    confidence: float


@dataclass(frozen=True, slots=True)
class MessageSender:
    message_id: str
    chat_id: str
    open_id: str
    chat_type: str


def clean_alias(value: str) -> str:
    """Return a display-safe alias with Unicode and whitespace normalized."""

    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if not cleaned:
        raise AliasError("name must not be empty")
    if len(cleaned) > 255:
        raise AliasError("name must be at most 255 characters")
    return cleaned


def normalize_alias(value: str) -> str:
    """Create the exact-match key used for deterministic name resolution."""

    return clean_alias(value).casefold()


class AliasRepository:
    """Persist and resolve confirmed, chat-scoped member aliases."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def bind(
        self,
        chat_id: str,
        open_id: str,
        alias: str,
        *,
        source: str = "manual",
        confidence: float = 1.0,
        require_observed: bool = True,
    ) -> AliasBinding:
        chat_id = chat_id.strip()
        open_id = open_id.strip()
        display_alias = clean_alias(alias)
        normalized = normalize_alias(display_alias)
        if not chat_id:
            raise AliasError("chat_id must not be empty")
        if not open_id:
            raise AliasError("open_id must not be empty")
        if not source.strip():
            raise AliasError("source must not be empty")
        if not 0.0 <= confidence <= 1.0:
            raise AliasError("confidence must be between 0 and 1")

        now = datetime.now(timezone.utc)
        with session_scope(self._session_factory) as session:
            if require_observed:
                self._require_observed_member(session, chat_id, open_id)
            else:
                self._require_active_member(session, chat_id, open_id)
            claimed_name = session.scalar(
                select(ChatMemberAlias).where(
                    ChatMemberAlias.chat_id == chat_id,
                    ChatMemberAlias.normalized_alias == normalized,
                )
            )
            if claimed_name is not None and claimed_name.open_id != open_id:
                raise AliasConflictError(
                    f'name "{display_alias}" is already mapped to '
                    f"{claimed_name.open_id} in chat {chat_id}"
                )

            existing = session.scalar(
                select(ChatMemberAlias).where(
                    ChatMemberAlias.chat_id == chat_id,
                    ChatMemberAlias.open_id == open_id,
                )
            )
            if existing is None:
                existing = ChatMemberAlias(
                    chat_id=chat_id,
                    open_id=open_id,
                    alias=display_alias,
                    normalized_alias=normalized,
                    source=source.strip(),
                    confidence=confidence,
                    verified_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
            else:
                existing.alias = display_alias
                existing.normalized_alias = normalized
                existing.source = source.strip()
                existing.confidence = confidence
                existing.verified_at = now
                existing.updated_at = now

            session.flush()
            return self._to_binding(existing)

    def bind_for_administrator(
        self,
        chat_id: str,
        open_id: str,
        alias: str,
    ) -> AliasBinding:
        """Bind a current group member even if they have not sent a message."""

        return self.bind(
            chat_id,
            open_id,
            alias,
            source="administrator_page",
            require_observed=False,
        )

    def resolve(self, chat_id: str, alias: str) -> AliasBinding | None:
        normalized = normalize_alias(alias)
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(ChatMemberAlias).where(
                    ChatMemberAlias.chat_id == chat_id.strip(),
                    ChatMemberAlias.normalized_alias == normalized,
                )
            )
            return None if record is None else self._to_binding(record)

    def resolve_open_ids_across_chats(
        self,
        alias: str,
        *,
        chat_ids: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Resolve a confirmed name to active members in an admin scope.

        The result deliberately contains only Open IDs from enabled groups and
        active memberships. A caller must still apply its own administrator
        authorization and handle ambiguity when a name maps to multiple users.
        """

        normalized = normalize_alias(alias)
        normalized_chat_ids = None
        if chat_ids is not None:
            normalized_chat_ids = frozenset(
                item.strip() for item in chat_ids if item.strip()
            )
            if not normalized_chat_ids:
                return frozenset()
        with session_scope(self._session_factory) as session:
            conditions = [
                ChatMemberAlias.normalized_alias == normalized,
                ChatMembership.active.is_(True),
                Chat.chat_type == "group",
                Chat.enabled.is_(True),
            ]
            if normalized_chat_ids is not None:
                conditions.append(ChatMemberAlias.chat_id.in_(normalized_chat_ids))
            return frozenset(
                session.scalars(
                    select(ChatMemberAlias.open_id)
                    .join(
                        ChatMembership,
                        (ChatMembership.chat_id == ChatMemberAlias.chat_id)
                        & (ChatMembership.open_id == ChatMemberAlias.open_id),
                    )
                    .join(Chat, Chat.chat_id == ChatMemberAlias.chat_id)
                    .where(*conditions)
                )
            )

    def for_member(self, chat_id: str, open_id: str) -> AliasBinding | None:
        """Return the member's single confirmed name in one chat."""

        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(ChatMemberAlias).where(
                    ChatMemberAlias.chat_id == chat_id.strip(),
                    ChatMemberAlias.open_id == open_id.strip(),
                )
            )
            return None if record is None else self._to_binding(record)

    def list_for_chat(self, chat_id: str) -> list[AliasBinding]:
        chat_id = chat_id.strip()
        with session_scope(self._session_factory) as session:
            if session.get(Chat, chat_id) is None:
                raise AliasError(f"unknown chat_id: {chat_id}")
            records = list(
                session.scalars(
                    select(ChatMemberAlias)
                    .where(ChatMemberAlias.chat_id == chat_id)
                    .order_by(
                        ChatMemberAlias.open_id,
                        ChatMemberAlias.normalized_alias,
                    )
                )
            )
            return [self._to_binding(record) for record in records]

    def sender_for_message(self, message_id: str) -> MessageSender:
        """Resolve a copied Feishu message ID to its chat and sender."""

        message_id = message_id.strip()
        if not message_id:
            raise AliasError("message_id must not be empty")
        with session_scope(self._session_factory) as session:
            messages = list(
                session.scalars(
                    select(Message).where(Message.message_id == message_id).limit(2)
                )
            )
            if not messages:
                raise AliasError(f"unknown message_id: {message_id}")
            if len(messages) > 1:
                raise AliasError(
                    f"message_id is ambiguous across tenants: {message_id}"
                )
            message = messages[0]
            chat = session.get(Chat, message.chat_id)
            if chat is None:
                raise AliasError(
                    f"message chat does not exist: {message.chat_id}"
                )
            return MessageSender(
                message_id=message.message_id,
                chat_id=message.chat_id,
                open_id=message.sender_open_id,
                chat_type=chat.chat_type,
            )

    @staticmethod
    def _require_observed_member(
        session: Session, chat_id: str, open_id: str
    ) -> None:
        if session.get(Chat, chat_id) is None:
            raise AliasError(f"unknown chat_id: {chat_id}")
        if session.get(User, open_id) is None:
            raise AliasError(f"unknown open_id: {open_id}")
        observed = session.scalar(
            select(Message.id)
            .where(
                Message.chat_id == chat_id,
                Message.sender_open_id == open_id,
            )
            .limit(1)
        )
        if observed is None:
            raise AliasError(
                f"user {open_id} has not sent a stored message in chat {chat_id}"
            )

    @staticmethod
    def _require_active_member(
        session: Session, chat_id: str, open_id: str
    ) -> None:
        if session.get(Chat, chat_id) is None:
            raise AliasError(f"unknown chat_id: {chat_id}")
        if session.get(User, open_id) is None:
            raise AliasError(f"unknown open_id: {open_id}")
        active = session.scalar(
            select(ChatMembership.id).where(
                ChatMembership.chat_id == chat_id,
                ChatMembership.open_id == open_id,
                ChatMembership.active.is_(True),
            )
        )
        if active is None:
            raise AliasError(
                f"user {open_id} is not an active member of chat {chat_id}"
            )

    @staticmethod
    def _to_binding(record: ChatMemberAlias) -> AliasBinding:
        return AliasBinding(
            chat_id=record.chat_id,
            open_id=record.open_id,
            alias=record.alias,
            source=record.source,
            confidence=record.confidence,
        )
