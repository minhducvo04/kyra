"use strict";
const byId = id => document.getElementById(id);
const statusClass = {review: "needs-you", approved: "done", changes_requested: "needs-you"};
const statusLabels = {review: "Ready for review", approved: "Approved", changes_requested: "Changes requested"};
const findingLabels = {
  dash: "Replace the dash in the document.",
  provider_name: "Remove the drafting attribution.",
  fact_missing: "A required fact is missing.",
  truncation: "The document is incomplete.",
  placeholder: "Replace unfinished text.",
  metadata: "Check the document details."
};
let workflows = [];
let selected = null;
let dirty = false;
let busy = false;

async function readJson(response) {
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "The task could not be completed.");
    error.code = data.error?.code;
    throw error;
  }
  return data;
}
async function api(path, method = "GET", body) {
  return readJson(await fetch(`/api/busy/${path}`, {
    method, headers: {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)
  }));
}
function message(text) { byId("message").textContent = text; }
function node(tag, text, className) {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}
function fields(container, keys, values = {}, readOnly = false) {
  const target = byId(container);
  target.replaceChildren();
  keys.forEach((key, index) => {
    const id = `${container}-${index}`;
    const label = node("label", key.replaceAll("_", " "));
    label.htmlFor = id;
    const input = document.createElement("input");
    input.id = id;
    input.dataset.fact = key;
    input.value = values[key] || "";
    input.readOnly = readOnly;
    input.addEventListener("input", () => {
      if (container === "review-facts") { dirty = true; syncButtons(); }
    });
    target.append(label, input);
  });
}
function facts(container) {
  return Object.fromEntries([...byId(container).querySelectorAll("input")].map(input => [input.dataset.fact, input.value]));
}
function syncButtons() {
  document.querySelectorAll("button").forEach(button => { button.disabled = busy; });
  byId("start-button").disabled = busy || !workflows.length;
  byId("approve").disabled = busy || dirty;
  byId("request-changes").disabled = busy || dirty;
}
async function action(work) {
  if (busy) return;
  busy = true;
  syncButtons();
  message("In progress...");
  try { await work(); }
  catch (error) {
    message(error.code === "approval_refused" ? "Approval refused. Resolve the findings and review again."
      : "The task could not be completed. Your entries are still here; try again.");
  }
  finally { busy = false; syncButtons(); }
}
function title(task) {
  return `${workflows.find(v => v.slug === task.slug)?.title || task.slug} · Version ${task.version}`;
}
async function refresh() {
  const data = await api("tasks");
  byId("tasks").replaceChildren();
  byId("history").replaceChildren();
  data.tasks.forEach(task => {
    const card = node("div", "", "task");
    const status = statusClass[task.status] || "needs-you";
    card.append(node("p", title(task)), node("span", status.replaceAll("-", " "), `status-badge status-${status}`),
      node("p", statusLabels[task.status] || "Check this task."));
    if (task.decided_at) card.append(node("p", new Date(task.decided_at).toLocaleString()));
    if (task.note) card.append(node("p", task.note));
    const button = node("button", "Review");
    button.type = "button";
    button.addEventListener("click", () => action(async () => { await review(task.id); message(""); }));
    card.append(document.createElement("br"), button);
    byId(task.status === "review" ? "tasks" : "history").append(card);
  });
  if (!byId("tasks").children.length) byId("tasks").append(node("p", "No tasks awaiting review."));
  if (!byId("history").children.length) byId("history").append(node("p", "No decisions yet."));
}
async function review(id) {
  selected = await api(`tasks/${id}`);
  dirty = false;
  byId("review").hidden = false;
  byId("review-title").textContent = `${title(selected)} · ${statusLabels[selected.status]}`;
  const findings = byId("findings");
  findings.replaceChildren();
  selected.report.findings.forEach(finding => findings.append(node("li", findingLabels[finding.kind] || "Check the document.")));
  if (selected.report.render_error) findings.append(node("li", "Page preview failed. Approval is unavailable until pages can be reviewed."));
  if (!findings.children.length) findings.append(node("li", "No findings."));
  const keys = [...new Set([...selected.required_facts, ...Object.keys(selected.facts)])];
  fields("review-facts", keys, selected.facts, selected.status !== "review");
  byId("review-button").hidden = selected.status !== "review";
  byId("decision-controls").hidden = selected.status !== "review";
  byId("decision-note").textContent = selected.note;
  byId("note").value = "";
  byId("pages").replaceChildren();
  selected.report.rendered_pages.forEach((_, index) => {
    const img = document.createElement("img");
    img.src = `/api/busy/tasks/${id}/pages/${index + 1}?v=${Date.now()}`;
    img.alt = `Document page ${index + 1}`;
    byId("pages").append(img);
  });
  byId("review-heading").focus();
}
byId("workflow").addEventListener("change", () => {
  const workflow = workflows.find(item => item.slug === byId("workflow").value);
  fields("start-facts", workflow?.required_facts || []);
});
byId("start-form").addEventListener("submit", event => {
  event.preventDefault();
  action(async () => {
    const task = await api("tasks", "POST", {slug: byId("workflow").value, facts: facts("start-facts")});
    await refresh(); await review(task.id); message("Ready for review.");
  });
});
byId("review-form").addEventListener("submit", event => {
  event.preventDefault();
  action(async () => {
    await api(`tasks/${selected.id}/facts`, "PUT", {facts: facts("review-facts")});
    await refresh(); await review(selected.id); message("Review updated.");
  });
});
async function decide(decision) {
  await action(async () => {
    await api(`tasks/${selected.id}/decision`, "POST", {decision, note: byId("note").value});
    await refresh(); await review(selected.id);
    message(decision === "approve" ? "Approved. Nothing has been sent." : "Changes requested.");
  });
}
byId("approve").addEventListener("click", () => decide("approve"));
byId("request-changes").addEventListener("click", () => decide("changes_requested"));
action(async () => {
  workflows = (await api("workflows")).workflows;
  workflows.forEach(workflow => {
    const option = node("option", workflow.title);
    option.value = workflow.slug;
    byId("workflow").append(option);
  });
  byId("workflow").dispatchEvent(new Event("change"));
  await refresh();
  message(workflows.length ? "" : "No approved workflows are available yet.");
});

// Daily report: the app prepares the sheets and the email drafts; the person sends each and ticks it.
let dailyDate = null;
const outlookLabels = {
  created: "Đã tạo nháp trong Outlook và mở sẵn để kiểm tra.",
  failed: "Outlook không tạo được nháp. Hãy mở file .eml bên dưới.",
  waiting: "Chờ gửi email ngày trước, rồi bấm “Tạo nháp Outlook”.",
  dry_run: "Chạy thử: chưa mở Outlook.",
  off: "Mở file .eml để có email nháp."
};
function dailyReminder(s) {
  if (!s.configured) return `Chưa có cấu hình. Cần tạo file ${s.config_path}.`;
  return {
    off_day: `Hôm nay nghỉ. Ngày làm việc tiếp theo: ${s.next_working_day}.`,
    holiday: `Hôm nay là ngày lễ (${s.report_date}); vẫn có sheet, không nhắc gửi.`,
    prepare: `Chưa chuẩn bị báo cáo ${s.report_date}.`,
    send: `Đã chuẩn bị báo cáo ${s.report_date}. Hãy mở email nháp, kiểm tra và tự bấm Gửi.`,
    overdue: `Chưa gửi báo cáo hôm nay (${s.report_date}). Đã quá ${s.remind_after}.`,
    sent: `Đã gửi báo cáo ${s.report_date}.`
  }[s.reminder] || "";
}
async function dailyPost(path, body) {
  const response = await fetch(`/api/busy/daily-report/${path}`, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || "Không làm được.");
  return data;
}
function draftRow(day, draft, template, sent) {
  const row = node("div", "", "daily-draft");
  const language = template.language ? ` (${template.language})` : "";
  row.append(node("p", `${template.subject}${language} → ${template.to.join(", ") || "chưa có người nhận"}`));
  if (draft) {
    row.append(node("p", outlookLabels[draft.outlook?.status] || ""));
    if (draft.outlook?.error) row.append(node("p", `Lỗi Outlook: ${draft.outlook.error}`));
    const eml = node("a", "Mở email nháp (.eml)");
    eml.href = `/api/busy/daily-report/files/${day}/eml?template=${encodeURIComponent(template.id)}`;
    eml.download = "";
    row.append(eml);
  }
  const label = node("label", "", "check");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(sent[template.id]);
  box.addEventListener("change", () => dailyPost("sent", {date: day, sent: box.checked, template: template.id})
    .then(dailyRefresh).catch(error => { byId("daily-result").textContent = error.message; }));
  label.append(box, ` Đã gửi “${template.subject}” ngày ${day}`);
  row.append(label);
  return row;
}
function dayBlock(day, drafts, sent, templates, holiday) {
  const block = node("div", "", "daily-day");
  block.append(node("h3", `Ngày ${day}${holiday ? " · ngày lễ" : ""}`));
  templates.forEach(template => block.append(draftRow(day, drafts?.[template.id], template, sent || {})));
  const waiting = templates.some(t => drafts?.[t.id] && !sent?.[t.id] && drafts[t.id].outlook?.status !== "created"
    && drafts[t.id].outlook?.status !== "off");
  if (waiting) {
    const again = node("button", "Tạo nháp Outlook");
    again.type = "button";
    again.addEventListener("click", () => dailyPost("drafts", {date: day}).then(dailyRefresh)
      .catch(error => { byId("daily-result").textContent = error.message; }));
    block.append(again);
  }
  return block;
}
async function dailyRefresh() {
  const s = await (await fetch("/api/busy/daily-report")).json();
  dailyDate = s.today || null;
  byId("daily-reminder").textContent = dailyReminder(s);
  byId("daily-report").classList.toggle("overdue", s.reminder === "overdue");
  document.title = s.reminder === "overdue" ? "(!) Busy mode" : "Busy mode";
  const ready = Boolean(s.configured && s.working_day);
  const missed = s.missed || [];
  byId("daily-missed").replaceChildren(...missed.map(day => node("li", `Chưa có báo cáo ngày ${day}.`)),
    ...(s.missed_error ? [node("li", s.missed_error)] : []));
  byId("daily-prepare").hidden = !ready || Boolean(s.prepared || s.all_sent);
  byId("daily-prepare").textContent = missed.length
    ? `Chuẩn bị báo cáo hôm nay và ${missed.length} ngày còn thiếu` : "Chuẩn bị báo cáo hôm nay";
  byId("daily-links").hidden = !s.prepared;
  if (s.prepared) {
    byId("daily-workbook").href = `/api/busy/daily-report/files/${dailyDate}/workbook`;
    byId("daily-result").textContent = `Sheet ${s.prepared.sheet} (chép từ ${s.prepared.source_sheet}).`;
    byId("daily-warnings").replaceChildren(...s.prepared.warnings.map(text => node("li", text)));
  }
  const blocks = (s.unsent || []).map(day => dayBlock(day.date, day.drafts, day.sent, s.templates, false));
  if (ready) blocks.push(dayBlock(s.today, s.prepared?.drafts, s.sent, s.templates, s.holiday));
  byId("daily-days").replaceChildren(...blocks);
}
byId("daily-prepare").addEventListener("click", async () => {
  byId("daily-result").textContent = "Đang chuẩn bị...";
  try {
    const data = await dailyPost("prepare", {date: dailyDate});
    await dailyRefresh();
    if (data.missed.length) byId("daily-result").textContent += ` Đã làm thêm ${data.missed.length} ngày còn thiếu.`;
  } catch (error) { byId("daily-result").textContent = error.message; }
});
dailyRefresh().catch(() => { byId("daily-reminder").textContent = "Không tải được trạng thái báo cáo."; });
setInterval(() => dailyRefresh().catch(() => {}), 5 * 60 * 1000);
