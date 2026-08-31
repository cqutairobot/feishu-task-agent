# 缺陷记录与修复方案

本文档记录一次全量代码审查中发现的缺陷，并为每条给出具体修复方案。审查未修改任何源码。

审查时的自动化验证结果：Python `unittest` 478 项全部通过、前端 ESLint 通过、前端生产构建通过、前端渲染测试 2 项通过。下列缺陷集中在并发竞态、跨组件状态同步、分页交互、通知游标推进和多租户隔离，现有测试未覆盖这些场景。

## 缺陷总览

| 编号 | 严重程度 | 模块 | 摘要 | 验证状态 |
| --- | --- | --- | --- | --- |
| [1](#1) | 高 | 管理后台前端 | 切换群聊后旧任务抽屉仍可编辑，跨群误操作 | 阅读代码确认 |
| [2](#2) | 高 | 管理后台前端 | 修改任务后编辑器保留旧值，可重复提交过期数据 | 阅读代码确认 |
| [3](#3) | 中高 | 管理后台前端 | 请求无竞态防护，慢响应覆盖新状态 | 阅读代码确认 |
| [4](#4) | 中高 | 通知同步 | 生命周期游标跳过越界事件后永久推进，通知丢失 | 阅读代码确认 |
| [5](#5) | 中 | 通知 worker | 发送成功后写审计失败无兜底，可能重复私聊 | 阅读代码确认 |
| [6](#6) | 中 | 管理后台前端 | 群设置两套重复状态，未保存的通知对象选择被静默丢弃 | 阅读代码确认 |
| [7](#7) | 中 | 管理后台前端 | 合并目标仅限当前分页，跨页重复任务无法合并 | 阅读代码确认 |
| [8](#8) | 中 | 管理后台前端 | 变更后未收敛页码，可停留在空的无效分页 | 阅读代码确认 |
| [9](#9) | 高（条件性） | 数据库模型 | 用户主键未按租户隔离，多租户下身份串号 | 取决于部署形态 |
| [10](#10) | 中（可移植性） | 备份脚本 | 依赖 GNU `sha256sum`，旧版 macOS 无此命令 | 本机存在，旧系统会失败 |
| [11](#11) | 低 | 管理后台前端 | 午夜截止时间理论上可格式化为 `T24:xx` | 本机未复现 |
| [12](#12) | 低 | 管理后台前端 | `refreshChatSummary` 使用全局 `chatId`，可能与来源群不一致 | 阅读代码确认 |

---

<a id="1"></a>
## 1. 切换群聊后旧任务抽屉仍可编辑（高）

**位置**

- `management-web/app/dashboard-client.tsx:319`（群聊选择器 `onChange`）
- `management-web/app/dashboard-client.tsx:323`（抽屉渲染）
- `management-web/app/dashboard-client.tsx:190-204`（`mutateTask`）

**问题**

群聊选择器的 `onChange` 重置了 `loading`、`chatId`、`page` 和两条提示文案，但没有清空 `detail`：

```tsx
setLoading(true); setChatId(nextChatId); setPage(1);
setAdministrationNotice(""); setSettingsNotice("");
```

抽屉的显示条件只依赖 `detail`，与当前 `chatId` 无关。

`mutateTask` 的请求路径使用任务自身的群聊，但之后的刷新用的是全局 `chatId`：

```tsx
await apiRequest(`/api/chats/${detail.task.chat_id}/tasks/${detail.task.task_id}/${resource}`, ...)
// ...
apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`),
apiRequest<TaskPage>(`/api/chats/${chatId}/tasks?${params}`),
```

**复现步骤**

1. 选择群聊 A，点击任意任务打开详情抽屉。
2. 用顶部选择器切换到群聊 B，抽屉不关闭。
3. 在抽屉内修改标题、负责人、截止时间或执行状态操作。

**结果**：修改实际作用于群聊 A 的任务，而列表和统计刷新为群聊 B 的数据。管理员在“当前是 B 群”的认知下改动了 A 群任务，且界面数据来源混杂。

**修复方案**

切换群聊时关闭抽屉，并让刷新绑定到被修改任务所属群聊：

```tsx
// 选择器 onChange 中追加
setDetail(null);
setCreating(false);

// mutateTask 中
const taskChatId = detail.task.chat_id;
const updated = await apiRequest<TaskDetail>(
  `/api/chats/${taskChatId}/tasks/${detail.task.task_id}/${resource}`, ...
);
if (taskChatId !== chatId) { setDetail(updated); return; }
```

即：跨群时只更新抽屉本身，不用别群数据覆盖当前列表。同时建议给抽屉加 `key={detail.task.task_id}`，配合缺陷 2 的修复。

---

<a id="2"></a>
## 2. 修改任务后编辑器保留旧值（高）

**位置**

- `management-web/app/dashboard-client.tsx:537`、`543`（`TaskDrawer` 截止时间）
- `management-web/app/dashboard-client.tsx:626`（`TitleEditor`）
- `management-web/app/dashboard-client.tsx:649`（`AssigneeEditor`）
- `management-web/app/dashboard-client.tsx:196`（`setDetail(updated)`）

**问题**

父组件在修改成功后执行 `setDetail(updated)`，但子编辑器的本地状态只在挂载时初始化，且 `task` 变化时没有同步：

```tsx
const [value, setValue] = useState(task.title);                              // TitleEditor
const [selected, setSelected] = useState(task.assignees.map((i) => i.open_id)); // AssigneeEditor
const [deadlineValue, setDeadlineValue] = useState(formatShanghaiInput(detail.task.deadline));
```

组件在同一位置持续挂载，`useState` 的初始值不会因 props 变化而重算。

**复现步骤**

1. 打开任务详情，把标题改为“新标题”并保存。
2. 服务端已返回新标题，抽屉顶部标题（直接读 `detail.task.title`）已更新。
3. 但标题输入框仍显示旧标题。
4. 再次点击“保存标题”。

**结果**：输入框、`unchanged` 判断、按钮禁用状态全部基于过期数据。用户可能把旧标题重新提交回去（`requestId` 已被清空，因此这是一次新的写入而非幂等重放），或收到难以理解的 409。负责人复选框和截止时间输入框同理。

**修复方案**

最小且稳妥的做法是让子组件随任务版本重新挂载。`TaskDetail` 含 `updated_at`，可用作版本标识：

```tsx
<TitleEditor key={`title-${detail.task.task_id}-${detail.task.updated_at}`} ... />
<AssigneeEditor key={`assignee-${detail.task.task_id}-${detail.task.updated_at}`} ... />
```

截止时间状态在 `TaskDrawer` 内部，可对整个抽屉内容加 key，或显式同步：

```tsx
useEffect(() => {
  setDeadlineValue(formatShanghaiInput(detail.task.deadline));
}, [detail.task.deadline, detail.task.updated_at]);
```

注意成功提示需要在重挂载后保留或改由父组件承载，否则提示会随重挂载消失。

---

<a id="3"></a>
## 3. 请求无竞态防护，慢响应覆盖新状态（中高）

**位置**

- `management-web/app/dashboard-client.tsx:110-121`（dashboard 与任务列表）
- `management-web/app/dashboard-client.tsx:163-172`（`openTask`）
- `management-web/app/dashboard-client.tsx:123-146`（成员与设置加载）

**问题**

所有请求都没有 `AbortController`，回调里也不校验发起时的上下文是否仍然有效：

```tsx
Promise.all([...]).then(([summary, tasks]) => {
  setDashboard(summary); setTaskPage(tasks); setError("");
})
```

effect 的清理函数只清了 `setTimeout`，没有取消在途请求。

**复现步骤**

1. 选择群聊 A，触发 dashboard 与任务列表请求。
2. 在响应返回前切换到群聊 B。
3. B 的响应先到，界面显示 B。
4. A 的慢响应随后到达，仍然执行 `setDashboard` / `setTaskPage`。

**结果**：顶部选择器显示 B，而统计卡片和任务列表是 A 的数据。快速连点两个任务时，先发起的详情请求也可能覆盖后打开的任务。这与缺陷 1 叠加时风险更高：抽屉内容可能属于第三个上下文。

**修复方案**

为每类请求引入单调递增的请求序号，只接受最新一次的结果：

```tsx
const dashboardRequest = useRef(0);

useEffect(() => {
  if (!chatId) return;
  const token = ++dashboardRequest.current;
  const controller = new AbortController();
  Promise.all([
    apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`, { signal: controller.signal }),
    apiRequest<TaskPage>(`/api/chats/${chatId}/tasks?${params}`, { signal: controller.signal }),
  ])
    .then(([summary, tasks]) => {
      if (token !== dashboardRequest.current) return;
      // ...
    })
    .catch((reason) => {
      if (controller.signal.aborted) return;
      handleFailure(reason, "群聊数据加载失败，请稍后刷新。");
    })
    .finally(() => { if (token === dashboardRequest.current) setLoading(false); });
  return () => controller.abort();
}, [chatId, filter, query, page, handleFailure]);
```

`apiRequest` 需要透传 `signal`（当前通过 `...init` 已经支持）。`openTask`、`loadAdministration`、`loadSettings` 同样处理。

---

<a id="4"></a>
## 4. 生命周期通知游标跳过越界事件后永久推进（中高）

**位置**

- `app/notifications/repository.py:390-395`（构造 `tasks_by_id`）
- `app/notifications/repository.py:511-513`（跳过越界事件）
- `app/notifications/repository.py:615-623`（推进游标）

**问题**

任务集合按允许的群聊过滤：

```python
tasks_by_id = {
    task.id: task
    for task in tasks
    if not admitted_chat_ids or task.chat_id in admitted_chat_ids
}
```

事件循环遇到不在集合内的任务时直接跳过：

```python
task = tasks_by_id.get(event.task_id)
if task is None:
    continue
```

但循环结束后，游标无条件推进到全局最新事件：

```python
newest_event_id = session.scalar(
    select(TaskLifecycleEvent.id)
    .where(TaskLifecycleEvent.id > state.last_lifecycle_event_id)
    .order_by(TaskLifecycleEvent.id.desc())
    .limit(1)
)
if newest_event_id is not None:
    state.last_lifecycle_event_id = newest_event_id
```

被跳过的事件 id 小于新游标，此后永远不会再进入查询范围。

**复现步骤**

1. 配置 `allowed_chat_ids`，使群聊 A 不在允许范围内。
2. 群聊 A 的任务发生完成、取消、延期、改名、改负责人、撤销或恢复。
3. 运行通知同步：该事件被跳过，游标推进到其之后。
4. 把群聊 A 加入允许范围，再次同步。

**结果**：该事件对应的管理员通知与共同负责人通知永久缺失。这是持久化的通知丢失，不会被后续同步自愈。同样的问题也适用于任何在同步瞬间不在 `tasks_by_id` 中的任务。

**修复方案**

不要越过尚未处理的事件。把游标推进到“首个被跳过事件之前”：

```python
processed_through = state.last_lifecycle_event_id
for event in events:
    task = tasks_by_id.get(event.task_id)
    if task is None:
        break          # 保留该事件及其后所有事件，等待下次同步
    # ... 现有处理逻辑（merge 分支属于已处理，不应 break）
    processed_through = event.id

if processed_through > state.last_lifecycle_event_id:
    state.last_lifecycle_event_id = processed_through
    state.updated_at = synced_at
```

注意 `merge` 分支（`repository.py:514-517`）是“有意不产生通知”，属于已处理，应继续推进游标，不能与越界跳过混为一谈。

若担心某个群长期越界导致游标停滞、每次同步都重复扫描，可改为在状态表中额外记录被跳过的事件 id 集合，游标正常推进，下次同步时单独补扫这些 id。`_ensure_notification` 已按 `(task_id, kind, recipient_open_id, dedupe_key)` 去重，重复扫描不会产生重复通知。

---

<a id="5"></a>
## 5. 通知发送成功后写审计失败无兜底（中）

**位置**

- `app/notifications/worker.py:96-112`
- 对照实现：`app/reminders/worker.py:120-157`

**问题**

投递有异常处理，但随后的 `mark_sent` 没有：

```python
try:
    receipt = self._sender.deliver(lease)
except TaskNotificationDeliveryError as exc:
    return self._record_failure(lease, error_code=exc.code, error=exc)
except Exception as exc:
    return self._record_failure(lease, error_code="delivery_error", error=exc)
self._repository.mark_sent(lease, ...)     # 无保护
```

提醒 worker 已经正确处理了同一场景：

```python
try:
    sent = self._repository.mark_sent(lease, ...)
except Exception as exc:
    return self._record_failure(lease, error_code="delivery_audit_error", error=exc)
```

提醒 worker 还额外捕获了 `KeyboardInterrupt` 并记录 `worker_interrupted`，通知 worker 也没有。

**复现步骤**

1. 飞书已成功接收并投递该通知。
2. `mark_sent` 因数据库锁竞争或连接中断等瞬时故障抛出异常。
3. 异常穿透 `run_once`，通知行仍处于 `leased`。
4. 租约到期后被恢复流程重新调度。

**结果**：同一条通知可能被再次投递，用户收到重复私聊；异常也可能终止 worker 循环，取决于外层调用方式。行为与提醒 worker 不一致，属于明显的实现遗漏。

**修复方案**

对齐提醒 worker：

```python
try:
    receipt = self._sender.deliver(lease)
except KeyboardInterrupt as exc:
    self._record_failure(lease, error_code="worker_interrupted", error=exc)
    raise
except TaskNotificationDeliveryError as exc:
    return self._record_failure(lease, error_code=exc.code, error=exc)
except Exception as exc:
    return self._record_failure(lease, error_code="delivery_error", error=exc)

try:
    self._repository.mark_sent(lease, ...)
except Exception as exc:
    return self._record_failure(lease, error_code="delivery_audit_error", error=exc)
```

补充说明：`app/notifications/repository.py:1057-1073` 的 `_require_owned_lease` 不比较 `active_at` 与租约到期时间，这与 `app/reminders/repository.py:479-499` 行为一致，后者已用注释说明这是有意设计（到期只是允许被回收，不立即剥夺所有权，由 `worker_id` 与递增的 `attempt` 拒绝旧 worker）。因此这一点不算缺陷，建议在通知侧补上同样的注释，避免后续误改。

---

<a id="6"></a>
## 6. 群设置两套重复状态，未保存的选择被静默丢弃（中）

**位置**

- `management-web/app/dashboard-client.tsx:343-373`（`SettingsWorkspace`）
- `management-web/app/dashboard-client.tsx:376-472`（`BaseSettingsWorkspace`）

**问题**

管理员通知配置被两个组件各自持有一份状态。`SettingsWorkspace` 用 `mode` / `selected` 渲染实际的选择控件；`BaseSettingsWorkspace` 另外维护 `administratorNotificationMode` / `administratorNotificationOpenIds`，自身不渲染任何对应控件，却在提交时把它们一并发送：

```tsx
administrator_notification_mode: administratorNotificationMode,
administrator_notification_open_ids:
  administratorNotificationMode === "all" ? [] : normalizedAdministratorNotificationOpenIds,
```

两者都通过 `useEffect` 从同一个 `settings` prop 初始化，彼此不通信。

**复现步骤**

1. 在下方“管理员通知对象”面板选择“指定管理员”并勾选管理员 A，暂不保存。
2. 修改上方“群设置”中的任意提醒项。
3. 保存上方表单。

**结果**：上方表单提交它自己那份仍为旧值的通知配置；保存成功后 `settings` 更新，触发下方面板的 `useEffect`，把刚才的勾选重置回保存值。用户的选择被静默丢弃，界面没有任何提示。

需要说明的是，落库值仍等于原有值，因此这不是数据库层面的错误写入，而是编辑中数据丢失与状态重复带来的可维护性问题。后续若有人给上方表单补上通知控件，就会立刻升级为真正的互相覆盖。

**修复方案**

单一数据源。管理接口已支持部分更新（`app/management/web.py:488-539` 中各字段均可缺省），所以让每个表单只提交自己拥有的字段即可：

```tsx
// BaseSettingsWorkspace：删除 administratorNotificationMode / OpenIds 两个 state、
// changed 中的对应比较项、administratorNotificationValid，以及 submit 中的两个字段。
```

通知对象仅由 `SettingsWorkspace` 负责提交。这样两个表单字段互不重叠，也不再需要跨组件同步。若希望统一为一次保存，则把通知控件移入同一个 `<form>`，只保留一套状态。

---

<a id="7"></a>
## 7. 合并目标仅限当前分页（中）

**位置**

- `management-web/app/dashboard-client.tsx:323`（`mergeTargets` 来源）
- `management-web/app/dashboard-client.tsx:596-603`（候选过滤）

**问题**

合并候选直接取自当前列表页：

```tsx
<TaskDrawer detail={detail} mergeTargets={taskPage?.tasks ?? []} ... />
```

`taskPage.tasks` 受 `TASK_PAGE_SIZE`（每页 10 条）和当前筛选条件限制。候选再过滤一次状态与群聊后，若为空则整个合并表单不渲染：

```tsx
if (!sourceMergeable || !options.length) return null;
```

**复现步骤**

1. 让重复任务落在第 2 页，或当前筛选为“待审核”而目标任务是“待办”。
2. 打开第 1 页某个重复任务的详情。

**结果**：下拉框中没有目标任务，合并入口整体消失。后端允许的合并操作在界面上无法完成，且没有任何说明，用户只能反复切换筛选与分页去碰运气。

**修复方案**

打开抽屉时单独拉取合并候选，不复用列表分页：

```tsx
const [mergeTargets, setMergeTargets] = useState<Task[]>([]);

async function openTask(task: Task) {
  const params = new URLSearchParams({ limit: "100", offset: "0" });
  ["pending", "todo", "overdue", "done"].forEach((s) => params.append("status", s));
  const [taskDetail, memberItems, candidates] = await Promise.all([
    apiRequest<TaskDetail>(`/api/chats/${task.chat_id}/tasks/${task.task_id}`),
    apiRequest<Member[]>(`/api/chats/${task.chat_id}/members`),
    apiRequest<TaskPage>(`/api/chats/${task.chat_id}/tasks?${params}`),
  ]);
  setDetail(taskDetail); setMembers(memberItems); setMergeTargets(candidates.tasks);
}
```

`limit` 上限为 100（`app/management/queries.py` 校验 `1 <= limit <= 100`）。任务量更大的群需要在合并表单内提供按编号或标题搜索，而不是一次性列出全部。

---

<a id="8"></a>
## 8. 变更后未收敛页码（中）

**位置**

- `management-web/app/dashboard-client.tsx:114-117`（列表加载时的页码收敛）
- `management-web/app/dashboard-client.tsx:197-203`（`mutateTask` 刷新）
- `management-web/app/dashboard-client.tsx:259-265`（`createTask` 刷新）

**问题**

常规加载路径会收敛页码：

```tsx
const resolvedPage = tasks.total_pages > 0 ? Math.min(page, tasks.total_pages) : 1;
if (resolvedPage !== page) { setPage(resolvedPage); writeTaskViewState({ page: resolvedPage }); }
```

但修改和创建后的刷新直接赋值，未做同样处理：

```tsx
setDashboard(summary); setTaskPage(tasks); setChats(chatItems);
```

由于 `page`、`filter`、`query` 都没变化，加载 effect 不会重新触发，收敛逻辑不会执行。

**复现步骤**

1. 任务共 11 条，每页 10 条，翻到第 2 页（仅 1 条）。
2. 取消或撤销该任务，使其离开当前筛选。
3. 刷新返回 `total_pages = 1`，而 React 中 `page` 仍为 2。

**结果**：列表为空并显示“当前筛选条件下没有任务”，分页栏因 `total_pages` 变为 1 而隐藏，用户无法直接回到第 1 页，只能改筛选或刷新页面。

**修复方案**

抽出共用的收敛函数，在所有写入 `taskPage` 的位置调用：

```tsx
function applyTaskPage(tasks: TaskPage) {
  const resolvedPage = tasks.total_pages > 0 ? Math.min(page, tasks.total_pages) : 1;
  if (resolvedPage !== page) { setPage(resolvedPage); writeTaskViewState({ page: resolvedPage }); }
  setTaskPage(tasks);
}
```

更简洁的替代方案是引入 `refreshToken` 状态并加入加载 effect 的依赖，变更后只做 `setRefreshToken((n) => n + 1)`，让唯一的加载路径统一处理收敛。这样也顺带消除 `mutateTask` 与 `createTask` 中重复的三段式刷新代码。

---

<a id="9"></a>
## 9. 用户主键未按租户隔离（高，取决于部署形态）

**位置**

- `app/database/models.py:320-326`（`User` 定义）
- `app/database/repository.py:556-582`（`_upsert_user`）
- 对照：`app/database/models.py:436-447`（`Message` 的唯一约束）

**问题**

`Message` 的唯一性按租户划分：

```python
UniqueConstraint("tenant_key", "event_id", name="uq_messages_tenant_event"),
UniqueConstraint("tenant_key", "message_id", name="uq_messages_tenant_message"),
```

但 `User` 只以 `open_id` 为主键，`tenant_key` 只是普通列：

```python
open_id: Mapped[str] = mapped_column(String(128), primary_key=True)
tenant_key: Mapped[str] = mapped_column(String(128), nullable=False)
```

`_upsert_user` 按 `open_id` 冲突更新，并直接覆盖 `tenant_key`：

```python
statement.on_conflict_do_update(
    index_elements=[User.open_id],
    set_={... "tenant_key": statement.excluded.tenant_key, ...},
)
```

飞书 `open_id` 是租户内标识，不保证跨租户全局唯一。`chat_member_aliases`、`chat_memberships`、`tasks`、`task_assignees` 等表都以 `users.open_id` 为外键，因此它们也隐含地假设了全局唯一。

**复现步骤（多租户部署下）**

1. 租户 A 的消息写入用户 `open_id = user_x`。
2. 租户 B 也出现 `open_id = user_x` 的消息。
3. `_upsert_user` 命中同一行，把 `tenant_key` 和 `name` 改成 B 的值。

**结果**：租户 A 的消息发送者、任务负责人、群成员关系、姓名别名以及后台权限判定全部解析到租户 B 的身份数据。

**修复方案**

若需要支持多租户，把用户身份改为按租户限定：

1. 新增 Alembic 迁移，将 `users` 主键改为复合主键 `(tenant_key, open_id)`。
2. 所有引用 `users.open_id` 的外键改为携带 `tenant_key` 的复合外键。
3. `_upsert_user` 的 `index_elements` 改为 `[User.tenant_key, User.open_id]`，并从 `set_` 中移除 `tenant_key`。
4. 补充回归测试：同一 `open_id` 在两个 `tenant_key` 下必须产生两行独立记录，且互不污染姓名与关联数据。

这是一次涉及多表外键的破坏性变更，需要停机窗口和数据迁移演练。

若确定“一个数据库只服务一个租户”，则当前实现没有实际风险。此时建议改为显式约束而非隐含假设：在配置中固定期望的 `tenant_key`，在 `_upsert_user` 或消息入库处校验来访 `tenant_key` 与之一致，不一致即拒绝并告警。并在 `User` 模型处加注释说明该前提，避免日后被误当作多租户方案扩展。

---

<a id="10"></a>
## 10. 备份脚本依赖 GNU `sha256sum`（中，可移植性）

**位置**

- `scripts/docker-backup.sh:108`
- `scripts/docker-verify-backup.sh:68`

**问题**

两处均调用 GNU 风格命令：

```sh
host_sha256="$(sha256sum "${staging_path}" | awk '{print $1}')"
source_sha256="$(sha256sum "${backup_path}" | awk '{print $1}')"
```

**验证结果修正**：在本机（macOS 26.5.2）`/sbin/sha256sum` 确实存在并可用，因此当前环境不会失败。但较旧的 macOS 只提供 `/usr/bin/shasum`，没有 `sha256sum`。README 与 Linux 部署指南把这两个脚本描述为通用备份流程，在旧版 macOS 主机上会在校验步骤中断。

**影响**：`docker-backup.sh` 中容器内快照与拷贝均已完成，脚本却在第 108 行校验时以非零码退出，运维会认为备份失败；`docker-verify-backup.sh` 在第 68 行就中止，恢复校验完全无法执行。

**修复方案**

加一个可移植的哈希函数，在两个脚本中复用：

```sh
file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "Neither sha256sum nor shasum is available." >&2
    return 1
  fi
}
```

调用处改为 `host_sha256="$(file_sha256 "${staging_path}")"`。另一种方案是统一在容器内用 Python 计算两侧哈希，与脚本中已有的容器侧实现保持一致，从而完全摆脱宿主机命令差异。

---

<a id="11"></a>
## 11. 午夜截止时间的 `T24:xx` 风险（低）

**位置**

- `management-web/app/dashboard-client.tsx:736-741`

**问题**

`formatShanghaiInput` 用 `Intl.DateTimeFormat` 配 `hour12: false` 拼装 `datetime-local` 的值：

```tsx
return `${item("year")}-${item("month")}-${item("day")}T${item("hour")}:${item("minute")}`;
```

历史上部分 ICU/V8 版本在 `hour12: false` 下会把午夜输出为 `24` 而非 `00`。若发生，`datetime-local` 无法接受 `T24:30`，输入框会空白，或提交出 `...T24:30:00+08:00` 这样的非法值。

**验证结果**：在本机 Node v26.7.0 上未复现。`2026-08-26T16:00:00Z` 与 `2026-08-26T16:30:00Z` 分别正确输出 `2026-08-27T00:00` 和 `2026-08-27T00:30`。因此这不是当前环境的实际缺陷，仅为跨运行时的健壮性隐患。

**修复方案**

明确用 `hourCycle` 并对结果做归一化：

```tsx
const parts = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
  day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
}).formatToParts(new Date(value));
const hour = item("hour") === "24" ? "00" : item("hour");
```

`hourCycle: "h23"` 比 `hour12: false` 语义更明确；locale 改用 `en-GB` 可避免中文 locale 的格式差异（此处只取 parts，不影响展示文案）。日期部分由格式化器给出，午夜归一化为 `00` 时无需自行进位。

---

<a id="12"></a>
## 12. `refreshChatSummary` 使用全局 `chatId`（低）

**位置**

- `management-web/app/dashboard-client.tsx:185-188`
- 调用点：`management-web/app/dashboard-client.tsx:280`

**问题**

```tsx
async function refreshChatSummary() {
  const [items, summary] = await Promise.all([
    apiRequest<Chat[]>("/api/chats"),
    apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`),
  ]);
  setChats(items); setDashboard(summary);
}
```

它在管理员授权/撤权流程结束后被调用。若该异步操作跨越了群聊切换，请求会打到新群，而结果被当作原操作的反馈；`chatId` 为空时还会请求出 `/api/chats//dashboard` 这种畸形路径。

**修复方案**

把群聊作为参数显式传入，并在函数开头做空值保护，同时套用缺陷 3 的请求序号校验：

```tsx
async function refreshChatSummary(targetChatId: string) {
  if (!targetChatId) return;
  // ...请求后校验 targetChatId === chatId 再 setDashboard
}
```

调用处改为 `refreshChatSummary(chatId)`，并在 `changeAdministrator` 起始处捕获当时的 `chatId`。

---

## 建议的修复顺序

1. **先做前端一致性**：缺陷 1、2、3。三者互相叠加，共同构成“看到或改到错误任务”的风险，且改动集中在 `dashboard-client.tsx`，不涉及数据迁移。
2. **再做通知可靠性**：缺陷 4、5。属于持久化的通知丢失与重复投递，用户可感知但不易归因；缺陷 5 有 `app/reminders/worker.py` 的现成实现可对齐。
3. **随后处理交互缺陷**：缺陷 6、7、8。功能可用性问题，无数据风险。
4. **明确多租户前提后处理缺陷 9**：先确认部署形态，再决定是做复合主键迁移，还是加固单租户校验。
5. **最后补健壮性**：缺陷 10、11、12。

## 建议补充的测试

现有 478 项测试全部通过却未能发现上述问题，说明以下方向缺少覆盖：

- 通知同步在 `allowed_chat_ids` 变化前后的游标行为（针对缺陷 4）。
- 通知 worker 在 `mark_sent` 抛异常时的状态收敛（针对缺陷 5，可参照提醒 worker 现有测试）。
- 管理接口部分字段更新时，未提交字段保持不变（针对缺陷 6）。
- 跨页合并候选查询（针对缺陷 7）。
- 前端目前仅有 2 项服务端渲染测试，没有交互测试。建议引入组件级测试，覆盖群聊切换、修改后状态同步、分页收敛这三类场景（针对缺陷 1、2、8）。
