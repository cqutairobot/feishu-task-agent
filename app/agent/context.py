"""Build stable, chat-isolated context for task detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Callable
from zoneinfo import ZoneInfo

from app.database.repository import ConversationLine, MessageRepository
from app.identity.aliases import AliasRepository


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class ContextMessage:
    message_id: str
    sender_open_id: str
    sender_name: str
    content: str
    created_at: datetime
    mentions: tuple["ContextMention", ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "sender_open_id": self.sender_open_id,
            "sender_name": self.sender_name,
            "content": self.content,
            "created_at": self.created_at.astimezone(SHANGHAI_TZ).isoformat(),
            "mentions": [mention.to_dict() for mention in self.mentions],
        }


@dataclass(frozen=True, slots=True)
class ContextMention:
    """Exact mention target with its chat-confirmed task name."""

    key: str
    open_id: str
    name: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "open_id": self.open_id,
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class ContextParticipant:
    open_id: str
    name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "open_id": self.open_id,
            "name": self.name,
        }

    @property
    def accepted_names(self) -> frozenset[str]:
        return frozenset((self.name,))


@dataclass(frozen=True, slots=True)
class TaskDetectionContext:
    chat_id: str
    trigger_message_id: str
    timezone: str
    reference_time: datetime
    participants: tuple[ContextParticipant, ...]
    messages: tuple[ContextMessage, ...]
    focus_message_ids: tuple[str, ...] = ()
    task_scope: str = "broad"

    def to_dict(self) -> dict[str, object]:
        return {
            "context_version": "1.3",
            "chat_id": self.chat_id,
            "trigger_message_id": self.trigger_message_id,
            "timezone": self.timezone,
            "task_scope": self.task_scope,
            "reference_time": self.reference_time.astimezone(
                SHANGHAI_TZ
            ).isoformat(),
            "known_participants": [
                participant.to_dict() for participant in self.participants
            ],
            "focus_message_ids": list(self.focus_message_ids),
            "messages": [message.to_dict() for message in self.messages],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class TaskDetectionContextBuilder:
    def __init__(
        self,
        messages: MessageRepository,
        aliases: AliasRepository,
        *,
        default_limit: int = 30,
        max_content_characters: int = 12_000,
        max_idle_gap: timedelta = timedelta(minutes=15),
        task_scope_resolver: Callable[[str], str] | None = None,
    ) -> None:
        if default_limit < 1:
            raise ValueError("default_limit must be at least 1")
        if max_content_characters < 1:
            raise ValueError("max_content_characters must be at least 1")
        if max_idle_gap <= timedelta(0):
            raise ValueError("max_idle_gap must be positive")
        self._messages = messages
        self._aliases = aliases
        self._default_limit = default_limit
        self._max_content_characters = max_content_characters
        self._max_idle_gap = max_idle_gap
        self._task_scope_resolver = task_scope_resolver

    def build(
        self,
        chat_id: str,
        trigger_message_id: str,
        *,
        limit: int | None = None,
        focus_since: datetime | None = None,
    ) -> TaskDetectionContext:
        window = self._messages.conversation_through(
            chat_id,
            trigger_message_id,
            limit=self._default_limit if limit is None else limit,
        )
        window = self._most_recent_conversation_segment(window)
        selected = self._fit_content_budget(window)
        if not selected or selected[-1].message_id != trigger_message_id:
            raise ValueError("trigger message is not an eligible human message")

        names_by_user = {
            binding.open_id: binding.alias
            for binding in self._aliases.list_for_chat(chat_id)
        }
        context_messages = tuple(
            ContextMessage(
                message_id=line.message_id,
                sender_open_id=line.sender_open_id,
                sender_name=line.sender_name,
                content=line.content,
                created_at=line.created_at,
                mentions=tuple(
                    ContextMention(
                        key=mention.key,
                        open_id=mention.open_id,
                        name=names_by_user.get(mention.open_id) or mention.name,
                    )
                    for mention in line.mentions
                ),
            )
            for line in selected
        )
        participants = self._participants(context_messages, names_by_user)
        if focus_since is None:
            focus_message_ids = (trigger_message_id,)
        else:
            if focus_since.tzinfo is None:
                raise ValueError("focus_since must include timezone information")
            focus_since_utc = focus_since.astimezone(timezone.utc)
            focus_message_ids = tuple(
                line.message_id
                for line in selected
                if line.received_at >= focus_since_utc
            )
            if not focus_message_ids:
                raise ValueError("no batch-focus messages remain in context")
        task_scope = (
            "broad"
            if self._task_scope_resolver is None
            else self._task_scope_resolver(chat_id)
        )
        if task_scope not in {"broad", "work_only"}:
            raise ValueError("task scope must be broad or work_only")
        return TaskDetectionContext(
            chat_id=chat_id,
            trigger_message_id=trigger_message_id,
            timezone="Asia/Shanghai",
            reference_time=context_messages[-1].created_at,
            participants=participants,
            messages=context_messages,
            focus_message_ids=focus_message_ids,
            task_scope=task_scope,
        )

    def _most_recent_conversation_segment(
        self, window: list[ConversationLine]
    ) -> list[ConversationLine]:
        """Drop stale topics before the most recent chat inactivity gap."""

        segment_start = 0
        for index in range(1, len(window)):
            idle_gap = (
                window[index].created_at - window[index - 1].created_at
            )
            if idle_gap > self._max_idle_gap:
                segment_start = index
        return window[segment_start:]

    def _fit_content_budget(
        self, window: list[ConversationLine]
    ) -> list[ConversationLine]:
        selected_reversed: list[ConversationLine] = []
        characters = 0
        for line in reversed(window):
            cost = len(line.sender_name) + len(line.content)
            if selected_reversed and characters + cost > self._max_content_characters:
                break
            selected_reversed.append(line)
            characters += cost
        return list(reversed(selected_reversed))

    def _participants(
        self,
        messages: tuple[ContextMessage, ...],
        names_by_user: dict[str, str],
    ) -> tuple[ContextParticipant, ...]:
        context_names: dict[str, str] = {}
        for message in messages:
            context_names[message.sender_open_id] = message.sender_name

        open_ids = set(context_names) | set(names_by_user)
        participants = []
        for open_id in sorted(open_ids):
            name = names_by_user.get(open_id) or context_names[open_id]
            participants.append(
                ContextParticipant(
                    open_id=open_id,
                    name=name,
                )
            )
        return tuple(participants)
