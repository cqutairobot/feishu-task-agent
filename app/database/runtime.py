"""Lifecycle management for the migrated message database."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

from sqlalchemy import Engine

from app.agent.queue import DetectionQueueRepository
from app.config import (
    DatabaseSettings,
    DetectionSettings,
    LifecycleSettings,
    ManagementWebSettings,
    ReminderSettings,
    TaskSettings,
)
from app.database.engine import create_database_engine, create_session_factory
from app.database.migrate import upgrade_database
from app.database.repository import MessageRepository
from app.identity.aliases import AliasRepository
from app.ingestion.service import MessageIngestionService
from app.lifecycle.mutations import LifecycleMutationService
from app.management.access import ChatAdministratorRepository
from app.management.auth import ManagementAuthRepository
from app.management.queries import ManagementReadApi
from app.management.settings import ChatSettingsRepository
from app.notifications.repository import TaskNotificationRepository
from app.reminders.repository import ReminderRepository
from app.tasks.repository import TaskRepository
from app.tasks.manual_creation import ManagementTaskCreationService
from app.tasks.notes import TaskNoteService


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    engine: Engine
    repository: MessageRepository
    aliases: AliasRepository
    ingestion: MessageIngestionService
    detection_queue: DetectionQueueRepository
    tasks: TaskRepository
    reminders: ReminderRepository
    notifications: TaskNotificationRepository
    lifecycle_mutations: LifecycleMutationService
    management_task_creation: ManagementTaskCreationService
    task_notes: TaskNoteService
    chat_administrators: ChatAdministratorRepository
    management: ManagementReadApi
    management_auth: ManagementAuthRepository
    chat_settings: ChatSettingsRepository


@contextmanager
def open_database_runtime(
    settings: DatabaseSettings,
    detection_settings: DetectionSettings | None = None,
    task_settings: TaskSettings | None = None,
    reminder_settings: ReminderSettings | None = None,
    lifecycle_settings: LifecycleSettings | None = None,
    management_web_settings: ManagementWebSettings | None = None,
    lifecycle_administrator_open_ids: frozenset[str] = frozenset(),
    lifecycle_allowed_chat_ids: frozenset[str] = frozenset(),
) -> Iterator[DatabaseRuntime]:
    """Migrate, open, and reliably dispose the configured database."""

    upgrade_database(settings.url)
    engine = create_database_engine(settings.url, echo=settings.echo)
    session_factory = create_session_factory(engine)
    repository = MessageRepository(session_factory)
    detection_settings = detection_settings or DetectionSettings()
    task_settings = task_settings or TaskSettings()
    reminder_settings = reminder_settings or ReminderSettings()
    lifecycle_settings = lifecycle_settings or LifecycleSettings()
    management_web_settings = management_web_settings or ManagementWebSettings()
    reminders = ReminderRepository(
        session_factory,
        settings=reminder_settings,
    )
    chat_administrators = ChatAdministratorRepository(session_factory)
    chat_settings = ChatSettingsRepository(
        session_factory,
        reminder_settings=reminder_settings,
    )
    runtime = DatabaseRuntime(
        engine=engine,
        repository=repository,
        aliases=AliasRepository(session_factory),
        ingestion=MessageIngestionService(
            repository,
            debounce_seconds=detection_settings.debounce_seconds,
            chat_settings=chat_settings,
        ),
        detection_queue=DetectionQueueRepository(session_factory),
        tasks=TaskRepository(
            session_factory,
            auto_todo_confidence=task_settings.auto_todo_confidence,
            reminder_settings=reminder_settings,
        ),
        reminders=reminders,
        notifications=TaskNotificationRepository(
            session_factory,
            administrator_open_ids=lifecycle_administrator_open_ids,
            allowed_chat_ids=lifecycle_allowed_chat_ids,
            settings=reminder_settings,
        ),
        lifecycle_mutations=LifecycleMutationService(
            session_factory,
            reminder_settings=reminder_settings,
            administrator_open_ids=lifecycle_administrator_open_ids,
            allowed_chat_ids=lifecycle_allowed_chat_ids,
            minimum_confidence=lifecycle_settings.minimum_confidence,
        ),
        management_task_creation=ManagementTaskCreationService(
            session_factory,
            reminder_settings=reminder_settings,
        ),
        task_notes=TaskNoteService(session_factory),
        chat_administrators=chat_administrators,
        management=ManagementReadApi(session_factory),
        management_auth=ManagementAuthRepository(
            session_factory,
            login_ttl=timedelta(
                minutes=management_web_settings.login_ttl_minutes
            ),
            session_ttl=timedelta(
                hours=management_web_settings.session_ttl_hours
            ),
        ),
        chat_settings=chat_settings,
    )
    try:
        yield runtime
    finally:
        engine.dispose()
