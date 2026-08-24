"""Deterministic group commands for verified member aliases."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging

from app.feishu.messages import IncomingMessage, MessageMention
from app.identity.aliases import (
    AliasConflictError,
    AliasError,
    AliasRepository,
    clean_alias,
)


LOGGER = logging.getLogger(__name__)


class IdentityCommandKind(StrEnum):
    SELF_BIND = "self_bind"
    QUERY_SELF = "query_self"
    ADMIN_BIND = "admin_bind"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ParsedIdentityCommand:
    kind: IdentityCommandKind
    alias: str | None = None
    target: MessageMention | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityCommandResult:
    kind: IdentityCommandKind
    succeeded: bool
    reply_text: str


class IdentityCommandProcessor:
    """Parse explicit bot commands and apply safe alias mutations."""

    def __init__(
        self,
        aliases: AliasRepository,
        *,
        bot_open_id: str,
        admin_open_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not bot_open_id.strip():
            raise ValueError("bot_open_id must not be empty")
        self._aliases = aliases
        self._bot_open_id = bot_open_id.strip()
        self._admin_open_ids = admin_open_ids

    def matches(self, message: IncomingMessage) -> bool:
        """Return whether this is an identity command without applying it."""

        return parse_identity_command(message, self._bot_open_id) is not None

    def handle(self, message: IncomingMessage) -> IdentityCommandResult | None:
        command = parse_identity_command(message, self._bot_open_id)
        if command is None:
            return None
        if command.kind is IdentityCommandKind.INVALID:
            return IdentityCommandResult(
                kind=command.kind,
                succeeded=False,
                reply_text=f"❌ 命令格式不正确：{command.error}",
            )
        if command.kind is IdentityCommandKind.QUERY_SELF:
            return self._query_self(message)
        if command.kind is IdentityCommandKind.ADMIN_BIND:
            return self._admin_bind(message, command)
        return self._self_bind(message, command)

    def _self_bind(
        self, message: IncomingMessage, command: ParsedIdentityCommand
    ) -> IdentityCommandResult:
        assert command.alias is not None
        try:
            binding = self._aliases.bind(
                message.chat_id,
                message.sender_open_id,
                command.alias,
                source="self_command",
            )
        except AliasConflictError:
            return IdentityCommandResult(
                kind=command.kind,
                succeeded=False,
                reply_text=(
                    f'❌ 绑定失败：姓名“{command.alias}”已被本群其他成员使用。'
                    "请联系管理员处理。"
                ),
            )
        except AliasError as exc:
            LOGGER.warning("Unable to apply self alias command: %s", exc)
            return self._internal_failure(command.kind)

        return IdentityCommandResult(
            kind=command.kind,
            succeeded=True,
            reply_text=(
                "✅ 姓名绑定成功\n\n"
                f"本群姓名：{binding.alias}\n"
                f'以后涉及“{binding.alias}”的任务将关联到你。'
            ),
        )

    def _query_self(self, message: IncomingMessage) -> IdentityCommandResult:
        binding = self._aliases.for_member(
            message.chat_id, message.sender_open_id
        )
        if binding is None:
            return IdentityCommandResult(
                kind=IdentityCommandKind.QUERY_SELF,
                succeeded=True,
                reply_text=(
                    "你还没有在本群绑定姓名。\n"
                    "请发送：@机器人 绑定姓名：你的姓名"
                ),
            )
        return IdentityCommandResult(
            kind=IdentityCommandKind.QUERY_SELF,
            succeeded=True,
            reply_text=f"你在本群绑定的姓名是：{binding.alias}",
        )

    def _admin_bind(
        self, message: IncomingMessage, command: ParsedIdentityCommand
    ) -> IdentityCommandResult:
        if message.sender_open_id not in self._admin_open_ids:
            return IdentityCommandResult(
                kind=command.kind,
                succeeded=False,
                reply_text="⛔ 只有身份管理员可以为其他成员绑定姓名。",
            )
        assert command.alias is not None
        assert command.target is not None
        try:
            binding = self._aliases.bind(
                message.chat_id,
                command.target.open_id,
                command.alias,
                source="admin_command",
            )
        except AliasConflictError:
            return IdentityCommandResult(
                kind=command.kind,
                succeeded=False,
                reply_text=(
                    f'❌ 绑定失败：姓名“{command.alias}”已被本群其他成员使用。'
                ),
            )
        except AliasError as exc:
            LOGGER.warning("Unable to apply administrator alias command: %s", exc)
            if "has not sent" in str(exc) or "unknown open_id" in str(exc):
                text = "❌ 该成员尚未在本群留下消息，请让他先发送一条消息。"
            else:
                text = "❌ 绑定失败，请查看本地服务日志。"
            return IdentityCommandResult(
                kind=command.kind,
                succeeded=False,
                reply_text=text,
            )

        return IdentityCommandResult(
            kind=command.kind,
            succeeded=True,
            reply_text=(
                "✅ 成员姓名绑定成功\n\n"
                f"本群姓名：{binding.alias}"
            ),
        )

    @staticmethod
    def _internal_failure(kind: IdentityCommandKind) -> IdentityCommandResult:
        return IdentityCommandResult(
            kind=kind,
            succeeded=False,
            reply_text="❌ 绑定失败，请查看本地服务日志。",
        )


def parse_identity_command(
    message: IncomingMessage, bot_open_id: str
) -> ParsedIdentityCommand | None:
    """Parse commands only when the current bot is the leading mention."""

    if (
        message.chat_type != "group"
        or message.message_type != "text"
        or message.sender_type == "bot"
    ):
        return None

    text = message.text.lstrip()
    bot_mention = next(
        (
            mention
            for mention in message.mentions
            if mention.open_id == bot_open_id and text.startswith(mention.key)
        ),
        None,
    )
    if bot_mention is None:
        return None

    body = text[len(bot_mention.key) :].strip()
    if body.rstrip("?？") == "我的姓名":
        return ParsedIdentityCommand(IdentityCommandKind.QUERY_SELF)

    if body.startswith("我的姓名"):
        suffix = body[len("我的姓名") :]
        if suffix and suffix[0] in " \t:：是":
            raw_name = suffix.lstrip(" \t:：是")
            return _name_command(
                IdentityCommandKind.SELF_BIND, raw_name, message
            )

    if body.startswith("绑定姓名"):
        raw_name = body[len("绑定姓名") :].lstrip(" \t:：")
        return _name_command(IdentityCommandKind.SELF_BIND, raw_name, message)

    if body.startswith("绑定成员"):
        remainder = body[len("绑定成员") :].strip()
        target = next(
            (
                mention
                for mention in message.mentions
                if mention.open_id != bot_open_id
                and remainder.startswith(mention.key)
            ),
            None,
        )
        if target is None:
            return ParsedIdentityCommand(
                IdentityCommandKind.INVALID,
                error="请使用飞书 @ 选择需要绑定的成员。",
            )
        raw_name = remainder[len(target.key) :].strip()
        if raw_name.startswith("为"):
            raw_name = raw_name[1:].strip()
        elif raw_name.startswith("姓名"):
            raw_name = raw_name[len("姓名") :].lstrip(" \t:：")
        else:
            return ParsedIdentityCommand(
                IdentityCommandKind.INVALID,
                error="请使用“绑定成员 @成员 为 姓名”。",
            )
        return _name_command(
            IdentityCommandKind.ADMIN_BIND,
            raw_name,
            message,
            target=target,
        )

    return None


def is_identity_command_message(message: IncomingMessage) -> bool:
    """Recognize identity commands addressed to any explicit bot mention."""

    return any(
        mention.mentioned_type == "bot"
        and parse_identity_command(message, mention.open_id) is not None
        for mention in message.mentions
    )


def _name_command(
    kind: IdentityCommandKind,
    raw_name: str,
    message: IncomingMessage,
    *,
    target: MessageMention | None = None,
) -> ParsedIdentityCommand:
    try:
        alias = clean_alias(raw_name)
    except AliasError:
        return ParsedIdentityCommand(
            IdentityCommandKind.INVALID,
            error="姓名不能为空。",
        )
    if len(alias) > 64:
        return ParsedIdentityCommand(
            IdentityCommandKind.INVALID,
            error="姓名不能超过 64 个字符。",
        )
    if any(mention.key in alias for mention in message.mentions):
        return ParsedIdentityCommand(
            IdentityCommandKind.INVALID,
            error="姓名中不能包含 @成员。",
        )
    return ParsedIdentityCommand(kind=kind, alias=alias, target=target)
