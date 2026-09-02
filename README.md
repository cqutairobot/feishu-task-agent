# 飞书群聊任务机器人

一个运行在飞书群聊中的任务协作机器人：它从群消息和长会议纪要中识别任务，记录负责人、截止时间和来源证据，按任务编号跟踪进度，并通过私聊提醒负责人。管理员可以在管理后台查看任务、责任链和生命周期记录，验收完成结果或要求返工。

## 项目能做什么

- 读取已授权群的普通消息，无需每条消息都 `@机器人`。
- 从多轮对话中识别单个或多个任务、负责人、截止时间和来源消息。
- 为任务生成稳定编号，例如 `T-1A`，支持用编号进行查询和自然语言操作。
- 支持多人共同任务、完成说明、来源证据、进度/阻塞/一般备注和完成周期。
- 在任务新建、临近截止、到期、逾期或缺少截止时间时发送私聊提醒。
- 负责人可以私聊机器人查看、完成、延期、取消任务，管理员可以验收或要求返工。
- 支持私聊自然语言查询未完成任务：成员查询自己；管理员的一般查询查看管理范围内全部任务，也可明确查询自己或按任务姓名查询指定成员。
- 记录发布者、负责人、实际完成人、复核人、操作原因、来源消息、通知投递和幂等键。
- 提供按群隔离的管理后台，以及 SQLite 持久化和一致性备份脚本。

## 运行方式

项目提供两种运行方式：

1. **本地开发（推荐修改代码时使用）**：Python 虚拟环境运行后端和 Worker，Node.js 运行管理前端。`python -u -m app dev` 会在一个终端监督全部进程。
2. **Docker Compose（推荐长期运行或 Linux/云服务器）**：容器内封装 Python、Node.js 和 Nginx 环境，数据保存在 Docker 命名卷中。

两种方式只能同时运行一套飞书 Listener；如果使用相同的飞书应用凭据同时启动，会造成重复处理消息。

## 系统要求

- Git
- Python 3.11–3.14（推荐 3.13）
- Node.js 22.13 或更高版本和 npm
- 已发布并加入测试群的飞书企业自建应用
- 一个支持 OpenAI Chat Completions 兼容接口和结构化 JSON 输出的模型服务
- Docker 方式还需要 Docker Engine 和 Docker Compose v2

检查版本：

```bash
git --version
python3 --version
node --version
npm --version
```

## 本地开发运行

### 1. 获取代码并安装依赖

```bash
git clone https://github.com/cqutairobot/feishu-task-agent.git
cd feishu-task-agent

python3 -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock

cd management-web
npm ci
cd ..

cp .env.example .env            # Windows PowerShell: Copy-Item .env.example .env
```

### 2. 配置 `.env`

只编辑项目根目录的 `.env`，不要把真实密钥写入源码、提交到 Git 或放进镜像。最少填写：

```dotenv
FEISHU_APP_ID=你的飞书App_ID
FEISHU_APP_SECRET=你的飞书App_Secret
TASK_LLM_API_KEY=你的模型API_Key
TASK_LLM_BASE_URL=https://你的OpenAI兼容服务/v1
TASK_LLM_MODEL=你的模型名称
```

首次开发可使用以下本地默认值：

```dotenv
MANAGEMENT_WEB_ENABLED=true
MANAGEMENT_WEB_PUBLIC_BASE_URL=http://127.0.0.1:8000
MANAGEMENT_WEB_FRONTEND_URL=http://127.0.0.1:3000
MANAGEMENT_WEB_BIND_HOST=127.0.0.1
MANAGEMENT_WEB_PORT=8000
LIFECYCLE_PRIVATE_WRITES_ENABLED=true
LIFECYCLE_REVIEW_WRITES_ENABLED=false
```

`LIFECYCLE_REVIEW_WRITES_ENABLED` 控制管理员通过私聊执行“验收通过/重新开启”这类高风险写入，默认关闭；后台验收仍按后台权限和确认流程执行。`FEISHU_ALLOWED_CHAT_IDS` 可以留空，之后由群主在群内执行“初始化本群”。

### 3. 检查配置并初始化数据库

```bash
source .venv/bin/activate
python -m app check
python -m app db-status
```

`data/feishu_task_agent.db` 是本地 SQLite 数据库，已经被 Git 忽略。`llm-check --probe` 会真实调用一次模型接口，确认模型配置可用：

```bash
python -m app llm-check --probe
```

### 4. 启动完整服务

```bash
source .venv/bin/activate
python -u -m app dev
```

该命令会启动并监督：

- 飞书 WebSocket Listener
- 任务识别 Worker
- 截止提醒 Worker
- 任务通知 Worker
- 管理 API（`http://127.0.0.1:8000`）
- 管理前端（`http://127.0.0.1:3000`）

浏览器打开 `http://127.0.0.1:3000` 进入管理后台。按 `Ctrl+C` 会停止全部本地进程；关闭终端或电脑后本地服务也会停止。

只启动后端诊断时可用：

```bash
source .venv/bin/activate
python -u -m app dev-backend
```

### 5. 第一次群聊验收

1. 群主在目标群发送：`@机器人 初始化本群`。
2. 每位成员发送：`@机器人 绑定姓名：自己的真实姓名`。
3. 发送明确任务，例如：`王政，请在周五前完成登录页面。`。
4. 负责人私聊机器人发送 `任务列表`，或直接说“我还有什么待办”，查看本人未完成任务；管理员可以说“还有什么任务没完成”查看管理范围内全部任务，也可以说“王政还有哪些任务没完成？”查询指定成员。
5. 负责人私聊 `T-1A 已完成` 或点击任务卡片提交完成说明。
6. 管理员私聊机器人发送 `管理后台`，使用机器人返回的一次性链接登录后台。

## Docker Compose 运行

Docker 方式适合 Linux 服务器或需要后台常驻的环境。先准备 `.env`，再在项目根目录执行：

```bash
docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 180
docker compose ps --all
curl -fsS http://127.0.0.1:8080/health
```

默认入口是 `http://127.0.0.1:8080`。如果要让同一局域网或云服务器公网访问，在 `.env` 中设置：

```dotenv
DEPLOY_PUBLIC_URL=http://服务器IP:8080
DEPLOY_BIND_HOST=0.0.0.0
DEPLOY_HTTP_PORT=8080
DEPLOY_IMAGE_TAG=local
```

常用命令：

```bash
docker compose logs --follow --tail 100  # 查看日志
docker compose stop                       # 停止但保留容器和数据
docker compose start                      # 重新启动
docker compose restart                    # 重启
docker compose down                       # 删除容器但保留命名卷
```

不要执行 `docker compose down --volumes`，除非你明确要删除数据库。数据库位于命名卷 `feishu-task-agent_task-data`，`.env` 只在运行时注入，不会复制进镜像。

### Docker 镜像与中国大陆云服务器

Docker 构建会从镜像仓库获取基础镜像；如果服务器访问 Docker Hub 超时，可以在本地构建 `linux/amd64` 镜像后用 `docker save | gzip` 导出，再通过 SSH/SCP 上传到服务器，用 `docker load` 导入并执行上面的 `docker compose up --no-build`。镜像只包含程序环境和代码，SQLite 数据仍在命名卷中，更新镜像不会覆盖数据库。

## 测试

后端完整回归：

```bash
source .venv/bin/activate
python -m unittest discover -s tests
```

前端检查：

```bash
cd management-web
npm run lint
npm test
```

## 更新代码后的基本流程

本地开发：

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.lock
cd management-web && npm ci && cd ..
python -u -m app dev
```

Docker Compose：

```bash
git pull --ff-only
docker compose build
docker compose up -d --wait --wait-timeout 180
docker compose ps --all
```

已有真实数据的部署，在更新前应先执行 `python -m app db-status` 和 `./scripts/docker-backup.sh`；不要删除数据卷。代码更新会运行数据库迁移，迁移失败时先停止发布并保留备份。

## 常见问题

- **群消息没有识别**：确认 Listener、任务识别 Worker、飞书消息读取权限和群初始化状态。
- **没有收到私聊**：负责人先私聊机器人发送一次 `任务列表`，并确认姓名已在来源群绑定。
- **后台打不开**：确认管理 API、管理前端和 `.env` 中的管理后台开关与地址。
- **端口被占用**：先在旧终端按 `Ctrl+C`，不要同时启动两套本地服务。
- **Docker Hub 超时**：确认 Docker 镜像加速器，或采用本地构建后上传镜像的方式。

## 安全提示

- 不要提交 `.env`、飞书 App Secret、模型 API Key、真实 Open ID、群 ID 或 SQLite 数据库。
- 生产环境应限制 SSH 和管理端口来源，不要长期把管理后台暴露给所有公网地址。
- 备份数据库后再做迁移或升级，恢复操作必须使用隔离卷验证。
