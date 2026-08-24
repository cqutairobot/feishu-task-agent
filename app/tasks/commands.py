"""Deterministic task queries with sender- and chat-scoped authorization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from app.agent.context import SHANGHAI_TZ
from app.feishu.messages import IncomingMessage
from app.management.access import ChatAdministratorRepository
from app.tasks.cards import build_private_task_list_card
from app.tasks.repository import (
    CrossChatTaskEntry,
    CrossChatTaskListPage,
    TaskListPage,
    TaskRepository,
    TaskSnapshot,
    TaskStatus,
)


class TaskCommandKind(StrEnum):
    LIST = "list"


@dataclass(frozen=True, slots=True)
class TaskCommandResult:
    kind: TaskCommandKind
    succeeded: bool
    reply_text: str
    reply_card: dict[str, Any] | None = None


class TaskCommandProcessor:
    """Show personal tasks by default and all tasks only to administrators."""

    def __init__(
        self,
        tasks: TaskRepository,
        *,
        bot_open_id: str,
        task_admin_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        reply_limit: int = 20,
        private_cards_enabled: bool = False,
        card_actions_enabled: bool = False,
        chat_name_refresher: Callable[[str], str | None] | None = None,
        chat_administrators: ChatAdministratorRepository | None = None,
    ) -> None:
        if not bot_open_id.strip():
            raise ValueError("bot_open_id must not be empty")
        if not 1 <= reply_limit <= 20:
            raise ValueError("reply_limit must be between 1 and 20")
        if any(not open_id.strip() for open_id in task_admin_open_ids):
            raise ValueError("task administrator Open IDs must not be empty")
        if any(not chat_id.strip() for chat_id in allowed_chat_ids):
            raise ValueError("allowed chat IDs must not be empty")
        if card_actions_enabled and not private_cards_enabled:
            raise ValueError(
                "card actions require private task cards to be enabled"
            )
        self._tasks = tasks
        self._bot_open_id = bot_open_id.strip()
        self._task_admin_open_ids = frozenset(
            open_id.strip() for open_id in task_admin_open_ids
        )
        self._allowed_chat_ids = frozenset(
            chat_id.strip() for chat_id in allowed_chat_ids
        )
        self._reply_limit = reply_limit
        self._private_cards_enabled = private_cards_enabled
        self._card_actions_enabled = card_actions_enabled
        self._chat_name_refresher = chat_name_refresher
        self._chat_administrators = chat_administrators

    def matches(self, message: IncomingMessage) -> bool:
        return parse_task_command(message, self._bot_open_id) is not None

    def handle(self, message: IncomingMessage) -> TaskCommandResult | None:
        kind = parse_task_command(message, self._bot_open_id)
        if kind is None:
            return None
        if message.chat_type == "group":
            is_admin = self._is_group_administrator(
                message.chat_id, message.sender_open_id
            )
            page = self._tasks.list_open_tasks(
                message.chat_id,
                owner_open_id=(
                    None if is_admin else message.sender_open_id
                ),
                limit=self._reply_limit,
            )
            reply_text = format_group_task_list(page, is_admin=is_admin)
            reply_card = None
        else:
            admin_chat_ids = self._administrator_chat_ids(
                message.sender_open_id
            )
            is_admin = admin_chat_ids is None or bool(admin_chat_ids)
            page = self._tasks.list_open_tasks_across_chats(
                owner_open_id=(
                    None if is_admin else message.sender_open_id
                ),
                chat_ids=(
                    admin_chat_ids
                    if is_admin
                    else self._admitted_chat_ids()
                ),
                limit=self._reply_limit,
            )
            page = _refresh_page_chat_names(
                page, self._chat_name_refresher
            )
            reply_text = format_private_task_list(page, is_admin=is_admin)
            reply_card = (
                build_private_task_list_card(
                    page,
                    is_admin=is_admin,
                    actions_enabled=self._card_actions_enabled,
                )
                if self._private_cards_enabled
                else None
            )
        return TaskCommandResult(
            kind=kind,
            succeeded=True,
            reply_text=reply_text,
            reply_card=reply_card,
        )

    def _is_group_administrator(self, chat_id: str, open_id: str) -> bool:
        if open_id in self._task_admin_open_ids:
            return True
        return (
            self._chat_administrators is not None
            and self._chat_administrators.is_administrator(chat_id, open_id)
        )

    def _administrator_chat_ids(
        self, open_id: str
    ) -> frozenset[str] | None:
        if open_id in self._task_admin_open_ids:
            return self._allowed_chat_ids or None
        if self._chat_administrators is None:
            return frozenset()
        return self._chat_administrators.chat_ids_for_administrator(open_id)

    def _admitted_chat_ids(self) -> frozenset[str] | None:
        if self._chat_administrators is None:
            return self._allowed_chat_ids or None
        return self._chat_administrators.admitted_chat_ids(
            self._allowed_chat_ids
        )


def parse_task_command(
    message: IncomingMessage, bot_open_id: str
) -> TaskCommandKind | None:
    """Parse group ``@bot`` commands or direct-message plain commands."""

    if message.message_type != "text" or message.sender_type == "bot":
        return None
    text = message.text.lstrip()
    if message.chat_type == "group":
        bot_mention = next(
            (
                mention
                for mention in message.mentions
                if mention.open_id == bot_open_id
                and mention.mentioned_type == "bot"
                and text.startswith(mention.key)
            ),
            None,
        )
        if bot_mention is None:
            return None
        body = text[len(bot_mention.key) :]
    elif message.chat_type == "p2p":
        body = text
    else:
        return None
    return _parse_task_list_body(body)


def is_task_command_message(message: IncomingMessage) -> bool:
    """Recognize commands without applying them or performing a task query."""

    if message.chat_type in {"group", "p2p"}:
        bare_command = (
            message.message_type == "text"
            and message.sender_type != "bot"
            and _parse_task_list_body(message.text) is not None
        )
        if bare_command:
            return True
    if message.chat_type == "p2p":
        return (
            message.message_type == "text"
            and message.sender_type != "bot"
            and _parse_task_list_body(message.text) is not None
        )
    return any(
        mention.mentioned_type == "bot"
        and parse_task_command(message, mention.open_id) is not None
        for mention in message.mentions
    )


def format_group_task_list(page: TaskListPage, *, is_admin: bool) -> str:
    if page.total_count == 0:
        return (
            "📋 本群当前没有未完成任务。"
            if is_admin
            else "📋 你在本群当前没有未完成任务。"
        )
    heading = (
        f"📋 本群未完成任务（管理员视图，共 {page.total_count} 项）"
        if is_admin
        else f"📋 你在本群的未完成任务（{page.total_count} 项）"
    )
    lines = [heading]
    for index, task in enumerate(page.tasks, start=1):
        lines.extend(
            _task_lines(
                index,
                task,
                show_owner=is_admin,
                chat_name=None,
            )
        )
    _append_hidden_count(lines, page.total_count, len(page.tasks))
    return "\n".join(lines)


def format_private_task_list(
    page: CrossChatTaskListPage, *, is_admin: bool
) -> str:
    if page.total_count == 0:
        return (
            "📋 当前没有未完成任务。"
            if is_admin
            else "📋 你当前没有未完成任务。"
        )
    heading = (
        f"📋 全部未完成任务（管理员视图，共 {page.total_count} 项）"
        if is_admin
        else f"📋 你的未完成任务（共 {page.total_count} 项）"
    )
    lines = [heading]
    for index, entry in enumerate(page.entries, start=1):
        lines.extend(
            _task_lines(
                index,
                entry.task,
                show_owner=is_admin,
                chat_name=entry.chat_name,
            )
        )
    _append_hidden_count(lines, page.total_count, len(page.entries))
    return "\n".join(lines)


def _parse_task_list_body(body: str) -> TaskCommandKind | None:
    body = body.strip().rstrip("?？").strip()
    if body in {"任务列表", "本群任务", "查看任务"}:
        return TaskCommandKind.LIST
    return None


def _task_lines(
    index: int,
    task: TaskSnapshot,
    *,
    show_owner: bool,
    chat_name: str | None,
) -> tuple[str, ...]:
    deadline = (
        "未设置"
        if task.deadline is None
        else task.deadline.astimezone(SHANGHAI_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
    )
    status = {
        TaskStatus.PENDING: "待确认",
        TaskStatus.TODO: "待办",
        TaskStatus.OVERDUE: "已逾期",
    }[task.status]
    lines = ["", f"{index}. [{task.public_code}] {task.title}"]
    if chat_name is not None:
        lines.append(f"群聊：{chat_name}")
    responsible = task.responsible_members
    if len(responsible) > 1:
        lines.append(
            "共同负责人：" + "、".join(
                member.name for member in responsible
            )
        )
    elif show_owner:
        lines.append(f"负责人：{responsible[0].name}")
    lines.append(f"截止：{deadline}｜状态：{status}")
    return tuple(lines)


def _append_hidden_count(
    lines: list[str], total_count: int, shown_count: int
) -> None:
    hidden = total_count - shown_count
    if hidden > 0:
        lines.extend(("", f"另有 {hidden} 项未显示。"))


def _refresh_page_chat_names(
    page: CrossChatTaskListPage,
    refresher: Callable[[str], str | None] | None,
) -> CrossChatTaskListPage:
    """Use fresh Feishu names, hiding a name when its refresh fails."""

    if refresher is None or not page.entries:
        return page
    names: dict[str, str | None] = {}
    for entry in page.entries:
        chat_id = entry.task.chat_id
        if chat_id not in names:
            names[chat_id] = refresher(chat_id)
    return CrossChatTaskListPage(
        total_count=page.total_count,
        entries=tuple(
            CrossChatTaskEntry(
                task=entry.task,
                chat_name=names[entry.task.chat_id],
            )
            for entry in page.entries
        ),
    )
