"""Prompt material for natural-language task-list query detection."""

from __future__ import annotations


TASK_QUERY_DETECTION_INSTRUCTIONS = """你是任务查询意图识别器。只判断用户是否在查询未完成任务，不回答问题，不创建任务，不修改任务，也不要输出任务内容。

规则：
1. 这一阶段只处理私聊（chat_type=p2p）的自然语言查询；群聊一律输出 is_task_query=false、scope=none。固定指令“任务列表”等由本地规则处理，不需要依赖你。
2. 当用户询问自己的未完成任务时，scope=self。例如“还有哪些任务没完成？”“现在还有什么事没做？”“最近还有哪些安排未处理？”“我还有什么待办？”“帮我看看未完成任务”。这里的“我”指当前私聊发送者，不需要猜测姓名或 Open ID。
3. 当用户点名询问其他成员时，scope=person，并把称呼原样整理到 target_name；当用户明确询问所有人的任务时，scope=all。scope=person 和 scope=all 都只会在本地管理员权限复核通过后执行；你只负责识别，不负责授权。
4. 与任务无关的寒暄、闲聊、任务创建/修改/完成/延期/复核、询问某条消息内容，都输出 is_task_query=false、scope=none。
5. status 固定为 open，表示未完成、待办、逾期但尚未结束的任务；不要识别已完成或已取消列表。
6. 只输出符合契约的严格 JSON，不要 Markdown、解释或额外字段。
"""


def build_task_query_input(
    text: str,
    *,
    chat_type: str,
    sender_name: str | None = None,
) -> dict[str, object]:
    """Build a small, non-authorizing input for the query classifier."""

    if not text.strip():
        raise ValueError("query text must not be empty")
    return {
        "instructions": TASK_QUERY_DETECTION_INSTRUCTIONS,
        "message": {
            "chat_type": chat_type,
            "sender_name": sender_name,
            "text": text,
        },
        "output_contract": {
            "is_task_query": "boolean",
            "scope": "none | self | person | all",
            "target_name": "name for person scope, otherwise null",
            "status": "open",
            "confidence": "number between 0 and 1",
        },
    }
