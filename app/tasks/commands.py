"""Deterministic task queries with sender- and chat-scoped authorization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import Any, Callable

from app.agent.context import SHANGHAI_TZ
from app.feishu.messages import IncomingMessage
from app.identity.aliases import AliasRepository
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
from app.tasks.query_contracts import (
    TaskQueryIntent,
    TaskQueryScope,
)


LOGGER = logging.getLogger(__name__)

_NATURAL_QUERY_TASK_HINTS = (
    "任务",
    "待办",
    "未完成",
    "没完成",
    "没有完成",
    "未做完",
    "没做完",
    "没做",
    "没有做",
    "未做",
    "未处理",
    "没处理",
    "待处理",
    "事项",
    "事情",
    "事",
    "工作",
    "安排",
    "项目",
    "清单",
)
_NATURAL_QUERY_ACTION_HINTS = (
    "还有",
    "现在",
    "目前",
    "最近",
    "接下来",
    "哪些",
    "什么",
    "查看",
    "查",
    "看看",
    "查询",
    "列出",
    "列举",
    "帮我",
    "我的",
    "我有",
    "有没有",
    "剩余",
)
_EXPLICIT_SELF_QUERY_HINTS = (
    "我的任务",
    "我的待办",
    "我的事项",
    "我有什么任务",
    "我有哪些任务",
    "我还有什么",
    "我还有哪些",
    "我负责的",
    "由我负责",
    "分配给我的",
    "交给我的",
    "我名下",
    "我未完成",
    "我没完成",
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
    """Show personal tasks by default and enforce admin query boundaries."""

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
        alias_repository: AliasRepository | None = None,
        query_detector: object | None = None,
        query_confidence_threshold: float = 0.80,
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
        if not 0 <= query_confidence_threshold <= 1:
            raise ValueError(
                "query_confidence_threshold must be between 0 and 1"
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
        self._alias_repository = alias_repository
        self._query_detector = query_detector
        self._query_confidence_threshold = query_confidence_threshold
        self._natural_query_cache: dict[str, TaskQueryIntent | None] = {}

    def matches(self, message: IncomingMessage) -> bool:
        if parse_task_command(message, self._bot_open_id) is not None:
            return True
        return self._supports_natural_query(message)

    def handle(self, message: IncomingMessage) -> TaskCommandResult | None:
        kind = parse_task_command(message, self._bot_open_id)
        natural_query = False
        if kind is None:
            intent = self._natural_query_intent(message)
            natural_query = self._supports_natural_query(
                message, intent=intent
            )
            if not natural_query or intent is None:
                return None
            kind = TaskCommandKind.LIST
            if intent.scope is TaskQueryScope.PERSON:
                return self._handle_person_query(message, intent)
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
            sender_is_admin = (
                admin_chat_ids is None or bool(admin_chat_ids)
            )
            # An unqualified query such as "还有什么任务没完成" means the
            # administrator's whole managed scope. Administrators can still
            # request a personal view by explicitly saying "我的任务" or an
            # equivalent phrase. Ordinary members always remain self-scoped.
            is_admin = sender_is_admin and (
                not natural_query
                or not _explicitly_requests_own_tasks(message.text)
            )
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

    def _supports_natural_query(
        self,
        message: IncomingMessage,
        *,
        intent: TaskQueryIntent | None = None,
    ) -> bool:
        """Classify a likely private query without suppressing other messages."""

        if not _looks_like_natural_query(message) or self._query_detector is None:
            return False
        intent = intent or self._natural_query_intent(message)
        return (
            intent is not None
            and intent.is_query
            and intent.confidence >= self._query_confidence_threshold
            and (
                intent.scope is TaskQueryScope.SELF
                or (
                    intent.scope is TaskQueryScope.PERSON
                    and self._can_query_other_members(message.sender_open_id)
                )
                or (
                    intent.scope is TaskQueryScope.ALL
                    and self._has_administrator_scope(message.sender_open_id)
                )
            )
        )

    def _natural_query_intent(
        self, message: IncomingMessage
    ) -> TaskQueryIntent | None:
        if not _looks_like_natural_query(message) or self._query_detector is None:
            return None
        return self._classify_natural_query(message)

    def _can_query_other_members(self, open_id: str) -> bool:
        if self._alias_repository is None:
            return False
        return self._has_administrator_scope(open_id)

    def _has_administrator_scope(self, open_id: str) -> bool:
        chat_ids = self._administrator_chat_ids(open_id)
        return chat_ids is None or bool(chat_ids)

    def _handle_person_query(
        self, message: IncomingMessage, intent: TaskQueryIntent
    ) -> TaskCommandResult:
        target_name = intent.target_name or ""
        chat_ids = self._administrator_chat_ids(message.sender_open_id)
        if not self._can_query_other_members(message.sender_open_id):
            return _rejected_task_query(
                f"❌ 只有任务管理员可以查询其他成员的任务。"
            )
        assert self._alias_repository is not None
        target_ids = self._alias_repository.resolve_open_ids_across_chats(
            target_name,
            chat_ids=chat_ids,
        )
        if not target_ids:
            return _rejected_task_query(
                f"❌ 未找到当前管理范围内名为“{target_name}”的成员。"
                "请先在管理后台为该成员设置任务姓名。"
            )
        if len(target_ids) > 1:
            return _rejected_task_query(
                f"❌ “{target_name}”对应多个不同成员，无法安全确定对象。"
                "请在管理后台为成员设置唯一任务姓名。"
            )
        target_open_id = next(iter(target_ids))
        page = self._tasks.list_open_tasks_across_chats(
            owner_open_id=target_open_id,
            chat_ids=chat_ids,
            limit=self._reply_limit,
        )
        page = _refresh_page_chat_names(page, self._chat_name_refresher)
        reply_text = format_private_task_list(
            page,
            is_admin=True,
            subject_name=target_name,
        )
        reply_card = (
            build_private_task_list_card(
                page,
                is_admin=True,
                actions_enabled=self._card_actions_enabled,
                subject_name=target_name,
            )
            if self._private_cards_enabled
            else None
        )
        return TaskCommandResult(
            kind=TaskCommandKind.LIST,
            succeeded=True,
            reply_text=reply_text,
            reply_card=reply_card,
        )

    def _classify_natural_query(
        self, message: IncomingMessage
    ) -> TaskQueryIntent | None:
        cached = self._natural_query_cache.get(message.message_id)
        if message.message_id in self._natural_query_cache:
            return cached
        try:
            call = self._query_detector.detect_task_query(  # type: ignore[union-attr]
                message.text,
                chat_type=message.chat_type,
                sender_name=message.sender_name,
            )
            intent = call.result
            if not isinstance(intent, TaskQueryIntent):
                raise TypeError("query detector returned an invalid result")
        except Exception as exc:
            # A classifier outage must never turn an ordinary message into a
            # command or prevent normal task detection from being queued.
            LOGGER.debug("Natural-language task query classification failed: %s", exc)
            intent = None
        self._natural_query_cache[message.message_id] = intent
        if len(self._natural_query_cache) > 256:
            self._natural_query_cache.pop(next(iter(self._natural_query_cache)))
        return intent

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


def _looks_like_natural_query(message: IncomingMessage) -> bool:
    """Cheap lexical gate so the LLM is not called for every private message."""

    if (
        message.chat_type != "p2p"
        or message.message_type != "text"
        or message.sender_type == "bot"
    ):
        return False
    text = message.text.strip().casefold()
    return bool(
        text
        and any(term in text for term in _NATURAL_QUERY_TASK_HINTS)
        and any(term in text for term in _NATURAL_QUERY_ACTION_HINTS)
    )


def _explicitly_requests_own_tasks(text: str) -> bool:
    """Return whether a query explicitly makes the sender its subject."""

    compact = "".join(text.casefold().split())
    return any(term in compact for term in _EXPLICIT_SELF_QUERY_HINTS)


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
    page: CrossChatTaskListPage,
    *,
    is_admin: bool,
    subject_name: str | None = None,
) -> str:
    if page.total_count == 0:
        return (
            f"📋 {subject_name}当前没有未完成任务。"
            if subject_name
            else
            "📋 当前没有未完成任务。"
            if is_admin
            else "📋 你当前没有未完成任务。"
        )
    heading = (
        f"📋 {subject_name}的未完成任务（共 {page.total_count} 项）"
        if subject_name
        else
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


def _rejected_task_query(reply_text: str) -> TaskCommandResult:
    return TaskCommandResult(
        kind=TaskCommandKind.LIST,
        succeeded=False,
        reply_text=reply_text,
    )


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
