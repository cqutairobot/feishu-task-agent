# 飞书群聊任务机器人

一个从飞书群聊中识别任务、跟踪负责人和截止时间，并自动私聊提醒的任务机器人。

当前仓库提供原生 Python + Node.js 开发运行方式，适用于 macOS、Windows 和 Linux。
Docker、OrbStack 演练和云服务器部署将在后续阶段加入，不是本页当前安装流程的一部分。

## 主要能力

- 无需 `@机器人`，保存授权群内的普通文本消息。
- 结合最近群聊上下文识别负责人、任务内容、截止时间和证据消息。
- 支持一句话多任务、多人共同任务和工作/生活两种识别范围。
- 为任务分配稳定编号，例如 `T-1A`。
- 新任务、临期、到期、逾期和缺少截止时间时优先私聊相关成员。
- 负责人可私聊机器人，用自然语言完成、取消、延期或纠正任务。
- 群管理员可通过登录链接进入按群隔离的管理后台。
- SQLite 持久化消息、任务、提醒、通知、权限和审计记录。

## 当前运行结构

完整功能需要同时运行以下进程：

1. 飞书 WebSocket Listener；
2. 任务识别 Worker；
3. 截止提醒 Worker；
4. 任务通知 Worker；
5. 管理 API；
6. 管理前端。

`python -m app dev` 会在一个终端中统一监督全部六个进程。关闭终端或电脑后程序会
停止；后续 Docker 阶段会提供后台运行和主机重启后的自动恢复能力。

## 系统要求

- Git
- Python `3.11`–`3.14`，推荐 Python `3.13`
- Node.js `22.13` 或更高版本，附带 npm
- 一个已发布的飞书企业自建应用
- 一个支持 OpenAI Chat Completions 兼容接口和结构化 JSON 输出的模型服务

安装前检查版本：

```text
git --version
python --version
node --version
npm --version
```

## 1. 克隆仓库

仓库创建后，会把下面的占位地址替换为真实 GitHub 地址：

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/feishu-task-agent.git
cd feishu-task-agent
```

## 2. 安装后端

### macOS 或 Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
cp .env.example .env
```

### Windows PowerShell

```powershell
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
Copy-Item .env.example .env
```

如果 Windows 没有 `py` 启动器，可以将第一条命令替换为：

```powershell
python -m venv .venv
```

验证 Python 环境：

```bash
python -m app check
```

正常输出类似：

```text
Feishu Task Agent local runtime is ready. Python 3.13.x
```

## 3. 安装管理前端

macOS、Windows 和 Linux 使用相同命令：

```bash
cd management-web
npm ci
cd ..
```

## 4. 配置飞书应用

在飞书开放平台创建企业自建应用并启用机器人，然后完成：

1. 申请“获取群组中所有消息”等读取权限，使普通群消息不需要 `@机器人`；
2. 申请“以应用的身份发消息”权限；
3. 使用长连接订阅 `im.message.receive_v1`；
4. 按飞书控制台要求配置卡片交互回调；
5. 创建版本、发布应用并完成企业管理员审批；
6. 把机器人加入测试群。

不同飞书版本显示的权限名称可能略有差异。详细操作参考
[`docs/phase-1c-feishu-setup.md`](docs/phase-1c-feishu-setup.md) 和
[`docs/phase-2e-b-identity-commands.md`](docs/phase-2e-b-identity-commands.md)。

## 5. 填写 `.env`

只编辑项目根目录的 `.env`，不要修改或提交 `.env.example` 来保存真实密钥。

至少填写：

```dotenv
FEISHU_APP_ID=你的飞书App ID
FEISHU_APP_SECRET=你的飞书App Secret

TASK_LLM_API_KEY=你的模型API Key
TASK_LLM_BASE_URL=你的OpenAI兼容接口地址
TASK_LLM_MODEL=你的模型名称
```

首次接入新群时可以保持以下名单为空：

```dotenv
FEISHU_ALLOWED_CHAT_IDS=
```

空名单不会让任意群自动获得管理权限。新群仍需由当前群主显式执行“初始化本群”。
如需只允许指定群，可在获取群 ID 后填写逗号分隔的 `chat_id`。

本地管理后台默认地址：

```dotenv
MANAGEMENT_WEB_PUBLIC_BASE_URL=http://127.0.0.1:8000
MANAGEMENT_WEB_FRONTEND_URL=http://127.0.0.1:3000
MANAGEMENT_WEB_BIND_HOST=127.0.0.1
MANAGEMENT_WEB_PORT=8000
```

不要把本地配置直接改成公网监听。HTTPS 和公网访问将在正式部署阶段统一配置。

## 6. 检查配置和初始化数据库

模型连接检查会真实调用一次你配置的模型接口：

```bash
python -m app llm-check --probe
```

创建或升级本地 SQLite 数据库，并显示非敏感计数：

```bash
python -m app db-status
```

默认数据库位于：

```text
data/feishu_task_agent.db
```

`data/` 已被 Git 忽略。

## 7. 启动完整程序

先激活 Python 虚拟环境，然后在项目根目录用一个终端启动完整程序：

```bash
python -u -m app dev
```

这个命令会先检查 Python 配置、Node.js/npm、前端依赖、`8000/3000` 端口并完成一次
数据库迁移，再依次启动：

- Listener
- Detection Worker
- Reminder Worker
- Notification Worker
- Management API
- Management Web

日志行会带对应服务名前缀。任一必需进程异常退出时，其余服务会一起停止，避免留下
只有部分功能在线的状态。Windows PowerShell 激活虚拟环境后使用相同命令。

如果只想诊断 Python 后端而不启动网页，可以使用：

```bash
python -u -m app dev-backend
```

默认服务地址：

- 管理 API：`http://127.0.0.1:8000/health`
- 管理前端：`http://127.0.0.1:3000`

按一次 `Ctrl+C` 会统一停止全部六个进程。

## 8. 首次群聊验收

1. 当前飞书群主在群内发送：`@机器人 初始化本群`。
2. 每位成员发送：`@机器人 绑定姓名：自己的真实姓名`。
3. 群内发送一条明确任务，例如：`王政，请在周五前完成登录页面。`
4. 等待识别窗口和 Worker 处理，负责人应收到新任务私聊。
5. 负责人私聊机器人发送：`任务列表`。
6. 负责人私聊：`1A 已完成`，或点击任务卡片完成。
7. 管理员私聊机器人发送：`管理后台`，使用一次性链接登录。

管理员设置、成员离群和群主更换规则参考
[`docs/phase-7c-group-administration.md`](docs/phase-7c-group-administration.md)。

## 9. 运行测试

后端完整回归：

```bash
python -m unittest discover -s tests
```

前端检查：

```bash
cd management-web
npm run lint
npm test
npm audit
```

`npm test` 会执行一次生产构建并通过标准 Vinext 生产服务器验证渲染结果。首次发布前
要求 `npm audit` 报告 0 个已知漏洞。

## 常见问题

### 群消息没有被识别

- 确认 Listener 和任务识别 Worker 都在运行。
- 确认机器人已经加入该群且应用版本已发布。
- 确认飞书开放平台已经审批“获取群组中所有消息”权限。
- 确认该群已经由群主初始化，或位于 `FEISHU_ALLOWED_CHAT_IDS` 中。
- 任务识别默认有 20 秒上下文收集窗口，不会在发送后立即创建。

### 创建任务但没有收到私聊

- 确认任务通知 Worker 正在运行。
- 让负责人先私聊机器人发送一次“任务列表”，建立可用私聊会话。
- 检查负责人是否已经在任务来源群绑定唯一任务姓名。

### 管理后台没有回复或无法打开

- 确认管理 API 和管理前端都在运行。
- 确认 `.env` 中 `MANAGEMENT_WEB_ENABLED=true`。
- 确认发送者仍是目标群的有效管理员。
- 本地登录链接只能在运行程序的同一台电脑上访问。

### 端口被占用

默认管理端口是 `8000` 和 `3000`。`dev` 会在启动前同时检查两个端口；如果提示
地址不可用，先在旧的运行终端按 `Ctrl+C`，不要重复启动完整程序。`dev-backend`
只检查后端使用的 `8000` 端口。

### 提示缺少前端依赖

确认已经执行：

```bash
cd management-web
npm ci
cd ..
```

## 数据和安全

- `.env`、数据库、日志、虚拟环境和前端构建产物不会提交到 Git。
- 不要把 App Secret、模型 API Key、真实 Open ID 或群 ID 写入源码和文档。
- 本地数据库备份可以复制 `data/feishu_task_agent.db`，但不要上传到 GitHub。
- 上传 GitHub 前应再次执行敏感信息扫描并检查待提交文件清单。

## 开发文档

历史阶段设计、验收记录和后续计划保存在 [`docs/`](docs/)；当前持久路线图为
[`docs/roadmap.md`](docs/roadmap.md)。这些文档用于继续开发，不是安装必读内容。
