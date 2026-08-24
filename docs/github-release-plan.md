# GitHub 首次发布计划

本计划先建立一个可以从全新克隆目录复现的原生开发版本，暂不引入 Docker。

## G1：仓库与安装入口整理 — 已实现，等待用户检查 README

- 将根 README 改为面向使用者的 macOS、Windows 和 Linux 安装入口。
- 开发历史保留在 `docs/`，不混入安装主流程。
- 排除 `.env`、密钥、真实飞书标识、SQLite、日志、虚拟环境和构建产物。
- 修正依赖命令中的平台专用写法。

自动检查结果：候选发布文件未匹配本机 `.env` 中的真实 App ID、Secret、API Key、
群 ID 或管理员 Open ID；README 本地链接有效；完整 Python 回归 452 项通过；管理
前端 lint、生产构建和 2 项页面契约测试通过。

## G2：跨平台统一启动 — 已完成真实验收

- `python -m app dev` 统一启动 Listener、三个 Worker、管理 API 和管理前端；
  `dev-backend` 保留为只运行五个 Python 后端的诊断命令。
- 启动前检查 `.env`、依赖、端口和数据库；失败时输出可操作的错误说明。
- `Ctrl+C` 时优雅停止所有子进程，避免遗留重复进程。
- 启动前验证 Node/npm、最低 Node 版本、前端依赖以及 `8000/3000` 端口。

真实验收确认 `python -m app dev` 可启动全部六项服务、页面和 API 可访问，并可用
一次 `Ctrl+C` 统一停止。

## G3：发布前验证 — 已完成

- 完整 Python 回归、前端 lint、测试和生产构建通过。
- 从不包含 `.venv`、数据库和构建产物的干净目录按 README 重装。
- 检查 Git 待提交清单和敏感信息扫描结果。

最终 223 个候选文件已完成两轮独立临时目录重装。空数据库迁移、465 项 Python
回归、前端 lint/生产构建/2 项服务器页面契约均通过；清理无用前端脚手架并升级兼容
补丁后，`npm audit` 为 0。详情见
[`phase-8b1d-clean-release-reproduction.md`](phase-8b1d-clean-release-reproduction.md)。

## G4：GitHub 首次推送 — 等待账号与仓库选择

- 由用户确认 GitHub 用户名、仓库名和 Private/Public。
- 用户通过浏览器或 `gh auth login` 授权，不在聊天中提供密码或 Token。
- 建立首次提交并推送，随后把 README 的克隆占位地址替换为真实地址。

## G5：GitHub 干净克隆验收

- 从 GitHub 克隆到新目录，只创建新的 `.env`。
- 完全按照 README 安装、启动并完成真实飞书核心验收。
- 使用 GitHub Actions 的 Windows Runner 验证 Windows 安装和自动化测试。
