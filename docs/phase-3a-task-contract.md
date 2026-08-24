# Phase 3A：任务识别上下文与输出契约

本阶段不调用 LLM，只固定模型将来能够看到的输入，以及模型输出必须通过的
程序校验。目标是避免跨群数据泄漏和模型虚构用户、证据或日期。

## 检查真实上下文

指定当前群和触发消息：

```bash
python -m app task-context \
  --chat-id oc_xxx \
  --message-id om_xxx \
  --limit 30
```

上下文满足以下约束：

- 只包含指定 `chat_id` 的消息。
- 最后一条固定为指定的触发消息，不读取触发之后的聊天。
- 默认最多 30 条，并受总字符预算限制。
- 机器人消息默认排除。
- 时间统一提供 `Asia/Shanghai` 时区和触发消息参考时间。
- 负责人候选只来自本群每位成员当前唯一的确认姓名和当前上下文发送者。

## 严格输出

模型只能输出下面七个字段：

```json
{
  "is_task": true,
  "confidence": 0.96,
  "owner": {
    "name": "王政",
    "open_id": "ou_xxx"
  },
  "title": "补充 baseline 实验",
  "description": "完成 ResNet50 baseline 实验",
  "deadline": "2026-08-27T23:59:59+08:00",
  "evidence_message_ids": ["om_xxx", "om_yyy"]
}
```

程序会拒绝：

- 额外字段、缺失字段、重复 JSON 字段或非有限数值。
- 当前上下文中不存在的证据消息 ID。
- 当前群候选人中不存在的负责人 Open ID。
- 姓名与负责人 Open ID 的已确认别名不匹配。
- 不带时区或格式错误的截止时间。
- `is_task=false` 时仍携带任务字段。

Phase 3B 接入模型后，即使模型违反契约，结果也不会进入任务数据库。
