"""Private natural-language commands that append one task note."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable
import re

from app.feishu.messages import IncomingMessage
from app.lifecycle.context import PrivateLifecycleDetectionContextBuilder
from app.management.access import ChatAdministratorRepository
from app.tasks.codes import TaskCodeError, find_task_code_mentions, parse_task_code
from app.tasks.notes import (
    TaskNoteAccessDenied,
    TaskNoteConflict,
    TaskNoteModelAudit,
    TaskNoteResult,
    TaskNoteService,
    build_task_note_idempotency_key,
)
from app.tasks.repository import CrossChatTaskEntry, TaskRepository


class PrivateTaskNoteCommandKind(StrEnum):
    NOTE = "task_note"


@dataclass(frozen=True, slots=True)
class PrivateTaskNoteCommandResult:
    kind: PrivateTaskNoteCommandKind
    succeeded: bool
    reply_text: str
    note: TaskNoteResult | None = None


_NOTE_MARKERS = (
    "进度",
    "进展",
    "阻塞",
    "卡住",
    "遇到问题",
    "问题是",
    "说明",
    "备注",
    "日志",
    "结果",
    "原因",
    "补充",
    "记录",
)
_LIFECYCLE_ONLY = re.compile(
    r"^(?:t-?)?[0-9a-z]+\s*(?:已完成|完成了?|延期(?:到|至)?[^，。；;]*|"
    r"取消|作废|撤销)(?:[。.!！])?$",
    re.IGNORECASE,
)
_COMPLETION_WITH_DETAILS = re.compile(
    r"^(?:(?:t-?)?[0-9a-z]+\s*)?(?:任务\s*)?"
    r"(?:已经|已)?(?:完成|做完)(?:了)?"
    r"(?=$|[\s，,。.!！:：;；])|"
    r"^(?:已经|已)?(?:完成|做完)(?:了)?\s*"
    r"(?:t-?)?[0-9a-z]+(?=$|[\s，,。.!！:：;；])",
    re.IGNORECASE,
)


class PrivateTaskNoteCommandProcessor:
    """Recognize and append one code-anchored natural-language note."""

    def __init__(
        self,
        tasks: TaskRepository,
        context_builder: PrivateLifecycleDetectionContextBuilder,
        detector: object,
        notes: TaskNoteService,
        *,
        administrator_open_ids: frozenset[str] = frozenset(),
        allowed_chat_ids: frozenset[str] = frozenset(),
        context_limit: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        chat_administrators: ChatAdministratorRepository | None = None,
    ) -> None:
        if not 1 <= context_limit <= 50:
            raise ValueError("context_limit must be between 1 and 50")
        self._tasks = tasks
        self._context_builder = context_builder
        self._detector = detector
        self._notes = notes
        self._administrator_open_ids = administrator_open_ids
        self._allowed_chat_ids = allowed_chat_ids
        self._context_limit = context_limit
        self._clock = clock
        self._chat_administrators = chat_administrators

    def matches(self, message: IncomingMessage) -> bool:
        if not _eligible_private_text(message):
            return False
        try:
            codes = find_task_code_mentions(message.text)
        except TaskCodeError:
            return True
        return bool(codes) and _looks_like_note(message.text)

    def handle(
        self, message: IncomingMessage
    ) -> PrivateTaskNoteCommandResult | None:
        if not _eligible_private_text(message):
            return None
        try:
            codes = find_task_code_mentions(message.text)
        except TaskCodeError:
            return _rejected(
                "❌ 任务编号无效，可能输错了校验位。请先发送“任务列表”复制编号。"
            )
        if not codes or not _looks_like_note(message.text):
            return None
        if len(codes) != 1:
            return _rejected(
                "❌ 一条消息暂时只能记录一个任务说明，请分别发送每个任务。"
            )
        task_code = codes[0]
        task_id = parse_task_code(task_code)
        task = self._tasks.find_lifecycle_target_across_chats(
            task_id,
            owner_open_id=None,
            chat_ids=self._admitted_chat_ids(),
        )
        is_admin = task is not None and self._is_group_administrator(
            task.task.chat_id, message.sender_open_id
        )
        is_responsible = task is not None and any(
            member.open_id == message.sender_open_id
            for member in task.task.responsible_members
        )
        if task is None or not (is_admin or is_responsible):
            return _rejected(
                f"❌ 找不到可记录说明的任务 {task_code}。它可能不属于你、"
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
            call = self._detector.detect_note(context)
        except (RuntimeError, ValueError):
            return _rejected(
                "⚠️ 暂时无法理解这条说明，任务没有写入。请稍后重试。"
            )
        candidates = call.result.notes
        if not candidates:
            return _rejected(
                f"ℹ️ 没有识别到对 {task_code} 的明确说明，任务没有写入。\n"
                f"例如：{task_code} 进度：已经完成两组实验，第三组仍在运行。"
            )
        if len(candidates) != 1 or candidates[0].task_id != task_id:
            return _rejected("⚠️ 模型结果与任务编号不一致，说明没有写入。")
        candidate = candidates[0]
        try:
            note = self._notes.append(
                actor_open_id=message.sender_open_id,
                chat_id=task.task.chat_id,
                task_id=task_id,
                note_type=candidate.note_type,
                content=candidate.content,
                source_message_id=message.message_id,
                source_chat_id=message.chat_id,
                idempotency_key=build_task_note_idempotency_key(
                    "private", message.message_id
                ),
                created_at=self._clock(),
                model_audit=_model_audit(call, candidate.confidence),
            )
        except TaskNoteAccessDenied as exc:
            if "requires an administrator" in str(exc):
                return _rejected("❌ 返工或纠错说明仅允许本群任务管理员记录。")
            return _rejected("❌ 你没有权限为这个任务记录说明。")
        except TaskNoteConflict:
            return _rejected(
                "❌ 说明没有写入：任务或原始私聊消息已发生变化，请重新发送。"
            )
        return PrivateTaskNoteCommandResult(
            kind=PrivateTaskNoteCommandKind.NOTE,
            succeeded=True,
            reply_text=_success_reply(task, note),
            note=note,
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


def is_private_task_note_message(message: IncomingMessage) -> bool:
    if not _eligible_private_text(message):
        return False
    try:
        codes = find_task_code_mentions(message.text)
    except TaskCodeError:
        return True
    return bool(codes) and _looks_like_note(message.text)


def _looks_like_note(text: str) -> bool:
    normalized = " ".join(text.strip().split())
    if (
        not normalized
        or _LIFECYCLE_ONLY.fullmatch(normalized)
        or _COMPLETION_WITH_DETAILS.search(normalized)
    ):
        return False
    return any(marker in normalized for marker in _NOTE_MARKERS)


def _eligible_private_text(message: IncomingMessage) -> bool:
    return (
        message.chat_type == "p2p"
        and message.message_type == "text"
        and message.sender_type != "bot"
    )


def _rejected(reply_text: str) -> PrivateTaskNoteCommandResult:
    return PrivateTaskNoteCommandResult(
        kind=PrivateTaskNoteCommandKind.NOTE,
        succeeded=False,
        reply_text=reply_text,
    )


def _model_audit(call: object, confidence: float) -> TaskNoteModelAudit:
    usage = getattr(call, "usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return TaskNoteModelAudit(
        provider="openai_compatible",
        model=str(getattr(call, "model", "unknown")),
        response_format=str(getattr(call, "response_format", "unknown")),
        request_id=getattr(call, "request_id", None),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        confidence=confidence,
    )


def _success_reply(task: CrossChatTaskEntry, note: TaskNoteResult) -> str:
    labels = {
        "progress": "进度说明",
        "blocker": "阻塞说明",
        "completion": "完成说明",
        "delay": "延期说明",
        "reopen": "返工说明",
        "general": "任务说明",
        "correction": "纠错说明",
    }
    lines = [
        "✅ 已记录任务说明",
        "",
        f"[{note.task_code}] {task.task.title}",
        f"类型：{labels.get(note.note_type.value, note.note_type.value)}",
        f"内容：{note.content}",
        f"完成周期：第 {note.completion_cycle} 轮",
        "任务状态未改变；如需完成、延期或取消，请单独发送对应操作。",
    ]
    if task.chat_name:
        lines.insert(3, f"来源群：{task.chat_name}")
    return "\n".join(lines)
