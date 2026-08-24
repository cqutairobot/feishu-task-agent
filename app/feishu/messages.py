"""Normalize Feishu message events into an application-owned model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from copy import deepcopy
import json
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class MessageEventError(ValueError):
    """Raised when a Feishu message event lacks required fields."""


@dataclass(frozen=True, slots=True)
class MessageMention:
    """A user or bot explicitly mentioned in a Feishu message."""

    key: str
    open_id: str
    name: str | None
    mentioned_type: str | None
    tenant_key: str | None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Fields needed by Phase 1 and retained for later database ingestion."""

    event_id: str
    tenant_key: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_union_id: str | None
    sender_name: str | None
    sender_type: str
    message_type: str
    text: str
    mentions: tuple[MessageMention, ...]
    raw_content: str
    raw_event_json: str
    root_id: str | None
    parent_id: str | None
    created_at: datetime
    received_at: datetime

    def terminal_output(self) -> str:
        """Format the message for the Phase 1 terminal acceptance test."""

        return "\n".join(
            (
                f"[{self.created_at:%H:%M:%S}]",
                f"message_id: {self.message_id}",
                f"chat_id: {self.chat_id}",
                f"sender_open_id: {self.sender_open_id}",
                f"sender_name: {self.sender_name or '(unresolved)'}",
                f"message_type: {self.message_type}",
                f"message: {self.text}",
            )
        )


def normalize_message_event(
    payload: dict[str, Any], *, received_at: datetime | None = None
) -> IncomingMessage:
    """Convert an ``im.message.receive_v1`` payload into ``IncomingMessage``."""

    try:
        header = payload["header"]
        event = payload["event"]
        sender_id = event["sender"]["sender_id"]
        message = event["message"]

        event_id = _required_text(header, "event_id")
        tenant_key = _required_text(header, "tenant_key")
        message_id = _required_text(message, "message_id")
        chat_id = _required_text(message, "chat_id")
        chat_type = _required_text(message, "chat_type")
        sender_open_id = _required_text(sender_id, "open_id")
        sender_union_id = _optional_text(sender_id, "union_id")
        sender_name = _optional_text(event["sender"], "name")
        sender_type = _required_text(event["sender"], "sender_type")
        message_type = _required_text(message, "message_type")
        raw_content = _required_text(message, "content")
        mentions = _parse_mentions(message.get("mentions"))
        root_id = _optional_text(message, "root_id")
        parent_id = _optional_text(message, "parent_id")
        created_at = _parse_created_at(message, header)
        text = _extract_text(message_type, raw_content)
    except (KeyError, TypeError) as exc:
        raise MessageEventError(f"missing or invalid event field: {exc}") from exc

    received_at = received_at or datetime.now(timezone.utc)
    if received_at.tzinfo is None:
        raise MessageEventError("received_at must include timezone information")

    return IncomingMessage(
        event_id=event_id,
        tenant_key=tenant_key,
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        sender_union_id=sender_union_id,
        sender_name=sender_name,
        sender_type=sender_type,
        message_type=message_type,
        text=text,
        mentions=mentions,
        raw_content=raw_content,
        raw_event_json=_sanitized_event_json(payload),
        root_id=root_id,
        parent_id=parent_id,
        created_at=created_at,
        received_at=received_at.astimezone(timezone.utc),
    )


def _required_text(container: dict[str, Any], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MessageEventError(f"{key} must be a non-empty string")
    return value


def _optional_text(container: dict[str, Any], key: str) -> str | None:
    value = container.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MessageEventError(f"{key} must be a string when present")
    return value


def _parse_created_at(
    message: dict[str, Any], header: dict[str, Any]
) -> datetime:
    timestamp = message.get("create_time") or header.get("create_time")
    try:
        milliseconds = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise MessageEventError("create_time must be a millisecond timestamp") from exc
    return datetime.fromtimestamp(milliseconds / 1000, tz=SHANGHAI_TZ)


def _extract_text(message_type: str, raw_content: str) -> str:
    if message_type != "text":
        return f"<{message_type} message>"

    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise MessageEventError("text message content must be valid JSON") from exc

    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        raise MessageEventError("text message content must contain a text field")
    return content["text"]


def _parse_mentions(value: Any) -> tuple[MessageMention, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MessageEventError("mentions must be a list when present")

    mentions: list[MessageMention] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise MessageEventError(f"mentions[{index}] must be an object")
        mention_id = item.get("id")
        if not isinstance(mention_id, dict):
            raise MessageEventError(f"mentions[{index}].id must be an object")
        key = item.get("key")
        open_id = mention_id.get("open_id")
        if not isinstance(key, str) or not key.strip():
            raise MessageEventError(f"mentions[{index}].key must be non-empty")
        if not isinstance(open_id, str) or not open_id.strip():
            raise MessageEventError(
                f"mentions[{index}].id.open_id must be non-empty"
            )
        mentions.append(
            MessageMention(
                key=key,
                open_id=open_id,
                name=_optional_mention_text(item.get("name")),
                mentioned_type=_optional_mention_text(
                    item.get("mentioned_type")
                ),
                tenant_key=_optional_mention_text(item.get("tenant_key")),
            )
        )
    return tuple(mentions)


def _optional_mention_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MessageEventError("mention text fields must be strings when present")
    return value


def _sanitized_event_json(payload: dict[str, Any]) -> str:
    sanitized = deepcopy(payload)
    header = sanitized.get("header")
    if isinstance(header, dict):
        header.pop("token", None)
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
