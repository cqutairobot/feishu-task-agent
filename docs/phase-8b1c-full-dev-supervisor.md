# Phase 8B-1C：完整开发环境单命令启动

## 目标

在 8B-1B 五后端统一启动基础上，把 Management Web 也纳入同一跨平台监督器：

```bash
python -u -m app dev
```

`dev-backend` 继续保留，用于只诊断五个 Python 后端。

## 完整前置检查

创建任何子进程前统一验证：

- 飞书、模型、任务、生命周期、提醒和管理后台配置；
- SQLite 单进程迁移；
- Management API 的 `127.0.0.1:8000` 可用；
- `node` 与 `npm` 均可执行；
- Node.js 不低于 `22.13`；
- `management-web/package.json` 和 `management-web/node_modules` 存在；
- Management Web 的 `127.0.0.1:3000` 可用。

缺少前端依赖时明确提示执行 `cd management-web && npm ci`。任何端口已有旧进程时
拒绝重复启动，不会先创建一部分服务。

## 六服务监督

- 五个 Python 后端继续使用当前虚拟环境解释器。
- 前端使用 PATH 中已验证的 npm，在 `management-web` 目录执行 `npm run dev`。
- 每个服务使用独立进程组；npm 启动的 Node 子进程也随组关闭。
- 日志使用 `[management-web]` 等前缀汇总到同一终端。
- 任一服务退出会停止其余五项；一次 `Ctrl+C` 统一停止六项。

## 验证状态

13 项开发监督针对性测试与完整 465 项 Python 回归已通过，覆盖前一阶段全部行为以及
Node/npm 解析、最低版本、缺少 `node_modules`、前端工作目录、六服务拓扑、3000
端口与 `dev` CLI 路由。管理前端 lint、生产构建和 2 项页面契约测试通过；README
链接与发布候选敏感标识扫描通过。旧服务占用 8000 时的真实重复启动检查会在零子进程
状态下明确拒绝。

真实验收已通过：单个 `python -m app dev` 终端成功启动六项服务，管理页面和健康
接口可访问，一次 `Ctrl+C` 可统一停止全部六项。Phase 8B-1C 完成。
