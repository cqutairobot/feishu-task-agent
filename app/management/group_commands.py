"""Group-owner self-service commands for chat administration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging

from app.feishu.directory import DirectoryService
from app.feishu.messages import IncomingMessage
from app.management.access import (
    AdministratorSource,
    ChatAdministratorError,
    ChatAdministratorRepository,
)


LOGGER = logging.getLogger(__name__)


class GroupManagementCommandKind(StrEnum):
    INITIALIZE = "initialize_chat_administration"
    TAKEOVER = "takeover_chat_administration"


@dataclass(frozen=True, slots=True)
class GroupManagementCommandResult:
    kind: GroupManagementCommandKind
    succeeded: bool
    reply_text: str


class GroupManagementCommandProcessor:
    """Verify the live Feishu owner before granting chat-scoped access."""

    def __init__(
        self,
        administrators: ChatAdministratorRepository,
        directory: DirectoryService,
        *,
        bot_open_id: str,
    ) -> None:
        if not bot_open_id.strip():
            raise ValueError("bot_open_id must not be empty")
        self._administrators = administrators
        self._directory = directory
        self._bot_open_id = bot_open_id.strip()

    def matches(self, message: IncomingMessage) -> bool:
        return parse_group_management_command(
            message, self._bot_open_id
        ) is not None

    def allows_chat(self, message: IncomingMessage) -> bool:
        """Allow an initialized group, or one explicit owner-onboarding command."""

        return self.matches(message) or self._administrators.has_administrator(
            message.chat_id
        )

    def handle(
        self, message: IncomingMessage
    ) -> GroupManagementCommandResult | None:
        kind = parse_group_management_command(message, self._bot_open_id)
        if kind is None:
            return None
        try:
            snapshot = self._directory.refresh_strict(
                message.chat_id,
                chat_type=message.chat_type,
                tenant_key=message.tenant_key,
            )
        except Exception:
            LOGGER.exception("Unable to verify current Feishu group owner")
            return GroupManagementCommandResult(
                kind=kind,
                succeeded=False,
                reply_text=(
                    "⚠️ 暂时无法向飞书核验本群群主，未修改管理员。"
                    "请稍后重试。"
                ),
            )
        if snapshot.owner_open_id != message.sender_open_id:
            return GroupManagementCommandResult(
                kind=kind,
                succeeded=False,
                reply_text="⛔ 只有当前飞书群主可以执行该命令。",
            )

        source = (
            AdministratorSource.GROUP_OWNER_INIT
            if kind is GroupManagementCommandKind.INITIALIZE
            else AdministratorSource.GROUP_OWNER_TAKEOVER
        )
        try:
            change = self._administrators.grant(
                message.chat_id,
                message.sender_open_id,
                source=source,
                actor_open_id=message.sender_open_id,
                granted_at=message.received_at,
            )
        except ChatAdministratorError as exc:
            LOGGER.warning("Unable to apply group-owner command: %s", exc)
            return GroupManagementCommandResult(
                kind=kind,
                succeeded=False,
                reply_text="❌ 管理员设置失败，群成员状态可能刚刚发生变化，请重试。",
            )

        if change.changed:
            action = "初始化" if kind is GroupManagementCommandKind.INITIALIZE else "接管"
            return GroupManagementCommandResult(
                kind=kind,
                succeeded=True,
                reply_text=(
                    f"✅ 本群任务管理已{action}成功\n\n"
                    "你已成为本群任务管理员。"
                    "现在可以私聊机器人发送“管理后台”。"
                ),
            )
        if change.administrator is not None:
            return GroupManagementCommandResult(
                kind=kind,
                succeeded=True,
                reply_text="ℹ️ 你已经是本群任务管理员，无需重复设置。",
            )
        return GroupManagementCommandResult(
            kind=kind,
            succeeded=False,
            reply_text=(
                "ℹ️ 本群已经完成管理员初始化。"
                "如需恢复群主权限，请发送：@机器人 接管本群"
            ),
        )


def parse_group_management_command(
    message: IncomingMessage, bot_open_id: str
) -> GroupManagementCommandKind | None:
    if (
        message.chat_type != "group"
        or message.message_type != "text"
        or message.sender_type == "bot"
    ):
        return None
    text = message.text.lstrip()
    mention = next(
        (
            item
            for item in message.mentions
            if item.open_id == bot_open_id and text.startswith(item.key)
        ),
        None,
    )
    if mention is None:
        return None
    body = text[len(mention.key) :].strip().rstrip("。.!！?？").strip()
    if body == "初始化本群":
        return GroupManagementCommandKind.INITIALIZE
    if body == "接管本群":
        return GroupManagementCommandKind.TAKEOVER
    return None


def is_group_management_command_message(message: IncomingMessage) -> bool:
    return any(
        mention.mentioned_type == "bot"
        and parse_group_management_command(message, mention.open_id) is not None
        for mention in message.mentions
    )
