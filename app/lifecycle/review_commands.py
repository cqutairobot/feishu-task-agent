"""Private natural-language review commands with an explicit write gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable

from app.feishu.messages import IncomingMessage
from app.lifecycle.mutations import (
    LifecycleModelAudit,
    LifecycleMutationError,
    LifecycleMutationResult,
    LifecycleMutationService,
)
from app.lifecycle.review_context import PrivateReviewDetectionContextBuilder
from app.lifecycle.review_confirmation import has_explicit_review_confirmation
from app.lifecycle.review_contracts import ReviewAction, ReviewActionCandidate
from app.management.access import ChatAdministratorRepository
from app.tasks.codes import TaskCodeError, find_task_code_mentions, parse_task_code
from app.tasks.repository import CrossChatTaskEntry, TaskRepository, TaskStatus


class PrivateReviewCommandKind(StrEnum):
    DETECT = "review_intent_read_only"


@dataclass(frozen=True, slots=True)
class PrivateReviewCommandResult:
    kind: PrivateReviewCommandKind
    succeeded: bool
    reply_text: str


class PrivateReviewCommandProcessor:
    """Recognize and optionally apply one administrator review decision.

    Phase 9E-1 leaves ``review_writes_enabled`` false and is therefore
    read-only. Phase 9E-2 requires the explicit confirmation prefix and then
    delegates all authorization, state, evidence, and idempotency checks to
    ``LifecycleMutationService``.
    """

    def __init__(
        self,
        tasks: TaskRepository,
        context_builder: PrivateReviewDetectionContextBuilder,
        detector: object,
        *,
        administrator_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        context_limit: int = 20,
        chat_administrators: ChatAdministratorRepository | None = None,
        mutations: LifecycleMutationService | None = None,
        review_writes_enabled: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not 1 <= context_limit <= 50:
            raise ValueError("context_limit must be between 1 and 50")
        if any(not item.strip() for item in administrator_open_ids):
            raise ValueError("administrator Open IDs must not be empty")
        if any(not item.strip() for item in allowed_chat_ids):
            raise ValueError("allowed chat IDs must not be empty")
        if not isinstance(review_writes_enabled, bool):
            raise ValueError("review_writes_enabled must be a bool")
        if review_writes_enabled and mutations is None:
            raise ValueError(
                "review_writes_enabled requires a mutation service"
            )
        self._tasks = tasks
        self._context_builder = context_builder
        self._detector = detector
        self._administrator_open_ids = administrator_open_ids
        self._allowed_chat_ids = allowed_chat_ids
        self._context_limit = context_limit
        self._chat_administrators = chat_administrators
        self._mutations = mutations
        self._review_writes_enabled = review_writes_enabled
        self._clock = clock

    @property
    def review_writes_enabled(self) -> bool:
        return self._review_writes_enabled

    def matches(self, message: IncomingMessage) -> bool:
        if not _eligible_private_text(message):
            return False
        try:
            return bool(find_task_code_mentions(message.text))
        except TaskCodeError:
            return True

    def handle(
        self, message: IncomingMessage
    ) -> PrivateReviewCommandResult | None:
        if not _eligible_private_text(message):
            return None
        try:
            codes = find_task_code_mentions(message.text)
        except TaskCodeError:
            # Let the established lifecycle processor provide the standard
            # invalid-code reply; this processor owns only reviewable tasks.
            return None
        if len(codes) != 1:
            return None
        task_code = codes[0]
        task_id = parse_task_code(task_code)
        task = self._tasks.find_review_target_across_chats(
            task_id,
            chat_ids=self._admitted_chat_ids(),
        )
        if task is None:
            return None
        if not self._is_group_administrator(
            task.task.chat_id, message.sender_open_id
        ):
            return PrivateReviewCommandResult(
                kind=PrivateReviewCommandKind.DETECT,
                succeeded=False,
                reply_text=(
                    f"❌ 找不到可复核的任务 {task_code}，或你不是该任务来源群的管理员。"
                ),
            )

        try:
            context = self._context_builder.build(
                message.chat_id,
                message.message_id,
                actor_open_id=message.sender_open_id,
                task=task,
                message_limit=self._context_limit,
            )
            call = self._detector.detect_review(context)
        except (RuntimeError, ValueError):
            return PrivateReviewCommandResult(
                kind=PrivateReviewCommandKind.DETECT,
                succeeded=False,
                reply_text=(
                    "⚠️ 暂时无法理解这条复核指令。当前为只读测试，任务没有修改。"
                ),
            )

        intents = call.result.intents
        if not intents:
            return PrivateReviewCommandResult(
                kind=PrivateReviewCommandKind.DETECT,
                succeeded=False,
                reply_text=(
                    f"ℹ️ 没有识别到对 {task_code} 的完整复核决定。\n"
                    "验收通过必须明确表达决定；重新开启必须同时给出具体原因。\n"
                    "当前为只读测试，任务没有修改。"
                ),
            )
        if len(intents) != 1 or intents[0].task_id != task_id:
            return PrivateReviewCommandResult(
                kind=PrivateReviewCommandKind.DETECT,
                succeeded=False,
                reply_text="⚠️ 模型结果与任务编号不一致，任务没有修改。",
            )

        intent = intents[0]
        if self._review_writes_enabled:
            return self._handle_confirmed_or_pending_write(
                message,
                task_code=task_code,
                task=task,
                intent=intent,
                call=call,
            )

        action_label = (
            "验收通过" if intent.action.value == "accept" else "重新开启"
        )
        lines = [
            "🧪 已识别复核意图（Phase 9E-1 只读测试）",
            "",
            f"任务：[{task_code}] {task.task.title}",
            f"识别动作：{action_label}",
        ]
        if task.chat_name:
            lines.append(f"来源群：{task.chat_name}")
        if intent.reason is not None:
            lines.append(f"返工原因：{intent.reason}")
        lines.extend(
            (
                "",
                "🔒 本阶段没有执行任何任务修改。",
                "后续 9E-2 将增加权限复核和明确确认后，才允许真正写入。",
            )
        )
        return PrivateReviewCommandResult(
            kind=PrivateReviewCommandKind.DETECT,
            succeeded=True,
            reply_text="\n".join(lines),
        )

    def _handle_confirmed_or_pending_write(
        self,
        message: IncomingMessage,
        *,
        task_code: str,
        task: CrossChatTaskEntry,
        intent: ReviewActionCandidate,
        call: object,
    ) -> PrivateReviewCommandResult:
        if not has_explicit_review_confirmation(message.text):
            action_label = (
                "验收通过" if intent.action is ReviewAction.ACCEPT else "重新开启"
            )
            if intent.action is ReviewAction.REOPEN:
                confirmation = (
                    f"确认执行 {task_code} 重新开启，原因是 {intent.reason}"
                )
            else:
                confirmation = f"确认执行 {task_code} 验收通过"
            return PrivateReviewCommandResult(
                kind=PrivateReviewCommandKind.DETECT,
                succeeded=False,
                reply_text=(
                    f"⚠️ 已识别复核意图：{action_label}\n"
                    f"任务：[{task_code}] {task.task.title}\n"
                    "这是高风险操作，需要明确确认后才会写入。\n"
                    f"如需执行，请重新发送：{confirmation}"
                ),
            )

        assert self._mutations is not None
        try:
            mutation = self._mutations.apply_private_review_action(
                intent,
                actor_open_id=message.sender_open_id,
                trigger_message_id=message.message_id,
                task_code=task_code,
                applied_at=self._clock(),
                model_audit=_model_audit(call),
            )
        except LifecycleMutationError:
            return PrivateReviewCommandResult(
                kind=PrivateReviewCommandKind.DETECT,
                succeeded=False,
                reply_text=(
                    "❌ 复核操作未执行：任务状态、权限或证据已发生变化。"
                    "请重新发送“任务列表”后再试。"
                ),
            )
        return PrivateReviewCommandResult(
            kind=PrivateReviewCommandKind.DETECT,
            succeeded=True,
            reply_text=_write_success_reply(task, mutation, intent),
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


def _eligible_private_text(message: IncomingMessage) -> bool:
    return (
        message.chat_type == "p2p"
        and message.message_type == "text"
        and message.sender_type != "bot"
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


def _write_success_reply(
    task: CrossChatTaskEntry,
    mutation: LifecycleMutationResult,
    intent: ReviewActionCandidate,
) -> str:
    if mutation.action.value == ReviewAction.ACCEPT.value:
        lines = ["✅ 任务已验收", ""]
    else:
        lines = ["🔁 任务已重新开启", ""]
    lines.append(f"[{mutation.task_code}] {task.task.title}")
    if task.chat_name:
        lines.append(f"来源群：{task.chat_name}")
    if intent.reason is not None:
        lines.append(f"返工原因：{intent.reason}")
    lines.append("复核操作者、来源消息、模型结果和证据已写入溯源记录。")
    status_label = {
        TaskStatus.DONE: "已完成",
        TaskStatus.TODO: "待办",
        TaskStatus.OVERDUE: "已逾期",
        TaskStatus.CANCELLED: "已取消",
    }.get(mutation.new_status, mutation.new_status.value)
    lines.append(f"当前状态：{status_label}")
    return "\n".join(lines)
