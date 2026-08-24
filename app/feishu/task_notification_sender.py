"""Strictly-private Feishu delivery for durable task notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from app.agent.context import SHANGHAI_TZ
from app.config import FeishuSettings, ReminderSettings
from app.feishu.replies import build_api_client
from app.notifications.repository import (
    TaskNotificationKind,
    TaskNotificationLease,
)


class TaskNotificationDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TaskNotificationDeliveryReceipt:
    message_id: str
    receive_id_type: str
    receive_id: str


class FeishuTaskNotificationSender:
    """Send task notifications privately without a group fallback."""

    def __init__(
        self,
        settings: FeishuSettings,
        *,
        reminder_settings: ReminderSettings = ReminderSettings(),
        client: lark.Client | None = None,
    ) -> None:
        self._client = client or build_api_client(settings)
        self._reminder_settings = reminder_settings

    def deliver(
        self, lease: TaskNotificationLease
    ) -> TaskNotificationDeliveryReceipt:
        receive_id_type = (
            "chat_id"
            if lease.recipient_private_chat_id is not None
            else "open_id"
        )
        receive_id = (
            lease.recipient_private_chat_id or lease.recipient_open_id
        )
        content = json.dumps(
            {
                "text": format_task_notification_text(
                    lease, settings=self._reminder_settings
                )
            },
            ensure_ascii=False,
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(content)
                .uuid(_delivery_uuid(lease))
                .build()
            )
            .build()
        )
        try:
            response = self._client.im.v1.message.create(request)
        except Exception as exc:
            message = " ".join(str(exc).split()) or type(exc).__name__
            raise TaskNotificationDeliveryError(
                "transport_error",
                f"{type(exc).__name__}: {message}"[:1_000],
            ) from exc
        if not response.success():
            code = str(getattr(response, "code", "unknown"))
            message = str(getattr(response, "msg", "unknown error"))
            raise TaskNotificationDeliveryError(code, message)
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id.strip():
            raise TaskNotificationDeliveryError(
                "missing_message_id",
                "Feishu accepted the notification without a message ID",
            )
        return TaskNotificationDeliveryReceipt(
            message_id=message_id.strip(),
            receive_id_type=receive_id_type,
            receive_id=receive_id,
        )


def format_task_notification_text(
    lease: TaskNotificationLease,
    *,
    settings: ReminderSettings = ReminderSettings(),
) -> str:
    # Retain the settings argument for compatibility with existing callers;
    # missing-deadline wording now follows the lease's persisted schedule.
    del settings
    task_line = f"[{lease.task_code}] {lease.title}"
    if lease.kind is TaskNotificationKind.TASK_CREATED_ASSIGNEE:
        return "\n".join(
            (
                "📌 你有一个新任务",
                "",
                task_line,
                f"负责人：{lease.owner_name}",
                f"截止时间：{_deadline_text(lease.deadline)}",
                "",
                "私聊机器人发送“任务列表”可查看全部任务。",
                f"也可以直接回复：{lease.task_code} 已完成。",
            )
        )
    if lease.kind is TaskNotificationKind.MISSING_DEADLINE_OWNER:
        elapsed = _missing_deadline_elapsed_text(lease)
        return "\n".join(
            (
                "⏳ 请设置任务截止时间",
                "",
                task_line,
                f"该任务已创建超过 {elapsed}，仍未设置截止时间。",
                f"请直接回复：{lease.task_code} 截止时间设为……",
            )
        )
    if lease.kind is TaskNotificationKind.MISSING_DEADLINE_ADMIN:
        elapsed = _missing_deadline_elapsed_text(lease)
        return "\n".join(
            (
                "⚠️ 任务仍未设置截止时间",
                "",
                task_line,
                f"负责人：{lease.owner_name}",
                f"该任务已创建超过 {elapsed}。请管理员设置截止时间。",
                "若该任务确实无需截止日期，可忽略；机器人不会继续追问。",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_DONE_ADMIN:
        return "\n".join(
            (
                "✅ 任务完成通知",
                "",
                f"{lease.owner_name} 已完成任务：",
                task_line,
            )
        )
    if lease.kind is TaskNotificationKind.TASK_CANCELLED_ADMIN:
        return "\n".join(
            (
                "❌ 任务取消通知",
                "",
                f"{lease.owner_name} 已取消任务：",
                task_line,
            )
        )
    if lease.kind is TaskNotificationKind.TASK_RESCHEDULED_ADMIN:
        return "\n".join(
            (
                "⏳ 任务延期通知",
                "",
                f"负责人：{lease.owner_name}",
                f"任务：{task_line}",
                f"原截止时间：{_deadline_text(lease.deadline_before)}",
                f"新截止时间：{_deadline_text(lease.deadline)}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_DONE_COASSIGNEE:
        return "\n".join(
            (
                "✅ 共同任务状态更新",
                "",
                f"{lease.owner_name} 已将共同任务标记为完成：",
                task_line,
            )
        )
    if lease.kind is TaskNotificationKind.TASK_CANCELLED_COASSIGNEE:
        return "\n".join(
            (
                "❌ 共同任务状态更新",
                "",
                f"{lease.owner_name} 已取消共同任务：",
                task_line,
            )
        )
    if lease.kind is TaskNotificationKind.TASK_RESCHEDULED_COASSIGNEE:
        return "\n".join(
            (
                "⏳ 共同任务截止时间已更新",
                "",
                f"操作人：{lease.owner_name}",
                task_line,
                f"原截止时间：{_deadline_text(lease.deadline_before)}",
                f"新截止时间：{_deadline_text(lease.deadline)}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_RENAMED_ASSIGNEE:
        return "\n".join(
            (
                "✏️ 任务标题已纠正",
                "",
                f"操作人：{lease.owner_name}",
                f"当前任务：{task_line}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_ASSIGNEE_ADDED:
        return "\n".join(
            (
                "👤 任务负责人已调整",
                "",
                f"你已被设为该任务的负责人：{task_line}",
                f"操作人：{lease.owner_name}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_ASSIGNEE_REMOVED:
        return "\n".join(
            (
                "👤 任务负责人已调整",
                "",
                f"你已不再负责该任务：{task_line}",
                f"操作人：{lease.owner_name}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_ASSIGNEES_CHANGED:
        return "\n".join(
            (
                "👥 共同负责人已调整",
                "",
                f"你仍是该任务的负责人：{task_line}",
                f"操作人：{lease.owner_name}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_INVALIDATED_ASSIGNEE:
        return "\n".join(
            (
                "↩️ 误识别任务已撤销",
                "",
                task_line,
                f"操作人：{lease.owner_name}",
                "该记录保留审计，但不会继续提醒。",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_RENAMED_ADMIN:
        return "\n".join(
            (
                "✏️ 管理员任务纠错通知",
                "",
                f"任务标题已更新：{task_line}",
                f"当前负责人：{lease.owner_name}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_REASSIGNED_ADMIN:
        return "\n".join(
            (
                "👥 管理员任务纠错通知",
                "",
                f"任务负责人已更新：{task_line}",
                f"当前负责人：{lease.owner_name}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_INVALIDATED_ADMIN:
        return "\n".join(
            (
                "↩️ 管理员任务纠错通知",
                "",
                f"误识别任务已撤销：{task_line}",
                f"原负责人：{lease.owner_name}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_RESTORED_COASSIGNEE:
        return "\n".join(
            (
                "↩️ 任务已恢复",
                "",
                f"管理员已恢复共同任务：{task_line}",
                f"当前负责人：{lease.owner_name}",
                f"当前状态：{_status_text(lease.status_snapshot)}",
                f"截止时间：{_deadline_text(lease.deadline)}",
            )
        )
    if lease.kind is TaskNotificationKind.TASK_RESTORED_ADMIN:
        return "\n".join(
            (
                "↩️ 管理员恢复任务通知",
                "",
                f"任务已恢复：{task_line}",
                f"当前负责人：{lease.owner_name}",
                f"当前状态：{_status_text(lease.status_snapshot)}",
                f"截止时间：{_deadline_text(lease.deadline)}",
            )
        )
    assert lease.kind is TaskNotificationKind.TASK_OVERDUE_ADMIN
    deadline = (
        "未设置"
        if lease.deadline is None
        else lease.deadline.astimezone(SHANGHAI_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
    )
    return "\n".join(
        (
            "🚨 任务逾期通知",
            "",
            f"负责人：{lease.owner_name}",
            f"任务：{task_line}",
            f"截止时间：{deadline}",
        )
    )


def _deadline_text(value: datetime | None) -> str:
    if value is None:
        return "未设置"
    return value.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")


def _missing_deadline_elapsed_text(lease: TaskNotificationLease) -> str:
    """Describe the actual per-chat delay captured by this notification."""

    delay = lease.scheduled_for - lease.task_created_at
    if delay <= timedelta(0):
        return "设定时间"
    seconds = int(delay.total_seconds())
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400} 天"
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _status_text(value: str) -> str:
    return {
        "todo": "待办",
        "overdue": "已逾期",
        "done": "已完成",
        "cancelled": "已取消",
    }.get(value, value)


def _delivery_uuid(lease: TaskNotificationLease) -> str:
    key = f"task-notification:{lease.notification_id}:{lease.kind.value}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"notification-{digest}"
