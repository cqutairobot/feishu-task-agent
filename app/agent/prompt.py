"""Stable prompt material for the future structured-output LLM call."""

from __future__ import annotations

from app.agent.context import TaskDetectionContext


TASK_DETECTION_INSTRUCTIONS = """你是群聊任务识别器。只依据输入中的当前群消息判断是否已经形成明确、可执行的任务。

规则：
1. messages 是理解上下文；只输出由 focus_message_ids 中本轮新消息形成的任务，每个任务的证据必须至少包含一条 focus_message_ids。
2. 一句明确指令本身即可形成任务，不要求负责人回复、确认或承诺。当指令已充分时，只引用指令作为证据，不得把“收到”“好的”等纯确认回复列为必要证据。
3. 负责人必须从 known_participants 中选择，并原样复制 open_id 和已确认姓名。
4. evidence_message_ids 只能复制 messages 中真实存在的 message_id。
5. 相对日期必须依据 reference_time 和 timezone 换算，deadline 必须带时区。只给出日历日期或星期、没有明确时分时，统一取该截止日 23:59:59；例如“周五前”是周五 23:59:59，不能解析成周五 00:00 或周四结束。数字时长必须从 reference_time 精确顺延并保留时分秒，例如“两天内”是 reference_time 加 48 小时，不能改成后天 23:59:59。
6. 不得猜测姓名、open_id、截止日期或消息 ID。
7. 身份绑定命令、查询命令和机器人回复不是工作任务。
8. 只有负责人、任务内容已经明确时 is_task 才能为 true；没有截止日期时 deadline 可以为 null。
9. 无法形成任务时，owner、title、description、deadline 必须为 null，evidence_message_ids 必须为空数组。
10. messages 中的 mentions 是飞书原始 @ 的精确目标映射；正文里的 `key`（例如 `@_user_1`）必须使用对应的 open_id，并将 mentions.name 作为本群已确认的任务姓名原样输出，不能改用飞书显示名或靠上下文猜测。有效群成员被 @ 后，如果同时给出可执行动作和时间，应视为已形成任务。
11. 组织、主持、召开或参加一个明确时间的会议/讨论也属于可执行任务；例如“@李明 今天 21:00 前开智能体讨论会议”应创建任务，deadline 为该时间。
12. focus 消息出现“我来做”“我来补”“好的”等省略表达时，只能回指它之前最近且语义连续的事项；不得越过更新的主题去续接更早的旧任务。
13. 只输出一个符合契约的 JSON 对象，不要 Markdown 或解释文字。
"""

TASK_BATCH_DETECTION_INSTRUCTIONS = """你是群聊任务识别器。只依据输入中的当前群消息，找出零个或多个已经形成的明确、可执行任务。

规则：
1. messages 是理解上下文；只输出由 focus_message_ids 中本轮新消息形成的任务，每个 candidate 的证据必须至少包含一条 focus_message_ids。不能仅因消息在同一时间批次就把它们视为同一任务。
2. 一句明确指令本身即可形成任务，不要求负责人回复、确认或承诺。当指令已充分时，只引用指令作为证据，不得把“收到”“好的”等纯确认回复列为必要证据。
3. 输出所有彼此独立的明确任务；没有任务时 candidates 必须为空数组。
4. 一项共同产出物由多人共同负责时，只输出一个 candidate：assignment_mode 为 shared，owner 是句子中第一位负责人，co_owners 按原文顺序列出其余负责人。
5. 只有一位负责人时 assignment_mode 为 single 且 co_owners 为空数组。“各自”、“每人一份”等表示每人分别交付时，必须拆成多个 single candidates。
6. 同一句消息给不同成员分配不同任务时，必须拆成多个 single candidates。不得合并内容或目标不同的任务，也不得把同一任务重复输出。
7. 所有负责人必须从 known_participants 中选择，并原样复制 open_id 和已确认姓名。
8. evidence_message_ids 只能复制 messages 中真实存在的 message_id，并只包含支持该 candidate 的必要消息。
9. 相对日期必须依据 reference_time 和 timezone 换算，deadline 必须带时区；只给出日历日期或星期、没有明确时分时，统一取该截止日 23:59:59，例如“周五前”是周五 23:59:59，不能解析成周五 00:00 或周四结束；数字时长必须从 reference_time 精确顺延并保留时分秒，例如“两天内”是 reference_time 加 48 小时，不能改成后天 23:59:59；没有明确截止日期时可以为 null。
10. 不得猜测姓名、open_id、截止日期或消息 ID。
11. 身份绑定命令、查询命令和机器人回复不是工作任务。
12. messages 中的 mentions 是飞书原始 @ 的精确目标映射；正文里的 `key`（例如 `@_user_1`）必须使用对应的 open_id，并将 mentions.name 作为本群已确认的任务姓名原样输出，不能改用飞书显示名或靠上下文猜测。有效群成员被 @ 后，如果同时给出可执行动作和时间，应视为已形成任务。
13. 组织、主持、召开或参加一个明确时间的会议/讨论也属于可执行任务；例如“@李明 今天 21:00 前开智能体讨论会议”应创建任务，deadline 为该时间。
14. focus 消息出现“我来做”“我来补”“好的”等省略表达时，只能回指它之前最近且语义连续的事项；不得越过更新的主题去续接更早的旧任务。
15. publisher 表示这项任务由谁在群里发布或明确布置：通常是证据消息中发出指令、分派任务的人，而不是负责人本人。publisher 必须从 known_participants 中选择，必须是 evidence_message_ids 中至少一条消息的 sender_open_id；如果无法可靠判断发布者，publisher 必须为 null，publisher_attribution_basis 必须为 unknown，publisher_attribution_confidence 必须为 null。
16. publisher_attribution_basis 只能是 message_sender（发布者就是证据消息发送者）或 explicit_assignment（会议纪要等证据正文明确写出由另一位成员布置/分派，发布者姓名或 @ 必须出现在证据中）；只有 publisher 不为 null 时才填写对应依据和 0 到 1 的置信度。
17. 只输出符合契约的 JSON 对象，不要 Markdown、解释文字或额外字段。
"""


def _task_scope_instruction(task_scope: str) -> str:
    if task_scope == "broad":
        return (
            "\n任务范围规则（最高优先级）：当前群使用 broad 宽泛模式。工作、科研、"
            "会议、学习、生活需求和个人跑腿都可以是任务；不要仅因为内容是带饭、"
            "送物品或其他生活事务而排除。仍必须有明确负责人和可执行动作，不能把"
            "闲聊、愿望、问题或被取消的事项当任务。"
        )
    if task_scope == "work_only":
        return (
            "\n任务范围规则（最高优先级）：当前群使用 work_only 模式。只识别工作、"
            "科研、正式项目、会议或明确交付事项；即使有负责人和时间，也必须排除"
            "带饭、购买餐饮、递送私人用品、聚餐安排等个人生活跑腿。"
        )
    raise ValueError("task_scope must be broad or work_only")


def build_task_detection_input(
    context: TaskDetectionContext,
) -> dict[str, object]:
    return {
        "instructions": TASK_DETECTION_INSTRUCTIONS
        + _task_scope_instruction(context.task_scope),
        "context": context.to_dict(),
        "output_contract": {
            "is_task": "boolean",
            "confidence": "number between 0 and 1",
            "owner": {"name": "string", "open_id": "string"},
            "title": "string",
            "description": "string",
            "deadline": "ISO 8601 string with timezone, or null",
            "evidence_message_ids": ["message_id"],
        },
    }


def build_task_batch_detection_input(
    context: TaskDetectionContext,
) -> dict[str, object]:
    return {
        "instructions": TASK_BATCH_DETECTION_INSTRUCTIONS
        + _task_scope_instruction(context.task_scope),
        "context": context.to_dict(),
        "output_contract": {
            "candidates": [
                {
                    "assignment_mode": "single or shared",
                    "confidence": "number between 0 and 1",
                    "co_owners": [
                        {"name": "string", "open_id": "string"}
                    ],
                    "owner": {"name": "string", "open_id": "string"},
                    "title": "string",
                    "description": "string",
                    "deadline": "ISO 8601 string with timezone, or null",
                    "evidence_message_ids": ["message_id"],
                    "publisher": "object with name/open_id, or null",
                    "publisher_attribution_basis": (
                        "message_sender, explicit_assignment, or unknown"
                    ),
                    "publisher_attribution_confidence": (
                        "number between 0 and 1, or null"
                    ),
                }
            ]
        },
    }
