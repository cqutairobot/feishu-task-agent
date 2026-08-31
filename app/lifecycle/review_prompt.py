"""Prompt for read-only acceptance and reopen intent detection."""

from __future__ import annotations

from app.lifecycle.review_context import ReviewDetectionContext


REVIEW_DETECTION_INSTRUCTIONS = """你是已完成任务的复核意图识别器。你只读取管理员私聊中的自然语言，不修改任务，也不代表管理员确认操作。

规则：
1. action 只能是 accept 或 reopen；每条消息最多识别一个动作。
2. accept 仅在管理员明确陈述“验收通过”“确认通过”“接受本次完成”等已经作出的决定时输出。疑问、建议、申请、将来式和条件句都不算，例如“能否验收”“如果没问题就通过”“申请验收”必须输出空数组。
3. reopen 仅在管理员明确要求任务重新开启、退回返工或重新完成，并且消息给出具体原因时输出。reason 只保留可直接展示给负责人、说明为何完成不合格的核心原因；不得补写或猜测。没有原因时必须输出空数组。
4. 负责人说“已经补齐，申请重新验收”不是管理员复核决定，必须输出空数组。
5. task_id 只能从 reviewable_tasks 中按消息里的 task_code（如 T-1A 或 1A）精确选择，不得按标题猜测，不得跨群匹配。
6. accept 只能用于 review_status=pending，reason 必须为 null。reopen 可用于 review_status=pending 或 accepted，reason 必须为非空字符串。
7. evidence_message_ids 只能复制 messages 中的真实 ID，并必须包含本轮 trigger/focus 消息。
8. 没有明确且完整的高风险动作时 intents 输出空数组。只输出契约 JSON，不要 Markdown、解释或额外字段。
"""


def build_review_detection_input(
    context: ReviewDetectionContext,
) -> dict[str, object]:
    return {
        "instructions": REVIEW_DETECTION_INSTRUCTIONS,
        "context": context.to_dict(),
        "output_contract": {
            "intents": [
                {
                    "action": "accept | reopen",
                    "confidence": "number between 0 and 1",
                    "task_id": "integer copied from reviewable_tasks",
                    "reason": "reopen reason, otherwise null",
                    "evidence_message_ids": ["message_id"],
                }
            ]
        },
    }
