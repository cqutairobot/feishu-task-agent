"""Prompt material for natural-language task-note detection."""

from __future__ import annotations

from app.lifecycle.context import LifecycleDetectionContext


TASK_NOTE_DETECTION_INSTRUCTIONS = """你是任务说明记录器。只识别用户对 open_tasks 中一个已经存在任务的补充说明，不创建任务，也不修改任务状态。

规则：
1. 只有明确在记录进度、阻塞/问题、完成结果、延期原因、一般说明、返工说明或纠错说明时才输出 notes；询问、寒暄和单纯的“已完成/延期到某日/取消”属于生命周期操作，应输出空数组。
2. task_id 必须从 open_tasks 原样复制；用户可能使用 T-1A 或 1A 指代任务，不能猜测或跨群匹配。
3. note_type 只能是 progress、blocker、completion、delay、reopen、general、correction。负责人不能通过说明改变任务状态；reopen 与 correction 仅管理员可写，权限由本地校验。
4. content 只整理用户本条说明的事实，保留路径、数字和阻塞原因，不添加模型推测，最长 8000 字符。
5. evidence_message_ids 只能复制 messages 中真实 ID，并且必须包含本轮 trigger/focus 消息。
6. 一条私聊最多输出一个说明；无法明确对应任务或不是说明时输出 notes 为空数组。
7. 只输出符合契约的 JSON，不要 Markdown、解释或额外字段。
"""


def build_task_note_detection_input(
    context: LifecycleDetectionContext,
) -> dict[str, object]:
    return {
        "instructions": TASK_NOTE_DETECTION_INSTRUCTIONS,
        "context": context.to_dict(),
        "output_contract": {
            "notes": [
                {
                    "task_id": "integer copied from open_tasks",
                    "note_type": (
                        "progress | blocker | completion | delay | reopen | "
                        "general | correction"
                    ),
                    "content": "the user's factual note, max 8000 chars",
                    "confidence": "number between 0 and 1",
                    "evidence_message_ids": ["message_id including trigger"],
                }
            ]
        },
    }
