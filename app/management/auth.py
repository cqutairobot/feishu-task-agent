"""One-time Feishu-issued login links and hashed browser sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import session_scope
from app.database.models import (
    ChatAdministrator,
    ManagementLoginToken,
    ManagementSession,
)


class ManagementAuthError(RuntimeError):
    """Raised for invalid, expired, consumed, or unauthorized credentials."""


@dataclass(frozen=True, slots=True)
class ManagementLoginTicket:
    actor_open_id: str
    raw_token: str
    login_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementLoginPreview:
    actor_open_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementSessionCredential:
    actor_open_id: str
    raw_session: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    actor_open_id: str
    expires_at: datetime


class ManagementAuthRepository:
    """Issue bearer secrets once, storing only SHA-256 hashes in SQLite."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        login_ttl: timedelta = timedelta(minutes=5),
        session_ttl: timedelta = timedelta(hours=12),
        token_factory=secrets.token_urlsafe,
    ) -> None:
        if not timedelta(minutes=1) <= login_ttl <= timedelta(minutes=30):
            raise ValueError("login_ttl must be between 1 and 30 minutes")
        if not timedelta(minutes=5) <= session_ttl <= timedelta(days=7):
            raise ValueError("session_ttl must be between 5 minutes and 7 days")
        self._session_factory = session_factory
        self._login_ttl = login_ttl
        self._session_ttl = session_ttl
        self._token_factory = token_factory

    def create_login_ticket(
        self,
        actor_open_id: str,
        *,
        public_base_url: str,
        created_at: datetime | None = None,
    ) -> ManagementLoginTicket:
        actor_open_id = _required(actor_open_id, "actor_open_id", 128)
        public_base_url = _required(public_base_url, "public_base_url", 500)
        created_at = _aware_utc(created_at or datetime.now(timezone.utc))
        raw_token = self._new_secret()
        expires_at = created_at + self._login_ttl
        with session_scope(self._session_factory) as session:
            if not _is_any_chat_administrator(session, actor_open_id):
                raise ManagementAuthError(
                    "only a configured group administrator can sign in"
                )
            session.add(
                ManagementLoginToken(
                    token_hash=_secret_hash(raw_token),
                    actor_open_id=actor_open_id,
                    expires_at=expires_at,
                    consumed_at=None,
                    created_at=created_at,
                )
            )
        query = urlencode({"token": raw_token})
        return ManagementLoginTicket(
            actor_open_id=actor_open_id,
            raw_token=raw_token,
            login_url=f"{public_base_url.rstrip('/')}/auth/start?{query}",
            expires_at=expires_at,
        )

    def inspect_login_token(
        self, raw_token: str, *, inspected_at: datetime | None = None
    ) -> ManagementLoginPreview:
        raw_token = _secret(raw_token, "login token")
        inspected_at = _aware_utc(inspected_at or datetime.now(timezone.utc))
        with session_scope(self._session_factory) as session:
            token = _usable_login_token(session, raw_token, inspected_at)
            return ManagementLoginPreview(
                actor_open_id=token.actor_open_id,
                expires_at=token.expires_at,
            )

    def consume_login_token(
        self, raw_token: str, *, consumed_at: datetime | None = None
    ) -> ManagementSessionCredential:
        raw_token = _secret(raw_token, "login token")
        consumed_at = _aware_utc(consumed_at or datetime.now(timezone.utc))
        raw_session = self._new_secret()
        with session_scope(self._session_factory) as session:
            connection = session.connection()
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            token = _usable_login_token(session, raw_token, consumed_at)
            if not _is_any_chat_administrator(session, token.actor_open_id):
                raise ManagementAuthError("administrator access was revoked")
            token.consumed_at = consumed_at
            expires_at = consumed_at + self._session_ttl
            session.add(
                ManagementSession(
                    session_hash=_secret_hash(raw_session),
                    actor_open_id=token.actor_open_id,
                    expires_at=expires_at,
                    revoked_at=None,
                    last_seen_at=consumed_at,
                    created_at=consumed_at,
                )
            )
            session.flush()
            return ManagementSessionCredential(
                actor_open_id=token.actor_open_id,
                raw_session=raw_session,
                expires_at=expires_at,
            )

    def authenticate_session(
        self, raw_session: str, *, authenticated_at: datetime | None = None
    ) -> ManagementPrincipal:
        raw_session = _secret(raw_session, "management session")
        authenticated_at = _aware_utc(
            authenticated_at or datetime.now(timezone.utc)
        )
        with session_scope(self._session_factory) as session:
            browser_session = session.scalar(
                select(ManagementSession).where(
                    ManagementSession.session_hash
                    == _secret_hash(raw_session)
                )
            )
            if (
                browser_session is None
                or browser_session.revoked_at is not None
                or browser_session.expires_at <= authenticated_at
                or not _is_any_chat_administrator(
                    session, browser_session.actor_open_id
                )
            ):
                raise ManagementAuthError("management session is not valid")
            browser_session.last_seen_at = authenticated_at
            return ManagementPrincipal(
                actor_open_id=browser_session.actor_open_id,
                expires_at=browser_session.expires_at,
            )

    def revoke_session(
        self, raw_session: str, *, revoked_at: datetime | None = None
    ) -> bool:
        raw_session = _secret(raw_session, "management session")
        revoked_at = _aware_utc(revoked_at or datetime.now(timezone.utc))
        with session_scope(self._session_factory) as session:
            browser_session = session.scalar(
                select(ManagementSession).where(
                    ManagementSession.session_hash
                    == _secret_hash(raw_session)
                )
            )
            if browser_session is None or browser_session.revoked_at is not None:
                return False
            browser_session.revoked_at = revoked_at
            return True

    def _new_secret(self) -> str:
        raw = self._token_factory(32)
        return _secret(raw, "generated secret")


def _usable_login_token(
    session: Session, raw_token: str, active_at: datetime
) -> ManagementLoginToken:
    token = session.scalar(
        select(ManagementLoginToken).where(
            ManagementLoginToken.token_hash == _secret_hash(raw_token)
        )
    )
    if (
        token is None
        or token.consumed_at is not None
        or token.expires_at <= active_at
    ):
        raise ManagementAuthError("login link is invalid or expired")
    return token


def _is_any_chat_administrator(session: Session, open_id: str) -> bool:
    return session.scalar(
        select(ChatAdministrator.id)
        .where(ChatAdministrator.open_id == open_id)
        .limit(1)
    ) is not None


def _secret_hash(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def _secret(value: str, name: str) -> str:
    value = _required(value, name, 256)
    if len(value) < 20:
        raise ManagementAuthError(f"{name} is invalid")
    return value


def _required(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagementAuthError(f"{name} must not be empty")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ManagementAuthError(f"{name} is too long")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ManagementAuthError("authentication time must be timezone-aware")
    return value.astimezone(timezone.utc)
