/* Job Agent dashboard. All numbers come from /api/status (real DB records). */
const $ = (sel) => document.querySelector(sel);

let state = null;
let statusFilter = "";

const api = {
  start: () => fetch("/api/start", { method: "POST" }).then(r => r.json()),
  pause: () => fetch("/api/pause", { method: "POST" }).then(r => r.json()),
  resume: () => fetch("/api/resume", { method: "POST" }).then(r => r.json()),
  stop: () => fetch("/api/stop", { method: "POST" }).then(r => r.json()),
  status: () => fetch("/api/status").then(r => r.json()),
  events: () => fetch("/api/events?limit=80").then(r => r.json()),
  apps: (status) =>
    fetch(`/api/applications?limit=300${status ? `&status=${status}` : ""}`).then(r => r.json()),
  skipped: () => fetch("/api/skipped").then(r => r.json()),
};

const CARD_DEFS = [
  ["jobs_discovered", "Jobs discovered", ""],
  ["jobs_eligible", "Eligible jobs", "blue"],
  ["jobs_ineligible", "Ineligible jobs", ""],
  ["applications_prepared", "Prepared", "blue"],
  ["submitted", "Submitted (confirmed)", "green"],
  ["skipped", "Skipped", "amber"],
  ["needs_manual_review", "Manual review", "amber"],
  ["applications_failed", "Failed", "red"],
];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return ""; }
}

function renderCards() {
  const c = state?.counts || {};
  $("#stats-cards").innerHTML = CARD_DEFS.map(([key, label, tone]) => `
    <div class="card ${tone}">
      <div class="value">${Number(c[key] ?? 0).toLocaleString()}</div>
      <div class="label">${label}</div>
    </div>`).join("");
}

function renderState() {
  const badge = $("#state-badge");
  badge.textContent = state?.state || "idle";
  badge.className = "badge " + (state?.state || "");
  $("#stage-label").textContent = "stage: " + (state?.stage || "idle");
  $("#btn-start").disabled = state?.state === "running" || state?.state === "paused";
  $("#btn-pause").disabled = state?.state !== "running";
  $("#btn-resume").disabled = state?.state !== "paused";
  $("#btn-stop").disabled = !["running", "paused", "stopping"].includes(state?.state);

  const lims = state?.limitations || [];
  const profileWarn = state?.profile_configured === false
    ? [`Candidate profile incomplete. Applications that need missing data are skipped; fill data/candidate.yml.`]
    : [];
  $("#limitations").innerHTML = [...profileWarn, ...lims]
    .map((t) => `<div class="limitation">${esc(t)}</div>`).join("");

  const srcs = state?.sources || [];
  $("#sources").innerHTML = srcs.length
    ? srcs.map((s) => `
      <span class="source-chip" title="${esc((s.errors || "").slice(0, 240))}">
        <b>${esc(s.source_id)}</b> · ${s.results} found
        ${s.ok ? "" : " · <span style='color:var(--red)'>errors</span>"}
      </span>`).join("")
    : `<span class="source-chip">no source runs yet</span>`;
}

async function refreshEvents() {
  try {
    const { events } = await api.events();
    const rows = (events || []).map((e) => `
      <div class="event ${e.level === "warn" ? "warn" : e.level === "error" ? "error" : ""}">
        <span class="ts">${esc(fmtTime(e.ts))}</span>
        <span>${esc(e.message)}</span>
      </div>`).join("");
    const box = $("#activity");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.innerHTML = rows || `<div class="event"><span class="ts">—</span><span>no activity yet</span></div>`;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) { /* keep UI alive */ }
}

function pill(status) {
  return `<span class="pill ${esc(status)}">${esc((status || "").replaceAll("_", " "))}</span>`;
}

async function refreshApps() {
  try {
    const { applications } = await api.apps(statusFilter);
    const rows = (applications || []).map((a) => `
      <tr>
        <td>${esc(a.company)}</td>
        <td>${esc(a.position)}</td>
        <td>${pill(a.status)}</td>
        <td>${esc(a.ats)}</td>
        <td>${esc(a.cv || "—")}</td>
        <td class="reason">
          ${a.confirmation_url
            ? `<a href="${esc(a.confirmation_url)}" target="_blank" rel="noopener">${esc((a.confirmation_text || "confirmation").slice(0, 120))}</a>`
            : esc((a.skip_reason || a.error || "").slice(0, 240))}
        </td>
        <td>${esc(fmtTime(a.date))}</td>
      </tr>`).join("");
    $("#app-table tbody").innerHTML =
      rows || `<tr><td colspan="7" class="muted">no applications yet</td></tr>`;
  } catch (e) { /* ignore */ }
}

let skippedCache = [];

async function refreshSkipped() {
  try {
    const data = await api.skipped();
    skippedCache = data.skipped || [];
    renderSkipped();
  } catch (e) { /* ignore */ }
}

function renderSkipped() {
  const q = ($("#skip-search").value || "").toLowerCase();
  const rows = skippedCache
    .filter((a) => !q || `${a.company} ${a.position} ${a.skip_reason}`.toLowerCase().includes(q))
    .map((a) => `
      <tr>
        <td>${esc(a.company)}</td>
        <td>${esc(a.position)}</td>
        <td class="reason">${esc((a.skip_reason || a.error || "").slice(0, 300))}</td>
        <td>${esc(a.ats)}</td>
        <td>${esc(a.cv || "—")}${a.prepared ? " ✓" : ""}</td>
        <td><a href="${esc(a.apply_url)}" target="_blank" rel="noopener">original listing</a></td>
        <td>${esc(fmtTime(a.date))}</td>
      </tr>`).join("");
  $("#skip-table tbody").innerHTML =
    rows || `<tr><td colspan="7" class="muted">nothing skipped</td></tr>`;
}

async function refresh() {
  try {
    state = await api.status();
    renderCards();
    renderState();
  } catch (e) { /* ignore */ }
  refreshEvents();
  refreshApps();
  refreshSkipped();
}

async function act(fn) {
  try { await fn(); } catch (e) { /* ignore */ }
  await refresh();
}

$("#btn-start").onclick = () => act(api.start);
$("#btn-pause").onclick = () => act(api.pause);
$("#btn-resume").onclick = () => act(api.resume);
$("#btn-stop").onclick = () => act(api.stop);

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    statusFilter = tab.dataset.filter;
    refreshApps();
  };
});
$("#skip-search").addEventListener("input", renderSkipped);

refresh();
setInterval(refresh, 3000);
