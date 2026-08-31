"""Private natural-language lifecycle commands anchored by a task code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable

from app.agent.context import SHANGHAI_TZ
from app.feishu.messages import IncomingMessage
from app.lifecycle.context import PrivateLifecycleDetectionContextBuilder
from app.lifecycle.contracts import LifecycleAction
from app.lifecycle.mutations import (
    LifecycleModelAudit,
    LifecycleMutationError,
    LifecycleMutationResult,
    LifecycleMutationService,
)
from app.management.access import ChatAdministratorRepository
from app.tasks.codes import (
    TaskCodeError,
    find_task_code_mentions,
    parse_task_code,
)
from app.tasks.repository import CrossChatTaskEntry, TaskRepository


class PrivateLifecycleCommandKind(StrEnum):
    UPDATE = "lifecycle_update"


@dataclass(frozen=True, slots=True)
class PrivateLifecycleCommandResult:
    kind: PrivateLifecycleCommandKind
    succeeded: bool
    reply_text: str
    mutation: LifecycleMutationResult | None = None


class PrivateLifecycleCommandProcessor:
    """Recognize and apply one code-anchored update from a P2P message."""

    def __init__(
        self,
        tasks: TaskRepository,
        context_builder: PrivateLifecycleDetectionContextBuilder,
        detector: object,
        mutations: LifecycleMutationService,
        *,
        administrator_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        context_limit: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        chat_administrators: ChatAdministratorRepository | None = None,
    ) -> None:
        if not 1 <= context_limit <= 50:
            raise ValueError("context_limit must be between 1 and 50")
        if any(not item.strip() for item in administrator_open_ids):
            raise ValueError("administrator Open IDs must not be empty")
        if any(not item.strip() for item in allowed_chat_ids):
            raise ValueError("allowed chat IDs must not be empty")
        self._tasks = tasks
        self._context_builder = context_builder
        self._detector = detector
        self._mutations = mutations
        self._administrator_open_ids = administrator_open_ids
        self._allowed_chat_ids = allowed_chat_ids
        self._context_limit = context_limit
        self._clock = clock
        self._chat_administrators = chat_administrators

    def matches(self, message: IncomingMessage) -> bool:
        if not _eligible_private_text(message):
            return False
        try:
            return bool(find_task_code_mentions(message.text))
        except TaskCodeError:
            return True

    def handle(
        self, message: IncomingMessage
    ) -> PrivateLifecycleCommandResult | None:
        if not _eligible_private_text(message):
            return None
        try:
            codes = find_task_code_mentions(message.text)
        except TaskCodeError:
            return _rejected(
                "❌ 任务编号无效，可能输错了校验位。请先发送“任务列表”复制编号。"
            )
        if not codes:
            return None
        if len(codes) != 1:
            return _rejected(
                "❌ 一条消息暂时只能修改一个任务，请分别发送每个任务的操作。"
            )
        task_code = codes[0]
        task_id = parse_task_code(task_code)
        task = self._tasks.find_lifecycle_target_across_chats(
            task_id,
            owner_open_id=None,
            chat_ids=self._admitted_chat_ids(),
        )
        is_admin = (
            task is not None
            and self._is_group_administrator(
                task.task.chat_id, message.sender_open_id
            )
        )
        is_responsible = task is not None and any(
            member.open_id == message.sender_open_id
            for member in task.task.responsible_members
        )
        if task is None or not (is_responsible or is_admin):
            return _rejected(
                f"❌ 找不到可操作的任务 {task_code}。它可能不属于你、"
                "已经结束，或不在机器人管理的群中。"
            )

        try:
            context = self._context_builder.build(
                message.chat_id,
                message.message_id,
                actor_open_id=message.sender_open_id,
                task=task,
                message_limit=self._context_limit,
            )
            call = self._detector.detect_lifecycle(context)
        except (RuntimeError, ValueError):
            return _rejected(
                "⚠️ 暂时无法理解这条操作，任务没有修改。请稍后重试。"
            )

        updates = call.result.updates
        if not updates:
            return _rejected(
                f"ℹ️ 没有识别到对 {task_code} 的明确操作，任务未修改。\n"
                f"例如：{task_code} 已完成；{task_code} 延期到 8 月 30 日；"
                f"取消 {task_code}。"
            )
        if len(updates) != 1 or updates[0].task_id != task_id:
            return _rejected(
                "⚠️ 模型结果与任务编号不一致，任务没有修改。"
            )
        if updates[0].action in {
            LifecycleAction.RENAME,
            LifecycleAction.REASSIGN,
            LifecycleAction.INVALIDATE,
        } and not is_admin:
            return _rejected(
                "❌ 标题、负责人和误识别纠错仅允许本群任务管理员操作。"
            )

        try:
            mutation = self._mutations.apply_candidate(
                updates[0],
                actor_open_id=message.sender_open_id,
                trigger_message_id=message.message_id,
                task_code=task_code,
                applied_at=self._clock(),
                model_audit=_model_audit(call),
            )
        except LifecycleMutationError:
            return _rejected(
                "❌ 操作未执行：任务状态、权限或截止时间已发生变化。"
                "请重新发送“任务列表”后再试。"
            )
        return PrivateLifecycleCommandResult(
            kind=PrivateLifecycleCommandKind.UPDATE,
            succeeded=True,
            reply_text=_success_reply(task, mutation),
            mutation=mutation,
        )

    def _is_group_administrator(self, chat_id: str, open_id: str) -> bool:
        if open_id in self._administrator_open_ids:
            return True
        return (
            self._chat_administrators is not None
            and self._chat_administrators.is_administrator(chat_id, open_id)
        )

    def _admitted_chat_ids(self) -> frozenset[str] | None:
        if self._chat_administrators is None:
            return self._allowed_chat_ids or None
        return self._chat_administrators.admitted_chat_ids(
            self._allowed_chat_ids
        )


def is_private_lifecycle_command_message(message: IncomingMessage) -> bool:
    if not _eligible_private_text(message):
        return False
    try:
        return bool(find_task_code_mentions(message.text))
    except TaskCodeError:
        return True


def _eligible_private_text(message: IncomingMessage) -> bool:
    return (
        message.chat_type == "p2p"
        and message.message_type == "text"
        and message.sender_type != "bot"
    )


def _rejected(reply_text: str) -> PrivateLifecycleCommandResult:
    return PrivateLifecycleCommandResult(
        kind=PrivateLifecycleCommandKind.UPDATE,
        succeeded=False,
        reply_text=reply_text,
    )


def _model_audit(call: object) -> LifecycleModelAudit:
    usage = getattr(call, "usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return LifecycleModelAudit(
        provider="openai_compatible",
        model=str(getattr(call, "model", "unknown")),
        response_format=str(getattr(call, "response_format", "unknown")),
        request_id=getattr(call, "request_id", None),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    )


def _success_reply(
    task: CrossChatTaskEntry,
    mutation: LifecycleMutationResult,
) -> str:
    heading = {
        "complete": "✅ 任务已标记完成",
        "cancel": "✅ 任务已取消",
        "reschedule": "✅ 截止时间已更新",
        "rename": "✅ 任务标题已纠正",
        "reassign": "✅ 任务负责人已纠正",
        "invalidate": "✅ 误识别任务已撤销",
    }[mutation.action.value]
    current_title = mutation.title_after or task.task.title
    lines = [heading, "", f"[{mutation.task_code}] {current_title}"]
    if task.chat_name:
        lines.append(f"来源群：{task.chat_name}")
    if mutation.action.value == "reschedule":
        assert mutation.deadline_after is not None
        lines.append(
            "新截止时间："
            + mutation.deadline_after.astimezone(SHANGHAI_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
        )
    if mutation.action.value == "reassign":
        lines.append(
            "当前负责人："
            + "、".join(owner.name for owner in mutation.assignees_after)
        )
    if mutation.action.value == "complete":
        lines.append("完成说明与来源证据已保存，当前等待本群管理员复核。")
    lines.append(
        "当前状态："
        + {
            "done": "已完成",
            "cancelled": "已取消",
            "todo": "待办",
            "overdue": "已逾期",
        }[mutation.new_status.value]
    )
    return "\n".join(lines)
