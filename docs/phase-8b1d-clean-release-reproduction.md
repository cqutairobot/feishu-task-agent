# Phase 8B-1D：GitHub 发布前干净环境复现

## 发布边界

使用 `git ls-files --cached --others --exclude-standard` 生成首次提交候选，而不是复制
整个开发目录。最终候选包含 223 个文件，明确排除：

- `.env` 和本机真实配置；
- `.venv`、`node_modules`；
- SQLite 数据库、备份和日志；
- `.next`、`.vinext`、`dist` 等构建产物；
- 前端历史嵌套 Git 元数据；
- 含真实飞书群/消息标识的一次性本地评测脚本。

常见密钥格式扫描和候选文件与本机 `.env` 真实值逐项比较均为零匹配。

## 两轮从零复现

在两个独立的系统临时目录中分别从候选清单建立副本，未复制开发目录的依赖、数据或
构建缓存。每轮均重新执行 Python `venv`、`pip install -r requirements.lock` 和
`npm ci`。最终候选验证结果：

- `python -m app check` 成功；
- 无 `.env` 时使用默认 SQLite 路径，从零迁移到当前 head，所有计数为 0；
- 完整 465 项 Python 回归通过；
- 管理前端 lint 通过；
- Vinext 生产构建和标准生产服务器渲染契约 2/2 通过；
- `npm audit` 为 0 个已知漏洞。

## 前端发布清理

第一次干净安装暴露 20 个 npm 安全告警。审计确认大部分来自未被项目使用的 Sites、
Cloudflare Worker、D1 和 Drizzle 脚手架。已删除对应示例、配置和依赖；仍在使用的
Vinext、Vite、React 与 React Server DOM 升级到兼容修复版本，并使用不带
`--force` 的锁文件安全更新处理剩余间接依赖。

新版 Vinext 使用标准 Node 生产服务器，不再导出旧 Cloudflare Worker `fetch()`。
页面契约测试同步改为真实启动 `vinext start` 后通过 HTTP 验证，覆盖更接近后续原生
和容器部署的运行路径。

## G4 前置状态

- 当前根 Git 仓库尚无首次提交和远程地址。
- 本机已安装 GitHub CLI，但尚未登录 GitHub。
- Git 提交用户名和邮箱尚未在此仓库配置。
- 首次推送前需要用户确认 GitHub 用户名、仓库名和 Private/Public，并通过
  `gh auth login` 完成授权。
