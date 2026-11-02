const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
const state = { token: "", identity: null, room: "", tab: "messages", selected: null, data: null, busy: false, ready: false };
const names = {
  open: "进行中", completed: "已完成", queued: "等待处理", running: "处理中", review: "待人工审核",
  scheduled: "等待到期", paused: "已暂停", claimed: "正在唤醒", fired: "已生成任务", cancelled: "已取消",
  pending: "等待接收", processed: "已处理", dispatched: "已交给 AstrBot", held_media: "媒体消息",
  candidate_requires_eval: "候选结果等待证据审核", candidate_rejected: "候选结果需重新审核",
  interrupted_unknown_result: "上次处理被中断，结果待确认", worker_failed: "处理失败，待人工确认",
  thread_opened: "建立目标", ticket_queued: "任务排队", ticket_claimed: "开始处理", candidate_submitted: "生成候选",
  candidate_approved: "审核通过", evidence_recorded: "添加证据", outbound_draft_created: "保存草稿",
  subscription_scheduled: "安排跟进", subscription_fired: "跟进已触发", thread_completed: "目标已完成",
};
const can = (cap) => Boolean(state.identity?.capabilities.includes(cap));
const date = (n) => n ? new Date(n * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
const key = () => globalThis.crypto?.randomUUID?.() || `manual-${Date.now()}-${Math.random().toString(36).slice(2)}`;
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function notice(text, error = false) { $("notice").textContent = text; $("notice").classList.toggle("error", error); }
function empty(parent, title, text) {
  const node = el("div", undefined, "empty");
  node.append(el("span", "◎", "empty-symbol"), el("h3", title), el("p", text));
  parent.replaceChildren(node);
}
function badge(status) { return el("span", names[status] || "待确认", `badge ${status}`); }
function locks() {
  document.querySelector("main").setAttribute("aria-busy", String(state.busy));
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = state.busy || Boolean(button.dataset.blocked) || (button.dataset.cap && !can(button.dataset.cap));
  });
  $("room").disabled = state.busy;
  $("refresh").disabled = state.busy || !state.identity;
  $("connect-form").querySelector("button").disabled = state.busy || !state.ready;
}
async function run(work) {
  if (state.busy) return;
  state.busy = true; locks();
  try { await work(); }
  catch (error) { notice(error.message || "操作未完成，请重试。", true); }
  finally { state.busy = false; locks(); }
}
async function api(operation, extra = {}) {
  return bridge.apiPost("tasks", { token: state.token, room: state.room, operation, ...extra });
}
async function action(operation, parameters) {
  const response = await api(operation, { parameters });
  return response.result;
}
function button(label, cap, handler, primary = false) {
  const node = el("button", label, primary ? "primary" : "");
  node.type = "button";
  if (cap) node.dataset.cap = cap;
  node.disabled = state.busy || (cap && !can(cap));
  node.addEventListener("click", () => { if (!state.busy) handler(); });
  return node;
}
const tabCopy = {
  messages: ["群消息", "选择一条已有文本消息，明确本次处理目标。", "thread:read"],
  tasks: ["处理目标", "查看任务进度，审核候选结果并保存草稿。", "thread:read"],
  subscriptions: ["定时跟进", "到期后生成处理任务，可随时暂停或取消。", "subscription:read"],
};
function renderList() {
  const [title, description, capability] = tabCopy[state.tab];
  $("list-title").textContent = title; $("list-description").textContent = description;
  document.querySelectorAll("[data-tab]").forEach((node) => {
    if (node.dataset.tab === state.tab) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  if (!state.identity) return empty($("list"), "先连接，再开始处理", "连接后可查看授权群的已有消息、任务和定时跟进。");
  if (!can(capability)) return empty($("list"), "当前凭据没有查看权限", "换用具有该群读取权限的操作者凭据。");
  const query = $("search").value.trim().toLowerCase();
  const rows = (state.data?.[state.tab === "tasks" ? "threads" : state.tab] || []).filter((row) =>
    [row.content, row.goal, row.task, row.sender].some((text) => String(text || "").toLowerCase().includes(query)));
  $("list").replaceChildren();
  if (!rows.length) return empty($("list"), query ? "没有匹配的记录" : "暂时没有记录", state.tab === "messages" ? "请确认当前群已有消息进入独立实例的收件箱，然后刷新。" : "先从“消息建单”建立处理目标，再添加任务或定时跟进。");
  for (const row of rows) {
    const id = row.event_id || row.thread_id || row.subscription_id;
    // Subscriptions also have thread_id, so always prefer their own identity.
    const recordId = state.tab === "subscriptions" ? row.subscription_id : id;
    const node = el("button", undefined, "record"); node.type = "button";
    node.setAttribute("aria-pressed", String(state.selected?.id === recordId));
    const top = el("div", undefined, "record-top");
    top.append(el("span", state.tab === "messages" ? row.sender : date(row.due_at || row.created_at), "subtle"), badge(row.status));
    node.append(top, el("div", row.content || row.goal || row.task || "非文本消息", "record-title"));
    if (state.tab === "messages") node.append(el("small", date(row.created_at)));
    if (state.tab === "tasks") {
      const tickets = state.data.tickets.filter((t) => t.thread_id === row.thread_id);
      node.append(el("small", `${tickets.filter((t) => t.status === "review").length} 项待审核 · 最近记录中 ${tickets.length} 项任务`));
    }
    node.addEventListener("click", () => run(async () => {
      state.selected = { type: state.tab, id: recordId }; renderList(); await renderDetail();
    }));
    $("list").append(node);
  }
}
async function refresh() {
  state.data = await api("snapshot");
  $("messages-count").textContent = state.data.messages.length;
  $("tasks-count").textContent = state.data.threads.length;
  $("subscriptions-count").textContent = state.data.subscriptions.length;
  $("updated").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  renderList(); await renderDetail();
}
function heading(title, status) {
  const wrap = el("div", undefined, "detail-title"); wrap.append(el("h2", title), badge(status)); $("detail").append(wrap);
}
function form(title, description, fields, saveLabel, onSave) {
  $("editor-title").textContent = title; $("editor-description").textContent = description;
  $("editor-error").textContent = ""; $("save-editor").textContent = saveLabel;
  $("editor-fields").replaceChildren();
  const controls = {};
  for (const field of fields) {
    const label = el("label", undefined, "field"); label.append(el("span", field.label));
    let input;
    if (field.options) {
      input = el("select");
      for (const [value, text] of field.options) { const option = el("option", text); option.value = value; input.append(option); }
    } else input = el(field.multiline ? "textarea" : "input");
    if (!field.multiline && !field.options) input.type = field.type || "text";
    input.name = field.name; input.required = !field.optional; input.value = field.value || "";
    if (field.max) input.maxLength = field.max;
    if (field.type === "datetime-local") input.min = localDate(new Date());
    label.append(input); if (field.help) label.append(el("small", field.help));
    $("editor-fields").append(label); controls[field.name] = input;
  }
  $("editor-form").onsubmit = (event) => {
    event.preventDefault();
    run(async () => {
      const values = Object.fromEntries(Object.entries(controls).map(([name, input]) => [name, input.value.trim()]));
      if (Object.values(values).some((v) => !v)) { $("editor-error").textContent = "请填写所有字段。"; return; }
      try { await onSave(values); }
      catch (error) { $("editor-error").textContent = error.message || "操作未完成，请检查填写内容。"; return; }
      $("editor").close(); notice("操作已保存。");
      try { await refresh(); }
      catch { notice("操作已保存，但刷新失败，请点击刷新查看最新状态。", true); }
    });
  };
  $("editor").showModal();
}
const localDate = (value) => new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
function addTicket(thread) {
  const clientKey = key();
  form("添加处理任务", "保存后，已启用的 Worker 会领取任务；结果先进入人工审核。", [
    { name: "task", label: "需要处理什么", multiline: true, max: 1300 },
  ], "加入处理队列", (values) => action("enqueue_ticket", { thread_id: thread.thread_id, client_key: clientKey, task: values.task }));
}
function schedule(thread) {
  const clientKey = key();
  form("安排定时跟进", "这是一次性跟进。到期后生成任务，由已启用的 Worker 处理。", [
    { name: "task", label: "跟进内容", multiline: true, max: 1300 },
    { name: "due_at", label: "跟进时间", type: "datetime-local", value: localDate(new Date(Date.now() + 3600000)), help: "按当前浏览器的本地时间填写。" },
  ], "安排跟进", (values) => {
    const dueAt = Math.floor(new Date(values.due_at).getTime() / 1000);
    if (!Number.isFinite(dueAt) || dueAt <= Date.now() / 1000) throw new Error("请选择未来的跟进时间。");
    return action("schedule_followup", { thread_id: thread.thread_id, client_key: clientKey, task: values.task, due_at: dueAt });
  });
}
async function renderDetail() {
  const box = $("detail"); box.replaceChildren();
  if (!state.selected) return empty(box, "选中一条记录", "这里显示原始内容、下一步操作和处理记录。");
  const { type, id } = state.selected;
  if (type === "messages") {
    const row = state.data.messages.find((r) => r.event_id === id);
    if (!row) return empty(box, "消息不在当前列表", "请重新选择一条最近消息。");
    heading("消息内容", row.status); box.append(el("p", `${row.sender} · ${date(row.created_at)}`, "subtle"), el("p", row.content || "此消息没有文本内容。", "content source"), el("p", `消息 ID：${row.event_id}`, "subtle"));
    if (row.content.length >= 4000) box.append(el("p", "此处仅显示消息前 4000 个字符。", "subtle"));
    if (row.kind !== 1) { box.append(el("p", "目前只支持从文本消息建立任务目标。", "subtle")); return; }
    const existing = state.data.threads.find((t) => t.trigger_event_id === row.event_id);
    const actions = el("div", undefined, "actions");
    if (existing) actions.append(button("查看已有目标", "thread:read", () => run(async () => { state.tab = "tasks"; state.selected = { type: "tasks", id: existing.thread_id }; renderList(); await renderDetail(); })));
    else actions.append(button("从这条消息建单", "thread:create", () => form("明确处理目标", "目标保存后不可修改；同一条来源消息只建立一个目标。", [
      { name: "goal", label: "这次要解决什么", multiline: true, max: 1300 },
    ], "建立目标", async (values) => {
      const threadId = await action("open_thread", { trigger_event_id: row.event_id, goal: values.goal });
      state.tab = "tasks"; state.selected = { type: "tasks", id: threadId };
    }), true));
    box.append(actions); return;
  }
  if (type === "tasks") {
    const data = await api("thread_detail", { thread_id: id });
    const thread = data.thread;
    heading(thread.goal, thread.status); box.append(el("p", `创建于 ${date(thread.created_at)}`, "subtle"));
    const actions = el("div", undefined, "actions");
    if (thread.status === "open") {
      actions.append(button("添加任务", "ticket:create", () => addTicket(thread), true), button("安排跟进", "subscription:manage", () => schedule(thread)));
      actions.append(button("完成目标", "thread:complete", () => form("完成这个目标", "所有任务须已完成，且没有待处理的订阅。", [
        { name: "confirmation", label: "输入“完成”确认", max: 10 },
      ], "完成目标", (values) => { if (values.confirmation !== "完成") throw new Error("请输入“完成”。"); return action("complete_thread", { thread_id: id }); })));
    }
    box.append(actions, el("h3", "处理任务"));
    if (!can("ticket:read")) box.append(el("p", "当前凭据没有任务读取权限。", "subtle"));
    else if (!data.tickets.length) box.append(el("p", "还没有任务。点击“添加任务”说明需要处理的内容。", "subtle"));
    for (const ticket of data.tickets) {
      const wrap = el("div", undefined, "ticket");
      const node = button(ticket.task, "ticket:read", () => run(async () => { state.selected = { type: "ticket", id: ticket.ticket_id, threadId: id }; await renderDetail(); }));
      wrap.append(badge(ticket.status), node); box.append(wrap);
    }
    if (data.tickets.length === 60) box.append(el("p", "显示此目标最近 60 项任务。", "subtle"));
    if (data.audit.length) {
      const details = el("details", undefined, "audit"); details.append(el("summary", "最近处理记录"));
      const list = el("ol"); for (const row of data.audit) list.append(el("li", `${date(row.created_at)} · ${row.actor} · ${names[row.action] || row.action}${row.detail ? `：${row.detail}` : ""}`));
      details.append(list); box.append(details);
    }
    return;
  }
  if (type === "ticket") {
    const ticket = await action("get_ticket", { ticket_id: id });
    const evidence = await action("list_evidence", { ticket_id: id });
    box.append(button("返回处理目标", "thread:read", () => run(async () => { state.selected = { type: "tasks", id: ticket.thread_id }; await renderDetail(); })));
    heading(ticket.task, ticket.status);
    if (ticket.review_reason) box.append(el("p", names[ticket.review_reason] || "处理记录需要人工确认。", "subtle"));
    const hasCandidate = ticket.status === "completed" || ["candidate_requires_eval", "candidate_rejected"].includes(ticket.review_reason);
    const resultText = hasCandidate ? ticket.result : (ticket.status === "queued" ? "等待已启用的 Worker 领取。" : ticket.status === "running" ? "Worker 正在处理，稍后刷新查看结果。" : "本次处理未形成可审核的候选，请人工确认后决定是否重新排队。");
    box.append(el("h3", "候选结果"), el("div", resultText || "暂时没有候选结果。", "candidate"));
    box.append(el("h3", `审核证据（${evidence.length}）`));
    if (!evidence.length) box.append(el("p", "审核通过前，至少添加一条独立证据，并自行核对原始内容。", "subtle"));
    for (const row of evidence) { const item = el("div", undefined, "evidence"); item.append(el("p", row.summary), el("small", row.source_ref)); box.append(item); }
    const actions = el("div", undefined, "actions");
    const reviewable = ticket.status === "review" && ticket.result && ["candidate_requires_eval", "candidate_rejected"].includes(ticket.review_reason);
    if (reviewable) {
      actions.append(button("添加证据", "ticket:review", () => form("添加独立证据", "仅记录依据，不自动访问网址或读取文件。来源消息本身不能作为独立证据。", [
        { name: "source_type", label: "证据类型", value: "inbox_message", options: [["inbox_message", "本群另一条消息"], ["https_url", "HTTPS 网址"], ["sha256", "文件 SHA-256"]] },
        { name: "source_ref", label: "消息 ID、网址或文件摘要", max: 1000 },
        { name: "summary", label: "这条依据说明什么", multiline: true, max: 600 },
      ], "记录证据", (values) => action("record_evidence", { ticket_id: id, ...values }))));
      const approve = button("审核通过", "ticket:review", () => form("审核通过", "确认已核对独立证据；本操作将任务标为完成，仍不会发送微信。", [
        { name: "reason", label: "审核依据", multiline: true, max: 300 },
      ], "确认通过", (values) => action("approve_candidate", { ticket_id: id, reason: values.reason })), true);
      approve.disabled = !evidence.length || !can("ticket:review");
      if (!evidence.length) approve.dataset.blocked = "true";
      actions.append(approve);
    }
    if (ticket.status === "review") actions.append(button("重新排队", "ticket:retry", () => form("重新处理任务", "已有候选将被清空，并再次调用模型。请先确认需要重新处理。", [
      { name: "reason", label: "重新处理的原因", multiline: true, max: 300 },
    ], "重新排队", (values) => action("requeue_review", { ticket_id: id, reason: values.reason }))));
    if (ticket.status === "completed" && evidence.length) actions.append(button("保存为草稿", "ticket:review", () => run(async () => {
      const draftId = await action("create_ticket_draft", { ticket_id: id });
      notice(`已保存草稿 #${draftId}，没有发送微信。`);
    }), true));
    if (ticket.status === "completed" && !evidence.length) box.append(el("p", "这项历史任务缺少审核证据，不能生成草稿。", "subtle"));
    box.append(actions); return;
  }
  const row = state.data.subscriptions.find((r) => r.subscription_id === id);
  if (!row) return empty(box, "记录不在当前列表", "请重新选择一条最近的定时跟进。");
  heading("定时跟进", row.status); box.append(el("p", row.task, "content"), el("p", `计划时间：${date(row.due_at)}`, "source"));
  if (row.review_reason) box.append(el("p", "这条订阅需要人工确认，请检查处理记录。", "subtle"));
  const actions = el("div", undefined, "actions");
  const change = (label, operation) => button(label, "subscription:manage", () => run(async () => {
    await action(operation, { subscription_id: id }); await refresh(); notice(`${label}已保存。`);
  }));
  if (["scheduled", "claimed"].includes(row.status)) actions.append(change("暂停跟进", "pause_followup"));
  if (row.status === "paused") actions.append(change("恢复跟进", "resume_followup"));
  if (!["fired", "cancelled"].includes(row.status)) actions.append(button("取消跟进", "subscription:manage", () => form("取消这次跟进", "取消后不再生成新的处理任务。", [
    { name: "confirmation", label: "输入“取消”确认", max: 10 },
  ], "确认取消", (values) => { if (values.confirmation !== "取消") throw new Error("请输入“取消”。"); return action("cancel_followup", { subscription_id: id }); })));
  actions.append(button("查看所属目标", "thread:read", () => run(async () => { state.tab = "tasks"; state.selected = { type: "tasks", id: row.thread_id }; renderList(); await renderDetail(); })));
  box.append(actions);
}

$("connect-form").addEventListener("submit", (event) => {
  event.preventDefault(); run(async () => {
    state.token = $("credential").value.trim();
    try {
      const identity = await api("connect");
      state.identity = identity; state.room = identity.rooms[0];
      $("subject").textContent = identity.subject; $("account").textContent = identity.owner;
      $("room").replaceChildren(...identity.rooms.map((room) => { const option = el("option", room); option.value = room; return option; }));
      $("credential").value = ""; $("access").hidden = true; $("scope").hidden = false;
      await refresh(); notice("已连接。选择消息开始建单，或查看已有任务。");
    } catch (error) { state.token = ""; state.identity = null; $("access").hidden = false; $("scope").hidden = true; throw error; }
  });
});
$("disconnect").addEventListener("click", () => {
  if (state.busy) return;
  state.token = ""; state.identity = null; state.data = null; state.selected = null;
  $("access").hidden = false; $("scope").hidden = true;
  for (const id of ["messages-count", "tasks-count", "subscriptions-count"]) $(id).textContent = "—";
  $("updated").textContent = ""; renderList(); renderDetail(); notice("已断开，页面中的操作凭据已清除。"); locks();
});
$("room").addEventListener("change", () => run(async () => {
  state.room = $("room").value; state.selected = null; state.data = null;
  renderList(); await renderDetail(); await refresh(); notice("已切换当前群。");
}));
$("refresh").addEventListener("click", () => run(async () => { await refresh(); notice("已刷新处理状态。"); }));
$("search").addEventListener("input", renderList);
document.querySelectorAll("[data-tab]").forEach((node) => node.addEventListener("click", () => run(async () => {
  state.tab = node.dataset.tab; state.selected = null; $("search").value = ""; renderList(); await renderDetail();
})));
for (const id of ["close-editor", "cancel-editor"]) $(id).addEventListener("click", () => { if (!state.busy) $("editor").close(); });
$("editor").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
locks();
if (!bridge) {
  notice("请在 AstrBot 后台的插件可视化视图中打开此页面。", true);
  $("connect-form").querySelector("button").disabled = true;
} else {
  bridge.ready().then(() => { state.ready = true; locks(); notice("AstrBot 页面接口已就绪，请填写操作凭据。"); })
    .catch(() => notice("页面接口连接失败，请刷新 AstrBot 插件视图。", true));
}
