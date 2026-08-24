# Phase 1C：飞书后台配置与真实群聊测试

本阶段只验证消息接收。不要添加数据库、LLM 或提醒功能。

## 1. 创建企业自建应用

1. 登录飞书开放平台。
2. 进入开发者后台，创建“企业自建应用”。
3. 填写应用名称、描述和图标。
4. 在应用能力中启用“机器人”。

## 2. 获取本地连接凭证

在“凭证与基础信息”页面复制：

- App ID
- App Secret

不要把 App Secret 发送到群聊、聊天窗口或提交到 Git。

在项目根目录执行：

```bash
cp .env.example .env
```

然后只在本机编辑 `.env`：

```dotenv
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=你的真实密钥
FEISHU_ALLOWED_CHAT_IDS=
FEISHU_LOG_LEVEL=INFO
```

首次连接时保持群白名单为空，因为此时还不知道测试群的 `chat_id`。

## 3. 申请消息权限

在“权限管理”中搜索并申请：

- 获取群组中所有消息

控制台显示的权限标识可能包含：

```text
im:message.group_msg
```

或对应的只读权限名称。应按控制台当前显示的“获取群组中所有消息”权限为准。

仅申请“接收群聊中 @ 机器人消息”不能满足本项目要求。上述权限通常需要管理员审批。

## 4. 启动本地长连接

在项目根目录执行：

```bash
source .venv/bin/activate
python -m app listen
```

程序会读取本机 `.env`，并主动连接飞书 WebSocket。SDK 日志固定为 WARNING，
避免 INFO 日志打印短期 WebSocket 连接参数；终端不得打印 App Secret。

如果飞书后台要求“先建立长连接才能保存订阅方式”，保持这个终端持续运行，再返回后台配置。

## 5. 配置事件订阅

1. 进入“事件与回调”或“事件配置”。
2. 将订阅方式设置为“使用长连接接收事件”。
3. 添加“接收消息 v2.0”事件。

事件标识应为：

```text
im.message.receive_v1
```

## 6. 发布并审批

1. 创建应用版本。
2. 确认应用可用范围包含测试成员。
3. 提交发布。
4. 由企业管理员审批应用及高级消息权限。

后台配置通常需要发布并审批后才会对群成员生效。

## 7. 把机器人加入测试群

在目标测试群中添加刚创建的应用机器人。只使用测试群，不要立即加入正式工作群。

## 8. 真实消息验收

保持监听程序运行，让至少两名群成员在不 `@机器人` 的情况下发送普通文本。

预期终端输出：

```text
[15:32:10]
message_id: om_xxxxx
chat_id: oc_xxxxx
sender_open_id: ou_xxxxx
message_type: text
message: 今天实验结果出来了吗？
```

记录终端显示的测试群 `chat_id`，然后可以把 `.env` 更新为：

```dotenv
FEISHU_ALLOWED_CHAT_IDS=oc_xxxxx
```

重启程序后，其他群的消息应被忽略。

## Phase 1 最终验收条件

- 两名不同成员的消息均能收到。
- 消息不需要 `@机器人`。
- 连续发送 20 条文本消息，无明显漏收。
- `message_id`、`chat_id`、`sender_open_id` 和正文正确。
- 停止并重新运行程序后可以重新连接。
- 非白名单群消息不显示。
- 日志中不包含 App Secret。
