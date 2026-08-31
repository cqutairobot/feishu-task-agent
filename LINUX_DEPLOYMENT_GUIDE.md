# 飞书任务机器人 Linux 部署指南

这是一份面向第一次接触 Linux、Git 和 Docker 的部署说明。目标是在一台长期在线的
Ubuntu 22.04 服务器上，从 GitHub 获取本项目，并使用 Docker Compose 启动机器人、
Worker、管理后台和数据库。

本文默认条件：

- 服务器系统是 Ubuntu 22.04 64 位；
- 使用普通用户登录，该用户可以执行 `sudo`；
- 源码仓库是 `cqutairobot/feishu-task-agent`；
- 仓库目前是私有仓库；
- 部署方式是 Docker Compose，不在服务器上单独安装 Python 和 Node.js；
- 数据库是 Docker 命名卷中的 SQLite，适合当前单机部署；
- 所有时间使用 `Asia/Shanghai`。

如果只是开发项目，请阅读 [README.md](README.md)；如果准备把机器人放到 Linux
服务器长期运行，请从本文开始。

如果目标是中国大陆阿里云 ECS，且 Docker Hub 访问缓慢、超时或镜像加速器缺少项目
需要的标签，请改用已经实际验收通过的
[《阿里云 ECS 部署指南》](ALIYUN_ECS_DEPLOYMENT_GUIDE.md)。该方案在 OrbStack
构建 `linux/amd64` 镜像后通过 SSH 上传，不要求 ECS 从 Docker Hub 构建项目镜像。

## 1. 先理解几个名词

| 名词 | 在这个项目中的含义 |
| --- | --- |
| Linux 服务器 | 一台长期联网、通常没有图形界面的远程电脑。阿里云 ECS 就是这种电脑。 |
| SSH | 从自己的电脑打开服务器终端的连接方式。 |
| GitHub 仓库 | 保存项目源码和版本历史的位置。 |
| Git | 下载、检查和更新 GitHub 源码的工具。 |
| Docker 镜像 | 已封装好程序和依赖的只读运行模板。 |
| Docker 容器 | 根据镜像启动的一个实际运行进程。 |
| Docker Compose | 根据 `compose.yaml` 一次管理本项目全部容器。 |
| `.env` | 只保存在服务器上的私密配置，包括飞书密钥和模型 API Key。 |
| Docker 命名卷 | 独立于容器保存数据库的磁盘空间。重建容器不会自动删除它。 |
| Gateway | 浏览器访问管理后台的统一入口，默认使用服务器的 8080 端口。 |
| Worker | 在后台持续识别任务、发送提醒或发送任务通知的进程。 |

本项目的长期服务共有七个：

1. `listener`：接收飞书消息和卡片交互；
2. `detection-worker`：调用模型识别并创建任务；
3. `reminder-worker`：发送临期、到期和逾期提醒；
4. `notification-worker`：发送新任务和生命周期通知；
5. `management-api`：提供管理后台 API；
6. `management-web`：提供管理后台网页；
7. `gateway`：向浏览器提供统一入口。

另有一个 `migrate` 容器，每次部署时负责升级数据库结构，完成后正常退出。

### OrbStack 演练命令不会出现在阿里云

之前演练中使用过的以下内容只属于 Mac 上的 OrbStack：

```text
orb -m feishu-server
orbctl restart feishu-server
http://feishu-server.orb.local:8080
/Users/Admin/Documents/ChatGPT/agent
```

真实阿里云部署时分别替换为：

- `ssh 用户名@公网IP`：进入云服务器；
- `sudo reboot`：重启云服务器；
- 公网 IP 或正式 HTTPS 域名：浏览器入口；
- `/home/用户名/feishu-task-agent`：服务器项目目录。

Docker 部署不需要在服务器上执行 `python -m venv`、`pip install` 或 `npm ci`。这些
依赖会在构建镜像时自动安装；它们仍会用于 Mac/Windows 的原生开发环境。

## 2. 看懂终端命令

终端一般显示类似提示符：

```text
deploy@server:~$
```

提示符不需要输入。只复制提示符后面的命令。

常见符号：

- `~`：当前登录用户的个人目录，例如 `/home/deploy`；
- `pwd`：显示当前所在目录；
- `cd 路径`：进入指定目录；
- `cd ~`：回到个人目录；
- `sudo`：临时用管理员权限执行安装或系统命令；
- `Ctrl+C`：取消当前提示或停止前台运行的命令；
- `\`：表示命令在下一行继续，整段仍是一条命令；
- `&&`：只有前一条命令成功，才执行后一条；
- `|`：把左侧命令的输出交给右侧命令处理；
- `>`：覆盖写入文件，不了解目标时不要随便使用；
- `sudo reboot`：重启整台服务器，SSH 会暂时断开。

注意：终端中必须使用纯文本网址。例如：

```text
https://github.com/cqutairobot/feishu-task-agent.git
```

不要粘贴下面这种 Markdown 链接：

```text
[https://github.com/...](https://github.com/...)
```

后者会被 Git 当成错误网址。

## 3. 部署前需要准备什么

开始前应具备：

- 一台 Ubuntu 22.04 服务器；
- 一个可以使用 `sudo` 的普通登录用户；
- 服务器可以主动访问 GitHub、飞书和模型接口；如果准备在服务器构建镜像，还必须能
  访问 Docker Hub；阿里云本地镜像上传方案不需要这一条件；
- 已发布并审批通过的飞书企业自建应用；
- 飞书 App ID 和 App Secret；
- 模型接口的 API Key、OpenAI 兼容地址和模型名称；
- GitHub 仓库读取权限；
- 首次测试时可从浏览器访问服务器的 8080 端口。

飞书消息使用 WebSocket 长连接，由服务器主动向外连接，所以飞书接收消息本身不要求
开放入站端口。8080 只用于管理后台；正式公网部署最终应通过域名和 HTTPS 访问。

## 4. 登录并检查 Linux 服务器

在自己的 Mac 或 Windows 终端使用阿里云提供的公网 IP 登录：

```bash
ssh deploy@服务器公网IP
```

`deploy` 应替换成真实登录用户名。首次连接会询问是否信任服务器指纹，确认 IP 正确
后输入 `yes`。

登录后执行：

```bash
whoami
cat /etc/os-release | head
uname -m
df -h
free -h
```

这些命令分别用于：

- `whoami`：确认当前用户；
- `cat /etc/os-release`：确认 Ubuntu 版本；
- `uname -m`：确认 CPU 架构，常见为 `x86_64` 或 `aarch64`；
- `df -h`：查看磁盘剩余空间；
- `free -h`：查看内存。

项目已同时使用 Ubuntu `amd64/x86_64` 环境完成部署演练。

## 5. 安装 Docker Engine 和 Compose

以下步骤来自 Docker 官方 Ubuntu 安装方式。先安装基础工具和 Docker 的签名密钥：

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

作用：让 Ubuntu 信任并能够从 Docker 官方软件源下载安装包。

添加 Docker 官方软件源：

```bash
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
```

安装 Docker、Buildx 和 Compose 插件：

```bash
sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
```

让当前用户以后不必在每条 Docker 命令前添加 `sudo`：

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

`docker` 用户组具有接近系统管理员的权限，所以只应加入可信的服务器账号。

验证安装：

```bash
sudo systemctl is-active docker
docker version
docker compose version
docker run --rm hello-world
```

预期：

- Docker 状态显示 `active`；
- `docker version` 同时显示 Client 和 Server；
- `docker compose version` 能显示版本；
- `hello-world` 输出 `Hello from Docker!`。

Ubuntu 通常会让 Docker 随系统自动启动。可以明确启用：

```bash
sudo systemctl enable docker.service
sudo systemctl enable containerd.service
```

官方参考：

- [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/)

## 6. 从私有 GitHub 仓库下载项目

先安装 Git 和 GitHub CLI：

```bash
sudo apt update
sudo apt install -y git gh
```

通过一次性网页验证码登录 GitHub：

```bash
gh auth login --hostname github.com --web
```

终端会显示一次性验证码。在自己的浏览器打开它提示的网址，登录有仓库权限的 GitHub
账号并授权。不要把验证码或令牌发给其他人。

让 Git 使用 GitHub CLI 保存的授权信息：

```bash
gh auth setup-git
gh auth status
```

如果 `gh auth status` 显示已登录账号和 HTTPS 协议，就可以克隆：

```bash
cd ~
gh repo clone cqutairobot/feishu-task-agent feishu-task-agent
cd ~/feishu-task-agent
```

验证下载结果：

```bash
git log -1 --oneline
git status
ls Dockerfile compose.yaml gateway/nginx.conf
test ! -e .env && echo ".env correctly absent"
```

含义：

- `git log -1 --oneline`：显示当前源码版本；
- `git status`：确认没有本地代码改动；
- `ls ...`：确认 Docker 发布文件存在；
- 最后一条命令确认 GitHub 中没有真实 `.env`。

如果 GitHub 出现 HTTP/2 或 TLS 握手错误，可让该仓库固定使用 HTTP/1.1 后重试：

```bash
git config http.version HTTP/1.1
git pull --ff-only
```

GitHub CLI 官方参考：[gh auth login](https://cli.github.com/manual/gh_auth_login)。

## 7. 创建并填写 `.env`

从模板创建真实配置，并限制为只有当前用户可读写：

```bash
cd ~/feishu-task-agent
cp .env.example .env
chmod 600 .env
```

安装一个较容易使用的终端编辑器：

```bash
sudo apt install -y nano
nano .env
```

Nano 中：

- 方向键移动光标；
- `Ctrl+W` 是搜索，按 `Ctrl+C` 取消搜索；
- `Ctrl+O` 保存，再按 Enter 确认文件名；
- `Ctrl+X` 退出。

至少填写：

```dotenv
FEISHU_APP_ID=你的飞书App ID
FEISHU_APP_SECRET=你的飞书App Secret

TASK_LLM_API_KEY=你的模型API Key
TASK_LLM_BASE_URL=你的OpenAI兼容接口地址
TASK_LLM_MODEL=你的模型名称
```

云服务器初次验收阶段的入口配置示例：

```dotenv
DEPLOY_PUBLIC_URL=http://服务器公网IP:8080
DEPLOY_BIND_HOST=0.0.0.0
DEPLOY_HTTP_PORT=8080
DEPLOY_IMAGE_TAG=local
MANAGEMENT_WEB_COOKIE_SECURE=false
```

这里的“服务器公网 IP”必须替换成真实 IP，不要保留中文文字。正式域名和 HTTPS 完成
后，应把 `DEPLOY_PUBLIC_URL` 改为 `https://你的域名`，并把 Cookie 安全开关设为
`true`。

重要配置含义：

| 配置 | 含义 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书应用身份。 |
| `FEISHU_APP_SECRET` | 飞书应用密钥。 |
| `FEISHU_ALLOWED_CHAT_IDS` | 可选群白名单；留空时仍可由群主执行“初始化本群”。 |
| `TASK_LLM_API_KEY` | 模型接口密钥。 |
| `TASK_LLM_BASE_URL` | OpenAI 兼容 API 地址。 |
| `TASK_LLM_MODEL` | 实际使用的模型名称。 |
| `DETECTION_DEBOUNCE_SECONDS` | 等待连续上下文的时间，当前默认 20 秒。 |
| `LIFECYCLE_PRIVATE_WRITES_ENABLED` | 是否允许负责人私聊完成、延期、取消等任务操作。 |
| `LIFECYCLE_REVIEW_WRITES_ENABLED` | 是否允许管理员私聊验收/返工；必须同时开启上一项。 |
| `REMINDER_TEST_MODE` | 仅临时验收提醒时使用；正式环境必须为 `false`。 |
| `MANAGEMENT_WEB_ENABLED` | 是否启用管理后台。 |
| `DEPLOY_PUBLIC_URL` | 飞书登录链接和浏览器实际访问的完整公网入口。 |
| `DEPLOY_BIND_HOST` | `0.0.0.0` 表示允许从服务器外部访问发布端口。 |
| `DEPLOY_HTTP_PORT` | Gateway 映射到服务器的端口，默认 8080。 |

检查非敏感部署参数：

```bash
stat -c '%a %n' .env
grep '^DEPLOY_' .env
```

`.env` 权限应显示 `600`。不要运行 `cat .env` 后把完整输出发到聊天或工单中。

## 8. 检查、构建并启动

本节是“服务器可以正常访问 Docker Hub”时的通用构建路线。中国大陆阿里云 ECS 请
优先按照 [《阿里云 ECS 部署指南》](ALIYUN_ECS_DEPLOYMENT_GUIDE.md) 在 OrbStack
构建并上传镜像；镜像导入后使用 `docker compose up --no-build`，不要在 ECS 重复
构建。

首先只检查 Compose 配置：

```bash
cd ~/feishu-task-agent
docker compose config --quiet
docker compose config --services
```

`config --quiet` 没有输出就代表语法检查通过。服务列表应包含八项：

```text
migrate
listener
detection-worker
reminder-worker
notification-worker
management-api
management-web
gateway
```

不要把完整的 `docker compose config` 输出公开，因为展开后的配置可能包含不应分享的
运行参数。

构建三个生产镜像：

```bash
docker compose build
```

作用：读取 `Dockerfile`，把 Python 后端、管理前端和 Gateway 打包为镜像。首次构建
需要下载依赖，可能持续数分钟。

后台启动并等待健康检查：

```bash
docker compose up -d --wait --wait-timeout 180
```

参数含义：

- `up`：创建或更新服务；
- `-d`：让服务在后台运行，退出 SSH 后也不会停止；
- `--wait`：等待服务健康；
- `--wait-timeout 180`：最长等待 180 秒。

查看结果：

```bash
docker compose ps --all
```

正确状态：

- `migrate` 显示 `Exited (0)`，表示迁移成功结束，不是故障；
- 其余七个服务显示 `Up ... (healthy)`。

检查网页入口：

```bash
curl -fsS http://127.0.0.1:8080/health
echo
```

预期：

```json
{"status":"ok","administrator_management":true}
```

`127.0.0.1` 表示从服务器内部访问自己。浏览器则使用 `.env` 中配置的
`DEPLOY_PUBLIC_URL`。

## 9. 阿里云安全组的最小配置

首次部署测试时：

- 22/TCP：SSH，只允许自己的公网 IP；
- 8080/TCP：临时管理后台，尽量只允许自己的公网 IP；
- 不需要为飞书 WebSocket 额外开放入站端口。

正式域名和 HTTPS 完成后：

- 开放 443/TCP；
- 80/TCP 只用于跳转 HTTPS 或证书验证；
- 关闭公网 8080，或继续限制为本机/内网访问；
- 不要把 Docker API 端口暴露到公网。

Docker 发布的端口可能绕过部分主机防火墙规则，最终应同时检查阿里云安全组和服务器
防火墙。首次真实上云时应按当时的网络方案单独验收。

## 10. 飞书端首次验收

容器全部健康后：

1. 把已发布机器人加入测试群；
2. 当前群主发送：`@机器人 初始化本群`；
3. 成员发送：`@机器人 绑定姓名：自己的真实姓名`；
4. 负责人先私聊机器人发送一次：`任务列表`，建立可用私聊会话；
5. 群里发送明确任务，例如：`王天，请在明天18:00前完成部署验收记录。`；
6. 等待约 20 秒上下文窗口和模型处理；
7. 负责人应收到新任务私聊通知；
8. 管理员私聊机器人发送：`管理后台`，打开一次性登录链接。

如果负责人从未与机器人私聊过，飞书不一定允许机器人仅凭 Open ID 主动建立第一次
私聊。先发送一次“任务列表”即可保存私聊会话。

## 11. 日常最常用的命令

以下命令都应在项目目录执行：

```bash
cd ~/feishu-task-agent
```

### 查看服务状态

```bash
docker compose ps --all
```

用途：判断服务是否运行、是否健康、迁移是否成功。

### 查看最近日志

```bash
docker compose logs --tail 100
```

查看某个服务：

```bash
docker compose logs --tail 100 listener
docker compose logs --tail 100 detection-worker
```

持续跟踪新日志：

```bash
docker compose logs --follow --tail 100
```

按 `Ctrl+C` 只会退出日志查看，不会停止后台容器。

### 重启一个服务

```bash
docker compose restart listener
```

用途：只重启飞书 Listener。也可以把 `listener` 换成其他服务名。

### 停止和重新启动整套服务

```bash
docker compose stop
docker compose start
```

`stop` 只停止容器，不删除容器或数据库；`start` 重新启动已有容器。人工执行 `stop`
后，系统会把它视为主动停止，服务器重启时不一定自动拉起，恢复运行后再重启服务器。

### 删除容器但保留数据库

```bash
docker compose down
```

`down` 会删除本项目容器和网络，但默认保留命名卷数据库。之后使用下面命令重建：

```bash
docker compose up -d --wait --wait-timeout 180
```

### 检查数据库非敏感计数

```bash
docker compose exec -T management-api python -m app db-status
```

用途：显示群、用户、消息、任务和提醒数量，不输出消息内容或密钥。

### 检查管理后台健康接口

```bash
curl -fsS http://127.0.0.1:8080/health
echo
```

`curl` 用于发起 HTTP 请求；`-f` 遇到错误状态就失败，`-sS` 减少正常噪声但保留错误。

## 12. 创建和验证数据库备份

创建一致性备份：

```bash
cd ~/feishu-task-agent
./scripts/docker-backup.sh
```

这个命令会：

1. 让运行中的 SQLite 创建一致快照；
2. 执行数据库完整性检查；
3. 比较容器内外 SHA-256；
4. 以 `0600` 权限保存到 `~/feishu-task-agent-backups`。

只有出现以下内容才算成功：

```text
backup_integrity: ok
backup_path: /home/.../feishu-task-agent-日期时间.db
backup_sha256: ...
```

列出备份：

```bash
ls -lh ~/feishu-task-agent-backups
```

在不接触正式数据库的情况下验证某份备份：

```bash
./scripts/docker-verify-backup.sh \
  ~/feishu-task-agent-backups/实际备份文件名.db
```

它会恢复到一次性临时卷，读取数据后自动清理。预期包含：

```text
source_integrity: ok
restore_verification: ok
live_volume_untouched: feishu-task-agent_task-data
```

备份仍位于同一台服务器时无法防止整台服务器磁盘损坏。正式上线后应把备份复制到
阿里云 OSS 或另一台独立存储。

## 13. 从 GitHub 升级项目

本节适用于服务器能够构建镜像的环境。采用阿里云本地镜像上传路线时，源码仍通过
`git pull --ff-only` 更新，但镜像应在 OrbStack 重建、上传并通过 `docker load` 导入；
完整步骤见 [《阿里云 ECS 部署指南》](ALIYUN_ECS_DEPLOYMENT_GUIDE.md)。

不要直接在服务器上修改源码。升级前先记录当前提交、数据库计数和容器使用的镜像：

```bash
cd ~/feishu-task-agent
OLD_COMMIT=$(git rev-parse HEAD)
echo "$OLD_COMMIT"
docker compose exec -T management-api python -m app db-status \
  | tee "$HOME/feishu-task-agent-backups/pre-upgrade-db-status.txt"
docker compose images
```

然后创建并隔离验证一致性备份：

```bash
./scripts/docker-backup.sh
LATEST_BACKUP=$(ls -1t "$HOME"/feishu-task-agent-backups/feishu-task-agent-*.db | head -n 1)
./scripts/docker-verify-backup.sh "$LATEST_BACKUP"
```

只有同时看到 `backup_integrity: ok`、`source_integrity: ok` 和
`restore_verification: ok` 才继续。备份必须位于 Docker 命名卷之外。

确认工作树干净并记录旧版本：

```bash
git status
git log -1 --oneline
```

获取新版本：

```bash
git pull --ff-only
git log -1 --oneline
```

`--ff-only` 表示只接受清晰的直线升级；如果服务器上有人改过源码导致历史冲突，它会
停止，而不是自动合并未知改动。

### 13.1 Phase 9 溯源升级的配置开关

从旧版本升级到任务溯源版本时，`migrate` 会依次应用 `0033`～`0039`：发布者与责任链、
系统逾期事件、任务说明、说明审计、完成说明关联、返工通知和验收通知。迁移只扩展现有
SQLite 数据，不会清空原任务，也不会把旧任务伪造成有完成说明的新任务。

先检查 `.env`：

```bash
grep -E '^LIFECYCLE_(PRIVATE|REVIEW)_WRITES_ENABLED=' .env
```

正式启用自然语言复核时应为：

```dotenv
LIFECYCLE_PRIVATE_WRITES_ENABLED=true
LIFECYCLE_REVIEW_WRITES_ENABLED=true
```

第二项依赖第一项。若希望先只部署页面和数据库、暂不允许自然语言验收/返工，可以保持
`LIFECYCLE_REVIEW_WRITES_ENABLED=false`；管理后台复核不受该私聊开关影响。编辑后执行
`docker compose config --quiet`，不要把 `.env` 提交到 Git。

### 13.2 构建、迁移并切换容器

重新构建并更新容器：

```bash
docker compose build
docker compose up -d --force-recreate --wait --wait-timeout 180
docker compose ps --all
curl -fsS http://127.0.0.1:8080/health
echo
docker compose exec -T management-api python -m app db-status
docker compose exec -T management-api python -c '
import sqlite3
db = sqlite3.connect("/app/data/feishu_task_agent.db")
print("schema_version:", db.execute("SELECT version_num FROM alembic_version").fetchone()[0])
'
```

预期 `migrate` 为 `Exited (0)`，其余服务健康，`schema_version` 为当前仓库最新迁移；本版
应为 `20260831_0039`。升级前后的非敏感业务计数应保持一致，除非升级期间仍有真实消息
写入。命名卷独立于镜像和容器，正常构建、更新或重建不会删除 SQLite 数据库。

### 13.3 溯源功能最小验收

1. 负责人给一个测试任务追加 `T-编号 进度：……`，确认状态不变；
2. 负责人发送带结果说明的 `T-编号 已完成……`，确认进入“已完成、待复核”；
3. 管理员发送 `T-编号 验收通过`，确认第一条只提示，再发送机器人给出的“确认执行”句；
4. 后台检查发布者、负责人、实际完成人、复核人、完成周期和统一时间线；
5. 确认同一接收人没有收到重复通知。

### 13.4 回退边界

- `git pull` 或构建失败、尚未切换容器：旧容器通常仍在运行，修复错误后重试即可；
- 新容器启动失败但数据库没有业务写入：保留 `$OLD_COMMIT`、旧镜像和备份，使用已验证的
  旧镜像临时恢复服务；不要执行 `git reset --hard`；
- 迁移后已经产生新的说明、完成周期、复核或通知记录：不要只降级数据库，也不要把旧
  SQLite 文件直接覆盖正在运行的命名卷。需要回到旧数据库时必须先停服务，并使用升级前
  已验证备份按正式灾难恢复流程处理；仓库当前只提供隔离恢复验证，不提供一键覆盖正式卷；
- 回退应用镜像不会自动回退数据库。`alembic downgrade`、删除表、删除命名卷都不是普通
  发布回退命令。

升级失败时不要执行 `git reset --hard`、删除数据库或删除命名卷。保留终端错误、旧提交
编号和最近备份，再按错误制定回退方案。

## 14. 服务器重启后检查

Docker 和本项目容器已经配置自动恢复。需要重启 Linux 时：

```bash
sudo reboot
```

SSH 连接会立即断开，这是正常现象。等待服务器恢复后重新 SSH 登录，然后执行：

```bash
cd ~/feishu-task-agent
docker compose ps --all
curl -fsS http://127.0.0.1:8080/health
echo
docker compose exec -T management-api python -m app db-status
```

如果刚启动时状态为 `starting`，等待约 20 秒再查看。正常情况下不需要重新执行
`docker compose up`。

## 15. 常见问题

### `Permission denied`，无法运行 Docker

执行：

```bash
sudo usermod -aG docker "$USER"
newgrp docker
docker run --rm hello-world
```

### `Username for 'https://github.com'`

GitHub 不接受账户密码作为 Git 密码。执行：

```bash
gh auth login --hostname github.com --web
gh auth setup-git
gh auth status
```

然后使用 `gh repo clone`，不要把普通 GitHub 密码输入终端。

### GitHub 出现 HTTP/2、GnuTLS 或 TLS 错误

先重试；仍失败时在仓库目录执行：

```bash
git config http.version HTTP/1.1
git pull --ff-only
```

### `address already in use`

说明端口已被其他程序占用。检查 8080：

```bash
sudo ss -ltnp | grep ':8080'
```

不要同时运行原生 `python -m app dev` 和 Docker Compose，它们会争抢端口和飞书事件。

### 群消息没有被识别

```bash
docker compose ps --all
docker compose logs --tail 100 listener
docker compose logs --tail 100 detection-worker
```

同时确认群已经初始化、机器人在群中、应用已发布并获得读取群消息权限。识别默认还会
等待 20 秒上下文窗口。

### 已创建任务但负责人没有收到私聊

让负责人先私聊机器人发送一次“任务列表”，然后检查：

```bash
docker compose logs --tail 100 notification-worker
```

### 管理链接打不开

检查：

```bash
grep '^DEPLOY_PUBLIC_URL=' .env
curl -fsS http://127.0.0.1:8080/health
docker compose logs --tail 100 gateway management-api management-web
```

`DEPLOY_PUBLIC_URL` 必须是浏览器真正能访问的服务器地址，不能在云服务器中继续使用
`127.0.0.1`。

## 16. 明确禁止随意执行的命令

以下操作可能永久删除数据库或大量系统文件。除非已经确认目标、备份和恢复方案，
否则不要执行：

```text
docker compose down --volumes
docker volume rm feishu-task-agent_task-data
docker system prune --volumes
rm -rf ...
git reset --hard
```

特别注意：

- `docker compose down` 默认保留数据库；加上 `--volumes` 会删除数据库卷；
- 不要把 `.env`、数据库、日志或备份上传到 GitHub；
- 不要在两个目录同时启动同一个机器人的 Listener；
- 不要为了处理权限问题而给文件或目录设置 `777`；
- 不要在聊天、截图或工单中公开 App Secret、模型 API Key 或 GitHub Token。

## 17. 最小命令速查表

| 目的 | 命令 |
| --- | --- |
| 进入项目 | `cd ~/feishu-task-agent` |
| 查看版本 | `git log -1 --oneline` |
| 查看代码状态 | `git status` |
| 获取升级 | `git pull --ff-only` |
| 检查 Compose | `docker compose config --quiet` |
| 构建镜像 | `docker compose build` |
| 启动或更新 | `docker compose up -d --wait --wait-timeout 180` |
| 查看服务 | `docker compose ps --all` |
| 查看日志 | `docker compose logs --follow --tail 100` |
| 查看数据库计数 | `docker compose exec -T management-api python -m app db-status` |
| 健康检查 | `curl -fsS http://127.0.0.1:8080/health` |
| 创建备份 | `./scripts/docker-backup.sh` |
| 验证备份 | `./scripts/docker-verify-backup.sh 备份文件路径` |
| 停止容器 | `docker compose stop` |
| 启动已有容器 | `docker compose start` |
| 重启 Linux | `sudo reboot` |

第一次部署时不要求记住这些命令。按照章节顺序执行，并在每一步确认预期结果即可。
