"""Normalize a Feishu event and persist it idempotently."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.database.repository import MessageRepository, SaveResult
from app.feishu.messages import IncomingMessage, normalize_message_event
from app.management.settings import ChatSettingsRepository


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    message: IncomingMessage
    persistence: SaveResult


class MessageIngestionService:
    def __init__(
        self,
        repository: MessageRepository,
        *,
        debounce_seconds: int = 20,
        chat_settings: ChatSettingsRepository | None = None,
    ) -> None:
        if not 1 <= debounce_seconds <= 60:
            raise ValueError("debounce_seconds must be between 1 and 60")
        self._repository = repository
        self._debounce_seconds = debounce_seconds
        self._chat_settings = chat_settings

    def process_payload(
        self,
        payload: dict[str, Any],
        *,
        received_at: datetime | None = None,
        enqueue_detection: bool = True,
    ) -> IngestionOutcome:
        message = normalize_message_event(payload, received_at=received_at)
        return self.process_message(
            message, enqueue_detection=enqueue_detection
        )

    def process_message(
        self,
        message: IncomingMessage,
        *,
        enqueue_detection: bool = True,
    ) -> IngestionOutcome:
        """Persist an already-normalized message."""

        eligible = (
            enqueue_detection
            and message.chat_type == "group"
            and message.message_type == "text"
            and message.sender_type != "bot"
            and (
                self._chat_settings is None
                or self._chat_settings.detection_enabled(message.chat_id)
            )
        )
        result = self._repository.save(
            message,
            enqueue_detection=eligible,
            debounce_seconds=self._debounce_seconds,
        )
        return IngestionOutcome(message=message, persistence=result)
