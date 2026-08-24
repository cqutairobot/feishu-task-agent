# Phase 8B-1B：跨平台开发后端统一启动

## 目标

macOS、Windows 和 Linux 使用同一个命令启动当前原生开发环境所需的五个 Python
后端，不再要求用户分别打开五个终端。

```bash
python -u -m app dev-backend
```

管理前端暂时保留在第二个终端运行：

```bash
cd management-web
npm run dev
```

## 启动前检查

统一命令在创建子进程前完成：

- 飞书、模型、Detection、Lifecycle、Reminder 和 Management 配置解析；
- `MANAGEMENT_WEB_ENABLED=true` 校验；
- 管理 API 监听地址与端口占用检查；
- 单进程完成 Alembic 数据库升级，避免五个进程并发执行首次迁移。

任何检查失败都不会启动部分服务。

## 进程监督

- 子进程统一使用当前虚拟环境的 `sys.executable`，不会误用系统 Python。
- 五个进程分别为 Listener、Detection Worker、Reminder Worker、Notification
  Worker 和 Management API。
- stdout/stderr 合并读取并增加服务名前缀，便于在一个终端中定位日志来源。
- 每个子进程使用无缓冲 UTF-8 输出。
- 任一必需进程退出，监督器返回非零状态并停止其他进程。
- 用户按一次 `Ctrl+C` 后，POSIX 使用独立进程组发送 SIGINT，Windows 使用独立
  进程组发送 CTRL_BREAK；超时未结束的进程会被强制终止。

## 自动化验收

针对性测试覆盖固定五进程拓扑、当前 Python 解释器、端口冲突、任一进程退出后的
整体停止、`Ctrl+C` 整体停止、CLI 路由、数据库预迁移和管理后台禁用时的失败关闭。
7 项针对性测试与完整 459 项 Python 回归均已通过；发布候选敏感标识扫描和 README
链接检查通过。

真实验收只需确认五个 `started` 行、完整运行提示以及一次 `Ctrl+C` 后的统一停止。
