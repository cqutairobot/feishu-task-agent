# 飞书群聊任务机器人

一个从飞书群聊中识别任务、跟踪负责人和截止时间，并自动私聊提醒的任务机器人。

第一次把项目部署到 Ubuntu 服务器，请阅读：
[《Linux 部署指南》](LINUX_DEPLOYMENT_GUIDE.md)。该文档同时解释常用终端、Git 和
Docker 命令的含义、预期结果及危险操作。

中国大陆阿里云 ECS 无法稳定访问 Docker Hub 时，请直接使用已经实际验收通过的
[《阿里云 ECS 部署指南》](ALIYUN_ECS_DEPLOYMENT_GUIDE.md)：在 OrbStack 构建
`linux/amd64` 镜像，通过 SSH 上传到 ECS，再使用 Docker Compose 启动。

当前仓库提供原生 Python + Node.js 开发运行方式，适用于 macOS、Windows 和 Linux；
同时提供面向 Linux 单机部署的生产镜像、Docker Compose 编排、同源网关和 SQLite
持久卷，以及 SQLite 一致性备份、隔离恢复验证和阿里云 ECS 公网部署说明。

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

原生开发模式的完整功能需要同时运行以下进程：

1. 飞书 WebSocket Listener；
2. 任务识别 Worker；
3. 截止提醒 Worker；
4. 任务通知 Worker；
5. 管理 API；
6. 管理前端。

`python -m app dev` 会在一个终端中统一监督全部六个进程。关闭终端或电脑后程序会
停止；Linux 长期部署应使用本文后面的 Docker Compose，容器会在后台运行并在主机
重启后自动恢复。

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

使用下面的地址克隆私有仓库；首次操作时 GitHub 会要求使用已获授权的账号：

```bash
git clone https://github.com/cqutairobot/feishu-task-agent.git
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

不同飞书版本显示的权限名称可能略有差异，请以开放平台中与群消息读取、应用消息
发送、长连接事件和卡片交互相关的权限为准。

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

进入管理后台后可以维护当前群的额外管理员；所有成员、任务和设置均按 `chat_id`
隔离，不会把其他群的数据混入当前群。

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

## 10. Docker Compose 单机运行

Docker Compose 会运行一次数据库迁移，并分别启动 Listener、三个 Worker、管理 API、
管理前端和同源网关。开始前先停止原生 `python -m app dev`，避免同一个机器人被两套
Listener 同时连接。

本机 Docker Desktop 默认可以使用 `.env.example` 中的部署入口：

```dotenv
DEPLOY_PUBLIC_URL=http://127.0.0.1:8080
DEPLOY_BIND_HOST=127.0.0.1
DEPLOY_HTTP_PORT=8080
DEPLOY_IMAGE_TAG=local
```

OrbStack Linux 虚拟机需要允许 Mac 访问虚拟机端口，并把公开地址换成实际机器名：

```dotenv
DEPLOY_PUBLIC_URL=http://feishu-server.orb.local:8080
DEPLOY_BIND_HOST=0.0.0.0
DEPLOY_HTTP_PORT=8080
DEPLOY_IMAGE_TAG=local
```

校验、构建并后台启动：

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps --all
```

预期 `migrate` 为 `Exited (0)`，其他七个服务为 `Up` 或 `healthy`。访问：

- 完整管理入口：`DEPLOY_PUBLIC_URL`；
- 健康检查：`DEPLOY_PUBLIC_URL/health`。

常用运维命令：

```bash
docker compose logs --follow --tail 100
docker compose stop
docker compose start
docker compose down
```

SQLite 保存在 Docker 命名卷 `feishu-task-agent_task-data`，普通 `stop`、`start`、
`restart` 和 `down` 不会删除数据。不要执行 `docker compose down --volumes`，除非明确
要永久删除该部署数据库。`.env` 仅在运行时注入，不会复制进镜像。

### 创建 SQLite 一致性备份

长期服务运行时不要直接复制命名卷里的数据库文件。仓库提供的备份命令会通过
SQLite Backup API 创建一致性快照，执行完整性检查，核对容器内外 SHA-256，并以
`0600` 权限保存到 Docker 卷之外：

```bash
./scripts/docker-backup.sh
```

默认保存目录是 `~/feishu-task-agent-backups`。也可以指定另一个位于持久磁盘上的目录：

```bash
BACKUP_DIR=/srv/feishu-task-agent/backups ./scripts/docker-backup.sh
```

命令输出 `backup_integrity: ok`、最终路径和 SHA-256 才表示备份成功。正式数据卷恢复
工具会在后续阶段加入；在此之前不要手工用备份覆盖正在运行的数据库。

可以先把任意一份备份恢复到一次性隔离卷，验证文件完整性、复制校验和、数据库迁移
兼容性及应用读取能力。该命令不会停止服务，也不会挂载或修改正式数据卷：

```bash
./scripts/docker-verify-backup.sh \
  ~/feishu-task-agent-backups/feishu-task-agent-20260824-190735.db
```

输出 `source_integrity: ok`、`restore_verification: ok` 和恢复后的非敏感数据计数才算
通过；临时卷无论成功或失败都会清理。真正覆盖正式卷的灾难恢复工具仍在后续阶段
加入。

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
