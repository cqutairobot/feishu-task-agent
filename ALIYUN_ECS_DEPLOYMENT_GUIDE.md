# 飞书任务机器人阿里云 ECS 部署指南

本文记录已经实际验收通过的部署路线：源码从 GitHub 克隆，三个 Docker 镜像在
OrbStack 的 `x86_64/amd64` Linux 环境中构建，压缩后通过 SSH 上传到阿里云 ECS，
服务器使用 `docker load` 导入镜像，再由 Docker Compose 启动全部服务。

这条路线不要求 ECS 从 Docker Hub 拉取项目基础镜像，适合 Docker Hub 在中国大陆
访问缓慢、超时或镜像加速器缺少新标签的情况。

本文不包含购买 ECS 的过程，默认已经拥有一台正在运行的服务器。

## 1. 本次已验证的环境

| 项目 | 已验证配置 |
| --- | --- |
| 云服务器 | 阿里云 ECS，北京地域 |
| 操作系统 | Ubuntu 22.04 64 位 |
| CPU 架构 | `x86_64`，Docker 中称为 `amd64` |
| 规格 | 2 vCPU、4 GiB 内存、40 GiB 系统盘 |
| 登录用户 | `deploy`，可以执行 `sudo` |
| 对外端口 | SSH 22、测试管理入口 8080 |
| 部署方式 | Docker Compose |
| 数据库 | Docker 命名卷中的 SQLite |
| 时区 | `Asia/Shanghai` |

2 vCPU 和 4 GiB 内存足够当前测试使用。正式服务的规格应根据群数量、消息量和模型
调用并发重新评估。

## 2. 先理解代码、镜像、配置和数据

本项目上线后有四类互相独立的内容：

| 内容 | 保存位置 | 更新程序时是否覆盖 |
| --- | --- | --- |
| 源代码 | GitHub 和 ECS 项目目录 | 会随 `git pull` 更新 |
| Docker 镜像 | OrbStack 构建，导入 ECS Docker | 会被新镜像替换 |
| `.env` | 只保存在部署机器上 | 不会随 Git 更新 |
| SQLite 数据 | ECS Docker 命名卷 | 不会随镜像更新 |

因此，重新打包和导入镜像不会自动删除群、成员、任务或提醒。真正危险的是删除 Docker
命名卷，例如执行 `docker compose down --volumes`。

本项目需要三个镜像：

```text
feishu-task-agent-backend:local
feishu-task-agent-web:local
feishu-task-agent-gateway:local
```

后端镜像会被 Listener、三个 Worker、数据库迁移和管理 API 共同使用。

## 3. Mac 上准备 SSH 登录

Mac 自带 `ssh` 和 `scp`，不需要额外安装 Xshell 或 Xftp。`ssh` 用来打开服务器终端，
`scp` 用来上传文件。

先在同一个 Mac 终端设置本次服务器信息。公网 IP 发生变化时只修改这里：

```bash
export FEISHU_ECS_IP=你的服务器公网IP
export FEISHU_ECS_USER=deploy
```

例如 `FEISHU_ECS_IP` 的值应类似 `182.92.234.59`，不要把中文说明原样保留在命令中。
关闭该终端后，这两个临时变量会消失；打开新终端时需要重新设置。

如果还没有专用 SSH 密钥，在 Mac 执行：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen \
  -t ed25519 \
  -f ~/.ssh/feishu_task_agent_aliyun \
  -C "feishu-task-agent-aliyun"
```

公钥可以查看并粘贴到阿里云服务器 `deploy` 用户的
`/home/deploy/.ssh/authorized_keys`：

```bash
cat ~/.ssh/feishu_task_agent_aliyun.pub
```

如果服务器还没有 `deploy` 用户，可以先通过阿里云 Workbench 以 `root` 登录，执行：

```bash
apt update
apt install -y nano
adduser deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
nano /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
```

在 Nano 中粘贴 Mac 刚刚输出的整行公钥，按 `Ctrl+O`、Enter 保存，再按 `Ctrl+X`
退出。为了与本次已验证环境一致，可以允许这个专用账号免密码执行 `sudo`：

```bash
echo 'deploy ALL=(ALL) NOPASSWD:ALL' \
  > /etc/sudoers.d/90-feishu-task-agent-deploy
chmod 440 /etc/sudoers.d/90-feishu-task-agent-deploy
visudo -cf /etc/sudoers.d/90-feishu-task-agent-deploy
```

只有 `visudo` 输出 `parsed OK` 才表示配置有效。免密码 `sudo` 依赖 SSH 私钥安全，
不应把该账号开放为弱密码登录。

私钥是下面这个不带 `.pub` 的文件，不能上传到 GitHub，也不要发给其他人：

```text
~/.ssh/feishu_task_agent_aliyun
```

测试登录：

```bash
ssh \
  -i ~/.ssh/feishu_task_agent_aliyun \
  "${FEISHU_ECS_USER}@${FEISHU_ECS_IP}"
```

登录后执行：

```bash
whoami
pwd
sudo whoami
cat /etc/os-release | head
uname -m
```

预期关键结果：

```text
deploy
/home/deploy
root
Ubuntu 22.04
x86_64
```

## 4. 阿里云安全组

测试阶段至少需要以下入方向规则：

| 端口 | 用途 | 建议来源 |
| --- | --- | --- |
| TCP 22 | SSH 登录 | 自己当前公网 IP |
| TCP 8080 | 临时管理后台 | 自己当前公网 IP，或测试期临时放开 |

飞书消息通过服务器主动发起的 WebSocket 长连接接收，不需要为它开放额外入站端口。
正式配置域名和 HTTPS 后，应改用 443，并关闭公网 8080。

如果为了换电脑方便而临时使用 `0.0.0.0/0`，代表全互联网均可连接该端口。至少应使用
SSH 密钥，不应把 Docker API、数据库端口或管理 API 内部端口直接暴露到公网。

## 5. 在 ECS 安装 Docker Engine 和 Compose

通过 SSH 登录 ECS 后执行：

```bash
sudo apt update
sudo apt install -y ca-certificates curl git gnupg nano

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

sudo systemctl enable --now docker
sudo usermod -aG docker deploy
```

执行完 `usermod` 后退出 SSH 并重新登录，使 `docker` 用户组生效：

```bash
exit
```

回到 Mac 后再次登录，再检查：

```bash
id -nG
docker version
docker compose version
sudo systemctl is-active docker
```

`id -nG` 应包含 `docker`，Docker 状态应为 `active`。本方案的项目镜像由本地上传，
所以即使 ECS 无法直接拉取 Docker Hub，也不妨碍后面的项目启动。

阿里云专属 Docker 镜像加速地址可以帮助测试 `hello-world`，但它不一定包含 Docker
Hub 最新标签，不能把它当成项目部署的唯一来源。

## 6. 在 ECS 克隆 GitHub 源码

仓库当前为私有仓库，使用 GitHub CLI 登录最方便：

```bash
sudo apt update
sudo apt install -y gh
gh auth login --hostname github.com --web
```

云服务器没有图形浏览器时，终端会给出一次性代码和网址。用自己电脑的浏览器打开网址
并完成授权即可。“无法自动打开浏览器”不是登录失败。

继续执行：

```bash
gh auth setup-git
gh auth status

cd ~
gh repo clone \
  cqutairobot/feishu-task-agent \
  feishu-task-agent-release

cd ~/feishu-task-agent-release
git log -1 --oneline
git status
ls Dockerfile compose.yaml gateway/nginx.conf
test ! -e .env && echo ".env correctly absent"
```

如果 GitHub 出现 HTTP/2、GnuTLS 或 TLS 握手错误，可在仓库目录固定使用 HTTP/1.1：

```bash
git config http.version HTTP/1.1
git pull --ff-only
```

终端中只能使用纯文本 URL，不要粘贴
`[https://example.com](https://example.com)` 这样的 Markdown 链接。

## 7. 把 `.env` 上传到 ECS

真实 `.env` 不在 GitHub 中。退出服务器、回到 Mac 终端后，从已经验证可用的本地项目
上传：

```bash
scp \
  -i ~/.ssh/feishu_task_agent_aliyun \
  /Users/Admin/Documents/ChatGPT/agent/.env \
  "${FEISHU_ECS_USER}@${FEISHU_ECS_IP}:/home/deploy/feishu-task-agent-release/.env"
```

重新登录 ECS，进入项目目录：

```bash
cd ~/feishu-task-agent-release
chmod 600 .env
nano .env
```

至少确认以下部署参数，公网 IP 应替换为当前 ECS 的真实地址：

```dotenv
MANAGEMENT_WEB_COOKIE_SECURE=false
DEPLOY_PUBLIC_URL=http://服务器公网IP:8080
DEPLOY_BIND_HOST=0.0.0.0
DEPLOY_HTTP_PORT=8080
DEPLOY_IMAGE_TAG=local
```

这里的 `MANAGEMENT_WEB_COOKIE_SECURE=false` 只适用于当前 HTTP 测试入口。以后配置 HTTPS
后应改为 `true`。

保存后检查非敏感字段：

```bash
stat -c '%a %n' .env
grep -E '^(DEPLOY_|MANAGEMENT_WEB_COOKIE_SECURE=)' .env
docker compose config --quiet && echo "compose config: ok"
docker compose config --services
git status --short
```

`.env` 权限应为 `600`，服务列表应有八项。不要把完整 `.env` 或完整展开的
`docker compose config` 发到公开聊天中。

## 8. 在 OrbStack 构建 AMD64 镜像

阿里云 ECS 是 `x86_64/amd64`，因此本次使用同为 `x86_64/amd64` 的 OrbStack Linux
机器构建。即使 Mac 本身是 Apple Silicon，也不要把未经检查的 `arm64` 镜像上传给这台
ECS。

先确保 OrbStack 中使用的是项目最新源码：

```bash
cd /Users/Admin/Documents/ChatGPT/agent
git status
git pull --ff-only
git log -1 --oneline
```

如果本地存在尚未提交的开发修改，不要直接 `git pull`。先完成测试、提交并推送，再构建
需要发布的版本。

构建镜像：

```bash
docker compose build
```

确认三个镜像及架构：

```bash
docker image ls | grep feishu-task-agent

for image in \
  feishu-task-agent-backend:local \
  feishu-task-agent-web:local \
  feishu-task-agent-gateway:local
do
  docker image inspect "$image" \
    --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
done
```

三个镜像都应显示 `linux/amd64`。

## 9. 打包镜像并上传到 ECS

仍在 OrbStack 中执行：

```bash
docker save \
  feishu-task-agent-backend:local \
  feishu-task-agent-web:local \
  feishu-task-agent-gateway:local \
  | gzip > /Users/Admin/Downloads/feishu-task-agent-images-amd64.tar.gz

git rev-parse HEAD \
  > /Users/Admin/Downloads/feishu-task-agent-images-amd64.version.txt

ls -lh \
  /Users/Admin/Downloads/feishu-task-agent-images-amd64.tar.gz \
  /Users/Admin/Downloads/feishu-task-agent-images-amd64.version.txt
```

`/Users/Admin` 是 OrbStack 与 Mac 的共享目录，所以文件会直接出现在 Mac 的“下载”
文件夹。

回到 Mac 终端，计算上传前校验值：

```bash
shasum -a 256 \
  ~/Downloads/feishu-task-agent-images-amd64.tar.gz
```

上传镜像包和版本记录：

```bash
scp \
  -i ~/.ssh/feishu_task_agent_aliyun \
  ~/Downloads/feishu-task-agent-images-amd64.tar.gz \
  ~/Downloads/feishu-task-agent-images-amd64.version.txt \
  "${FEISHU_ECS_USER}@${FEISHU_ECS_IP}:/home/deploy/"
```

这一步与 Xftp 上传文件的作用相同。镜像包可能较大，上传时间取决于 Mac 的上行带宽。
SSH 偶尔断开时重新上传即可；以后更新频繁后可再改为支持分层传输的阿里云 ACR。

## 10. 在 ECS 导入镜像并启动

登录 ECS 后先核对上传后的文件校验值：

```bash
sha256sum \
  /home/deploy/feishu-task-agent-images-amd64.tar.gz

cat /home/deploy/feishu-task-agent-images-amd64.version.txt
git -C /home/deploy/feishu-task-agent-release rev-parse HEAD
```

Linux 的 `sha256sum` 应与 Mac 的 `shasum -a 256` 一致。版本文件应与服务器 Git 提交
一致；如果不一致，应先确认究竟准备部署哪个版本。

导入三个镜像：

```bash
gunzip -c \
  /home/deploy/feishu-task-agent-images-amd64.tar.gz \
  | docker load

docker image ls | grep feishu-task-agent
```

应看到：

```text
Loaded image: feishu-task-agent-backend:local
Loaded image: feishu-task-agent-web:local
Loaded image: feishu-task-agent-gateway:local
```

启动前必须停止其他电脑或 OrbStack 中使用同一飞书 App ID 的 Listener。同一个机器人
不应同时运行两套生产 Listener，否则可能出现重复处理或连接行为难以判断。

首次启动或更新启动：

```bash
cd ~/feishu-task-agent-release

docker compose up \
  -d \
  --no-build \
  --force-recreate \
  --wait \
  --wait-timeout 180
```

参数含义：

- `-d`：后台运行，退出 SSH 后服务不会停止；
- `--no-build`：只使用刚导入的镜像，不访问 Docker Hub 构建；
- `--force-recreate`：确保已有容器切换到新导入的镜像；
- `--wait`：等待长期服务通过健康检查；
- `--wait-timeout 180`：最多等待 180 秒。

检查结果：

```bash
docker compose ps --all

curl -fsS http://127.0.0.1:8080/health
echo

docker compose exec -T management-api python -m app db-status
```

正确状态：

- `migrate` 为 `Exited (0)`，代表数据库迁移正常完成；
- 其余七个服务为 `Up ... (healthy)`；
- 健康接口返回 `{"status":"ok","administrator_management":true}`；
- `db-status` 能输出非敏感数据计数。

浏览器访问：

```text
http://服务器公网IP:8080
http://服务器公网IP:8080/health
```

首次启动会创建新的 Docker 命名卷 `feishu-task-agent_task-data`，因此默认是一个空的云端
数据库。本流程不会自动迁移 OrbStack 中的聊天和任务数据。

## 11. 飞书端验收

容器健康后按下面顺序进行最小验收：

1. 当前群主发送：`@机器人 初始化本群`；
2. 成员发送：`@机器人 绑定姓名：自己的真实姓名`；
3. 负责人先私聊机器人发送：`任务列表`；
4. 群里发送明确任务，例如：`王天，请在明天18:00前完成阿里云部署验收记录。`；
5. 等待约 20 秒上下文窗口和模型处理；
6. 负责人应收到新任务私聊；
7. 管理员私聊机器人发送：`管理后台`；
8. 登录链接应指向 ECS 公网入口并能打开后台。

## 12. 日常启动、关闭和查看

所有命令都在 ECS 项目目录执行：

```bash
cd ~/feishu-task-agent-release
```

查看状态：

```bash
docker compose ps --all
```

查看最近日志：

```bash
docker compose logs --tail 100
docker compose logs --tail 100 listener
docker compose logs --tail 100 detection-worker
```

停止但保留容器和数据：

```bash
docker compose stop
```

重新启动：

```bash
docker compose start
```

删除容器和网络、保留数据库卷：

```bash
docker compose down
```

重新创建：

```bash
docker compose up \
  -d \
  --no-build \
  --wait \
  --wait-timeout 180
```

Compose 中长期服务使用 `restart: unless-stopped`，Docker 也已设置为随系统启动。ECS
重启后正常情况下容器会自动恢复。重启后可检查：

```bash
docker compose ps --all
curl -fsS http://127.0.0.1:8080/health
echo
```

## 13. 本地修复代码后的重新部署

### 13.1 本地开发机

1. 修改并完成自动化测试；
2. 提交并推送到 GitHub；
3. 在 OrbStack 切到需要发布的提交；
4. 重新执行本文第 8 节构建镜像；
5. 重新执行第 9 节生成并上传镜像包。

不要只上传源码而继续使用旧镜像，因为容器内运行的是镜像中的代码，不会自动读取
服务器项目目录里的 Python 或前端源码。

### 13.2 ECS 更新前备份数据库

```bash
cd ~/feishu-task-agent-release
./scripts/docker-backup.sh
```

只有输出 `backup_integrity: ok`、备份路径和 SHA-256 才算成功。

### 13.3 ECS 更新源码和镜像

```bash
cd ~/feishu-task-agent-release
git status
git pull --ff-only
git log -1 --oneline
```

然后执行本文第 10 节的校验、`docker load` 和带 `--force-recreate` 的启动命令。
Docker 命名卷与镜像独立，因此正常更新不会清空 SQLite 数据。

更新后检查：

```bash
docker compose ps --all
curl -fsS http://127.0.0.1:8080/health
echo
docker compose exec -T management-api python -m app db-status
```

## 14. 常见问题

### ECS 无法从 Docker Hub 拉取镜像

本方案不要求 ECS 构建项目镜像。不要在 ECS 执行 `docker compose build`，改为在
OrbStack 构建、`docker save`、`scp` 上传和 `docker load`。

### `python:... not found`

阿里云 Docker Hub 镜像加速器可能没有同步对应的新标签。这不代表项目 Dockerfile
错误。使用本文的本地镜像上传路线即可。

### 上传过程中 SSH 断开

重新执行 `scp`。上传完成后必须比较 Mac 与 ECS 的 SHA-256，确认文件没有损坏。

### `address already in use`

检查 8080：

```bash
sudo ss -ltnp | grep ':8080'
docker compose ps --all
```

不要同时在同一台服务器运行原生 `python -m app dev` 和 Docker Compose。

### 飞书机器人没有回复

```bash
docker compose ps --all
docker compose logs --tail 100 listener
```

同时确认 OrbStack 或其他服务器上没有运行同一 App ID 的 Listener。

### 创建任务但没有收到私聊

让负责人先私聊机器人发送一次“任务列表”，再检查：

```bash
docker compose logs --tail 100 notification-worker
```

### 管理后台打不开

```bash
grep -E '^(DEPLOY_|MANAGEMENT_WEB_COOKIE_SECURE=)' .env
curl -fsS http://127.0.0.1:8080/health
docker compose logs --tail 100 gateway management-api management-web
```

还要确认阿里云安全组已经允许 TCP 8080。

## 15. 禁止随意执行的命令

以下命令可能永久删除数据库或大量数据，不要在没有明确备份和恢复方案时执行：

```text
docker compose down --volumes
docker volume rm feishu-task-agent_task-data
docker system prune --volumes
rm -rf ...
git reset --hard
```

尤其注意：

- `docker compose down` 默认保留数据库；
- `docker compose down --volumes` 会删除数据库卷；
- `.env`、数据库和备份均不能提交到 GitHub；
- 不要在两个位置同时运行同一个飞书机器人的 Listener；
- 更新镜像前先创建 SQLite 一致性备份。
