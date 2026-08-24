"""Resolve Feishu chat and member display names with a bounded TTL cache."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import time
from typing import Callable, Protocol

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetChatMembersRequest, GetChatRequest

from app.config import FeishuSettings
from app.database.repository import MessageRepository
from app.feishu.messages import IncomingMessage


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DirectoryMember:
    open_id: str
    name: str
    tenant_key: str | None


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    chat_id: str
    chat_name: str
    chat_tenant_key: str | None
    owner_open_id: str | None
    members: dict[str, DirectoryMember]


class DirectoryProvider(Protocol):
    def fetch(self, chat_id: str) -> DirectorySnapshot: ...

    def fetch_chat_name(self, chat_id: str) -> str: ...


class DirectoryLookupError(RuntimeError):
    """Raised when the Feishu directory APIs cannot return a usable snapshot."""


class FeishuDirectoryProvider:
    """Fetch chat metadata and all member pages through the official SDK."""

    def __init__(self, settings: FeishuSettings) -> None:
        self._client = (
            lark.Client.builder()
            .app_id(settings.app_id)
            .app_secret(settings.app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def fetch(self, chat_id: str) -> DirectorySnapshot:
        chat_name, chat_tenant_key, owner_open_id = self._fetch_chat_metadata(
            chat_id
        )

        members: dict[str, DirectoryMember] = {}
        page_token: str | None = None
        for _page_number in range(100):
            builder = (
                GetChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .page_size(100)
            )
            if page_token:
                builder = builder.page_token(page_token)
            response = self._client.im.v1.chat_members.get(builder.build())
            self._ensure_success(response, "get chat members")
            data = response.data
            for item in (data.items if data else None) or []:
                if item.member_id and item.name:
                    members[item.member_id] = DirectoryMember(
                        open_id=item.member_id,
                        name=item.name,
                        tenant_key=item.tenant_key,
                    )
            if not data or not data.has_more:
                break
            if not data.page_token or data.page_token == page_token:
                raise DirectoryLookupError("member pagination returned no new token")
            page_token = data.page_token
        else:
            raise DirectoryLookupError("member pagination exceeded 100 pages")

        if owner_open_id is not None and owner_open_id not in members:
            raise DirectoryLookupError(
                "get chat owner is not present in the member list"
            )

        return DirectorySnapshot(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_tenant_key=chat_tenant_key,
            owner_open_id=owner_open_id,
            members=members,
        )

    def fetch_chat_name(self, chat_id: str) -> str:
        """Fetch only mutable chat metadata without listing all members."""

        chat_name, _tenant_key, _owner_open_id = self._fetch_chat_metadata(
            chat_id
        )
        return chat_name

    def _fetch_chat_metadata(
        self, chat_id: str
    ) -> tuple[str, str | None, str | None]:
        """Fetch the fields used by directory and ownership synchronization."""

        chat_response = self._client.im.v1.chat.get(
            GetChatRequest.builder()
            .chat_id(chat_id)
            .user_id_type("open_id")
            .build()
        )
        self._ensure_success(chat_response, "get chat")
        chat_name = getattr(chat_response.data, "name", None)
        if not isinstance(chat_name, str) or not chat_name:
            raise DirectoryLookupError("get chat returned no chat name")
        tenant_key = _optional_non_empty(
            getattr(chat_response.data, "tenant_key", None)
        )
        owner_open_id = _optional_non_empty(
            getattr(chat_response.data, "owner_id", None)
        )
        return chat_name, tenant_key, owner_open_id

    @staticmethod
    def _ensure_success(response: object, operation: str) -> None:
        if response.success():
            return
        code = getattr(response, "code", "unknown")
        message = getattr(response, "msg", "unknown error")
        raise DirectoryLookupError(f"{operation} failed: code={code}, msg={message}")


class DirectoryService:
    """Cache directory results and keep database display names current."""

    def __init__(
        self,
        provider: DirectoryProvider,
        repository: MessageRepository,
        *,
        ttl_seconds: float = 900,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._provider = provider
        self._repository = repository
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._cache: dict[str, tuple[float, DirectorySnapshot]] = {}

    def refresh(
        self,
        chat_id: str,
        *,
        force: bool = False,
        chat_type: str | None = None,
        tenant_key: str | None = None,
    ) -> DirectorySnapshot | None:
        now = self._monotonic()
        cached = self._cache.get(chat_id)
        if not force and cached and cached[0] > now:
            return cached[1]

        try:
            return self.refresh_strict(
                chat_id,
                chat_type=chat_type,
                tenant_key=tenant_key,
            )
        except Exception as exc:
            LOGGER.warning("Unable to refresh Feishu directory: %s", exc)
            return cached[1] if cached else None

    def refresh_strict(
        self,
        chat_id: str,
        *,
        chat_type: str | None = None,
        tenant_key: str | None = None,
    ) -> DirectorySnapshot:
        """Force an authoritative refresh and surface every failure."""

        snapshot = self._provider.fetch(chat_id)
        self._repository.apply_directory_snapshot(
            snapshot.chat_id,
            snapshot.chat_name,
            {
                open_id: member.name
                for open_id, member in snapshot.members.items()
            },
            owner_open_id=snapshot.owner_open_id,
            member_tenant_keys={
                open_id: member.tenant_key
                for open_id, member in snapshot.members.items()
            },
            authoritative_members=True,
            chat_type=chat_type,
            tenant_key=tenant_key or snapshot.chat_tenant_key,
            updated_at=datetime.now(timezone.utc),
        )
        now = self._monotonic()
        self._cache[chat_id] = (now + self._ttl_seconds, snapshot)
        return snapshot

    def refresh_chat_name(self, chat_id: str) -> str | None:
        """Force-refresh one chat name; never return a stale cached name."""

        try:
            chat_name = self._provider.fetch_chat_name(chat_id)
            updated_at = datetime.now(timezone.utc)
            self._repository.apply_directory_snapshot(
                chat_id,
                chat_name,
                {},
                updated_at=updated_at,
            )
        except Exception as exc:
            LOGGER.warning("Unable to refresh Feishu chat name: %s", exc)
            return None

        cached = self._cache.get(chat_id)
        if cached is not None:
            expires_at, snapshot = cached
            self._cache[chat_id] = (
                expires_at,
                replace(snapshot, chat_name=chat_name),
            )
        return chat_name

    def enrich(
        self, message: IncomingMessage, *, force: bool = False
    ) -> IncomingMessage:
        snapshot = self.refresh(
            message.chat_id,
            force=force,
            chat_type=message.chat_type,
            tenant_key=message.tenant_key,
        )
        if snapshot is None:
            return message
        member = snapshot.members.get(message.sender_open_id)
        if member is None:
            return message
        return replace(message, sender_name=member.name)


def _optional_non_empty(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
