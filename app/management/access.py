"""Persistent, chat-scoped task-administrator membership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    Chat,
    ChatAdministrator,
    ChatAdministratorEvent,
    ChatMemberAlias,
    ChatMembership,
    User,
)


class AdministratorSource(StrEnum):
    LOCAL_CLI = "local_cli"
    MANAGEMENT_PAGE = "management_page"
    BOOTSTRAP = "bootstrap"
    GROUP_OWNER_INIT = "group_owner_init"
    GROUP_OWNER_TAKEOVER = "group_owner_takeover"


class ChatAdministratorError(ValueError):
    """Raised when an administrator membership change is invalid."""


@dataclass(frozen=True, slots=True)
class ChatAdministratorSnapshot:
    chat_id: str
    open_id: str
    name: str
    source: AdministratorSource
    granted_by_open_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatAdministratorChange:
    action: str
    changed: bool
    administrator: ChatAdministratorSnapshot | None


class ChatAdministratorRepository:
    """Manage exact group memberships and preserve grant/revoke audit rows."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def grant(
        self,
        chat_id: str,
        open_id: str,
        *,
        source: AdministratorSource = AdministratorSource.LOCAL_CLI,
        actor_open_id: str | None = None,
        granted_at: datetime | None = None,
    ) -> ChatAdministratorChange:
        chat_id = _required(chat_id, "chat_id", 128)
        open_id = _required(open_id, "open_id", 128)
        actor_open_id = _optional(actor_open_id, "actor_open_id", 128)
        source = _source(source)
        granted_at = _aware_utc(granted_at or datetime.now(timezone.utc))

        with session_scope(self._session_factory) as session:
            chat, member_name = _eligible_member(session, chat_id, open_id)
            _validate_actor(session, actor_open_id)
            _authorize_grant(
                session,
                chat,
                open_id,
                source=source,
                actor_open_id=actor_open_id,
            )
            existing = session.scalar(
                select(ChatAdministrator).where(
                    ChatAdministrator.chat_id == chat.chat_id,
                    ChatAdministrator.open_id == open_id,
                )
            )
            if existing is not None:
                return ChatAdministratorChange(
                    action="grant",
                    changed=False,
                    administrator=_snapshot(existing, member_name),
                )
            if (
                source is AdministratorSource.GROUP_OWNER_INIT
                and _administrator_count(session, chat.chat_id) > 0
            ):
                return ChatAdministratorChange(
                    action="grant", changed=False, administrator=None
                )
            membership = ChatAdministrator(
                chat_id=chat.chat_id,
                open_id=open_id,
                granted_by_open_id=actor_open_id,
                source=source.value,
                created_at=granted_at,
            )
            session.add(membership)
            session.add(
                ChatAdministratorEvent(
                    chat_id=chat.chat_id,
                    target_open_id=open_id,
                    actor_open_id=actor_open_id,
                    action="grant",
                    source=source.value,
                    created_at=granted_at,
                )
            )
            session.flush()
            return ChatAdministratorChange(
                action="grant",
                changed=True,
                administrator=_snapshot(membership, member_name),
            )

    def revoke(
        self,
        chat_id: str,
        open_id: str,
        *,
        source: AdministratorSource = AdministratorSource.LOCAL_CLI,
        actor_open_id: str | None = None,
        revoked_at: datetime | None = None,
    ) -> ChatAdministratorChange:
        chat_id = _required(chat_id, "chat_id", 128)
        open_id = _required(open_id, "open_id", 128)
        actor_open_id = _optional(actor_open_id, "actor_open_id", 128)
        source = _source(source)
        revoked_at = _aware_utc(revoked_at or datetime.now(timezone.utc))

        with session_scope(self._session_factory) as session:
            chat = _eligible_group(session, chat_id)
            _validate_actor(session, actor_open_id)
            if source is AdministratorSource.MANAGEMENT_PAGE:
                _require_administrator(session, chat.chat_id, actor_open_id)
            membership = session.scalar(
                select(ChatAdministrator).where(
                    ChatAdministrator.chat_id == chat_id,
                    ChatAdministrator.open_id == open_id,
                )
            )
            if membership is None:
                return ChatAdministratorChange(
                    action="revoke", changed=False, administrator=None
                )
            if _administrator_count(session, chat.chat_id) <= 1:
                raise ChatAdministratorError(
                    "the last administrator cannot be removed"
                )
            snapshot = _snapshot(
                membership, _member_name(session, chat_id, open_id)
            )
            session.add(
                ChatAdministratorEvent(
                    chat_id=chat_id,
                    target_open_id=open_id,
                    actor_open_id=actor_open_id,
                    action="revoke",
                    source=source.value,
                    created_at=revoked_at,
                )
            )
            session.delete(membership)
            return ChatAdministratorChange(
                action="revoke", changed=True, administrator=snapshot
            )

    def list_chat(self, chat_id: str) -> tuple[ChatAdministratorSnapshot, ...]:
        chat_id = _required(chat_id, "chat_id", 128)
        with session_scope(self._session_factory) as session:
            memberships = tuple(
                session.scalars(
                    select(ChatAdministrator)
                    .where(ChatAdministrator.chat_id == chat_id)
                    .order_by(ChatAdministrator.open_id)
                )
            )
            snapshots = tuple(
                _snapshot(
                    membership,
                    _member_name(
                        session,
                        membership.chat_id,
                        membership.open_id,
                    ),
                )
                for membership in memberships
            )
            return tuple(
                sorted(snapshots, key=lambda item: (item.name, item.open_id))
            )

    def is_administrator(self, chat_id: str, open_id: str) -> bool:
        chat_id = _required(chat_id, "chat_id", 128)
        open_id = _required(open_id, "open_id", 128)
        with session_scope(self._session_factory) as session:
            return self.is_administrator_in_session(session, chat_id, open_id)

    def has_administrator(self, chat_id: str) -> bool:
        """Return whether an enabled group has completed administrator setup."""

        chat_id = _required(chat_id, "chat_id", 128)
        with session_scope(self._session_factory) as session:
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

    @staticmethod
    def is_administrator_in_session(
        session: Session, chat_id: str, open_id: str
    ) -> bool:
        return session.scalar(
            select(ChatAdministrator.id).where(
                ChatAdministrator.chat_id == chat_id,
                ChatAdministrator.open_id == open_id,
            )
        ) is not None

    def chat_ids_for_administrator(self, open_id: str) -> frozenset[str]:
        open_id = _required(open_id, "open_id", 128)
        with session_scope(self._session_factory) as session:
            return frozenset(
                session.scalars(
                    select(ChatAdministrator.chat_id)
                    .join(Chat, Chat.chat_id == ChatAdministrator.chat_id)
                    .where(
                        ChatAdministrator.open_id == open_id,
                        Chat.chat_type == "group",
                        Chat.enabled.is_(True),
                    )
                )
            )

    def managed_chat_ids(self) -> frozenset[str]:
        """Return every enabled group admitted by persistent administration."""

        with session_scope(self._session_factory) as session:
            return frozenset(
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

    def admitted_chat_ids(
        self, configured_chat_ids: frozenset[str]
    ) -> frozenset[str] | None:
        """Combine a bounded static allowlist with self-service groups.

        An empty static allowlist retains the existing meaning of unrestricted
        app-visible groups, represented by ``None`` to repository queries.
        """

        if not configured_chat_ids:
            return None
        return configured_chat_ids | self.managed_chat_ids()


def _eligible_member(
    session: Session, chat_id: str, open_id: str
) -> tuple[Chat, str]:
    chat = _eligible_group(session, chat_id)
    if session.get(User, open_id) is None:
        raise ChatAdministratorError("administrator user does not exist")
    membership = session.scalar(
        select(ChatMembership).where(
            ChatMembership.chat_id == chat_id,
            ChatMembership.open_id == open_id,
            ChatMembership.active.is_(True),
        )
    )
    if membership is None:
        raise ChatAdministratorError(
            "administrator must be a current verified member of this group"
        )
    return chat, _member_name(session, chat_id, open_id)


def _eligible_group(session: Session, chat_id: str) -> Chat:
    chat = session.get(Chat, chat_id)
    if chat is None or chat.chat_type != "group" or not chat.enabled:
        raise ChatAdministratorError("chat must be an enabled group")
    return chat


def _authorize_grant(
    session: Session,
    chat: Chat,
    target_open_id: str,
    *,
    source: AdministratorSource,
    actor_open_id: str | None,
) -> None:
    if source is AdministratorSource.MANAGEMENT_PAGE:
        _require_administrator(session, chat.chat_id, actor_open_id)
        return
    if source not in {
        AdministratorSource.GROUP_OWNER_INIT,
        AdministratorSource.GROUP_OWNER_TAKEOVER,
    }:
        return
    if actor_open_id != target_open_id:
        raise ChatAdministratorError("group owner can only grant access to self")
    is_owner = session.scalar(
        select(ChatMembership.is_owner).where(
            ChatMembership.chat_id == chat.chat_id,
            ChatMembership.open_id == actor_open_id,
            ChatMembership.active.is_(True),
        )
    )
    if is_owner is not True:
        raise ChatAdministratorError("only the current group owner can do this")


def _require_administrator(
    session: Session, chat_id: str, actor_open_id: str | None
) -> None:
    if actor_open_id is None or not ChatAdministratorRepository.is_administrator_in_session(
        session, chat_id, actor_open_id
    ):
        raise ChatAdministratorError(
            "actor must be an administrator of this group"
        )


def _administrator_count(session: Session, chat_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(ChatAdministrator.id)).where(
                ChatAdministrator.chat_id == chat_id
            )
        )
        or 0
    )


def _member_name(session: Session, chat_id: str, open_id: str) -> str:
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
    user_name = session.scalar(
        select(User.name).where(User.open_id == open_id)
    )
    return user_name or open_id


def _validate_actor(session: Session, actor_open_id: str | None) -> None:
    if actor_open_id is not None and session.get(User, actor_open_id) is None:
        raise ChatAdministratorError("granting actor does not exist")


def _snapshot(
    membership: ChatAdministrator, name: str
) -> ChatAdministratorSnapshot:
    return ChatAdministratorSnapshot(
        chat_id=membership.chat_id,
        open_id=membership.open_id,
        name=name,
        source=AdministratorSource(membership.source),
        granted_by_open_id=membership.granted_by_open_id,
        created_at=membership.created_at,
    )


def _source(value: AdministratorSource | str) -> AdministratorSource:
    try:
        return AdministratorSource(value)
    except (TypeError, ValueError) as exc:
        raise ChatAdministratorError("administrator source is invalid") from exc


def _required(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChatAdministratorError(f"{name} must not be empty")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ChatAdministratorError(f"{name} is too long")
    return normalized


def _optional(value: str | None, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required(value, name, maximum)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChatAdministratorError("administrator timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
