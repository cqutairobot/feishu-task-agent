"""Bot identity lookup and idempotent text replies through Feishu OpenAPI."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import json
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
from lark_oapi.channel.bot_identity import fetch_bot_identity

from app.config import FeishuSettings


class FeishuReplyError(RuntimeError):
    """Raised when Feishu rejects a command response."""


class BotIdentityError(RuntimeError):
    """Raised when the current application's bot Open ID cannot be resolved."""


def build_api_client(settings: FeishuSettings) -> lark.Client:
    return (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


class FeishuMessageReplier:
    def __init__(
        self, settings: FeishuSettings, *, client: lark.Client | None = None
    ) -> None:
        self._client = client or build_api_client(settings)

    def reply_text(self, message_id: str, text: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        self._reply(
            message_id,
            msg_type="text",
            content=content,
            uuid_prefix="identity",
        )

    def reply_card(
        self,
        message_id: str,
        card: Mapping[str, Any],
        *,
        fallback_text: str | None = None,
    ) -> bool:
        """Reply with an interactive card, optionally falling back to text."""

        try:
            content = json.dumps(
                card,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._reply(
                message_id,
                msg_type="interactive",
                content=content,
                uuid_prefix="task-card",
            )
        except (FeishuReplyError, TypeError, ValueError):
            if fallback_text is None:
                raise
            self.reply_text(message_id, fallback_text)
            return False
        return True

    def _reply(
        self,
        message_id: str,
        *,
        msg_type: str,
        content: str,
        uuid_prefix: str,
    ) -> None:
        uuid = (
            f"{uuid_prefix}-"
            + hashlib.sha256(message_id.encode()).hexdigest()[:32]
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(msg_type)
                .uuid(uuid)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if response.success():
            return
        code = getattr(response, "code", "unknown")
        message = getattr(response, "msg", "unknown error")
        raise FeishuReplyError(
            f"reply message failed: code={code}, msg={message}"
        )


def resolve_bot_open_id(settings: FeishuSettings) -> str:
    """Resolve the bot identity before accepting explicit @bot commands."""

    client = build_api_client(settings)
    config = client.config
    if config is None:
        raise BotIdentityError("Feishu client has no configuration")
    identity = asyncio.run(fetch_bot_identity(config))
    if identity is None or not identity.open_id:
        raise BotIdentityError("Feishu bot Open ID could not be resolved")
    return identity.open_id
