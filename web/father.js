"use strict";
const byId = id => document.getElementById(id);
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
  return readJson(await fetch(`/api/father/${path}`, {
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
    card.append(node("p", title(task)), node("span", statusLabels[task.status], `badge ${task.status}`));
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
    img.src = `/api/father/tasks/${id}/pages/${index + 1}?v=${Date.now()}`;
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
