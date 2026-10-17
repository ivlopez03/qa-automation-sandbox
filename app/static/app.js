// Small vanilla-JS layer: every UI action goes through the same REST API you test directly.
const UI_DELAY = Number(document.body.dataset.uiDelay || 0);
const randomDelay = () => new Promise(r => setTimeout(r, Math.random() * UI_DELAY));

async function api(method, url, body, isForm = false) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(url, opts);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  return { ok: res.ok, status: res.status, data };
}

function errorText(data) {
  const d = data && data.detail !== undefined ? data.detail : data;
  if (Array.isArray(d)) return d.map(e => (typeof e === "string" ? e : `${(e.loc || []).slice(-1)[0]}: ${e.msg}`)).join("\n");
  return typeof d === "string" ? d : "Request failed";
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3000);
}

function formToJson(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.disabled || el.type === "submit") continue;
    let v = el.value;
    if (v === "" && !el.hasAttribute("data-keep-empty")) continue;
    if (el.dataset.type === "number") v = Number(v);
    if (el.dataset.type === "int-or-null") v = v === "none" ? null : Number(v);
    if (el.dataset.type === "bool") v = v === "true";
    out[el.name] = v;
  }
  return out;
}

// <form data-api="/api/v1/..." data-method="POST" data-redirect="/leads/{id}" data-success="Saved">
document.addEventListener("submit", async (ev) => {
  const form = ev.target;
  if (!form.dataset.api) return;
  ev.preventDefault();
  const errorEl = form.querySelector("[data-role=error]");
  const submit = form.querySelector("[type=submit]");
  if (errorEl) errorEl.textContent = "";
  if (submit) submit.disabled = true;
  let res;
  try {
    if (form.dataset.upload !== undefined) res = await api("POST", form.dataset.api, new FormData(form), true);
    else res = await api(form.dataset.method || "POST", form.dataset.api, formToJson(form));
  } catch (e) {
    res = { ok: false, data: { detail: "Network error. Check that the server is running." } };
  }
  if (submit) submit.disabled = false;
  if (!res.ok) {
    if (errorEl) errorEl.textContent = errorText(res.data);
    return;
  }
  if (form.dataset.success) sessionStorage.setItem("toast", form.dataset.success);
  const redirect = form.dataset.redirect;
  if (redirect) window.location = redirect.replace("{id}", res.data && res.data.id);
  else window.location.reload();
});

// <button data-action-api="/api/v1/..." data-method="DELETE" data-body='{"a":1}' data-confirm="Sure?">
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-action-api]");
  if (!btn) return;
  if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
  btn.disabled = true;
  const body = btn.dataset.body ? JSON.parse(btn.dataset.body) : undefined;
  const res = await api(btn.dataset.method || "POST", btn.dataset.actionApi, body);
  btn.disabled = false;
  if (!res.ok) { toast(errorText(res.data)); return; }
  if (btn.dataset.pollJob !== undefined && res.data && res.data.id) return pollJob(res.data.id);
  if (btn.dataset.success) sessionStorage.setItem("toast", btn.dataset.success);
  if (btn.dataset.redirect) window.location = btn.dataset.redirect;
  else window.location.reload();
});

async function pollJob(jobId) {
  const box = document.querySelector("[data-testid=job-progress]");
  for (;;) {
    const res = await api("GET", `/api/v1/etl/jobs/${jobId}`);
    if (res.ok && box) {
      const j = res.data;
      box.hidden = false;
      box.innerHTML = `<span class="pill ${j.status}" data-testid="job-status">${j.status}</span>
        <span data-testid="job-counts">processed ${j.processed} · rejected ${j.rejected} · duplicates ${j.duplicates} of ${j.total}</span>
        ${j.error ? `<div class="form-error" data-testid="job-error">${j.error}</div>` : ""}`;
      if (j.status === "succeeded" || j.status === "failed") {
        sessionStorage.setItem("toast", j.status === "succeeded" ? "Processing finished" : "Processing failed");
        setTimeout(() => window.location.reload(), 900);
        return;
      }
    }
    await new Promise(r => setTimeout(r, 500));
  }
}

// Dashboard KPIs load asynchronously (with ui_delay_ms jitter) to practice explicit waits.
async function loadKpis() {
  const cells = document.querySelectorAll("[data-kpi]");
  if (!cells.length) return;
  await randomDelay();
  const res = await api("GET", "/api/v1/reports/summary");
  const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  cells.forEach(cell => {
    cell.classList.remove("loading");
    if (!res.ok) { cell.textContent = "—"; cell.title = errorText(res.data); return; }
    const key = cell.dataset.kpi;
    const v = key.includes(".") ? key.split(".").reduce((o, k) => o[k], res.data) : res.data[key];
    cell.textContent = cell.dataset.money !== undefined ? "$" + fmt.format(v)
      : cell.dataset.pct !== undefined ? fmt.format(v) + "%" : fmt.format(v);
  });
  document.querySelector("[data-testid=kpi-strip]").dataset.loaded = "true";
}

// Buttons that become usable only after a random delay (flaky-test practice).
async function enableDelayed() {
  for (const el of document.querySelectorAll("[data-delayed-enable]")) {
    await randomDelay();
    el.disabled = false;
  }
}

// Show the amount field only when converting a lead.
function wireTransition() {
  const sel = document.querySelector("[data-testid=transition-select]");
  const amount = document.querySelector("[data-testid=amount-field]");
  if (!sel || !amount) return;
  const sync = () => {
    const show = sel.value === "converted";
    amount.hidden = !show;
    amount.querySelector("input").disabled = !show;
  };
  sel.addEventListener("change", sync);
  sync();
}

document.addEventListener("DOMContentLoaded", () => {
  const pending = sessionStorage.getItem("toast");
  if (pending) { sessionStorage.removeItem("toast"); toast(pending); }
  loadKpis();
  enableDelayed();
  wireTransition();
});
