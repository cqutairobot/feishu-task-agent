"""Prompt material for read-only lifecycle update detection."""

from __future__ import annotations

from app.lifecycle.context import LifecycleDetectionContext


LIFECYCLE_DETECTION_INSTRUCTIONS = """你是现有任务生命周期识别器。只识别聊天对 open_tasks 中现有任务的明确更新，不创建新任务，也不执行数据库修改。

规则：
1. action 只能是 complete、reschedule、cancel、rename、reassign、invalidate。
2. complete 必须是明确陈述任务已经完成；“完成了吗”“快完成了”“正在做”都不是完成。
3. reschedule 必须明确给出新的截止时间，并依据 reference_time 与 timezone 转成带时区的 ISO 8601；只有“延期”“以后再说”而没有新日期时不要输出。
4. cancel 必须明确表示任务取消、不再需要或不用继续；“先别急”本身不是取消。
4a. rename 仅表示纠正任务标题，必须给出完整的新标题；new_title 填新标题。
4b. reassign 仅表示纠正完整负责人名单。new_owners 必须按用户表达的顺序从 eligible_owners 原样复制姓名和 Open ID；“增加王哈”表示保留原负责人并加入王哈，“负责人改为王哈”表示完整替换为王哈。不得选择 eligible_owners 之外的人。
4c. invalidate 仅用于明确表示机器人误识别、误创建、这根本不是任务。正常业务取消仍使用 cancel。
5. task_id 只能原样选择 open_tasks 中与聊天内容明确对应的任务。用户可能使用 task_code（如 T-1A 或 1A）指代任务；必须先在 open_tasks 中精确匹配其 task_code，再返回对应 task_id。不得猜测。task_scope.mode 为 same_chat 时不得跨群匹配；为 private_authorized_task 时，open_tasks 已经由本地权限层限定，可使用其中任务的 source_chat。
6. 如果同一负责人有多个相似任务而聊天不足以唯一匹配，updates 输出空数组。
7. evidence_message_ids 只能复制 messages 中的真实 ID，且必须包含本轮 trigger/focus 消息；疑问句或纯确认不能作为生命周期更新。
8. new_deadline 仅 reschedule 时非 null；new_title 仅 rename 时非 null；new_owners 仅 reassign 时非空。其他 action 对应字段必须分别为 null、null、空数组。
9. 输出所有彼此独立且明确的更新；同一 task_id 最多一个更新；没有明确更新时 updates 为空数组。
10. 只输出符合契约的 JSON，不要 Markdown、解释或额外字段。
"""


def build_lifecycle_detection_input(
    context: LifecycleDetectionContext,
) -> dict[str, object]:
    return {
        "instructions": LIFECYCLE_DETECTION_INSTRUCTIONS,
        "context": context.to_dict(),
        "output_contract": {
            "updates": [
                {
                    "action": (
                        "complete | reschedule | cancel | rename | "
                        "reassign | invalidate"
                    ),
                    "confidence": "number between 0 and 1",
                    "task_id": "integer copied from open_tasks",
                    "new_deadline": (
                        "ISO 8601 string with timezone for reschedule, "
                        "otherwise null"
                    ),
                    "new_title": "complete new title for rename, otherwise null",
                    "new_owners": [
                        {
                            "name": "name copied from eligible_owners",
                            "open_id": "Open ID copied from eligible_owners",
                        }
                    ],
                    "evidence_message_ids": ["message_id"],
                }
            ]
        },
    }
