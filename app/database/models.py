"""SQLAlchemy models for Phase 2 message persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.database.types import UTCDateTime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative model base."""


class Chat(Base):
    __tablename__ = "chats"

    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    chat_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    messages: Mapped[list["Message"]] = relationship(back_populates="chat")
    member_aliases: Mapped[list["ChatMemberAlias"]] = relationship(
        back_populates="chat"
    )
    memberships: Mapped[list["ChatMembership"]] = relationship(
        back_populates="chat"
    )
    detection_jobs: Mapped[list["DetectionJob"]] = relationship(
        back_populates="chat"
    )
    tasks: Mapped[list["Task"]] = relationship(back_populates="chat")
    settings: Mapped["ChatSettings | None"] = relationship(
        back_populates="chat", uselist=False, cascade="all, delete-orphan"
    )
    setting_events: Mapped[list["ChatSettingEvent"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )


class ChatSettings(Base):
    """Persistent settings scoped to one exact group chat."""

    __tablename__ = "chat_settings"
    __table_args__ = (
        CheckConstraint(
            "auto_todo_confidence >= 0 AND auto_todo_confidence <= 1",
            name="ck_chat_settings_auto_todo_confidence",
        ),
        CheckConstraint(
            "timezone <> ''", name="ck_chat_settings_timezone_nonempty"
        ),
        CheckConstraint(
            "task_scope IN ('broad', 'work_only')",
            name="ck_chat_settings_task_scope",
        ),
        CheckConstraint(
            "administrator_notification_mode IN ('all', 'selected')",
            name="ck_chat_settings_administrator_notification_mode",
        ),
    )

    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), primary_key=True
    )
    detection_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    auto_todo_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.85
    )
    task_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="broad"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai"
    )
    reminder_due_72h_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    reminder_due_24h_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    reminder_due_today_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    reminder_overdue_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    reminder_due_72h_offset_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=72
    )
    reminder_due_24h_offset_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    reminder_due_today_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, default=9
    )
    reminder_overdue_grace_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    missing_deadline_owner_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    missing_deadline_admin_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    missing_deadline_owner_delay_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    missing_deadline_admin_delay_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=72
    )
    administrator_notification_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="all"
    )
    administrator_notification_open_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    updated_by_open_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    chat: Mapped[Chat] = relationship(back_populates="settings")


class ChatSettingEvent(Base):
    """Append-only audit record for group setting changes."""

    __tablename__ = "chat_setting_events"
    __table_args__ = (
        Index("ix_chat_setting_events_chat_created", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    actor_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    changed_fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    chat: Mapped[Chat] = relationship(back_populates="setting_events")


class ChatAdministrator(Base):
    """One currently active task administrator for one exact group chat."""

    __tablename__ = "chat_administrators"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "open_id", name="uq_chat_administrators_chat_user"
        ),
        CheckConstraint(
            "source IN ('local_cli', 'management_page', 'bootstrap', "
            "'group_owner_init', 'group_owner_takeover')",
            name="ck_chat_administrators_source",
        ),
        Index("ix_chat_administrators_user_chat", "open_id", "chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    granted_by_open_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class ChatAdministratorEvent(Base):
    """Append-only audit record for group-administrator membership changes."""

    __tablename__ = "chat_administrator_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('grant', 'revoke')",
            name="ck_chat_administrator_events_action",
        ),
        CheckConstraint(
            "source IN ('local_cli', 'management_page', 'bootstrap', "
            "'group_owner_init', 'group_owner_takeover', "
            "'membership_sync')",
            name="ck_chat_administrator_events_source",
        ),
        Index(
            "ix_chat_administrator_events_chat_created",
            "chat_id",
            "created_at",
        ),
        Index(
            "ix_chat_administrator_events_target_created",
            "target_open_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="RESTRICT"), nullable=False
    )
    target_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    actor_open_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ManagementLoginToken(Base):
    """Hashed, short-lived, one-time browser-login token."""

    __tablename__ = "management_login_tokens"
    __table_args__ = (
        UniqueConstraint(
            "token_hash", name="uq_management_login_tokens_hash"
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_management_login_tokens_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_management_login_tokens_consumed",
        ),
        Index(
            "ix_management_login_tokens_actor_expiry",
            "actor_open_id",
            "expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ManagementSession(Base):
    """Hashed browser session bound to one verified Feishu Open ID."""

    __tablename__ = "management_sessions"
    __table_args__ = (
        UniqueConstraint(
            "session_hash", name="uq_management_sessions_hash"
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_management_sessions_expiry",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_management_sessions_revoked",
        ),
        Index(
            "ix_management_sessions_actor_expiry",
            "actor_open_id",
            "expires_at",
        ),
        Index("ix_management_sessions_expiry", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class User(Base):
    """One Feishu identity inside this database's single tenant boundary.

    ``open_id`` is only safe as the primary key because message ingestion
    rejects a second event tenant before users or chats are upserted.
    """

    __tablename__ = "users"

    open_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    union_id: Mapped[str | None] = mapped_column(String(128), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    tenant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    messages: Mapped[list["Message"]] = relationship(back_populates="sender")
    chat_aliases: Mapped[list["ChatMemberAlias"]] = relationship(
        back_populates="user"
    )
    chat_memberships: Mapped[list["ChatMembership"]] = relationship(
        back_populates="user"
    )
    assigned_tasks: Mapped[list["Task"]] = relationship(
        back_populates="owner"
    )
    task_assignments: Mapped[list["TaskAssignee"]] = relationship(
        back_populates="user"
    )


class ChatMemberAlias(Base):
    """A verified, chat-scoped name mapped to a Feishu user Open ID."""

    __tablename__ = "chat_member_aliases"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "normalized_alias",
            name="uq_chat_member_aliases_chat_normalized",
        ),
        UniqueConstraint(
            "chat_id",
            "open_id",
            name="uq_chat_member_aliases_chat_user",
        ),
        Index("ix_chat_member_aliases_open_id", "open_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    chat: Mapped[Chat] = relationship(back_populates="member_aliases")
    user: Mapped[User] = relationship(back_populates="chat_aliases")


class ChatMembership(Base):
    """The latest authoritative Feishu membership state for one chat user."""

    __tablename__ = "chat_memberships"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "open_id", name="uq_chat_memberships_chat_user"
        ),
        CheckConstraint(
            "is_owner = 0 OR active = 1",
            name="ck_chat_memberships_owner_active",
        ),
        CheckConstraint(
            "(active = 1 AND left_at IS NULL) OR "
            "(active = 0 AND left_at IS NOT NULL)",
            name="ck_chat_memberships_active_state",
        ),
        Index("ix_chat_memberships_chat_active", "chat_id", "active"),
        Index("ix_chat_memberships_user_active", "open_id", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="CASCADE"), nullable=False
    )
    display_name_snapshot: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_synced_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    chat: Mapped[Chat] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="chat_memberships")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_key", "event_id", name="uq_messages_tenant_event"
        ),
        UniqueConstraint(
            "tenant_key", "message_id", name="uq_messages_tenant_message"
        ),
        Index("ix_messages_chat_created", "chat_id", "message_created_at"),
        Index("ix_messages_sender_created", "sender_open_id", "message_created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="RESTRICT"), nullable=False
    )
    sender_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    sender_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text_content: Mapped[str | None] = mapped_column(Text)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    raw_event_json: Mapped[str] = mapped_column(Text, nullable=False)
    root_id: Mapped[str | None] = mapped_column(String(128))
    parent_id: Mapped[str | None] = mapped_column(String(128))
    message_created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    is_from_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    chat: Mapped[Chat] = relationship(back_populates="messages")
    sender: Mapped[User] = relationship(back_populates="messages")
    task_evidence: Mapped[list["TaskEvidence"]] = relationship(
        back_populates="message"
    )
    detection_focus_links: Mapped[list["DetectionRunFocusMessage"]] = (
        relationship(back_populates="message")
    )


class DetectionJob(Base):
    """A durable request to detect a task through one trigger message."""

    __tablename__ = "detection_jobs"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "trigger_message_id",
            name="uq_detection_jobs_chat_trigger",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'dead', 'cancelled')",
            name="ck_detection_jobs_status",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10",
            name="ck_detection_jobs_max_attempts",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_detection_jobs_attempt_count",
        ),
        CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL "
            "AND leased_at IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'running' AND worker_id IS NULL "
            "AND leased_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_detection_jobs_lease_state",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) "
            "OR (status != 'completed' AND completed_at IS NULL)",
            name="ck_detection_jobs_completed_state",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancel_reason IS NULL)",
            name="ck_detection_jobs_cancelled_state",
        ),
        Index(
            "ix_detection_jobs_ready",
            "status",
            "available_at",
            "priority",
            "id",
        ),
        Index("ix_detection_jobs_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    leased_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    chat: Mapped[Chat] = relationship(back_populates="detection_jobs")
    runs: Mapped[list["DetectionRun"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class DetectionRun(Base):
    """Audit record for one model attempt made for a detection job."""

    __tablename__ = "detection_runs"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "attempt", name="uq_detection_runs_job_attempt"
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_detection_runs_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_detection_runs_attempt"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_detection_runs_latency",
        ),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_detection_runs_prompt_tokens",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_detection_runs_completion_tokens",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_detection_runs_total_tokens",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) "
            "OR (status != 'running' AND finished_at IS NOT NULL)",
            name="ck_detection_runs_finished_state",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND result_json IS NOT NULL "
            "AND error_code IS NULL) "
            "OR (status = 'failed' AND result_json IS NULL "
            "AND error_code IS NOT NULL) "
            "OR status = 'running'",
            name="ck_detection_runs_result_state",
        ),
        Index("ix_detection_runs_status_started", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("detection_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running"
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    response_format: Mapped[str] = mapped_column(String(32), nullable=False)
    context_version: Mapped[str] = mapped_column(String(16), nullable=False)
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    context_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    job: Mapped[DetectionJob] = relationship(back_populates="runs")
    task_sources: Mapped[list["TaskSource"]] = relationship(
        back_populates="detection_run"
    )
    materialization: Mapped["DetectionMaterialization | None"] = relationship(
        back_populates="detection_run",
        uselist=False,
    )
    focus_messages: Mapped[list["DetectionRunFocusMessage"]] = relationship(
        back_populates="detection_run",
        cascade="all, delete-orphan",
        order_by="DetectionRunFocusMessage.position",
    )


class DetectionRunFocusMessage(Base):
    """One newly arrived message that a run is allowed to act upon."""

    __tablename__ = "detection_run_focus_messages"
    __table_args__ = (
        UniqueConstraint(
            "detection_run_id",
            "position",
            name="uq_detection_run_focus_position",
        ),
        CheckConstraint(
            "position >= 0",
            name="ck_detection_run_focus_position",
        ),
        Index("ix_detection_run_focus_message", "message_db_id"),
    )

    detection_run_id: Mapped[int] = mapped_column(
        ForeignKey("detection_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_db_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    detection_run: Mapped[DetectionRun] = relationship(
        back_populates="focus_messages"
    )
    message: Mapped[Message] = relationship(
        back_populates="detection_focus_links"
    )


class Task(Base):
    """A chat-isolated lifecycle task materialized from one or more candidates."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'todo', 'done', 'cancelled', 'overdue')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_tasks_confidence",
        ),
        CheckConstraint(
            "(status = 'done' AND completed_at IS NOT NULL) "
            "OR (status != 'done' AND completed_at IS NULL)",
            name="ck_tasks_completed_state",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) "
            "OR (status != 'cancelled' AND cancelled_at IS NULL)",
            name="ck_tasks_cancelled_state",
        ),
        Index("ix_tasks_chat_status", "chat_id", "status"),
        Index("ix_tasks_owner_status", "owner_open_id", "status"),
        Index("ix_tasks_deadline_status", "deadline", "status"),
        Index(
            "ix_tasks_dedupe_lookup",
            "chat_id",
            "owner_open_id",
            "normalized_title",
            "deadline",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    owner_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    owner_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="todo")
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    merged_into_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT")
    )
    merged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    chat: Mapped[Chat] = relationship(back_populates="tasks")
    owner: Mapped[User] = relationship(back_populates="assigned_tasks")
    assignees: Mapped[list["TaskAssignee"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskAssignee.position",
    )
    evidence: Mapped[list["TaskEvidence"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    sources: Mapped[list["TaskSource"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    reminders: Mapped[list["TaskReminder"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["TaskNotification"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    creation_event: Mapped["TaskCreationEvent | None"] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        uselist=False,
    )


class TaskAssignee(Base):
    """One ordered, verified member responsible for a shared task."""

    __tablename__ = "task_assignees"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "open_id", name="uq_task_assignees_task_user"
        ),
        UniqueConstraint(
            "task_id", "position", name="uq_task_assignees_task_position"
        ),
        CheckConstraint("position >= 0", name="ck_task_assignees_position"),
        Index("ix_task_assignees_user_task", "open_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    task: Mapped[Task] = relationship(back_populates="assignees")
    user: Mapped[User] = relationship(back_populates="task_assignments")


class TaskCreationEvent(Base):
    """Immutable provenance for a task created outside model detection."""

    __tablename__ = "task_creation_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id", name="uq_task_creation_events_task"
        ),
        UniqueConstraint(
            "request_id", name="uq_task_creation_events_request"
        ),
        CheckConstraint(
            "source = 'management_page'",
            name="ck_task_creation_events_source",
        ),
        Index(
            "ix_task_creation_events_actor_created",
            "actor_open_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    actor_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="management_page"
    )
    title_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    description_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    deadline_snapshot: Mapped[datetime | None] = mapped_column(UTCDateTime())
    assignees_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    task: Mapped[Task] = relationship(back_populates="creation_event")


class TaskLifecycleEvent(Base):
    """One authorized and atomically applied task lifecycle transition."""

    __tablename__ = "task_lifecycle_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "trigger_message_db_id",
            name="uq_task_lifecycle_events_task_trigger",
        ),
        UniqueConstraint(
            "trigger_card_action_id",
            name="uq_task_lifecycle_events_card_action",
        ),
        UniqueConstraint(
            "trigger_management_request_id",
            name="uq_task_lifecycle_events_management_request",
        ),
        CheckConstraint(
            "(trigger_source = 'message' "
            "AND trigger_message_db_id IS NOT NULL "
            "AND trigger_card_action_id IS NULL "
            "AND trigger_card_message_id IS NULL "
            "AND trigger_card_chat_id IS NULL "
            "AND trigger_management_request_id IS NULL) OR "
            "(trigger_source = 'card_action' "
            "AND trigger_message_db_id IS NULL "
            "AND trigger_card_action_id IS NOT NULL "
            "AND trigger_card_message_id IS NOT NULL "
            "AND trigger_card_chat_id IS NOT NULL "
            "AND trigger_management_request_id IS NULL) OR "
            "(trigger_source = 'management_page' "
            "AND trigger_message_db_id IS NULL "
            "AND trigger_card_action_id IS NULL "
            "AND trigger_card_message_id IS NULL "
            "AND trigger_card_chat_id IS NULL "
            "AND trigger_management_request_id IS NOT NULL)",
            name="ck_task_lifecycle_events_trigger_source",
        ),
        CheckConstraint(
            "action IN ('confirm', 'complete', 'reschedule', 'cancel', 'rename', "
            "'reassign', 'invalidate', 'restore', 'merge')",
            name="ck_task_lifecycle_events_action",
        ),
        CheckConstraint(
            "authorization_role IN ('owner', 'administrator')",
            name="ck_task_lifecycle_events_authorization",
        ),
        CheckConstraint(
            "previous_status IN ('pending', 'todo', 'overdue', 'done', 'cancelled')",
            name="ck_task_lifecycle_events_previous_status",
        ),
        CheckConstraint(
            "new_status IN ('todo', 'overdue', 'done', 'cancelled')",
            name="ck_task_lifecycle_events_new_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_task_lifecycle_events_confidence",
        ),
        CheckConstraint(
            "prompt_tokens IS NULL OR prompt_tokens >= 0",
            name="ck_task_lifecycle_events_prompt_tokens",
        ),
        CheckConstraint(
            "completion_tokens IS NULL OR completion_tokens >= 0",
            name="ck_task_lifecycle_events_completion_tokens",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_task_lifecycle_events_total_tokens",
        ),
        CheckConstraint(
            "(action = 'confirm' AND previous_status = 'pending' "
            "AND new_status = 'todo') OR "
            "(action = 'complete' AND new_status = 'done') OR "
            "(action = 'cancel' AND new_status = 'cancelled') OR "
            "(action = 'invalidate' AND new_status = 'cancelled') OR "
            "(action = 'restore' AND previous_status IN ('done', 'cancelled') "
            "AND new_status IN ('todo', 'overdue')) OR "
            "(action = 'merge' AND previous_status IN "
            "('pending', 'todo', 'overdue', 'done') "
            "AND new_status = 'cancelled') OR "
            "(action = 'reschedule' AND new_status = 'todo' "
            "AND deadline_after IS NOT NULL) OR "
            "(action IN ('rename', 'reassign') "
            "AND new_status = previous_status)",
            name="ck_task_lifecycle_events_outcome",
        ),
        CheckConstraint(
            "(action = 'merge' AND merge_target_task_id IS NOT NULL) OR "
            "(action != 'merge' AND merge_target_task_id IS NULL)",
            name="ck_task_lifecycle_events_merge_target",
        ),
        CheckConstraint(
            "(action = 'rename' AND title_before IS NOT NULL "
            "AND title_after IS NOT NULL AND title_before != title_after "
            "AND assignees_before_json IS NULL "
            "AND assignees_after_json IS NULL) OR "
            "(action = 'reassign' AND title_before IS NULL "
            "AND title_after IS NULL AND assignees_before_json IS NOT NULL "
            "AND assignees_after_json IS NOT NULL "
            "AND assignees_before_json != assignees_after_json) OR "
            "(action NOT IN ('rename', 'reassign') "
            "AND title_before IS NULL AND title_after IS NULL "
            "AND assignees_before_json IS NULL "
            "AND assignees_after_json IS NULL)",
            name="ck_task_lifecycle_events_correction_payload",
        ),
        Index("ix_task_lifecycle_events_task_applied", "task_id", "applied_at"),
        Index(
            "ix_task_lifecycle_events_actor_applied",
            "actor_open_id",
            "applied_at",
        ),
        Index(
            "ix_task_lifecycle_events_trigger", "trigger_message_db_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    actor_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    trigger_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="message", server_default="message"
    )
    trigger_message_db_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT")
    )
    trigger_card_action_id: Mapped[str | None] = mapped_column(String(128))
    trigger_card_message_id: Mapped[str | None] = mapped_column(String(128))
    trigger_card_chat_id: Mapped[str | None] = mapped_column(String(128))
    trigger_management_request_id: Mapped[str | None] = mapped_column(
        String(128)
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    authorization_role: Mapped[str] = mapped_column(String(16), nullable=False)
    task_code_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    new_status: Mapped[str] = mapped_column(String(16), nullable=False)
    deadline_before: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline_after: Mapped[datetime | None] = mapped_column(UTCDateTime())
    title_before: Mapped[str | None] = mapped_column(String(200))
    title_after: Mapped[str | None] = mapped_column(String(200))
    assignees_before_json: Mapped[str | None] = mapped_column(Text)
    assignees_after_json: Mapped[str | None] = mapped_column(Text)
    merge_target_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    response_format: Mapped[str | None] = mapped_column(String(32))
    model_request_id: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    evidence_links: Mapped[list["TaskLifecycleEvidence"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="TaskLifecycleEvidence.position",
    )


class TaskLifecycleEvidence(Base):
    """Ordered message evidence preserved for one lifecycle transition."""

    __tablename__ = "task_lifecycle_evidence"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "position",
            name="uq_task_lifecycle_evidence_position",
        ),
        CheckConstraint(
            "position >= 0",
            name="ck_task_lifecycle_evidence_position",
        ),
        Index("ix_task_lifecycle_evidence_message", "message_db_id"),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("task_lifecycle_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_db_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    event: Mapped[TaskLifecycleEvent] = relationship(
        back_populates="evidence_links"
    )
    message: Mapped[Message] = relationship()


class TaskReminder(Base):
    """One durable reminder stage tied to an exact deadline snapshot."""

    __tablename__ = "task_reminders"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "recipient_open_id",
            "kind",
            "deadline_snapshot",
            name="uq_task_reminders_task_recipient_kind_deadline",
        ),
        CheckConstraint(
            "kind IN ('due_72h', 'due_24h', 'due_today', 'overdue')",
            name="ck_task_reminders_kind",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'leased', 'sent', 'cancelled', 'dead')",
            name="ck_task_reminders_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_task_reminders_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10",
            name="ck_task_reminders_max_attempts",
        ),
        CheckConstraint(
            "(status = 'leased' AND worker_id IS NOT NULL "
            "AND leased_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'leased' AND worker_id IS NULL "
            "AND leased_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_reminders_lease_state",
        ),
        CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL "
            "AND feishu_message_id IS NOT NULL) OR "
            "(status != 'sent' AND sent_at IS NULL "
            "AND feishu_message_id IS NULL)",
            name="ck_task_reminders_sent_state",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancel_reason IS NULL)",
            name="ck_task_reminders_cancelled_state",
        ),
        CheckConstraint(
            "(status = 'sent' AND delivery_receive_id_type IS NOT NULL "
            "AND delivery_receive_id IS NOT NULL) OR "
            "(status != 'sent' AND delivery_receive_id_type IS NULL "
            "AND delivery_receive_id IS NULL)",
            name="ck_task_reminders_delivery_state",
        ),
        CheckConstraint(
            "delivery_receive_id_type IS NULL OR "
            "delivery_receive_id_type IN ('open_id', 'chat_id')",
            name="ck_task_reminders_delivery_receive_type",
        ),
        Index(
            "ix_task_reminders_ready",
            "status",
            "available_at",
            "scheduled_for",
        ),
        Index("ix_task_reminders_task_status", "task_id", "status"),
        Index(
            "ix_task_reminders_recipient_status",
            "recipient_open_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    recipient_open_id: Mapped[str] = mapped_column(
        ForeignKey("users.open_id", ondelete="RESTRICT"), nullable=False
    )
    recipient_name_snapshot: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    deadline_snapshot: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    leased_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    feishu_message_id: Mapped[str | None] = mapped_column(String(128))
    delivery_receive_id_type: Mapped[str | None] = mapped_column(String(16))
    delivery_receive_id: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(2000))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    task: Mapped[Task] = relationship(back_populates="reminders")


class TaskNotification(Base):
    """One durable private notification about a task or its lifecycle."""

    __tablename__ = "task_notifications"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "kind",
            "recipient_open_id",
            "dedupe_key",
            name="uq_task_notifications_dedupe",
        ),
        CheckConstraint(
            "kind IN ('task_created_assignee', "
            "'missing_deadline_owner', 'missing_deadline_admin', "
            "'task_done_admin', 'task_cancelled_admin', "
            "'task_overdue_admin', 'task_rescheduled_admin', "
            "'task_done_coassignee', 'task_cancelled_coassignee', "
            "'task_rescheduled_coassignee', 'task_renamed_assignee', "
            "'task_assignee_added', 'task_assignee_removed', "
            "'task_assignees_changed', 'task_invalidated_assignee', "
            "'task_renamed_admin', 'task_reassigned_admin', "
            "'task_invalidated_admin', 'task_restored_coassignee', "
            "'task_restored_admin')",
            name="ck_task_notifications_kind",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'leased', 'sent', 'cancelled', 'dead')",
            name="ck_task_notifications_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_task_notifications_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10",
            name="ck_task_notifications_max_attempts",
        ),
        CheckConstraint(
            "(status = 'leased' AND worker_id IS NOT NULL "
            "AND leased_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status != 'leased' AND worker_id IS NULL "
            "AND leased_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_notifications_lease_state",
        ),
        CheckConstraint(
            "(status = 'sent' AND sent_at IS NOT NULL "
            "AND feishu_message_id IS NOT NULL "
            "AND delivery_receive_id_type IS NOT NULL "
            "AND delivery_receive_id IS NOT NULL) OR "
            "(status != 'sent' AND sent_at IS NULL "
            "AND feishu_message_id IS NULL "
            "AND delivery_receive_id_type IS NULL "
            "AND delivery_receive_id IS NULL)",
            name="ck_task_notifications_sent_state",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancel_reason IS NOT NULL) OR "
            "(status != 'cancelled' AND cancelled_at IS NULL "
            "AND cancel_reason IS NULL)",
            name="ck_task_notifications_cancelled_state",
        ),
        CheckConstraint(
            "delivery_receive_id_type IS NULL OR "
            "delivery_receive_id_type IN ('open_id', 'chat_id')",
            name="ck_task_notifications_delivery_receive_type",
        ),
        Index(
            "ix_task_notifications_ready",
            "status",
            "available_at",
            "scheduled_for",
        ),
        Index("ix_task_notifications_task_status", "task_id", "status"),
        Index(
            "ix_task_notifications_recipient_status",
            "recipient_open_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    source_lifecycle_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_lifecycle_events.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    task_code_snapshot: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_open_id_snapshot: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    owner_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    status_snapshot: Mapped[str] = mapped_column(String(16), nullable=False)
    deadline_snapshot: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline_before_snapshot: Mapped[datetime | None] = mapped_column(
        UTCDateTime()
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    leased_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    feishu_message_id: Mapped[str | None] = mapped_column(String(128))
    delivery_receive_id_type: Mapped[str | None] = mapped_column(String(16))
    delivery_receive_id: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(2000))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )

    task: Mapped[Task] = relationship(back_populates="notifications")


class TaskNotificationState(Base):
    """Singleton cursor preventing retroactive lifecycle notifications."""

    __tablename__ = "task_notification_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_task_notification_state_singleton"),
        CheckConstraint(
            "last_lifecycle_event_id >= 0",
            name="ck_task_notification_state_event_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_lifecycle_event_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class TaskNotificationDeferredLifecycleEvent(Base):
    """Lifecycle event retained while its task's chat is not admitted."""

    __tablename__ = "task_notification_deferred_lifecycle_events"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("task_lifecycle_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    deferred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )


class TaskEvidence(Base):
    __tablename__ = "task_evidence"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "message_db_id",
            name="uq_task_evidence_task_message",
        ),
        Index("ix_task_evidence_message", "message_db_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    message_db_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    task: Mapped[Task] = relationship(back_populates="evidence")
    message: Mapped[Message] = relationship(back_populates="task_evidence")


class TaskSource(Base):
    __tablename__ = "task_sources"
    __table_args__ = (
        UniqueConstraint(
            "detection_run_id",
            "candidate_index",
            name="uq_task_sources_run_candidate",
        ),
        CheckConstraint(
            "candidate_index >= 0 AND candidate_index < 10",
            name="ck_task_sources_candidate_index",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_task_sources_confidence",
        ),
        Index("ix_task_sources_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    detection_run_id: Mapped[int] = mapped_column(
        ForeignKey("detection_runs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )

    task: Mapped[Task] = relationship(back_populates="sources")
    detection_run: Mapped[DetectionRun] = relationship(
        back_populates="task_sources"
    )


class DetectionMaterialization(Base):
    __tablename__ = "detection_materializations"
    __table_args__ = (
        CheckConstraint(
            "candidate_count >= 0 AND candidate_count <= 10",
            name="ck_detection_materializations_candidate_count",
        ),
        CheckConstraint(
            "created_task_count >= 0 AND reused_task_count >= 0 "
            "AND created_task_count + reused_task_count = candidate_count",
            name="ck_detection_materializations_counts",
        ),
    )

    detection_run_id: Mapped[int] = mapped_column(
        ForeignKey("detection_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reused_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    materialized_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False
    )

    detection_run: Mapped[DetectionRun] = relationship(
        back_populates="materialization"
    )
