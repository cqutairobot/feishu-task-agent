"""Private-first Feishu delivery for durable task reminders."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from app.agent.context import SHANGHAI_TZ
from app.config import FeishuSettings, ReminderSettings
from app.feishu.replies import build_api_client
from app.reminders.repository import ReminderLease
from app.reminders.schedule import ReminderKind
from app.tasks.repository import TaskSnapshot


class ReminderDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReminderDeliveryReceipt:
    message_id: str
    receive_id_type: str
    receive_id: str
    private_error_code: str | None = None
    private_error_message: str | None = None


class FeishuReminderSender:
    def __init__(
        self,
        settings: FeishuSettings,
        *,
        client: lark.Client | None = None,
        reminder_settings: ReminderSettings = ReminderSettings(),
    ) -> None:
        self._client = client or build_api_client(settings)
        self._reminder_settings = reminder_settings

    def deliver(self, lease: ReminderLease) -> ReminderDeliveryReceipt:
        key = (
            f"reminder:{lease.reminder_id}:{lease.deadline.isoformat()}"
        )
        return self._private_first(
            key=key,
            owner_open_id=lease.owner_open_id,
            private_chat_id=lease.owner_private_chat_id,
            chat_id=lease.chat_id,
            private_text=format_reminder_text(
                lease,
                mention_owner=False,
                settings=self._reminder_settings,
            ),
            group_text=format_reminder_text(
                lease,
                mention_owner=True,
                settings=self._reminder_settings,
            ),
            fallback_on_uncertain=(lease.attempt >= lease.max_attempts),
        )

    def probe(
        self,
        task: TaskSnapshot,
        *,
        probe_key: str,
        private_chat_id: str | None = None,
    ) -> ReminderDeliveryReceipt:
        private_text = format_probe_text(task, mention_owner=False)
        group_text = format_probe_text(task, mention_owner=True)
        return self._private_first(
            key=f"probe:{probe_key}",
            owner_open_id=task.owner_open_id,
            private_chat_id=private_chat_id,
            chat_id=task.chat_id,
            private_text=private_text,
            group_text=group_text,
            fallback_on_uncertain=False,
        )

    def _private_first(
        self,
        *,
        key: str,
        owner_open_id: str,
        private_chat_id: str | None,
        chat_id: str,
        private_text: str,
        group_text: str,
        fallback_on_uncertain: bool,
    ) -> ReminderDeliveryReceipt:
        private_receive_id_type = (
            "chat_id" if private_chat_id is not None else "open_id"
        )
        private_receive_id = private_chat_id or owner_open_id
        private_channel = (
            "private-chat" if private_chat_id is not None else "private-open-id"
        )
        try:
            message_id = self._send_text(
                receive_id_type=private_receive_id_type,
                receive_id=private_receive_id,
                text=private_text,
                uuid=_delivery_uuid(key, private_channel),
            )
            return ReminderDeliveryReceipt(
                message_id=message_id,
                receive_id_type=private_receive_id_type,
                receive_id=private_receive_id,
            )
        except ReminderDeliveryError as private_error:
            if (
                private_error.code in _UNCERTAIN_DELIVERY_CODES
                and not fallback_on_uncertain
            ):
                raise
            try:
                message_id = self._send_text(
                    receive_id_type="chat_id",
                    receive_id=chat_id,
                    text=group_text,
                    uuid=_delivery_uuid(key, "group"),
                )
            except ReminderDeliveryError as group_error:
                raise ReminderDeliveryError(
                    "all_delivery_failed",
                    "private delivery failed "
                    f"({private_error.code}: {private_error}); "
                    "group fallback failed "
                    f"({group_error.code}: {group_error})",
                ) from group_error
            return ReminderDeliveryReceipt(
                message_id=message_id,
                receive_id_type="chat_id",
                receive_id=chat_id,
                private_error_code=private_error.code,
                private_error_message=str(private_error),
            )

    def _send_text(
        self,
        *,
        receive_id_type: str,
        receive_id: str,
        text: str,
        uuid: str,
    ) -> str:
        content = json.dumps({"text": text}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(content)
                .uuid(uuid)
                .build()
            )
            .build()
        )
        try:
            response = self._client.im.v1.message.create(request)
        except Exception as exc:
            message = " ".join(str(exc).split()) or type(exc).__name__
            raise ReminderDeliveryError(
                "transport_error",
                f"{type(exc).__name__}: {message}"[:1_000],
            ) from exc
        if not response.success():
            code = str(getattr(response, "code", "unknown"))
            message = str(getattr(response, "msg", "unknown error"))
            raise ReminderDeliveryError(code, message)
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not isinstance(message_id, str) or not message_id.strip():
            raise ReminderDeliveryError(
                "missing_message_id",
                "Feishu accepted the message without returning a message ID",
            )
        return message_id.strip()


def format_reminder_text(
    lease: ReminderLease,
    *,
    mention_owner: bool,
    settings: ReminderSettings = ReminderSettings(),
) -> str:
    stage = (
        {
            ReminderKind.DUE_72H: "【测试提醒 1/4】任务将在 6 分钟后截止。",
            ReminderKind.DUE_24H: "【测试提醒 2/4】任务将在 4 分钟后截止。",
            ReminderKind.DUE_TODAY: "【测试提醒 3/4】任务将在 2 分钟后截止。",
            ReminderKind.OVERDUE: "【测试提醒 4/4】任务已逾期。",
        }
        if settings.test_mode
        else {
            ReminderKind.DUE_72H: (
                "第一提醒（截止前 "
                f"{_scheduled_offset_hours(lease)} 小时）。"
            ),
            ReminderKind.DUE_24H: (
                "第二提醒（截止前 "
                f"{_scheduled_offset_hours(lease)} 小时）。"
            ),
            ReminderKind.DUE_TODAY: "截止当天提醒。",
            ReminderKind.OVERDUE: "逾期提醒：任务已逾期，请尽快处理。",
        }
    )[lease.kind]
    owner_line = (
        _owner_mention(lease.owner_open_id, lease.owner_name)
        if mention_owner
        else lease.owner_name
    )
    deadline = lease.deadline.astimezone(SHANGHAI_TZ).strftime(
        "%Y-%m-%d %H:%M"
    )
    status = "已逾期" if lease.task_status == "overdue" else "未完成"
    return "\n".join(
        (
            "⏰ 任务提醒",
            "",
            owner_line,
            stage,
            f"任务：{lease.title}",
            f"截止时间：{deadline}",
            f"当前状态：{status}",
        )
    )


def _scheduled_offset_hours(lease: ReminderLease) -> int:
    seconds = (lease.deadline - lease.scheduled_for).total_seconds()
    return max(0, round(seconds / 3_600))


def format_probe_text(task: TaskSnapshot, *, mention_owner: bool) -> str:
    owner_line = (
        _owner_mention(task.owner_open_id, task.owner_name)
        if mention_owner
        else task.owner_name
    )
    deadline = (
        "未设置"
        if task.deadline is None
        else task.deadline.astimezone(SHANGHAI_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
    )
    return "\n".join(
        (
            "🧪 提醒链路测试",
            "",
            owner_line,
            f"任务：{task.title}",
            f"截止时间：{deadline}",
            "此消息只验证私聊优先、群聊回退，不会改变正式提醒计划。",
        )
    )


def _owner_mention(open_id: str, name: str) -> str:
    return (
        f'<at user_id="{escape(open_id, quote=True)}">'
        f"{escape(name)}</at>"
    )


def _delivery_uuid(key: str, channel: str) -> str:
    digest = hashlib.sha256(f"{key}:{channel}".encode()).hexdigest()[:32]
    return f"reminder-{digest}"


_UNCERTAIN_DELIVERY_CODES = frozenset(
    {
        "230049",  # Feishu reports that the message is still being sent.
        "230101",  # Observed to arrive after a temporary-unavailable response.
        "transport_error",  # The request may have reached Feishu.
    }
)
