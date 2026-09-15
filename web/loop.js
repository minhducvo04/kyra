"use strict";
const $ = id => document.getElementById(id);
let records = [], selected = "", busy = false, reconciliationTarget = null, continuationTarget = null;
const cards = new Map();
const statusLabel = {queued:"Queued", dispatching:"Running", done:"Complete", failed:"Failed",
  unreconciled:"Needs checking", mismatch:"Model changed"};
const errorLabel = {
  provider_unavailable:"The model app could not be found. Check its installation.",
  provider_exit_failed:"The model app could not complete this call. Check its sign-in and availability.",
  dispatch_interrupted:"The call was interrupted. Its completion could not be confirmed.",
  subject_changed:"The answer changed before review. The review was stopped.",
  parent_changed:"The earlier contribution changed. This follow-up was stopped before calling the model.",
  session_mismatch:"The provider answered in a different conversation. This follow-up could not be verified.",
  served_model_mismatch:"The provider reported a different model from the one requested.",
  malformed_stream:"The model app returned an incomplete or unexpected receipt."
};
const pending = r => ["queued", "dispatching"].includes(r.status);
const modelName = r => r.developer === "Anthropic" ? "Claude" : "Codex";
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function readJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || data.detail?.[0]?.msg || "Request failed");
  return data;
}
function showTopics() {
  const root = $("topics"); root.replaceChildren();
  const query = $("filter").value.trim().toLowerCase();
  for (const topic of [...new Set(records.map(r => r.topic))].filter(t => t.toLowerCase().includes(query))) {
    const button = el("button", topic, "topic-button" + (topic === selected ? " active" : ""));
    button.onclick = () => { resetContinuation(); selected = topic; $("topic").value = topic; render(); showTopics(); };
    root.append(button);
  }
}
async function inspect(record) {
  const detail = await readJson(`/api/loop/runs/${record.id}`);
  const card = el("article", undefined, "card"); card.id = `run-${record.id}`;
  const top = el("div", undefined, "card-top");
  top.append(el("strong", `${modelName(record)} ${record.review_subject_id ? "· Review" : "· Answer"}`),
    el("span", statusLabel[record.status] || record.status, `badge ${record.status}`));
  card.append(top, el("p", `#${record.id} · ${record.requested_model} · ${record.effort || "default effort"}`, "meta"));
  if (record.review_subject_id) card.append(el("p", `Review of contribution #${record.review_subject_id}. No automatic approval.`, "meta"));
  if (record.continued_from_run_id) card.append(el("p", `Continues contribution #${record.continued_from_run_id}. ${record.status === "done" ? "Same provider conversation verified." : "See the receipt for continuation status."}`, "meta"));
  const fallback = pending(record) ? "Waiting for the model. You can leave this page open." : "No completed answer was recorded.";
  card.append(el("pre", detail.artifact.output || fallback, "answer"));
  if (record.error) card.append(el("p", `${errorLabel[record.error] || "This call could not be verified. Open its receipt for the recorded reason."} No automatic retry.`, "meta"));
  for (const review of detail.reviews) {
    card.append(el("p", `Review #${review.reviewer_run_id}: ${review.stale ? "stale, artifact changed" : review.verdict}`, "meta"));
    for (const decision of review.decisions) card.append(el("p",
      `Your decision: ${decision.decision}${decision.stale ? " (stale, artifact changed)" : ""} · ${decision.created_at}`, "meta"));
    if (!review.stale) {
      const controls = el("div", undefined, "owner-controls"); controls.append(el("span", "Your decision on this review:"));
      for (const value of ["approve", "reject"]) {
        const button = el("button", value === "approve" ? "Approve" : "Reject");
        button.onclick = async () => {
          controls.querySelectorAll("button").forEach(b => { b.disabled = true; });
          try {
            await readJson(`/api/loop/reviews/${review.id}/decision`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({decision:value})});
            await refresh();
          } catch (error) { $("notice").textContent = error.message; }
          finally { controls.querySelectorAll("button").forEach(b => { b.disabled = false; }); }
        };
        controls.append(button);
      }
      card.append(controls, el("p", "Records your decision only. Nothing is sent or executed.", "meta"));
    }
  }
  for (const reconciliation of detail.reconciliations) {
    const note = el("section", undefined, "declaration");
    note.append(el("strong", "Your outcome note"), el("p",
      `${reconciliation.outcome === "nothing_happened" ? "You found no processing" : "You found that the provider processed it"}. This is your declaration, not a verified provider result.`),
      el("p", reconciliation.note === null ? "The note file is missing. Its original receipt is retained." : reconciliation.note), el("p", `${reconciliation.created_at}${reconciliation.note_changed ? " · Note file changed since recording" : ""}`, "meta"));
    card.append(note);
  }
  if (detail.can_reconcile) {
    const button = el("button", "Record what happened");
    button.onclick = () => {
      reconciliationTarget = record.id;
      $("reconcile-run").textContent = `Contribution #${record.id} · ${modelName(record)}`;
      $("outcome-note").value = ""; $("reconcile-error").textContent = "";
      $("reconcile-dialog").showModal();
    };
    card.append(button);
  }
  if (record.status === "done") {
    const reviewButton = el("button", `Ask ${record.developer === "Anthropic" ? "Codex" : "Claude"} to review`);
    reviewButton.onclick = async () => {
      reviewButton.disabled = true;
      try { await readJson(`/api/loop/runs/${record.id}/review`, {method:"POST"}); await refresh(); }
      catch (error) { $("notice").textContent = error.message; }
      finally { reviewButton.disabled = false; }
    };
    card.append(reviewButton);
  }
  if (detail.can_continue) {
    const button = el("button", "Continue this conversation");
    button.onclick = () => {
      continuationTarget = record.id;
      $("topic").value = record.topic; $("choice").value = record.choice_key;
      $("topic").disabled = true; $("choice").disabled = true;
      $("continuation").hidden = false;
      $("continuation-label").textContent = `Following up on ${modelName(record)} contribution #${record.id}. The same conversation will receive your request.`;
      $("run").textContent = "Send follow-up"; $("prompt").focus();
      $("request").scrollIntoView({behavior:"smooth", block:"start"});
    };
    card.append(button);
  }
  const request = el("details"); request.append(el("summary", "Request sent"), el("pre", detail.artifact.prompt, "receipt"));
  const receipt = el("details");
  receipt.append(el("summary", "Execution receipt"), el("pre", [
    `Developer: ${record.developer} · Host: ${record.host} · Method: ${record.method}`,
    `Requested: ${record.requested_model}`,
    `Reported by provider: ${record.served_model || "Not supplied by this CLI"}`,
    `Session: ${record.provider_session_id || "Not received"}`,
    `Continued from: ${record.continued_from_run_id ? `#${record.continued_from_run_id}` : "Fresh conversation"}`,
    `Requested session: ${record.requested_session_id || "New"}`,
    `Request: ${record.provider_request_id || "Not supplied"}`,
    `Input SHA-256: ${record.input_sha256}`, `Output SHA-256: ${record.output_sha256 || "Not received"}`,
    `Usage: ${record.usage ? JSON.stringify(record.usage, null, 2) : "Not supplied"}`,
    `Per-model usage: ${record.model_usage ? JSON.stringify(record.model_usage, null, 2) : "Not supplied"}`,
    "Cost: subscription usage; marginal billed cost is not supplied. Any provider costUSD is a list-price estimate.",
    `Started: ${record.started_at || "Not started"} · Finished: ${record.finished_at || "Not finished"}`,
    `Policy: ${record.policy_version} · Recorded error: ${record.error || "None"}`
  ].join("\n"), "receipt"));
  card.append(request, receipt); return card;
}
async function render() {
  const topic = selected;
  const visible = records.filter(r => r.topic === topic).reverse();
  $("count").textContent = `${visible.length} saved`;
  const nodes = [];
  for (const record of visible) {
    // Refresh review metadata too, while preserving opened receipts and stable cards otherwise.
    const detail = await inspect(record);
    if (selected !== topic) return;
    const previous = cards.get(record.id);
    if (previous) previous.querySelectorAll("details").forEach((d, i) => { detail.querySelectorAll("details")[i].open = d.open; });
    cards.set(record.id, detail); nodes.push(detail);
  }
  $("runs").replaceChildren(...(nodes.length ? nodes : [el("p", "No contributions yet. Send the first request above.", "empty")]));
}
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    records = (await readJson("/api/loop/runs")).runs;
    if (!selected && records.length && !$("topic").value) { selected = records[0].topic; $("topic").value = selected; }
    showTopics(); await render();
  } catch (error) { $("notice").textContent = error.message; }
  finally { busy = false; }
}
$("filter").oninput = showTopics;
function resetContinuation() {
  continuationTarget = null; $("continuation").hidden = true;
  $("topic").disabled = false; $("choice").disabled = false; $("run").textContent = "Run model";
}
$("fresh-request").onclick = resetContinuation;
$("new-topic").onclick = () => { resetContinuation(); selected = ""; $("topic").value = ""; $("prompt").value = ""; $("topic").focus(); render(); showTopics(); };
$("request").onsubmit = async event => {
  event.preventDefault(); $("run").disabled = true; $("notice").textContent = "";
  try {
    const topic = $("topic").value.trim();
    const url = continuationTarget ? `/api/loop/runs/${continuationTarget}/continue` : "/api/loop/runs";
    const body = continuationTarget ? {prompt:$("prompt").value} : {choice:$("choice").value, prompt:$("prompt").value, topic};
    await readJson(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    selected = topic; $("prompt").value = ""; resetContinuation(); await refresh();
  } catch (error) { $("notice").textContent = `${error.message}. Check saved contributions before submitting again.`; }
  finally { $("run").disabled = false; }
};
refresh();
setInterval(() => { if (records.some(pending)) refresh(); }, 2500);

$("cancel-reconcile").onclick = () => $("reconcile-dialog").close();
$("reconcile-form").onsubmit = async event => {
  event.preventDefault(); $("save-reconcile").disabled = true;
  try {
    await readJson(`/api/loop/runs/${reconciliationTarget}/reconcile`, {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({outcome:$("outcome").value, note:$("outcome-note").value})});
    $("reconcile-dialog").close(); await refresh();
  } catch (error) { $("reconcile-error").textContent = error.message; }
  finally { $("save-reconcile").disabled = false; }
};
