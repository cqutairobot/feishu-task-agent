import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { createServer } from "node:net";
import test from "node:test";
import { fileURLToPath } from "node:url";

async function render() {
  const frontendRoot = fileURLToPath(new URL("..", import.meta.url));
  const cli = fileURLToPath(
    new URL("../node_modules/vinext/dist/cli.js", import.meta.url),
  );
  const port = await availablePort();
  const child = spawn(
    process.execPath,
    [cli, "start", "--hostname", "127.0.0.1", "--port", String(port)],
    {
      cwd: frontendRoot,
      env: { ...process.env, NODE_ENV: "production" },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk; });
  child.stderr.on("data", (chunk) => { output += chunk; });
  try {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (child.exitCode !== null) {
        throw new Error(`vinext start exited with ${child.exitCode}:\n${output}`);
      }
      try {
        const response = await fetch(`http://127.0.0.1:${port}/`, {
          headers: { accept: "text/html" },
        });
        const body = await response.arrayBuffer();
        return new Response(body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      } catch {
        await delay(100);
      }
    }
    throw new Error(`vinext start was not ready after 10 seconds:\n${output}`);
  } finally {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      await Promise.race([once(child, "exit"), delay(3_000)]);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
  }
}

async function availablePort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : null;
  await new Promise((resolve) => server.close(resolve));
  if (port === null) throw new Error("could not allocate test port");
  return port;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

test("server-renders the Lab Task management shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /任务总览/);
  assert.match(html, /Lab Task Console/i);
  assert.match(html, /群管理员与审计/);
  assert.match(html, /群设置/);
  assert.match(html, /权限连接正常/);
  assert.doesNotMatch(html, /starter|loading skeleton/i);
});

test("browser API requests rely on the HttpOnly session cookie", async () => {
  const source = await readFile(
    new URL("../app/dashboard-client.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /credentials:\s*"include"/);
  assert.match(source, /NEXT_PUBLIC_MANAGEMENT_API_URL \?\? ""/);
  assert.match(source, /\/api\/chats/);
  assert.match(source, /飞书名称：/);
  assert.match(source, /resource: "deadline" \| "title" \| "assignees" \| "status"/);
  assert.match(source, /\/tasks\/\$\{taskId\}\/\$\{resource\}/);
  assert.match(source, /mutateTask\("deadline"/);
  assert.match(source, /mutateTask\("title"/);
  assert.match(source, /mutateTask\("assignees"/);
  assert.match(source, /mutateTask\("status"/);
  assert.match(source, /标记完成/);
  assert.match(source, /取消任务/);
  assert.match(source, /撤销误识别/);
  assert.match(source, /待审核/);
  assert.match(source, /确认是真实任务/);
  assert.match(source, /不是任务，撤销/);
  assert.match(source, /恢复为开放任务/);
  assert.match(source, /"restore"/);
  assert.match(source, /验收通过/);
  assert.match(source, /待管理员验收/);
  assert.match(source, /已验收通过/);
  assert.match(source, /"accept"/);
  assert.match(source, /completionReviewable/);
  assert.match(source, /合并重复任务/);
  assert.match(source, /onSave\("merge"/);
  assert.match(source, /sourceMergeable/);
  assert.match(source, /\["pending", "todo", "overdue", "done"\]\.includes\(item\.status\)/);
  assert.match(source, /"confirm" \| "complete"/);
  assert.match(source, /＋ 新建任务/);
  assert.match(source, /创建并通知负责人/);
  assert.match(source, /正在核验群成员/);
  assert.match(source, /重新加载群成员/);
  assert.match(source, /已填写的任务内容会保留/);
  assert.match(source, /setTaskCreationMembersError/);
  assert.doesNotMatch(source, /setCreating\(false\);\s*handleFailure\(reason,\s*"当前群成员加载失败/);
  assert.match(source, /管理员手动补建/);
  assert.match(source, /creation_source/);
  assert.match(source, /搜索任务/);
  assert.match(source, /编号、标题或说明/);
  assert.match(source, /buildTaskParams\(filter, query/);
  assert.match(source, /writeTaskViewState/);
  assert.match(source, /query\.trim\(\)/);
  assert.match(source, /TASK_PAGE_SIZE = 10/);
  assert.match(source, /自动任务识别/);
  assert.match(source, /自动进入待办的置信度/);
  assert.match(source, /chat-detection-enabled/);
  assert.match(source, /chat-auto-todo-confidence/);
  assert.match(source, /chat-auto-todo-confidence[\s\S]{0,300}disabled=\{saving \|\| !enabled\}/);
  assert.match(source, /负责人截止提醒/);
  assert.match(source, /第一提醒/);
  assert.match(source, /第二提醒/);
  assert.match(source, /截止当天/);
  assert.match(source, /任务逾期/);
  assert.match(source, /reminder_due_72h_enabled/);
  assert.match(source, /reminder_due_24h_enabled/);
  assert.match(source, /reminder_due_today_enabled/);
  assert.match(source, /reminder_overdue_enabled/);
  assert.match(source, /reminder_due_72h_offset_hours/);
  assert.match(source, /reminder_due_24h_offset_hours/);
  assert.match(source, /reminder_due_today_hour/);
  assert.match(source, /reminder_overdue_grace_minutes/);
  assert.match(source, /第一提醒必须早于第二提醒/);
  assert.match(source, /未设置截止时间/);
  assert.match(source, /提醒负责人/);
  assert.match(source, /升级管理员/);
  assert.match(source, /missing_deadline_owner_enabled/);
  assert.match(source, /missing_deadline_admin_enabled/);
  assert.match(source, /missing_deadline_owner_delay_hours/);
  assert.match(source, /missing_deadline_admin_delay_hours/);
  assert.match(source, /升级管理员必须晚于提醒负责人/);
  assert.match(source, /管理员通知对象/);
  assert.match(source, /全部管理员/);
  assert.match(source, /指定管理员/);
  assert.match(source, /administrator_notification_mode/);
  assert.match(source, /administrator_notification_open_ids/);
  assert.match(source, /任务识别范围/);
  assert.match(source, /宽泛任务（默认）/);
  assert.match(source, /仅工作 \/ 科研/);
  assert.match(source, /task_scope/);
  assert.match(source, /请至少选择一名当前群管理员/);
  assert.match(source, /const \[filter, setFilter\] = useState\("open"\)/);
  assert.match(source, /const \[query, setQuery\] = useState\(""\)/);
  assert.match(source, /const \[page, setPage\] = useState\(1\)/);
  assert.match(source, /useEffect\(\(\) => \{\s*const state = readTaskViewState\(\)/);
  assert.match(source, /total_pages/);
  assert.match(source, /page: number/);
  assert.match(source, /上一页/);
  assert.match(source, /下一页/);
  assert.match(source, /第 \{page\} \/ \{taskPage\.total_pages\} 页/);
  assert.match(source, /method:\s*"POST"/);
  assert.match(source, /open_ids:\s*selected/);
  assert.match(source, /window\.confirm/);
  assert.match(source, /lab-task-console:last-chat-id/);
  assert.match(source, /window\.localStorage\.getItem/);
  assert.match(source, /items\.some\(\(chat\) => chat\.chat_id === remembered\)/);
  assert.match(source, /尚未绑定任务姓名，不可选择/);
  assert.match(source, /第一位为主负责人/);
  assert.match(source, /北京时间（UTC\+8）/);
  assert.match(source, /window\.crypto\.randomUUID\(\)/);
  assert.match(source, /const dashboardRequest = useRef\(0\)/);
  assert.match(source, /const taskDetailAbort = useRef<AbortController \| null>\(null\)/);
  assert.match(source, /token !== dashboardRequest\.current/);
  assert.match(source, /activeChatId\.current !== requestedChatId/);
  assert.match(source, /function changeChat[\s\S]{0,900}setDetail\(null\)/);
  assert.match(source, /setRefreshVersion\(\(version\) => version \+ 1\)/);
  assert.match(source, /loadAllMergeTargets/);
  assert.match(source, /offset >= page\.total_count/);
  assert.match(source, /setValue\(task\.title\)/);
  assert.match(source, /setSelected\(task\.assignees\.map/);
  assert.match(source, /hourCycle: "h23"/);
  assert.match(source, /item\("hour"\) === "24" \? "00"/);
  assert.match(source, /责任链/);
  assert.match(source, /完成周期/);
  assert.match(source, /任务全过程/);
  assert.match(source, /completion_submissions/);
  assert.match(source, /detail\.timeline/);
  assert.match(source, /截止时间已调整，旧提醒自动取消/);
  assert.match(source, /readableReason\(event\.reason\)/);
  assert.match(source, /role="dialog"/);
  assert.match(source, /event\.key === "Escape"/);
  assert.match(source, /previousFocus\?\.focus\(\)/);
  assert.doesNotMatch(source, /actor_open_id=|请输入.*Open ID/i);
});

test("base group settings cannot overwrite administrator notification recipients", async () => {
  const source = await readFile(
    new URL("../app/dashboard-client.tsx", import.meta.url),
    "utf8",
  );
  const start = source.indexOf("function BaseSettingsWorkspace");
  const end = source.indexOf("\nfunction ReminderStage", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const baseSettingsSource = source.slice(start, end);

  assert.doesNotMatch(baseSettingsSource, /administratorNotificationMode/);
  assert.doesNotMatch(baseSettingsSource, /administratorNotificationOpenIds/);
  assert.doesNotMatch(baseSettingsSource, /administrator_notification_mode/);
  assert.doesNotMatch(baseSettingsSource, /administrator_notification_open_ids/);
});

test("provenance timeline filters and long content disclosures remain accessible", async () => {
  const source = await readFile(
    new URL("../app/dashboard-client.tsx", import.meta.url),
    "utf8",
  );
  const styles = await readFile(
    new URL("../app/globals.css", import.meta.url),
    "utf8",
  );

  assert.match(source, /type TimelineFilter = "all" \| "status" \| "note" \| "completion_submission" \| "delivery"/);
  assert.match(source, /aria-label="按事件类型筛选"/);
  assert.match(source, /aria-pressed=\{filter === option\.value\}/);
  assert.match(source, /aria-live="polite"/);
  assert.match(source, /timelineEventMatchesFilter\(event, filter\)/);
  assert.match(source, /function ExpandableText/);
  assert.match(source, /aria-controls=\{contentId\}/);
  assert.match(source, /aria-expanded=\{expanded\}/);
  assert.match(source, /content\.scrollHeight > content\.clientHeight \+ 1/);
  assert.match(source, /new ResizeObserver\(update\)/);
  assert.match(source, /展开全文/);
  assert.match(source, /收起全文/);
  assert.match(styles, /\.timeline-filter-bar \{[^}]*flex-wrap:wrap/);
  assert.match(styles, /\.expandable-copy\.is-collapsed>p \{[^}]*-webkit-line-clamp:4/);
  assert.match(styles, /@media \(max-width:600px\)[\s\S]*\.timeline-filter-bar button \{ min-height:44px/);
});

test("task detail requests are invalidated when the active group changes", async () => {
  const source = await readFile(
    new URL("../app/dashboard-client.tsx", import.meta.url),
    "utf8",
  );
  const openStart = source.indexOf("async function openTask");
  const openEnd = source.indexOf("\n  async function loadTaskCreationMembers", openStart);
  const switchStart = source.indexOf("function changeChat");
  const switchEnd = source.indexOf("\n  async function logout", switchStart);
  assert.notEqual(openStart, -1);
  assert.notEqual(openEnd, -1);
  assert.notEqual(switchStart, -1);
  assert.notEqual(switchEnd, -1);

  const openTaskSource = source.slice(openStart, openEnd);
  const changeChatSource = source.slice(switchStart, switchEnd);
  assert.match(openTaskSource, /taskDetailAbort\.current\?\.abort\(\)/);
  assert.match(openTaskSource, /token !== taskDetailRequest\.current \|\| activeChatId\.current !== requestedChatId/);
  assert.match(openTaskSource, /setDetail\(taskDetail\)/);
  assert.match(changeChatSource, /activeChatId\.current = nextChatId/);
  assert.match(changeChatSource, /taskDetailRequest\.current \+= 1/);
  assert.match(changeChatSource, /taskDetailAbort\.current\?\.abort\(\)/);
  assert.match(changeChatSource, /setDetail\(null\)/);
});
