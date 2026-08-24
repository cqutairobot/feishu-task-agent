"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API_ORIGIN = process.env.NEXT_PUBLIC_MANAGEMENT_API_URL ?? "";
const LAST_CHAT_STORAGE_KEY = "lab-task-console:last-chat-id";
const TASK_PAGE_SIZE = 10;

type Chat = { chat_id: string; chat_name: string | null; administrator_count: number; open_task_count: number };
type Assignee = { open_id: string; name: string; position: number };
type Task = { task_id: number; task_code: string; chat_id: string; title: string; description: string; status: string; merged_into_task_id: number | null; merged_into_task_code: string | null; deadline: string | null; confidence: number; creation_source: "model_detection" | "management_page"; assignees: Assignee[]; created_at: string; updated_at: string };
type Dashboard = { chat_id: string; chat_name: string | null; member_count: number; administrator_count: number; total_task_count: number; pending_count: number; todo_count: number; overdue_count: number; done_count: number; cancelled_count: number; open_without_deadline_count: number; due_next_7_days_count: number };
type Member = { open_id: string; name: string; feishu_name: string; task_alias: string | null; is_owner: boolean; is_administrator: boolean; last_synced_at: string };
type AdministratorEvent = { event_id: number; action: "grant" | "revoke"; source: string; target_open_id: string; target_name: string; actor_open_id: string | null; actor_name: string | null; created_at: string };
type ChatSettings = { chat_id: string; detection_enabled: boolean; auto_todo_confidence: number; task_scope: "broad" | "work_only"; timezone: string; reminder_due_72h_enabled: boolean; reminder_due_24h_enabled: boolean; reminder_due_today_enabled: boolean; reminder_overdue_enabled: boolean; reminder_due_72h_offset_hours: number; reminder_due_24h_offset_hours: number; reminder_due_today_hour: number; reminder_overdue_grace_minutes: number; missing_deadline_owner_enabled: boolean; missing_deadline_admin_enabled: boolean; missing_deadline_owner_delay_hours: number; missing_deadline_admin_delay_hours: number; administrator_notification_mode: "all" | "selected"; administrator_notification_open_ids: string[]; updated_by_open_id: string | null; updated_at: string | null };
type ChatSettingValues = Pick<ChatSettings, "detection_enabled" | "auto_todo_confidence" | "task_scope" | "reminder_due_72h_enabled" | "reminder_due_24h_enabled" | "reminder_due_today_enabled" | "reminder_overdue_enabled" | "reminder_due_72h_offset_hours" | "reminder_due_24h_offset_hours" | "reminder_due_today_hour" | "reminder_overdue_grace_minutes" | "missing_deadline_owner_enabled" | "missing_deadline_admin_enabled" | "missing_deadline_owner_delay_hours" | "missing_deadline_admin_delay_hours" | "administrator_notification_mode" | "administrator_notification_open_ids">;
type ChatSettingEvent = { event_id: number; chat_id: string; actor_open_id: string; changed_fields: { before?: ChatSettingValues; after?: ChatSettingValues }; created_at: string };
type TaskPage = { total_count: number; total_pages: number; page: number; limit: number; offset: number; tasks: Task[] };
type TaskDetail = {
  task: Task;
  evidence: Array<{ message_id: string; sender_name: string | null; content: string | null; created_at: string }>;
  lifecycle: Array<{ event_id: number; action: string; previous_status: string; new_status: string; actor_open_id: string; applied_at: string }>;
  deliveries: Array<{ delivery_type: string; kind: string; status: string; scheduled_for: string }>;
};
type ManagementStatusAction = "confirm" | "complete" | "cancel" | "invalidate" | "restore" | "merge";
type ManualTaskInput = { title: string; description: string; deadline: string | null; open_ids: string[]; request_id: string };

const statusCopy: Record<string, string> = { pending: "待确认", todo: "待办", overdue: "已逾期", done: "已完成", cancelled: "已取消", merged: "已合并" };
const sourceCopy: Record<string, string> = { local_cli: "本地应急操作", management_page: "管理后台", bootstrap: "初始配置", group_owner_init: "群主初始化", group_owner_takeover: "群主接管", membership_sync: "成员同步" };

class UnauthorizedError extends Error {}
class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ORIGIN}${path}`, { ...init, credentials: "include", headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  if (response.status === 401) throw new UnauthorizedError("unauthorized");
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { error?: string };
    throw new ApiError(response.status, payload.error ?? "request failed");
  }
  if (response.status === 204) return null as T;
  return (await response.json()) as T;
}

export function DashboardClient() {
  const [view, setView] = useState<"tasks" | "administrators" | "settings">("tasks");
  const [chats, setChats] = useState<Chat[]>([]);
  const [chatId, setChatId] = useState("");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [taskPage, setTaskPage] = useState<TaskPage | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [administratorEvents, setAdministratorEvents] = useState<AdministratorEvent[]>([]);
  const [chatSettings, setChatSettings] = useState<ChatSettings | null>(null);
  const [chatSettingEvents, setChatSettingEvents] = useState<ChatSettingEvent[]>([]);
  // Keep the first render identical on the server and browser. URL state is
  // restored after hydration so refreshes with search or pagination do not
  // trigger a server/client markup mismatch.
  const [filter, setFilter] = useState("open");
  const [query, setQuery] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [administrationLoading, setAdministrationLoading] = useState(false);
  const [mutatingOpenId, setMutatingOpenId] = useState("");
  const [unauthorized, setUnauthorized] = useState(false);
  const [error, setError] = useState("");
  const [administrationNotice, setAdministrationNotice] = useState("");
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsNotice, setSettingsNotice] = useState("");

  const handleFailure = useCallback((reason: unknown, fallback: string) => {
    if (reason instanceof UnauthorizedError) setUnauthorized(true);
    else setError(fallback);
  }, []);

  useEffect(() => {
    const state = readTaskViewState();
    const timer = window.setTimeout(() => {
      setFilter(state.filter);
      setQuery(state.query);
      setSearchInput(state.query);
      setPage(state.page);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    apiRequest<Chat[]>("/api/chats")
      .then((items) => {
        const requested = readTaskViewState().chatId;
        const remembered = readLastChatId();
        const nextChatId = items.some((chat) => chat.chat_id === requested)
          ? requested
          : items.some((chat) => chat.chat_id === remembered)
            ? remembered
          : items[0]?.chat_id ?? "";
        setChats(items); setChatId(nextChatId); writeTaskViewState({ chatId: nextChatId });
      })
      .catch((reason) => handleFailure(reason, "暂时无法连接管理服务，请稍后刷新。"))
      .finally(() => setLoading(false));
  }, [handleFailure]);

  useEffect(() => {
    if (chatId) rememberChatId(chatId);
  }, [chatId]);

  useEffect(() => {
    if (!chatId) return;
    const params = buildTaskParams(filter, query, TASK_PAGE_SIZE, (page - 1) * TASK_PAGE_SIZE);
    Promise.all([apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`), apiRequest<TaskPage>(`/api/chats/${chatId}/tasks?${params}`)])
      .then(([summary, tasks]) => {
        const resolvedPage = tasks.total_pages > 0 ? Math.min(page, tasks.total_pages) : 1;
        if (resolvedPage !== page) { setPage(resolvedPage); writeTaskViewState({ page: resolvedPage }); }
        setDashboard(summary); setTaskPage(tasks); setError("");
      })
      .catch((reason) => handleFailure(reason, "群聊数据加载失败，请稍后刷新。"))
      .finally(() => setLoading(false));
  }, [chatId, filter, query, page, handleFailure]);

  const loadAdministration = useCallback(async () => {
    if (!chatId) return;
    setAdministrationLoading(true);
    try {
      const memberItems = await apiRequest<Member[]>(`/api/chats/${chatId}/members`);
      const eventItems = await apiRequest<AdministratorEvent[]>(`/api/chats/${chatId}/administrator-events?limit=100`);
      setMembers(memberItems); setAdministratorEvents(eventItems); setError("");
    } catch (reason) { handleFailure(reason, "管理员数据加载失败，请稍后刷新。"); }
    finally { setAdministrationLoading(false); }
  }, [chatId, handleFailure]);

  const loadSettings = useCallback(async () => {
    if (!chatId) return;
    setSettingsLoading(true);
    try {
      const [settings, events, memberItems] = await Promise.all([
        apiRequest<ChatSettings>(`/api/chats/${chatId}/settings`),
        apiRequest<ChatSettingEvent[]>(`/api/chats/${chatId}/settings/events?limit=20`),
        apiRequest<Member[]>(`/api/chats/${chatId}/members`),
      ]);
      setChatSettings(settings); setChatSettingEvents(events); setMembers(memberItems); setError("");
    } catch (reason) { handleFailure(reason, "群设置加载失败，请稍后刷新。"); }
    finally { setSettingsLoading(false); }
  }, [chatId, handleFailure]);

  useEffect(() => {
    if (view !== "administrators") return;
    const timer = window.setTimeout(() => void loadAdministration(), 0);
    return () => window.clearTimeout(timer);
  }, [loadAdministration, view]);

  useEffect(() => {
    if (view !== "settings") return;
    const timer = window.setTimeout(() => void loadSettings(), 0);
    return () => window.clearTimeout(timer);
  }, [loadSettings, view]);

  const selectedChat = useMemo(() => chats.find((chat) => chat.chat_id === chatId), [chatId, chats]);
  const administratorCount = members.filter((member) => member.is_administrator).length;

  async function openTask(task: Task) {
    try {
      const [taskDetail, memberItems] = await Promise.all([
        apiRequest<TaskDetail>(`/api/chats/${task.chat_id}/tasks/${task.task_id}`),
        apiRequest<Member[]>(`/api/chats/${task.chat_id}/members`),
      ]);
      setDetail(taskDetail); setMembers(memberItems);
    }
    catch (reason) { handleFailure(reason, "任务详情加载失败。"); }
  }

  async function openTaskCreation() {
    if (!chatId) return;
    setCreating(true);
    try {
      const memberItems = await apiRequest<Member[]>(`/api/chats/${chatId}/members`);
      setMembers(memberItems); setError("");
    } catch (reason) {
      setCreating(false); handleFailure(reason, "当前群成员加载失败，暂时无法新建任务。");
    }
  }

  async function refreshChatSummary() {
    const [items, summary] = await Promise.all([apiRequest<Chat[]>("/api/chats"), apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`)]);
    setChats(items); setDashboard(summary);
  }

  async function mutateTask(resource: "deadline" | "title" | "assignees" | "status", body: Record<string, string | string[] | number>) {
    if (!detail) return;
    const updated = await apiRequest<TaskDetail>(`/api/chats/${detail.task.chat_id}/tasks/${detail.task.task_id}/${resource}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    setDetail(updated);
    const params = buildTaskParams(filter, query, TASK_PAGE_SIZE, (page - 1) * TASK_PAGE_SIZE);
    const [summary, tasks, chatItems] = await Promise.all([
      apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`),
      apiRequest<TaskPage>(`/api/chats/${chatId}/tasks?${params}`),
      apiRequest<Chat[]>("/api/chats"),
    ]);
    setDashboard(summary); setTaskPage(tasks); setChats(chatItems);
  }

  async function rescheduleTask(deadline: string, requestId: string) {
    await mutateTask("deadline", { deadline, request_id: requestId });
  }

  async function renameTask(title: string, requestId: string) {
    await mutateTask("title", { title, request_id: requestId });
  }

  async function reassignTask(openIds: string[], requestId: string) {
    await mutateTask("assignees", { open_ids: openIds, request_id: requestId });
  }

  async function transitionTask(action: ManagementStatusAction, requestId: string, targetTaskId?: number) {
    await mutateTask("status", targetTaskId ? { action, request_id: requestId, target_task_id: targetTaskId } : { action, request_id: requestId });
  }

  function applySearch(value: string) {
    const normalized = value.trim().replace(/\s+/g, " ");
    setLoading(true);
    setSearchInput(normalized);
    setQuery(normalized);
    setPage(1);
    writeTaskViewState({ query: normalized, page: 1 });
  }

  function clearSearch() {
    setSearchInput("");
    setQuery("");
    setPage(1);
    writeTaskViewState({ query: "", page: 1 });
    setLoading(true);
  }

  function changeFilter(value: string) {
    setLoading(true);
    setFilter(value);
    setPage(1);
    writeTaskViewState({ filter: value, page: 1 });
  }

  function changePage(nextPage: number) {
    if (!taskPage || nextPage < 1 || nextPage > taskPage.total_pages || nextPage === page) return;
    setLoading(true);
    setPage(nextPage);
    writeTaskViewState({ page: nextPage });
  }

  async function createTask(input: ManualTaskInput) {
    const created = await apiRequest<TaskDetail>(`/api/chats/${chatId}/tasks`, {
      method: "POST",
      body: JSON.stringify(input),
    });
    setCreating(false); setDetail(created);
    const params = buildTaskParams(filter, query, TASK_PAGE_SIZE, (page - 1) * TASK_PAGE_SIZE);
    const [summary, tasks, chatItems] = await Promise.all([
      apiRequest<Dashboard>(`/api/chats/${chatId}/dashboard`),
      apiRequest<TaskPage>(`/api/chats/${chatId}/tasks?${params}`),
      apiRequest<Chat[]>("/api/chats"),
    ]);
    setDashboard(summary); setTaskPage(tasks); setChats(chatItems);
  }

  async function changeAdministrator(member: Member) {
    if (member.is_administrator && administratorCount <= 1) { setAdministrationNotice("每个群必须保留至少一名管理员。"); return; }
    if (member.is_administrator && !window.confirm(`确认撤销“${member.name}”的本群管理员权限？`)) return;
    setMutatingOpenId(member.open_id); setAdministrationNotice("");
    try {
      if (member.is_administrator) {
        await apiRequest(`/api/chats/${chatId}/administrators/${member.open_id}`, { method: "DELETE" });
        setAdministrationNotice(`已撤销“${member.name}”的管理员权限。`);
      } else {
        await apiRequest(`/api/chats/${chatId}/administrators`, { method: "POST", body: JSON.stringify({ open_id: member.open_id }) });
        setAdministrationNotice(`已将“${member.name}”设为本群管理员。`);
      }
      await Promise.all([loadAdministration(), refreshChatSummary()]);
    } catch (reason) {
      if (reason instanceof UnauthorizedError) setUnauthorized(true);
      else if (reason instanceof ApiError && reason.message.includes("last administrator")) setAdministrationNotice("操作被拒绝：每个群必须保留至少一名管理员。");
      else if (reason instanceof ApiError && reason.status === 503) setAdministrationNotice("暂时无法向飞书核验最新成员，请稍后重试。");
      else setAdministrationNotice("管理员设置失败，成员状态可能刚刚发生变化。");
    } finally { setMutatingOpenId(""); }
  }

  async function saveSettings(next: Partial<ChatSettingValues>) {
    if (!chatId) return;
    setSettingsSaving(true); setSettingsNotice("");
    try {
      const updated = await apiRequest<ChatSettings>(`/api/chats/${chatId}/settings`, {
        method: "POST", body: JSON.stringify(next),
      });
      setChatSettings(updated);
      const events = await apiRequest<ChatSettingEvent[]>(`/api/chats/${chatId}/settings/events?limit=20`);
      setChatSettingEvents(events); setSettingsNotice("群设置已保存。"); setError("");
    } catch (reason) {
      if (reason instanceof UnauthorizedError) setUnauthorized(true);
      else setSettingsNotice("群设置保存失败，请稍后重试。");
    } finally { setSettingsSaving(false); }
  }

  async function logout() { try { await apiRequest<null>("/auth/logout", { method: "POST" }); } finally { setUnauthorized(true); } }
  if (unauthorized) return <SignedOut />;

  return <main className="shell">
    <aside className="rail" aria-label="主导航">
      <div className="brand-mark" aria-label="Lab Task Console">LT</div>
      <nav className="rail-nav">
        <button className={`rail-button ${view === "tasks" ? "active" : ""}`} type="button" aria-label="任务总览" onClick={() => setView("tasks")}><span>▦</span></button>
        <button className={`rail-button ${view === "administrators" ? "active" : ""}`} type="button" aria-label="群管理员与审计" onClick={() => setView("administrators")}><span>◎</span></button>
        <button className={`rail-button ${view === "settings" ? "active" : ""}`} type="button" aria-label="群设置" onClick={() => setView("settings")}><span>⚙</span></button>
      </nav>
      <button className="avatar avatar-button" type="button" onClick={logout} title="退出登录">管</button>
    </aside>
    <section className="workspace">
      <header className="topbar"><div><p className="eyebrow">LAB TASK CONSOLE</p><h1>{view === "tasks" ? "任务总览" : view === "administrators" ? "群管理员" : "群设置"}</h1></div><div className="topbar-actions"><span className="live-pill"><i /> 权限连接正常</span><label className="group-switcher"><span className="sr-only">选择群聊</span><select value={chatId} onChange={(event) => { const nextChatId = event.target.value; setLoading(true); setChatId(nextChatId); setPage(1); setAdministrationNotice(""); setSettingsNotice(""); writeTaskViewState({ chatId: nextChatId, page: 1 }); }}>{chats.map((chat) => <option key={chat.chat_id} value={chat.chat_id}>{chat.chat_name ?? "未命名群聊"}</option>)}</select><b>⌄</b></label></div></header>
      {view === "tasks" ? <TaskWorkspace dashboard={dashboard} error={error} filter={filter} loading={loading} page={page} query={query} searchInput={searchInput} selectedChat={selectedChat} setFilter={changeFilter} setSearchInput={setSearchInput} onSearch={applySearch} onClearSearch={clearSearch} onPageChange={changePage} taskPage={taskPage} openTask={openTask} openTaskCreation={openTaskCreation} /> : view === "administrators" ? <AdministratorWorkspace chatName={selectedChat?.chat_name ?? "当前群聊"} error={error} loading={administrationLoading} members={members} events={administratorEvents} mutatingOpenId={mutatingOpenId} notice={administrationNotice} onChange={changeAdministrator} /> : <SettingsWorkspace chatName={selectedChat?.chat_name ?? "当前群聊"} error={error} loading={settingsLoading} saving={settingsSaving} settings={chatSettings} members={members} events={chatSettingEvents} notice={settingsNotice} onSave={saveSettings} />}
    </section>
    {creating ? <CreateTaskDialog chatName={selectedChat?.chat_name ?? "当前群聊"} members={members} onClose={() => setCreating(false)} onSave={createTask} /> : null}
    {detail ? <TaskDrawer detail={detail} mergeTargets={taskPage?.tasks ?? []} members={members} onClose={() => setDetail(null)} onRename={renameTask} onReassign={reassignTask} onReschedule={rescheduleTask} onTransition={transitionTask} /> : null}
  </main>;
}

function TaskWorkspace({ dashboard, error, filter, loading, page, query, searchInput, selectedChat, setFilter, setSearchInput, onSearch, onClearSearch, onPageChange, taskPage, openTask, openTaskCreation }: { dashboard: Dashboard | null; error: string; filter: string; loading: boolean; page: number; query: string; searchInput: string; selectedChat: Chat | undefined; setFilter: (value: string) => void; setSearchInput: (value: string) => void; onSearch: (value: string) => void; onClearSearch: () => void; onPageChange: (value: number) => void; taskPage: TaskPage | null; openTask: (task: Task) => void; openTaskCreation: () => void }) {
  const pendingCount = dashboard?.pending_count ?? 0;
  const overdueCount = dashboard?.overdue_count ?? 0;
  const attentionCount = pendingCount + overdueCount;
  const attentionFilter = pendingCount ? "pending" : "overdue";
  function submitSearch(event: React.FormEvent<HTMLFormElement>) { event.preventDefault(); onSearch(searchInput); }
  return <div className="content-grid"><section className="primary-column"><div className="welcome-row"><div><h2>管理员视图</h2><p>这里是{selectedChat?.chat_name ?? "当前群聊"}的实时任务状态。</p></div><div className="welcome-actions"><p className="updated">刚刚同步</p><button className="create-task-button" type="button" onClick={openTaskCreation}>＋ 新建任务</button></div></div>{error ? <div className="error-banner">{error}</div> : null}<div className="metric-grid" aria-label="任务统计"><Metric label="未完成任务" value={pendingCount + (dashboard?.todo_count ?? 0) + overdueCount} note={pendingCount ? `${pendingCount} 项等待管理员审核` : `${dashboard?.todo_count ?? 0} 项按计划进行`} tone="primary" /><Metric label="7 天内截止" value={dashboard?.due_next_7_days_count ?? 0} note="均由机器人持续检查" /><Metric label="已逾期" value={overdueCount} note={overdueCount ? "需要尽快跟进" : "当前无需处理"} tone={overdueCount ? "danger" : ""} /><Metric label="缺少截止时间" value={dashboard?.open_without_deadline_count ?? 0} note={dashboard?.open_without_deadline_count ? "将按规则提醒设置" : "当前无需处理"} /></div><section className="task-panel"><div className="panel-heading"><div><h3>任务清单</h3><p>{query ? `搜索“${query}” · ${taskPage?.total_count ?? 0} 项结果` : `${taskPage?.total_count ?? 0} 项结果，按截止时间排序`}</p></div><div className="filter-row" role="group" aria-label="任务筛选">{[["open","未完成"],["pending","待审核"],["todo","待办"],["overdue","逾期"],["all","全部"]].map(([value,label]) => <button key={value} className={`filter ${filter === value ? "active" : ""}`} type="button" onClick={() => setFilter(value)}>{label}</button>)}</div></div><form className="task-search" role="search" onSubmit={submitSearch}><label htmlFor="task-search-input">搜索任务</label><input id="task-search-input" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="编号、标题或说明" /><button type="submit">搜索</button>{query ? <button className="search-clear" type="button" onClick={onClearSearch} aria-label="清除任务搜索">清除</button> : null}</form><div className="task-table" role="table" aria-label="任务列表"><div className="task-table-head" role="row"><span>任务</span><span>负责人</span><span>截止时间</span><span>状态</span></div>{loading && !taskPage ? <div className="table-message">正在读取群聊任务…</div> : null}{!loading && taskPage?.tasks.length === 0 ? <div className="table-message">当前筛选条件下没有任务。</div> : null}{taskPage?.tasks.map((task) => <button className="task-row" role="row" type="button" key={task.task_id} onClick={() => openTask(task)}><span className="task-title-cell"><b>{task.title}</b><small>{task.task_code}</small></span><span className="owner-cell"><i>{task.assignees[0]?.name.slice(0,1) ?? "?"}</i>{task.assignees.map((item) => item.name).join("、")}</span><span className={task.status === "overdue" ? "date-cell late" : "date-cell"}>{formatDeadline(task.deadline)}</span><span><em className={`status ${taskStatusTone(task.status)}`}>{statusCopy[task.status] ?? task.status}</em></span></button>)}</div>{taskPage && taskPage.total_pages > 1 ? <nav className="task-pagination" aria-label="任务分页"><button type="button" disabled={loading || page <= 1} onClick={() => onPageChange(page - 1)}>上一页</button><span>第 {page} / {taskPage.total_pages} 页 · 共 {taskPage.total_count} 项</span><button type="button" disabled={loading || page >= taskPage.total_pages} onClick={() => onPageChange(page + 1)}>下一页</button></nav> : null}</section></section><aside className="insight-column"><section className={`risk-card ${attentionCount ? "" : "risk-clear"}`}><div className="risk-topline"><span>需要关注</span><b>{attentionCount}</b></div><h3>{pendingCount ? "存在等待审核的识别结果" : overdueCount ? "存在已经逾期的任务" : "当前没有待处理风险"}</h3><p>{pendingCount ? "待审核任务不会提醒负责人；确认后才会进入正式任务流程。" : overdueCount ? "机器人将继续私聊负责人，并通知本群管理员。" : "所有未完成任务均处于可控状态。"}</p>{attentionCount ? <button type="button" onClick={() => setFilter(attentionFilter)}>{pendingCount ? "审核待确认任务" : "查看逾期任务"} <span>→</span></button> : null}</section><section className="activity-card"><div className="side-heading"><h3>当前范围</h3><span className="readonly-badge">LIVE</span></div><dl className="scope-list"><div><dt>当前群成员</dt><dd>{dashboard?.member_count ?? 0}</dd></div><div><dt>本群管理员</dt><dd>{dashboard?.administrator_count ?? 0}</dd></div><div><dt>历史任务</dt><dd>{dashboard?.total_task_count ?? 0}</dd></div></dl></section><section className="scope-note"><span>权限说明</span><p>待确认识别结果由本群管理员审核；正式任务仍可通过飞书私聊维护。</p></section></aside></div>;
}

function AdministratorWorkspace({ chatName, error, loading, members, events, mutatingOpenId, notice, onChange }: { chatName: string; error: string; loading: boolean; members: Member[]; events: AdministratorEvent[]; mutatingOpenId: string; notice: string; onChange: (member: Member) => void }) {
  const administrators = members.filter((member) => member.is_administrator);
  return <div className="admin-workspace"><div className="welcome-row"><div><h2>本群管理员</h2><p>管理 {chatName} 的任务查看、成员纠错和生命周期权限。</p></div><p className="updated">成员身份来自飞书实时核验</p></div>{error ? <div className="error-banner">{error}</div> : null}{notice ? <div className="notice-banner" role="status">{notice}</div> : null}<section className="admin-summary-grid"><article className="admin-summary-card"><span>管理员</span><strong>{administrators.length}</strong><p>至少保留 1 名</p></article><article className="admin-summary-card"><span>当前成员</span><strong>{members.length}</strong><p>离群后自动撤权</p></article><article className="admin-summary-card"><span>群主</span><strong>{members.find((member) => member.is_owner)?.name ?? "待同步"}</strong><p>可在群内接管权限</p></article></section><div className="admin-grid"><section className="member-panel"><div className="panel-heading"><div><h3>成员权限</h3><p>只有当前群成员可以成为管理员</p></div><span className="secure-badge">飞书已核验</span></div>{loading ? <div className="table-message">正在核验群成员…</div> : null}{!loading && members.map((member) => <article className="member-row" key={member.open_id}><div className="member-avatar">{member.name.slice(0, 1)}</div><div className="member-copy"><div><b>{member.name}</b>{member.is_owner ? <span className="owner-badge">群主</span> : null}{member.is_administrator ? <span className="admin-badge">管理员</span> : null}</div><small>飞书名称：{member.feishu_name} · {member.task_alias ? `任务姓名：${member.task_alias}` : "尚未绑定任务姓名"}</small></div><button className={member.is_administrator ? "permission-button revoke" : "permission-button grant"} type="button" disabled={mutatingOpenId !== "" || (member.is_administrator && administrators.length <= 1)} onClick={() => onChange(member)}>{mutatingOpenId === member.open_id ? "处理中…" : member.is_administrator ? "撤销权限" : "设为管理员"}</button></article>)}</section><section className="admin-audit-panel"><div className="panel-heading"><div><h3>管理员审计</h3><p>授权、撤权和自动回收均会保留记录</p></div></div><div className="admin-audit-list">{!loading && events.length === 0 ? <div className="table-message">尚无管理员变更记录。</div> : null}{events.map((event) => <article className="admin-audit-row" key={event.event_id}><i className={event.action === "grant" ? "grant" : "revoke"}>{event.action === "grant" ? "+" : "−"}</i><div><b>{event.action === "grant" ? "授予管理员" : "撤销管理员"}</b><p>{event.target_name}</p><small>{event.actor_name ? `操作人：${event.actor_name}` : "系统自动处理"} · {sourceCopy[event.source] ?? event.source}</small></div><time>{formatTime(event.created_at)}</time></article>)}</div></section></div></div>;
}

type SettingsWorkspaceProps = { chatName: string; error: string; loading: boolean; saving: boolean; settings: ChatSettings | null; members: Member[]; events: ChatSettingEvent[]; notice: string; onSave: (next: Partial<ChatSettingValues>) => Promise<void> };

function SettingsWorkspace(props: SettingsWorkspaceProps) {
  const { settings, members, saving, onSave } = props;
  const [mode, setMode] = useState<"all" | "selected">("all");
  const [selected, setSelected] = useState<string[]>([]);
  useEffect(() => {
    if (!settings) return;
    const timer = window.setTimeout(() => {
      setMode(settings.administrator_notification_mode);
      setSelected(settings.administrator_notification_open_ids);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [settings]);
  const administrators = members.filter((member) => member.is_administrator);
  const normalized = [...selected].sort();
  const saved = [...(settings?.administrator_notification_open_ids ?? [])].sort();
  const changed = settings !== null && (mode !== settings.administrator_notification_mode || normalized.join("\u0000") !== saved.join("\u0000"));
  const valid = mode === "all" || (selected.length > 0 && selected.every((openId) => administrators.some((member) => member.open_id === openId)));
  function toggle(openId: string, checked: boolean) {
    setSelected((items) => checked ? [...items, openId] : items.filter((item) => item !== openId));
  }
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void onSave({
      administrator_notification_mode: mode,
      administrator_notification_open_ids: mode === "all" ? [] : normalized,
    });
  }
  // Each label below wraps its own radio/checkbox and visible text. The
  // accessibility rule cannot resolve those associations on the mapped JSX.
  // eslint-disable-next-line jsx-a11y/label-has-associated-control
  return <><BaseSettingsWorkspace {...props} /><section className="notification-policy-workspace" aria-labelledby="administrator-notification-heading"><div className="panel-heading"><div><h2 id="administrator-notification-heading">管理员通知对象</h2><p>任务完成、取消、延期、逾期和缺少截止时间时，按此范围发送私聊。</p></div><span className="secure-badge">按群隔离</span></div>{props.loading || !settings ? <div className="table-message">正在读取管理员通知设置…</div> : <form className="notification-policy-form" onSubmit={submit}><div className="notification-mode-grid"><label className={mode === "all" ? "notification-mode selected" : "notification-mode"}><input type="radio" name="administrator-notification-mode" value="all" checked={mode === "all"} disabled={saving} onChange={() => { setMode("all"); setSelected([]); }} /><span><b>全部管理员</b><small>默认策略；向当前群全部管理员发送</small></span></label><label className={mode === "selected" ? "notification-mode selected" : "notification-mode"}><input type="radio" name="administrator-notification-mode" value="selected" checked={mode === "selected"} disabled={saving} onChange={() => setMode("selected")} /><span><b>指定管理员</b><small>可以选择一名或多名本群管理员</small></span></label></div>{mode === "selected" ? <fieldset className="notification-recipient-fieldset"><legend>选择接收人</legend>{administrators.map((member) => <label key={member.open_id}><input type="checkbox" checked={selected.includes(member.open_id)} disabled={saving} onChange={(event) => toggle(member.open_id, event.target.checked)} /><span><b>{member.name}</b><small>{member.is_owner ? "群主 · 管理员" : "本群管理员"}</small></span></label>)}</fieldset> : null}{!valid ? <p className="settings-validation" role="alert">请至少选择一名当前群管理员。</p> : null}<button className="save-settings-button" type="submit" disabled={saving || !changed || !valid}>{saving ? "保存中…" : "保存通知对象"}</button></form>}</section></>;
}

function BaseSettingsWorkspace({ chatName, error, loading, saving, settings, members, events, notice, onSave }: SettingsWorkspaceProps) {
  const [enabled, setEnabled] = useState(true);
  const [confidence, setConfidence] = useState(0.85);
  const [taskScope, setTaskScope] = useState<"broad" | "work_only">("broad");
  const [due72h, setDue72h] = useState(true);
  const [due24h, setDue24h] = useState(true);
  const [dueToday, setDueToday] = useState(true);
  const [overdue, setOverdue] = useState(true);
  const [due72Offset, setDue72Offset] = useState(72);
  const [due24Offset, setDue24Offset] = useState(24);
  const [dueTodayHour, setDueTodayHour] = useState(9);
  const [overdueGraceMinutes, setOverdueGraceMinutes] = useState(1);
  const [missingOwnerEnabled, setMissingOwnerEnabled] = useState(true);
  const [missingAdminEnabled, setMissingAdminEnabled] = useState(true);
  const [missingOwnerHours, setMissingOwnerHours] = useState(24);
  const [missingAdminHours, setMissingAdminHours] = useState(72);
  const [administratorNotificationMode, setAdministratorNotificationMode] = useState<"all" | "selected">("all");
  const [administratorNotificationOpenIds, setAdministratorNotificationOpenIds] = useState<string[]>([]);
  useEffect(() => {
    if (!settings) return;
    const timer = window.setTimeout(() => {
      setEnabled(settings.detection_enabled);
      setConfidence(settings.auto_todo_confidence);
      setTaskScope(settings.task_scope);
      setDue72h(settings.reminder_due_72h_enabled);
      setDue24h(settings.reminder_due_24h_enabled);
      setDueToday(settings.reminder_due_today_enabled);
      setOverdue(settings.reminder_overdue_enabled);
      setDue72Offset(settings.reminder_due_72h_offset_hours);
      setDue24Offset(settings.reminder_due_24h_offset_hours);
      setDueTodayHour(settings.reminder_due_today_hour);
      setOverdueGraceMinutes(settings.reminder_overdue_grace_minutes);
      setMissingOwnerEnabled(settings.missing_deadline_owner_enabled);
      setMissingAdminEnabled(settings.missing_deadline_admin_enabled);
      setMissingOwnerHours(settings.missing_deadline_owner_delay_hours);
      setMissingAdminHours(settings.missing_deadline_admin_delay_hours);
      setAdministratorNotificationMode(settings.administrator_notification_mode);
      setAdministratorNotificationOpenIds(settings.administrator_notification_open_ids);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [settings]);
  const administrators = members.filter((member) => member.is_administrator);
  const normalizedAdministratorNotificationOpenIds = [...administratorNotificationOpenIds].sort();
  const savedAdministratorNotificationOpenIds = [...(settings?.administrator_notification_open_ids ?? [])].sort();
  const changed = settings !== null && (
    enabled !== settings.detection_enabled
    || Math.abs(confidence - settings.auto_todo_confidence) > 0.0001
    || taskScope !== settings.task_scope
    || due72h !== settings.reminder_due_72h_enabled
    || due24h !== settings.reminder_due_24h_enabled
    || dueToday !== settings.reminder_due_today_enabled
    || overdue !== settings.reminder_overdue_enabled
    || due72Offset !== settings.reminder_due_72h_offset_hours
    || due24Offset !== settings.reminder_due_24h_offset_hours
    || dueTodayHour !== settings.reminder_due_today_hour
    || overdueGraceMinutes !== settings.reminder_overdue_grace_minutes
    || missingOwnerEnabled !== settings.missing_deadline_owner_enabled
    || missingAdminEnabled !== settings.missing_deadline_admin_enabled
    || missingOwnerHours !== settings.missing_deadline_owner_delay_hours
    || missingAdminHours !== settings.missing_deadline_admin_delay_hours
    || administratorNotificationMode !== settings.administrator_notification_mode
    || normalizedAdministratorNotificationOpenIds.join("\u0000") !== savedAdministratorNotificationOpenIds.join("\u0000")
  );
  const timingValid = due72Offset > due24Offset;
  const missingDeadlineValid = missingAdminHours > missingOwnerHours;
  const administratorNotificationValid = administratorNotificationMode === "all" || (
    administratorNotificationOpenIds.length > 0
    && administratorNotificationOpenIds.every((openId) => administrators.some((member) => member.open_id === openId))
  );
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!administratorNotificationValid) return;
    void onSave({
      detection_enabled: enabled,
      auto_todo_confidence: confidence,
      task_scope: taskScope,
      reminder_due_72h_enabled: due72h,
      reminder_due_24h_enabled: due24h,
      reminder_due_today_enabled: dueToday,
      reminder_overdue_enabled: overdue,
      reminder_due_72h_offset_hours: due72Offset,
      reminder_due_24h_offset_hours: due24Offset,
      reminder_due_today_hour: dueTodayHour,
      reminder_overdue_grace_minutes: overdueGraceMinutes,
      missing_deadline_owner_enabled: missingOwnerEnabled,
      missing_deadline_admin_enabled: missingAdminEnabled,
      missing_deadline_owner_delay_hours: missingOwnerHours,
      missing_deadline_admin_delay_hours: missingAdminHours,
      administrator_notification_mode: administratorNotificationMode,
      administrator_notification_open_ids: administratorNotificationMode === "all" ? [] : normalizedAdministratorNotificationOpenIds,
    });
  }
  // Both radio labels on the compact JSX line wrap their input and visible
  // text; the accessibility rule cannot infer the association on that line.
  // eslint-disable-next-line jsx-a11y/label-has-associated-control
  return <div className="settings-workspace"><div className="welcome-row"><div><h2>群设置</h2><p>管理 {chatName} 的自动识别与提醒规则。</p></div><p className="updated">设置只作用于当前群</p></div>{error ? <div className="error-banner">{error}</div> : null}{notice ? <div className="notice-banner" role="status">{notice}</div> : null}<div className="settings-grid"><section className="settings-panel"><div className="panel-heading"><div><h3>识别与提醒规则</h3><p>关闭的提醒阶段会取消对应的未发送提醒。</p></div><span className="secure-badge">按群隔离</span></div>{loading || !settings ? <div className="table-message">正在读取群设置…</div> : <form className="settings-form" onSubmit={submit}><div className="setting-toggle"><span><label htmlFor="chat-detection-enabled"><b>自动任务识别</b></label><small>{enabled ? "新群消息会进入识别队列" : "新群消息只保存，不创建识别任务"}</small></span><input id="chat-detection-enabled" type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={saving} /><label className="setting-toggle-switch" htmlFor="chat-detection-enabled" aria-label="切换自动任务识别"><i aria-hidden="true" /></label></div><fieldset className="task-scope-fieldset"><legend>任务识别范围</legend><p>默认包含明确分配的生活需求；可按群限制为工作与科研事项。</p><div className="notification-mode-grid"><label className={taskScope === "broad" ? "notification-mode selected" : "notification-mode"}><input type="radio" name="task-scope" value="broad" checked={taskScope === "broad"} disabled={saving || !enabled} onChange={() => setTaskScope("broad")} /><span><b>宽泛任务（默认）</b><small>工作、科研、会议、生活需求和明确跑腿都可登记</small></span></label><label className={taskScope === "work_only" ? "notification-mode selected" : "notification-mode"}><input type="radio" name="task-scope" value="work_only" checked={taskScope === "work_only"} disabled={saving || !enabled} onChange={() => setTaskScope("work_only")} /><span><b>仅工作 / 科研</b><small>排除带饭、私人物品递送等生活跑腿</small></span></label></div></fieldset><div className="setting-range"><span><label htmlFor="chat-auto-todo-confidence"><b>自动进入待办的置信度</b></label><small>低于此值的识别结果进入待审核</small></span><output>{Math.round(confidence * 100)}%</output><input id="chat-auto-todo-confidence" type="range" min="0" max="1" step="0.05" value={confidence} onChange={(event) => setConfidence(Number(event.target.value))} disabled={saving || !enabled} /></div><fieldset className="reminder-stage-fieldset"><legend>负责人截止提醒</legend><p>分别控制本群任务的四个提醒阶段。</p><div className="reminder-stage-grid"><ReminderStage id="reminder-due-72h" label="第一提醒" checked={due72h} disabled={saving} onChange={setDue72h} /><ReminderStage id="reminder-due-24h" label="第二提醒" checked={due24h} disabled={saving} onChange={setDue24h} /><ReminderStage id="reminder-due-today" label="截止当天" checked={dueToday} disabled={saving} onChange={setDueToday} /><ReminderStage id="reminder-overdue" label="任务逾期" checked={overdue} disabled={saving} onChange={setOverdue} /></div></fieldset><fieldset className="reminder-timing-fieldset"><legend>提醒时间</legend><p>关闭某个提醒阶段后，对应时间不可修改。</p><div className="reminder-timing-grid"><ReminderTimingField id="reminder-due-72h-offset" label="第一提醒" suffix="小时前" min={2} max={720} value={due72Offset} disabled={saving || !due72h} onChange={setDue72Offset} /><ReminderTimingField id="reminder-due-24h-offset" label="第二提醒" suffix="小时前" min={1} max={719} value={due24Offset} disabled={saving || !due24h} onChange={setDue24Offset} /><ReminderTimingField id="reminder-due-today-hour" label="截止当天" suffix="时（整点）" min={0} max={23} value={dueTodayHour} disabled={saving || !dueToday} onChange={setDueTodayHour} /><ReminderTimingField id="reminder-overdue-grace" label="逾期后" suffix="分钟" min={0} max={1440} value={overdueGraceMinutes} disabled={saving || !overdue} onChange={setOverdueGraceMinutes} /></div>{!timingValid ? <p className="settings-validation" role="alert">第一提醒必须早于第二提醒。</p> : null}</fieldset><fieldset className="missing-deadline-fieldset"><legend>未设置截止时间</legend><p>最多提醒负责人一次、升级管理员一次，之后停止追问。</p><div className="reminder-stage-grid"><ReminderStage id="missing-deadline-owner-enabled" label="提醒负责人" checked={missingOwnerEnabled} disabled={saving} onChange={setMissingOwnerEnabled} /><ReminderStage id="missing-deadline-admin-enabled" label="升级管理员" checked={missingAdminEnabled} disabled={saving} onChange={setMissingAdminEnabled} /></div><div className="reminder-timing-grid missing-deadline-timing"><ReminderTimingField id="missing-deadline-owner-hours" label="创建任务后" suffix="小时提醒负责人" min={1} max={720} value={missingOwnerHours} disabled={saving || !missingOwnerEnabled} onChange={setMissingOwnerHours} /><ReminderTimingField id="missing-deadline-admin-hours" label="创建任务后" suffix="小时升级管理员" min={2} max={2160} value={missingAdminHours} disabled={saving || !missingAdminEnabled} onChange={setMissingAdminHours} /></div>{!missingDeadlineValid ? <p className="settings-validation" role="alert">升级管理员必须晚于提醒负责人。</p> : null}</fieldset><div className="settings-meta"><span>群时区</span><b>{settings.timezone}</b></div><button className="save-settings-button" type="submit" disabled={saving || !changed || !timingValid || !missingDeadlineValid}>{saving ? "保存中…" : "保存群设置"}</button></form>}</section><section className="settings-audit-panel"><div className="panel-heading"><div><h3>设置审计</h3><p>每次修改都会记录操作人和前后值。</p></div></div>{loading ? <div className="table-message">正在读取审计…</div> : events.length === 0 ? <div className="table-message">尚无设置修改记录。</div> : <div className="settings-audit-list">{events.map((event) => <article className="settings-audit-row" key={event.event_id}><i>●</i><div><b>设置已修改</b><p>{event.changed_fields.after?.detection_enabled ? "识别开启" : "识别暂停"} · {event.changed_fields.after?.task_scope === "work_only" ? "仅工作/科研" : "宽泛任务"} · 待办门槛 {Math.round((event.changed_fields.after?.auto_todo_confidence ?? 0) * 100)}% · 提醒 {enabledReminderStages(event.changed_fields.after)} · 无截止 {missingDeadlineSummary(event.changed_fields.after)}</p><small>操作人：{event.actor_open_id}</small></div><time>{formatTime(event.created_at)}</time></article>)}</div>}</section></div></div>;
}

function ReminderStage({ id, label, checked, disabled, onChange }: { id: string; label: string; checked: boolean; disabled: boolean; onChange: (checked: boolean) => void }) {
  return <label className={checked ? "reminder-stage enabled" : "reminder-stage"} htmlFor={id}><input id={id} type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span><i aria-hidden="true">✓</i>{label}</span></label>;
}

function ReminderTimingField({ id, label, suffix, min, max, value, disabled, onChange }: { id: string; label: string; suffix: string; min: number; max: number; value: number; disabled: boolean; onChange: (value: number) => void }) {
  return <label className="reminder-timing" htmlFor={id}><span>{label}</span><div><input id={id} type="number" min={min} max={max} step={1} value={value} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} /><small>{suffix}</small></div></label>;
}

function enabledReminderStages(settings?: ChatSettingValues) {
  if (!settings) return "历史记录";
  const stages = [settings.reminder_due_72h_enabled ? `提前${settings.reminder_due_72h_offset_hours ?? 72}h` : "", settings.reminder_due_24h_enabled ? `提前${settings.reminder_due_24h_offset_hours ?? 24}h` : "", settings.reminder_due_today_enabled ? `当天${String(settings.reminder_due_today_hour ?? 9).padStart(2, "0")}:00` : "", settings.reminder_overdue_enabled ? `逾期+${settings.reminder_overdue_grace_minutes ?? 1}m` : ""].filter(Boolean);
  return stages.length ? stages.join("/") : "全部关闭";
}

function missingDeadlineSummary(settings?: ChatSettingValues) {
  if (!settings) return "历史记录";
  const stages = [settings.missing_deadline_owner_enabled ? `负责人+${settings.missing_deadline_owner_delay_hours ?? 24}h` : "", settings.missing_deadline_admin_enabled ? `管理员+${settings.missing_deadline_admin_delay_hours ?? 72}h` : ""].filter(Boolean);
  const recipients = settings.administrator_notification_mode === "selected" ? `指定${settings.administrator_notification_open_ids.length}人` : "全部管理员";
  return `${stages.length ? stages.join("/") : "全部关闭"} · 通知${recipients}`;
}

function Metric({ label, value, note, tone = "" }: { label: string; value: number; note: string; tone?: string }) { return <article className={`metric-card ${tone}`}><span className="metric-label">{label}</span><strong>{value}</strong><span className={`metric-note ${tone === "danger" ? "" : "calm"}`}>{note}</span></article>; }

function CreateTaskDialog({ chatName, members, onClose, onSave }: { chatName: string; members: Member[]; onClose: () => void; onSave: (input: ManualTaskInput) => Promise<void> }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [deadline, setDeadline] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef("");
  const normalizedTitle = title.trim().replace(/\s+/g, " ");

  function changed() { setError(""); requestId.current = ""; }
  function toggle(openId: string, checked: boolean) {
    setSelected((items) => checked ? [...items, openId] : items.filter((item) => item !== openId));
    changed();
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!normalizedTitle || !selected.length || normalizedTitle.length > 200 || description.length > 2000) return;
    setSaving(true); setError("");
    if (!requestId.current) requestId.current = window.crypto.randomUUID();
    try {
      await onSave({
        title: normalizedTitle,
        description: description.trim(),
        deadline: deadline ? `${deadline}:00+08:00` : null,
        open_ids: selected,
        request_id: requestId.current,
      });
      requestId.current = "";
    } catch (reason) {
      if (reason instanceof UnauthorizedError) setError("登录已失效，请返回飞书重新进入后台。");
      else if (reason instanceof ApiError && reason.status === 409) setError("负责人、截止时间或群成员状态已变化，请检查后重试。");
      else setError("任务创建失败，请稍后重试。");
    } finally { setSaving(false); }
  }

  return <div className="drawer-backdrop"><button className="drawer-dismiss" type="button" onClick={onClose} aria-label="关闭新建任务" /><aside className="task-drawer create-task-drawer" role="dialog" aria-modal="true" aria-label="手动新建任务"><button className="drawer-close" type="button" onClick={onClose} aria-label="关闭">×</button><p className="drawer-code">管理员补建</p><h2>新建任务</h2><p className="drawer-description">为 {chatName} 补建一项漏识别任务。创建后立即进入待办，并私聊每位负责人。</p><form className="create-task-form" onSubmit={submit}><label>任务标题<span>必填，1–200 个字符</span><input maxLength={200} value={title} onChange={(event) => { setTitle(event.target.value); changed(); }} placeholder="例如：完成前端验收报告" /></label><label>任务说明<span>选填，最多 2000 个字符</span><textarea maxLength={2000} rows={4} value={description} onChange={(event) => { setDescription(event.target.value); changed(); }} placeholder="补充交付内容、文件位置或验收要求" /></label><label>截止时间<span>选填，北京时间（UTC+8）</span><input type="datetime-local" value={deadline} onChange={(event) => { setDeadline(event.target.value); changed(); }} /></label><fieldset><legend>负责人<span>至少选择 1 人；选择顺序决定主负责人</span></legend><div className="assignee-options">{members.map((member) => { const checked = selected.includes(member.open_id); const position = selected.indexOf(member.open_id); const disabled = !member.task_alias || (!checked && selected.length >= 20); return <label className={disabled ? "disabled" : ""} key={member.open_id}><input type="checkbox" checked={checked} disabled={disabled || saving} onChange={(event) => toggle(member.open_id, event.target.checked)} /><span><b>{member.task_alias ?? member.feishu_name}</b><small>{member.task_alias ? `飞书名称：${member.feishu_name}` : "尚未绑定任务姓名，不可选择"}</small></span>{position === 0 ? <em>主负责人</em> : position > 0 ? <em>共同负责</em> : null}</label>; })}</div></fieldset>{error ? <p className="editor-error" role="alert">{error}</p> : null}<div className="create-task-submit"><p>系统将生成唯一任务编号、负责人私聊和对应提醒计划。</p><button type="submit" disabled={saving || !normalizedTitle || !selected.length}>{saving ? "创建中…" : "创建并通知负责人"}</button></div></form></aside></div>;
}

function TaskDrawer({ detail, mergeTargets, members, onClose, onRename, onReassign, onReschedule, onTransition }: { detail: TaskDetail; mergeTargets: Task[]; members: Member[]; onClose: () => void; onRename: (title: string, requestId: string) => Promise<void>; onReassign: (openIds: string[], requestId: string) => Promise<void>; onReschedule: (deadline: string, requestId: string) => Promise<void>; onTransition: (action: ManagementStatusAction, requestId: string, targetTaskId?: number) => Promise<void> }) {
  const [deadlineValue, setDeadlineValue] = useState(formatShanghaiInput(detail.task.deadline));
  const [savingDeadline, setSavingDeadline] = useState(false);
  const [deadlineNotice, setDeadlineNotice] = useState("");
  const [deadlineError, setDeadlineError] = useState("");
  const pendingRequestId = useRef("");
  const actionable = detail.task.status === "todo" || detail.task.status === "overdue";
  const unchanged = deadlineValue === formatShanghaiInput(detail.task.deadline);

  async function submitDeadline(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!deadlineValue || !actionable || unchanged) return;
    setSavingDeadline(true); setDeadlineNotice(""); setDeadlineError("");
    if (!pendingRequestId.current) pendingRequestId.current = window.crypto.randomUUID();
    try {
      await onReschedule(`${deadlineValue}:00+08:00`, pendingRequestId.current);
      pendingRequestId.current = "";
      setDeadlineNotice("截止时间已更新，提醒计划已重新生成。");
    } catch (reason) {
      if (reason instanceof UnauthorizedError) setDeadlineError("登录已失效，请返回飞书重新进入后台。");
      else if (reason instanceof ApiError && reason.status === 409) setDeadlineError("任务状态或截止时间已经变化，请关闭详情后重新打开。");
      else setDeadlineError("截止时间修改失败，请稍后重试。");
    } finally { setSavingDeadline(false); }
  }

  return <div className="drawer-backdrop"><button className="drawer-dismiss" type="button" onClick={onClose} aria-label="关闭任务详情" /><aside className="task-drawer" role="dialog" aria-modal="true" aria-label="任务详情"><button className="drawer-close" type="button" onClick={onClose} aria-label="关闭">×</button><p className="drawer-code">{detail.task.task_code}</p><h2>{detail.task.title}</h2>{detail.task.description ? <p className="drawer-description">{detail.task.description}</p> : null}<dl className="detail-grid"><div><dt>负责人</dt><dd>{detail.task.assignees.map((item) => item.name).join("、")}</dd></div><div><dt>状态</dt><dd>{statusCopy[detail.task.status] ?? detail.task.status}</dd></div><div><dt>截止时间</dt><dd>{formatDeadline(detail.task.deadline)}</dd></div><div><dt>创建方式</dt><dd>{detail.task.creation_source === "management_page" ? "管理员手动补建" : `模型识别 · ${Math.round(detail.task.confidence * 100)}%`}</dd></div>{detail.task.merged_into_task_code ? <div><dt>保留任务</dt><dd>{detail.task.merged_into_task_code}</dd></div> : null}</dl>{actionable ? <div className="task-editors"><TitleEditor task={detail.task} onSave={onRename} /><AssigneeEditor members={members} task={detail.task} onSave={onReassign} /><form className="deadline-editor" onSubmit={submitDeadline}><div><label htmlFor={`deadline-${detail.task.task_id}`}>修改截止时间</label><small>北京时间（UTC+8）</small></div><input id={`deadline-${detail.task.task_id}`} type="datetime-local" value={deadlineValue} onChange={(event) => { setDeadlineValue(event.target.value); setDeadlineNotice(""); setDeadlineError(""); pendingRequestId.current = ""; }} /><button type="submit" disabled={savingDeadline || !deadlineValue || unchanged}>{savingDeadline ? "保存中…" : "保存新时间"}</button>{deadlineNotice ? <p className="deadline-success" role="status">{deadlineNotice}</p> : null}{deadlineError ? <p className="deadline-error" role="alert">{deadlineError}</p> : null}</form></div> : null}<LifecycleControls task={detail.task} onSave={onTransition} /><MergeEditor task={detail.task} targets={mergeTargets} onSave={onTransition} /><DetailSection title={`证据消息 · ${detail.evidence.length}`} empty={detail.task.creation_source === "management_page" ? "管理员手动补建任务没有模型证据消息。" : "没有保存的证据消息。"}>{detail.evidence.map((item) => <article className="audit-item" key={item.message_id}><b>{item.sender_name ?? "未知成员"}</b><p>{item.content ?? "非文本消息"}</p><small>{formatTime(item.created_at)}</small></article>)}</DetailSection><DetailSection title={`生命周期 · ${detail.lifecycle.length}`} empty="该任务还没有状态变更记录。">{detail.lifecycle.map((item) => <article className="audit-item" key={item.event_id}><b>{item.action}</b><p>{statusCopy[item.previous_status] ?? item.previous_status} → {statusCopy[item.new_status] ?? item.new_status}</p><small>{formatTime(item.applied_at)}</small></article>)}</DetailSection><DetailSection title={`提醒与通知 · ${detail.deliveries.length}`} empty="该任务没有提醒投递记录。">{detail.deliveries.slice(0, 8).map((item, index) => <article className="audit-item compact" key={`${item.delivery_type}-${index}`}><b>{item.kind}</b><p>{item.status}</p><small>{formatTime(item.scheduled_for)}</small></article>)}</DetailSection></aside></div>;
}

function LifecycleControls({ task, onSave }: { task: Task; onSave: (action: ManagementStatusAction, requestId: string) => Promise<void> }) {
  const [saving, setSaving] = useState<ManagementStatusAction | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const requestIds = useRef<Partial<Record<ManagementStatusAction, string>>>({});
  const actionable = task.status === "todo" || task.status === "overdue";
  const reviewable = task.status === "pending";
  const restoreable = task.status === "done" || task.status === "cancelled";

  async function apply(action: ManagementStatusAction) {
    const copy = {
      confirm: { question: `确认 ${task.task_code} 是真实任务并进入待办？确认后会私聊负责人。`, success: "任务已确认，负责人通知与提醒计划已生成。" },
      complete: { question: `确认将 ${task.task_code} 标记为已完成？`, success: "任务已完成，未发送提醒已取消。" },
      cancel: { question: `确认取消 ${task.task_code}？该操作会取消所有未发送提醒。`, success: "任务已取消，未发送提醒已取消。" },
      invalidate: { question: `确认将 ${task.task_code} 标记为误识别并撤销？`, success: "误识别任务已撤销，并保留审计记录。" },
      restore: { question: `确认恢复 ${task.task_code}？原完成或取消记录会保留，并按当前截止时间重新生成提醒。`, success: "任务已恢复，提醒计划已重建。" },
      merge: { question: "", success: "任务已合并。" },
    }[action];
    if (!window.confirm(copy.question)) return;
    setSaving(action); setNotice(""); setError("");
    if (!requestIds.current[action]) requestIds.current[action] = window.crypto.randomUUID();
    try {
      await onSave(action, requestIds.current[action]!);
      requestIds.current[action] = ""; setNotice(copy.success);
    } catch (reason) { setError(taskMutationError(reason)); }
    finally { setSaving(null); }
  }

  if (!actionable && !reviewable && !restoreable && !notice && !error) return null;
  return <section className={`lifecycle-controls ${reviewable ? "review" : ""}`} aria-label={reviewable ? "待确认任务审核" : "任务生命周期操作"}><div><h3>{reviewable ? "审核识别结果" : restoreable ? "终态任务恢复" : "任务状态"}</h3><p>{reviewable ? "该结果置信度较低，确认前不会提醒负责人或生成截止提醒。" : restoreable ? "恢复会保留原完成或取消审计，并按当前截止时间重建提醒。" : "状态操作会写入审计，并同步取消未发送提醒。"}</p></div>{reviewable ? <div className="lifecycle-actions review-actions"><button className="confirm" type="button" disabled={saving !== null} onClick={() => apply("confirm")}>{saving === "confirm" ? "确认中…" : "确认是真实任务"}</button><button className="invalidate" type="button" disabled={saving !== null} onClick={() => apply("invalidate")}>{saving === "invalidate" ? "处理中…" : "不是任务，撤销"}</button></div> : null}{actionable ? <div className="lifecycle-actions"><button className="complete" type="button" disabled={saving !== null} onClick={() => apply("complete")}>{saving === "complete" ? "处理中…" : "标记完成"}</button><button className="cancel" type="button" disabled={saving !== null} onClick={() => apply("cancel")}>{saving === "cancel" ? "处理中…" : "取消任务"}</button><button className="invalidate" type="button" disabled={saving !== null} onClick={() => apply("invalidate")}>{saving === "invalidate" ? "处理中…" : "撤销误识别"}</button></div> : null}{restoreable ? <div className="lifecycle-actions restore-actions"><button className="restore" type="button" disabled={saving !== null} onClick={() => apply("restore")}>{saving === "restore" ? "恢复中…" : "恢复为开放任务"}</button></div> : null}{notice ? <p className="editor-success" role="status">{notice}</p> : null}{error ? <p className="editor-error" role="alert">{error}</p> : null}</section>;
}

function MergeEditor({ task, targets, onSave }: { task: Task; targets: Task[]; onSave: (action: ManagementStatusAction, requestId: string, targetTaskId?: number) => Promise<void> }) {
  const sourceMergeable = ["pending", "todo", "overdue", "done"].includes(task.status);
  const options = targets.filter((item) => item.task_id !== task.task_id && item.chat_id === task.chat_id && ["pending", "todo", "overdue", "done"].includes(item.status));
  const [targetTaskId, setTargetTaskId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef("");
  if (!sourceMergeable || !options.length) return null;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!targetTaskId) return;
    const target = options.find((item) => item.task_id === Number(targetTaskId));
    if (!target || !window.confirm(`确认将 ${task.task_code} 合并到 ${target.task_code}？原任务不会删除，证据和检测来源会并入保留任务，未发送提醒会取消。`)) return;
    setSaving(true); setError("");
    if (!requestId.current) requestId.current = window.crypto.randomUUID();
    try {
      await onSave("merge", requestId.current, target.task_id);
      requestId.current = "";
    } catch (reason) {
      if (reason instanceof UnauthorizedError) setError("登录已失效，请返回飞书重新进入后台。");
      else if (reason instanceof ApiError && reason.status === 409) setError("任务或合并目标状态已经变化，请关闭详情后重新打开。");
      else setError("合并失败，请稍后重试。");
    } finally { setSaving(false); }
  }

  return <form className="merge-editor" onSubmit={submit}><div className="editor-heading"><div><label htmlFor={`merge-${task.task_id}`}>合并重复任务</label><small>保留目标任务编号，原任务仍保留审计</small></div></div><select id={`merge-${task.task_id}`} value={targetTaskId} onChange={(event) => { setTargetTaskId(event.target.value); setError(""); requestId.current = ""; }}><option value="">选择保留任务</option>{options.map((item) => <option key={item.task_id} value={item.task_id}>{item.task_code} · {item.title}</option>)}</select><button type="submit" disabled={saving || !targetTaskId}>{saving ? "合并中…" : "合并到选定任务"}</button>{error ? <p className="editor-error" role="alert">{error}</p> : null}</form>;
}

function TitleEditor({ task, onSave }: { task: Task; onSave: (title: string, requestId: string) => Promise<void> }) {
  const [value, setValue] = useState(task.title);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const requestId = useRef("");
  const normalized = value.trim().replace(/\s+/g, " ");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!normalized || normalized === task.title || normalized.length > 200) return;
    setSaving(true); setNotice(""); setError("");
    if (!requestId.current) requestId.current = window.crypto.randomUUID();
    try {
      await onSave(normalized, requestId.current);
      requestId.current = ""; setNotice("任务标题已更新。");
    } catch (reason) { setError(taskMutationError(reason)); }
    finally { setSaving(false); }
  }

  return <form className="correction-editor" onSubmit={submit}><div className="editor-heading"><div><label htmlFor={`title-${task.task_id}`}>修改任务标题</label><small>1–200 个字符</small></div></div><input id={`title-${task.task_id}`} maxLength={200} value={value} onChange={(event) => { setValue(event.target.value); setNotice(""); setError(""); requestId.current = ""; }} /><button type="submit" disabled={saving || !normalized || normalized === task.title}>{saving ? "保存中…" : "保存标题"}</button>{notice ? <p className="editor-success" role="status">{notice}</p> : null}{error ? <p className="editor-error" role="alert">{error}</p> : null}</form>;
}

function AssigneeEditor({ members, task, onSave }: { members: Member[]; task: Task; onSave: (openIds: string[], requestId: string) => Promise<void> }) {
  const [selected, setSelected] = useState(task.assignees.map((item) => item.open_id));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const requestId = useRef("");
  const current = task.assignees.map((item) => item.open_id);
  const unchanged = sameOrder(selected, current);

  function toggle(openId: string, checked: boolean) {
    setSelected((items) => checked ? [...items, openId] : items.filter((item) => item !== openId));
    setNotice(""); setError(""); requestId.current = "";
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected.length || unchanged || selected.length > 20) return;
    setSaving(true); setNotice(""); setError("");
    if (!requestId.current) requestId.current = window.crypto.randomUUID();
    try {
      await onSave(selected, requestId.current);
      requestId.current = ""; setNotice("负责人已更新，提醒计划已重新生成。");
    } catch (reason) { setError(taskMutationError(reason)); }
    finally { setSaving(false); }
  }

  return <form className="correction-editor assignee-editor" onSubmit={submit}><div className="editor-heading"><div><span>修改负责人</span><small>至少选择 1 人；第一位为主负责人</small></div><b>{selected.length}/20</b></div><div className="assignee-options">{members.map((member) => { const checked = selected.includes(member.open_id); const position = selected.indexOf(member.open_id); const disabled = !member.task_alias || (!checked && selected.length >= 20); return <label className={disabled ? "disabled" : ""} key={member.open_id}><input type="checkbox" checked={checked} disabled={disabled || saving} onChange={(event) => toggle(member.open_id, event.target.checked)} /><span><b>{member.task_alias ?? member.feishu_name}</b><small>{member.task_alias ? `飞书名称：${member.feishu_name}` : "尚未绑定任务姓名，不可选择"}</small></span>{position === 0 ? <em>主负责人</em> : position > 0 ? <em>共同负责</em> : null}</label>; })}</div><button type="submit" disabled={saving || !selected.length || unchanged}>{saving ? "保存中…" : "保存负责人"}</button>{notice ? <p className="editor-success" role="status">{notice}</p> : null}{error ? <p className="editor-error" role="alert">{error}</p> : null}</form>;
}

function sameOrder(first: string[], second: string[]) { return first.length === second.length && first.every((item, index) => item === second[index]); }
function readLastChatId() {
  try { return window.localStorage.getItem(LAST_CHAT_STORAGE_KEY) ?? ""; }
  catch { return ""; }
}
function rememberChatId(chatId: string) {
  try { window.localStorage.setItem(LAST_CHAT_STORAGE_KEY, chatId); }
  catch { /* The selector still works when browser storage is unavailable. */ }
}
function buildTaskParams(filter: string, query: string, limit = TASK_PAGE_SIZE, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filter === "open") ["pending", "todo", "overdue"].forEach((status) => params.append("status", status));
  else if (filter !== "all") params.append("status", filter);
  if (query.trim()) params.set("query", query.trim());
  return params;
}
function readTaskViewState() {
  if (typeof window === "undefined") return { chatId: "", filter: "open", query: "", page: 1 };
  const params = new URLSearchParams(window.location.search);
  const rawPage = Number(params.get("page") ?? "1");
  const requestedFilter = params.get("filter") ?? "open";
  return {
    chatId: params.get("chat")?.trim() ?? "",
    filter: ["open", "pending", "todo", "overdue", "all"].includes(requestedFilter) ? requestedFilter : "open",
    query: params.get("q")?.trim() ?? "",
    page: Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1,
  };
}
function writeTaskViewState(next: { chatId?: string; filter?: string; query?: string; page?: number }) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (next.chatId !== undefined) {
    if (next.chatId) url.searchParams.set("chat", next.chatId);
    else url.searchParams.delete("chat");
  }
  if (next.filter !== undefined) {
    if (next.filter && next.filter !== "open") url.searchParams.set("filter", next.filter);
    else url.searchParams.delete("filter");
  }
  if (next.query !== undefined) {
    if (next.query) url.searchParams.set("q", next.query);
    else url.searchParams.delete("q");
  }
  if (next.page !== undefined) {
    if (next.page > 1) url.searchParams.set("page", String(next.page));
    else url.searchParams.delete("page");
  }
  window.history.replaceState({}, "", url);
}
function taskMutationError(reason: unknown) {
  if (reason instanceof UnauthorizedError) return "登录已失效，请返回飞书重新进入后台。";
  if (reason instanceof ApiError && reason.status === 409) return "任务或群成员状态已经变化，请关闭详情后重新打开。";
  return "修改失败，请稍后重试。";
}
function taskStatusTone(status: string) { return status === "overdue" ? "red" : status === "done" ? "green" : status === "pending" ? "amber" : "blue"; }
function DetailSection({ title, empty, children }: { title: string; empty: string; children: React.ReactNode }) { const count = Array.isArray(children) ? children.length : children ? 1 : 0; return <section className="detail-section"><h3>{title}</h3>{count ? children : <p className="empty-copy">{empty}</p>}</section>; }
function SignedOut() { return <main className="signed-out"><section><div className="brand-mark">LT</div><p className="eyebrow">LAB TASK CONSOLE</p><h1>请从飞书进入管理后台</h1><p>私聊机器人发送“管理后台”，打开 5 分钟内有效的一次性链接。</p><span>页面不会接受手动填写的 Open ID。</span></section></main>; }
function formatDeadline(value: string | null) { return value ? formatTime(value) : "未设置"; }
function formatTime(value: string) { return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)); }
function formatShanghaiInput(value: string | null) {
  if (!value) return "";
  const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date(value));
  const item = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "";
  return `${item("year")}-${item("month")}-${item("day")}T${item("hour")}:${item("minute")}`;
}
