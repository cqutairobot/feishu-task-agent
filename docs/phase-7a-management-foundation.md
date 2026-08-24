# Phase 7A：群级管理权限与只读管理底座

本阶段先解决管理页面最重要的两个前置问题：谁有权查看哪个群，以及所有
管理查询是否始终限定在这个群。暂不提供网页写入，也不开放未认证的 HTTP
接口。

## 群级管理员

迁移 `20260823_0016` 新增：

- `chat_administrators`：当前有效的 `chat_id + open_id` 管理关系；
- `chat_administrator_events`：追加式记录管理员的授予和撤销。

被授予者必须已经在该群绑定过唯一姓名。管理员关系不会跨群继承。同一个人
可以管理多个群，但每个群都需要单独授予。

首次配置通过本机受信任的命令完成：

```bash
python -m app chat-admin grant --chat-id oc_xxx --open-id ou_xxx
python -m app chat-admin list --chat-id oc_xxx
python -m app chat-admin revoke --chat-id oc_xxx --open-id ou_xxx
```

授予和撤销幂等；重复执行不会制造重复关系或重复审计。Phase 7B 管理页会
复用同一个存储层，不另建一份管理员名单。

`FEISHU_TASK_ADMIN_OPEN_IDS` 只保留为旧部署的过渡兼容。完成群级管理员
初始化后应留空，避免一个 Open ID 自动成为所有允许群的全局管理员。

## 同一权限覆盖机器人现有能力

持久化的群级管理员不仅用于未来网页，也用于：

- 群内和私聊“任务列表”的管理员视图；
- 私聊自然语言完成、取消、延期和纠错；
- 私聊任务卡片操作后的管理员任务刷新；
- 管理员截止时间提示、逾期和生命周期通知。

私聊管理员视图只汇总该用户实际管理的群。撤销管理员后，尚未发送的
“请管理员设置截止时间”和逾期提示会被取消。

## 只读管理 API

`ManagementReadApi` 是网页适配层将调用的服务端应用 API，提供：

- 当前身份可管理的群列表；
- 单群任务统计；
- 按状态、负责人、关键词、是否缺少截止时间、截止上限分页筛选任务；
- 单个任务的证据消息、生命周期审计、提醒和通知投递记录。

每一个带 `chat_id` 的读取都先在同一数据库会话中校验管理员关系，再查询
任务。任务查询本身仍重复添加 `Task.chat_id == chat_id` 条件，防止前端参数
错误造成跨群读取。未授权时统一返回模糊错误，不透露任务是否存在。

本机可用以下命令查看相同的 JSON 读模型：

```bash
python -m app management chats --actor-open-id ou_xxx
python -m app management dashboard --actor-open-id ou_xxx --chat-id oc_xxx
python -m app management tasks --actor-open-id ou_xxx --chat-id oc_xxx --status todo
python -m app management task --actor-open-id ou_xxx --chat-id oc_xxx --task-id 1
```

这些命令只供本机诊断，调用者身份参数不等于网页登录认证。因此 Phase 7A
不监听管理端口。Phase 7B 接入飞书网页登录后，服务端会从已验证会话取得
Open ID，再调用同一 API；浏览器提交的 Open ID 不会被信任。

## 验证范围

专项测试覆盖：

- 管理员授予、撤销、幂等和审计；
- 必须是本群已绑定成员；
- 群列表、统计、筛选、分页和任务详情；
- 未授权与跨群任务读取；
- 机器人任务列表、私聊生命周期、卡片刷新和通知对群级权限的复用；
- SQLite 约束、唯一键和实际查询索引。
