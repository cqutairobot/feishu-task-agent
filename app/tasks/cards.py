"""Feishu interactive cards for private task-list replies."""

from __future__ import annotations

from typing import Any

from app.agent.context import SHANGHAI_TZ
from app.tasks.repository import (
    CrossChatTaskListPage,
    TaskSnapshot,
    TaskStatus,
)


CardPayload = dict[str, Any]


def build_private_task_list_card(
    page: CrossChatTaskListPage,
    *,
    is_admin: bool,
    actions_enabled: bool = False,
) -> CardPayload:
    """Build a read-only card from an already-authorized task-list page."""

    title = "全部未完成任务" if is_admin else "我的未完成任务"
    elements: list[CardPayload] = []
    if page.total_count == 0:
        empty_text = (
            "当前配置群中没有未完成任务。"
            if is_admin
            else "你当前没有未完成任务。"
        )
        elements.append(_markdown(f"📭 **{empty_text}**"))
    else:
        view_note = "管理员视图" if is_admin else "个人视图"
        elements.append(
            _markdown(f"{view_note} · 共 **{page.total_count}** 项")
        )
        for index, entry in enumerate(page.entries, start=1):
            if index > 1:
                elements.append({"tag": "hr"})
            elements.append(
                _task_element(
                    index,
                    entry.task,
                    chat_name=entry.chat_name,
                    show_owner=is_admin,
                )
            )
            if actions_enabled and entry.task.status in {
                TaskStatus.TODO,
                TaskStatus.OVERDUE,
            }:
                elements.append(_task_actions(entry.task))

        hidden = page.total_count - len(page.entries)
        if hidden > 0:
            elements.extend(
                (
                    {"tag": "hr"},
                    _markdown(f"另有 **{hidden}** 项未显示。"),
                )
            )
    return {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": False,
        },
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": title,
            },
        },
        "elements": elements,
    }


def _task_element(
    index: int,
    task: TaskSnapshot,
    *,
    chat_name: str | None,
    show_owner: bool,
) -> CardPayload:
    deadline = (
        "未设置"
        if task.deadline is None
        else task.deadline.astimezone(SHANGHAI_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
    )
    status = {
        TaskStatus.PENDING: "🟡 待确认",
        TaskStatus.TODO: "🔵 待办",
        TaskStatus.OVERDUE: "🔴 已逾期",
    }[task.status]
    title = _escape_lark_markdown(task.title, limit=160)
    lines = [f"**{index}. [{task.public_code}] {title}**"]
    if chat_name is not None:
        group = _escape_lark_markdown(chat_name, limit=80)
        lines.append(f"群聊：{group}")
    responsible = task.responsible_members
    if len(responsible) > 1:
        names = "、".join(
            _escape_lark_markdown(member.name, limit=80)
            for member in responsible
        )
        lines.append(f"共同负责人：{names}")
    elif show_owner:
        owner = _escape_lark_markdown(responsible[0].name, limit=80)
        lines.append(f"负责人：{owner}")
    lines.append(f"截止：{deadline}　{status}")
    return _markdown("\n".join(lines))


def _markdown(content: str) -> CardPayload:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
        },
    }


def _task_actions(task: TaskSnapshot) -> CardPayload:
    shared = {
        "command": "task_lifecycle",
        "version": "1",
        "task_code": task.public_code,
    }
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 完成"},
                "type": "primary",
                "value": {**shared, "action": "complete"},
            },
            {
                "tag": "picker_datetime",
                "name": "new_deadline",
                "placeholder": {
                    "tag": "plain_text",
                    "content": "📅 选择新截止时间",
                },
                **(
                    {
                        "initial_datetime": task.deadline.astimezone(
                            SHANGHAI_TZ
                        ).strftime("%Y-%m-%d %H:%M")
                    }
                    if task.deadline is not None
                    else {}
                ),
                "value": {**shared, "action": "reschedule"},
                "confirm": {
                    "title": {
                        "tag": "plain_text",
                        "content": "确认延期任务？",
                    },
                    "text": {
                        "tag": "plain_text",
                        "content": (
                            f"将 [{task.public_code}] 的截止时间更新为所选时间，"
                            "并重新安排提醒。"
                        ),
                    },
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消任务"},
                "type": "danger",
                "value": {**shared, "action": "cancel"},
                "confirm": {
                    "title": {
                        "tag": "plain_text",
                        "content": "确认取消任务？",
                    },
                    "text": {
                        "tag": "plain_text",
                        "content": (
                            f"取消后将停止 [{task.public_code}] 的未发送提醒。"
                        ),
                    },
                },
            },
        ],
    }


def _escape_lark_markdown(value: str, *, limit: int) -> str:
    """Render stored chat text without allowing card markup or mentions."""

    compact = " ".join(value.split())[:limit]
    replacements = (
        ("\\", "\\\\"),
        ("<", "‹"),
        (">", "›"),
        ("`", "ˋ"),
        ("*", "\\*"),
        ("_", "\\_"),
        ("~", "\\~"),
        ("[", "\\["),
        ("]", "\\]"),
    )
    for source, replacement in replacements:
        compact = compact.replace(source, replacement)
    return compact or "（未命名）"
