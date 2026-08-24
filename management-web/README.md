# Lab Task Console

飞书群聊任务机器人的管理前端。管理员通过机器人签发的飞书私聊一次性链接登录，
浏览器使用 HttpOnly 会话访问 Python 管理 API，不要求用户输入 Open ID。

## 本地运行

先在项目根目录启动管理 API：

```bash
python -u -m app management-server
```

再在本目录安装依赖并启动前端：

```bash
npm ci
npm run dev
```

默认前端为 `http://127.0.0.1:3000`，后端为 `http://127.0.0.1:8000`。如需覆盖
后端地址，可在本机前端环境中设置 `NEXT_PUBLIC_MANAGEMENT_API_URL`。

## 验证

```bash
npm run lint
npm test
```

完整安装、配置与群聊验收步骤见仓库根目录 [`README.md`](../README.md)。
