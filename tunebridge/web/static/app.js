/* TuneBridge web UI. No framework, no build step. Every string from the server is set
   with textContent, never innerHTML. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-TuneBridge": "1" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
}

const fmtDur = (s) => {
  if (!s) return "";
  const m = Math.floor(s / 60), r = String(s % 60).padStart(2, "0");
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${r}` : `${m}:${r}`;
};
const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

/* ---------------------------------------------------------------- state */

const S = {
  platforms: [],
  source: null,
  target: null,
  playlists: [],
  chosen: new Set(),
  job: null,        // snapshot from the server
  jobId: null,
  view: "all",
  es: null,
  pollTimer: null,
};

const STEP_ORDER = ["connect", "playlists", "review", "transfer", "report"];

function setStep(step) {
  const idx = STEP_ORDER.indexOf(step);
  $$("#steps li").forEach((li, i) => {
    li.classList.toggle("current", i === idx);
    li.classList.toggle("done", i < idx);
  });
}

function show(view) {
  $$(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  window.scrollTo({ top: 0 });
}

const label = (id) => (S.platforms.find((p) => p.id === id) || {}).label || id;

/* ---------------------------------------------------------------- theme */

(function theme() {
  const saved = (() => { try { return localStorage.getItem("tb-theme"); } catch (_) { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("tb-theme", next); } catch (_) { /* private mode */ }
  });
})();

/* ---------------------------------------------------------------- 1. connect */

async function loadPlatforms() {
  S.platforms = await api("/api/platforms");
  renderPlatforms();
  renderRoute();
  const busy = S.platforms.some((p) => ["waiting", "checking"].includes((p.login || {}).state));
  clearTimeout(S.pollTimer);
  if (busy) S.pollTimer = setTimeout(loadPlatforms, 1500);
}

function sessionBadge(p) {
  const st = (p.login || {}).state;
  if (!p.uses_browser) return h("span", { class: "badge" }, "No login needed");
  if (st === "waiting") return h("span", { class: "badge accent" }, "Waiting for you");
  if (st === "checking") return h("span", { class: "badge info" }, "Checking");
  if (st === "ok") return h("span", { class: "badge ok" }, "Logged in");
  if (st === "failed") return h("span", { class: "badge bad" }, "Not logged in");
  return p.session ? h("span", { class: "badge ok" }, "Session saved") : h("span", { class: "badge warn" }, "Not logged in");
}

function renderPlatforms() {
  const box = $("#platforms");
  box.replaceChildren(...S.platforms.map((p) => {
    const st = (p.login || {}).state;
    const busy = st === "waiting" || st === "checking";
    const btns = p.uses_browser ? [
      h("button", { class: p.session ? "ghost small" : "primary small", type: "button", disabled: busy,
        onclick: () => doLogin(p) }, p.session ? "Log in again" : "Log in"),
      h("button", { class: "ghost small", type: "button", disabled: busy || !p.session, onclick: () => doCheck(p) }, "Check"),
      h("button", { class: "ghost small danger", type: "button", disabled: busy || !p.session, title: "Delete the saved session",
        onclick: () => doForget(p) }, "Forget"),
    ] : [];
    const mode = !p.uses_browser ? "Works offline" : p.headless ? "Runs in the background" : "Opens a browser window while working";
    return h("div", { class: "platform" },
      h("div", { class: "name" }, p.label, sessionBadge(p), p.experimental ? h("span", { class: "badge warn" }, "Experimental") : null),
      h("div", { class: "btns" }, btns),
      h("div", { class: "meta" }, `${mode}. ${p.note || ""}`),
      p.login && p.login.message ? h("div", { class: "msg muted" }, p.login.message) : null,
    );
  }));
}

async function doLogin(p) {
  try {
    const r = await api(`/api/platforms/${p.id}/login`, { method: "POST" });
    toast(r.message || "Browser window opened.");
  } catch (e) { toast(e.message); }
  loadPlatforms();
}
async function doCheck(p) {
  try { await api(`/api/platforms/${p.id}/check`, { method: "POST" }); } catch (e) { toast(e.message); }
  loadPlatforms();
}
async function doForget(p) {
  if (!confirm(`Forget the saved ${p.label} session? You will need to log in again.`)) return;
  try { await api(`/api/platforms/${p.id}/session`, { method: "DELETE" }); } catch (e) { toast(e.message); }
  loadPlatforms();
}

function renderRoute() {
  for (const [id, key] of [["#source", "source"], ["#target", "target"]]) {
    const sel = $(id);
    const cur = S[key] || sel.value;
    sel.replaceChildren(h("option", { value: "" }, "Choose a platform"),
      ...S.platforms.map((p) => h("option", { value: p.id, selected: p.id === cur }, p.label)));
  }
  updateRouteHint();
}

function updateRouteHint() {
  S.source = $("#source").value || null;
  S.target = $("#target").value || null;
  const hint = $("#route-hint");
  const btn = $("#to-playlists");
  btn.disabled = true;
  if (!S.source || !S.target) { hint.textContent = "Choose where to move from and to."; return; }
  if (S.source === S.target) { hint.textContent = "Pick two different platforms."; return; }
  const src = S.platforms.find((p) => p.id === S.source);
  const dst = S.platforms.find((p) => p.id === S.target);
  const missing = [src, dst].filter((p) => p.uses_browser && !p.session && (p.login || {}).state !== "ok");
  hint.textContent = missing.length
    ? `Log in to ${missing.map((p) => p.label).join(" and ")} first.`
    : `${src.label} to ${dst.label}. You will choose the playlists next.`;
  btn.disabled = missing.length > 0;
}

$("#source").addEventListener("change", updateRouteHint);
$("#target").addEventListener("change", updateRouteHint);
$("#swap").addEventListener("click", () => {
  const a = $("#source").value; $("#source").value = $("#target").value; $("#target").value = a;
  updateRouteHint();
});
$("#refresh-platforms").addEventListener("click", loadPlatforms);
$("#to-playlists").addEventListener("click", () => openPlaylists());

async function loadPresets() {
  const list = await api("/api/presets");
  const box = $("#presets");
  if (!list.length) {
    box.replaceChildren(h("p", { class: "muted small" }, "No presets yet. Save one on the next screen."));
    return;
  }
  box.replaceChildren(...list.map((p) => h("div", { class: "preset" },
    h("b", {}, p.name),
    h("div", { class: "btns" },
      h("button", { class: "primary small", type: "button", onclick: () => runPreset(p.name, false) }, "Run"),
      h("button", { class: "ghost small", type: "button", onclick: () => runPreset(p.name, true) }, "Dry run"),
      h("button", { class: "ghost small danger", type: "button", onclick: () => removePreset(p.name) }, "Delete")),
    h("div", { class: "meta" },
      `${label(p.source)} to ${label(p.target)}  /  ${p.all_playlists ? "all playlists" : p.playlists.join(", ")}`),
  )));
}

async function runPreset(name, dry) {
  try {
    const r = await api(`/api/presets/${encodeURIComponent(name)}/run?dry_run=${dry}`, { method: "POST" });
    startRun(r.id);
  } catch (e) { toast(e.message); }
}
async function removePreset(name) {
  if (!confirm(`Delete preset "${name}"?`)) return;
  await api(`/api/presets/${encodeURIComponent(name)}`, { method: "DELETE" });
  loadPresets();
}

function goConnect() {
  stopEvents();
  S.job = null; S.jobId = null;
  history.replaceState(null, "", "#");
  setStep("connect");
  show("connect");
  loadPlatforms();
  loadPresets();
}

/* ---------------------------------------------------------------- 2. playlists */

async function openPlaylists() {
  setStep("playlists");
  show("playlists");
  $("#route-crumb").textContent = `${label(S.source)} to ${label(S.target)}`;
  $("#playlists-lead").textContent = `Tick the playlists to move from ${label(S.source)}. Each song is looked up on ${label(S.target)}.`;
  $("#pl-table").hidden = true;
  $("#pl-error").hidden = true;
  $("#pl-loading").hidden = false;
  const src = S.platforms.find((p) => p.id === S.source);
  $("#pl-loading-msg").textContent = src && src.uses_browser
    ? `Opening ${src.label}${src.headless ? " in the background" : " in a browser window"} and reading your library...`
    : "Reading...";
  S.chosen = new Set();
  try {
    S.playlists = await api(`/api/platforms/${S.source}/playlists`);
  } catch (e) {
    $("#pl-loading").hidden = true;
    $("#pl-error").hidden = false;
    $("#pl-error").textContent = e.message;
    return;
  }
  $("#pl-loading").hidden = true;
  $("#pl-table").hidden = false;
  renderPlaylists();
}

function renderPlaylists() {
  const q = $("#pl-filter").value.trim().toLowerCase();
  const rows = S.playlists.filter((p) => !q || p.name.toLowerCase().includes(q));
  const body = $("#pl-table tbody");
  if (!rows.length) {
    body.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "muted" }, S.playlists.length ? "No playlist matches the filter." : "No playlists found.")));
  } else {
    body.replaceChildren(...rows.map((p) => {
      const on = S.chosen.has(p.id);
      const tr = h("tr", { class: on ? "selected" : "" },
        h("td", { class: "col-check" }, h("input", { type: "checkbox", checked: on, "aria-label": `Select ${p.name}` })),
        h("td", {}, h("div", { class: "t-title" }, p.name)),
        h("td", { class: "num" }, p.track_count ?? ""),
        h("td", { class: "muted" }, p.owner || ""),
        h("td", {}, p.kind === "liked" ? h("span", { class: "badge info" }, "Liked") : p.kind === "file" ? h("span", { class: "badge" }, "File") : h("span", { class: "badge" }, "Playlist")),
      );
      tr.addEventListener("click", (ev) => {
        if (S.chosen.has(p.id)) S.chosen.delete(p.id); else S.chosen.add(p.id);
        if (ev.target.tagName !== "INPUT") tr.querySelector("input").checked = S.chosen.has(p.id);
        tr.classList.toggle("selected", S.chosen.has(p.id));
        updatePlCount();
      });
      return tr;
    }));
  }
  updatePlCount();
}

function updatePlCount() {
  const picked = S.playlists.filter((p) => S.chosen.has(p.id));
  const tracks = picked.reduce((n, p) => n + (p.track_count || 0), 0);
  $("#pl-count").textContent = `${picked.length} of ${S.playlists.length} selected${tracks ? `  /  about ${tracks} tracks` : ""}`;
  $("#start").disabled = picked.length === 0;
}

$("#pl-filter").addEventListener("input", renderPlaylists);
$("#pl-all").addEventListener("click", () => {
  const q = $("#pl-filter").value.trim().toLowerCase();
  S.playlists.filter((p) => !q || p.name.toLowerCase().includes(q)).forEach((p) => S.chosen.add(p.id));
  renderPlaylists();
});
$("#pl-none").addEventListener("click", () => { S.chosen.clear(); renderPlaylists(); });
$("#opt-score").addEventListener("input", (e) => { $("#opt-score-out").textContent = e.target.value; });
$("#start").addEventListener("click", async () => {
  const body = {
    source: S.source,
    target: S.target,
    playlists: S.playlists.filter((p) => S.chosen.has(p.id)),
    dry_run: $("#opt-dry").checked,
    prefix: $("#opt-prefix").value,
    min_score: Number($("#opt-score").value),
    preset_name: $("#opt-preset").value.trim(),
  };
  $("#start").disabled = true;
  try {
    const r = await api("/api/jobs", { method: "POST", body });
    if (body.preset_name) toast(`Saved preset "${body.preset_name}".`);
    startRun(r.id);
  } catch (e) {
    toast(e.message);
    $("#start").disabled = false;
  }
});
$$("[data-go='connect']").forEach((b) => b.addEventListener("click", goConnect));

/* ---------------------------------------------------------------- 3-5. run */

const STATUS = {
  pending: ["Waiting", ""],
  matched: ["Match", "ok"],
  review: ["Check", "warn"],
  not_found: ["Not found", "bad"],
  already: ["Already", ""],
  added: ["Added", "ok"],
  failed: ["Failed", "bad"],
  would_add: ["Dry run", "info"],
};

function startRun(id) {
  S.jobId = id;
  S.view = "all";
  history.replaceState(null, "", `#job=${id}`);
  $("#log").replaceChildren();
  $("#bars").replaceChildren();
  $("#results tbody").replaceChildren();
  $("#report").hidden = true;
  $("#actionbar").hidden = false;
  setStep("review");
  show("run");
  refreshJob().then(listen);
}

function stopEvents() {
  if (S.es) { S.es.close(); S.es = null; }
}

let refreshQueued = false;
function queueRefresh() {
  if (refreshQueued) return;
  refreshQueued = true;
  setTimeout(() => { refreshQueued = false; refreshJob(); }, 600);
}

function listen() {
  stopEvents();
  const es = new EventSource(`/api/jobs/${S.jobId}/events`);
  S.es = es;
  es.addEventListener("tb", (msg) => {
    const ev = JSON.parse(msg.data);
    onEvent(ev);
  });
  es.addEventListener("end", () => { stopEvents(); refreshJob(); });
  es.onerror = () => { /* the browser reconnects on its own; a final refresh keeps the page right */ queueRefresh(); };
}

function addLog(kind, text) {
  const log = $("#log");
  const li = h("li", { class: kind }, h("time", {}, clock()), h("span", {}, text));
  log.append(li);
  log.scrollTop = log.scrollHeight;
}

const progress = {};
function onEvent(ev) {
  const d = ev.data || {};
  switch (ev.kind) {
    case "phase": addLog("phase", ev.message); queueRefresh(); break;
    case "log": addLog("", ev.message); break;
    case "warn": addLog("warn", ev.message); break;
    case "error": addLog("error", ev.message); queueRefresh(); break;
    case "progress": progress[d.index] = d; renderBars(); break;
    case "track": queueRefresh(); break;
    case "state": queueRefresh(); break;
    case "done": queueRefresh(); break;
    default: break;
  }
}

async function refreshJob() {
  if (!S.jobId) return;
  try {
    S.job = await api(`/api/jobs/${S.jobId}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  renderRun();
}

function renderBars() {
  const job = S.job;
  if (!job) return;
  const box = $("#bars");
  const phase = job.state;
  const rows = job.playlists.map((pl, i) => {
    const p = progress[i] || {};
    const total = p.total || (phase === "write" ? pl.counts.approved : pl.counts.total) || 0;
    const done = p.phase === phaseKey(phase) ? (p.done || 0) : (["review", "done"].includes(phase) ? total : 0);
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const busy = !total && ["read", "match", "write", "starting"].includes(phase);
    const bar = h("div", { class: `bar${pct >= 100 ? " done" : ""}${busy ? " busy" : ""}` }, h("i"));
    bar.firstChild.style.width = `${pct}%`;
    return h("div", { class: "bar-row" },
      h("div", { class: "bar-top" }, h("span", { class: "name" }, pl.target_name || pl.source.name),
        h("span", { class: "count" }, total ? `${done}/${total}` : pl.error ? "error" : "")),
      bar);
  });
  if (!rows.length) rows.push(h("p", { class: "muted small" }, "Starting..."));
  box.replaceChildren(...rows);
}
const phaseKey = (state) => (state === "write" ? "write" : state === "read" ? "read" : "match");

function renderRun() {
  const job = S.job;
  const st = job.state;
  const step = st === "write" ? "transfer" : ["done", "cancelled", "error"].includes(st) ? "report" : "review";
  setStep(step);
  $("#run-crumb").textContent = `${label(job.source)} to ${label(job.target)}${job.dry_run ? "  (dry run)" : ""}`;
  const stateBadge = $("#run-state");
  const names = { starting: "Starting", read: "Reading", match: "Matching", review: "Ready", write: "Writing",
    done: "Finished", error: "Error", cancelled: "Cancelled" };
  stateBadge.textContent = names[st] || st;
  stateBadge.className = `badge ${st === "done" ? "ok" : st === "error" ? "bad" : st === "review" ? "accent" : "info"}`;

  const titles = {
    starting: ["Starting", "Opening the browsers..."],
    read: ["Reading your playlists", `Reading songs from ${label(job.source)}.`],
    match: ["Finding matches", `Searching ${label(job.target)} for every song. Results fill in as they are found.`],
    review: ["Review the matches", "Songs marked Check were not a sure match. Pick the right version or leave them out, then transfer."],
    write: ["Transferring", `Adding songs to ${label(job.target)}. You can watch the progress on the right.`],
    done: [job.dry_run ? "Dry run finished" : "Transfer finished", job.dry_run ? "Nothing was written. This is what would happen." : "Here is what moved. Run it again later and only new songs will be added."],
    error: ["Something went wrong", job.error || ""],
    cancelled: ["Cancelled", "Songs added before cancelling are remembered and will not be added twice."],
  };
  const [t, l] = titles[st] || [st, ""];
  $("#run-title").textContent = t;
  $("#run-lead").textContent = l;

  renderBars();
  renderTabs();
  renderResults();
  renderSummary();
  renderReport();
}

function allResults() {
  const out = [];
  (S.job ? S.job.playlists : []).forEach((pl, pi) => pl.results.forEach((r, ri) => out.push({ pl, pi, r, ri })));
  return out;
}

function inView(r) {
  switch (S.view) {
    case "review": return r.status === "review";
    case "not_found": return r.status === "not_found" || r.status === "failed";
    case "already": return r.status === "already";
    default: return true;
  }
}

function renderTabs() {
  const res = allResults();
  const count = (f) => res.filter(({ r }) => f(r)).length;
  const counts = {
    all: res.length,
    review: count((r) => r.status === "review"),
    not_found: count((r) => r.status === "not_found" || r.status === "failed"),
    already: count((r) => r.status === "already"),
  };
  $$("#tabs .tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === S.view);
    b.querySelector("span").textContent = counts[b.dataset.view];
  });
  $("#approve-all").hidden = !(S.job && S.job.state === "review" && counts.review);
}

$$("#tabs .tab").forEach((b) => b.addEventListener("click", () => { S.view = b.dataset.view; renderTabs(); renderResults(); }));

function trackCell(t) {
  if (!t) return h("span", { class: "muted" }, "");
  return h("div", {},
    h("div", { class: "t-title" }, t.title, t.duration ? h("span", { class: "t-dur" }, fmtDur(t.duration)) : null),
    h("div", { class: "t-sub" }, (t.artists || []).join(", ")));
}

function scoreCell(r) {
  if (!r.match || r.status === "already" || r.status === "not_found") return "";
  const cls = r.score >= 80 ? "" : r.score >= 60 ? " warn" : " bad";
  const m = h("span", { class: `meter${cls}` }, h("i"));
  m.firstChild.style.width = `${Math.round(r.score)}%`;
  return h("span", { class: "score" }, r.score.toFixed(0), m);
}

function renderResults() {
  const body = $("#results tbody");
  const reviewing = S.job && S.job.state === "review";
  const rows = [];
  let lastPl = -1;
  const multi = S.job && S.job.playlists.length > 1;
  for (const { pl, pi, r, ri } of allResults()) {
    if (!inView(r)) continue;
    if (multi && pi !== lastPl) {
      rows.push(h("tr", { class: "playlist-row" }, h("td", { colspan: 5 }, pl.source.name)));
      lastPl = pi;
    }
    let [txt, cls] = STATUS[r.status] || [r.status, ""];
    if (r.status === "review" && r.approved) { txt = "Approved"; cls = "ok"; }
    let matchCell;
    if (r.status === "already") matchCell = h("span", { class: "muted" }, "Moved in an earlier run");
    else if (reviewing && r.status === "not_found" && r.candidates.length) {
      const sel = h("select", { "aria-label": "Pick a song anyway" },
        h("option", { value: "", selected: true }, "No good match - pick one anyway"),
        ...r.candidates.map((c, ci) => h("option", { value: ci },
          `${c.score.toFixed(0)}  ${c.track.title} - ${(c.track.artists || []).join(", ")} ${fmtDur(c.track.duration)}`)));
      sel.addEventListener("change", () => { if (sel.value !== "") decide(pi, ri, true, Number(sel.value)); });
      matchCell = sel;
    } else if (reviewing && r.candidates.length > 1 && r.status === "review") {
      const sel = h("select", { "aria-label": "Choose the matching song" },
        ...r.candidates.map((c, ci) => h("option", { value: ci, selected: r.match && c.track.id === r.match.id && c.track.title === r.match.title },
          `${c.score.toFixed(0)}  ${c.track.title} - ${(c.track.artists || []).join(", ")} ${fmtDur(c.track.duration)}`)));
      sel.addEventListener("change", () => decide(pi, ri, true, Number(sel.value)));
      matchCell = sel;
    } else if (r.status === "not_found") matchCell = h("span", { class: "muted" }, "No good match");
    else if (r.match) matchCell = trackCell(r.match);
    else matchCell = h("span", { class: "muted" }, "");
    const canToggle = reviewing && r.match && ["matched", "review"].includes(r.status);
    const box = h("input", { type: "checkbox", checked: r.approved && ["matched", "review", "added", "would_add"].includes(r.status),
      disabled: !canToggle, "aria-label": "Include this song" });
    if (canToggle) box.addEventListener("change", () => decide(pi, ri, box.checked, null));
    rows.push(h("tr", { class: r.status === "already" ? "dim" : "" },
      h("td", {}, h("span", { class: `badge ${cls}` }, txt)),
      h("td", { class: "num" }, scoreCell(r)),
      h("td", {}, trackCell(r.source)),
      h("td", {}, matchCell),
      h("td", { class: "col-check" }, box)));
  }
  body.replaceChildren(...rows);
  $("#results-empty").hidden = rows.length > 0;
  if (!rows.length && S.job && !["starting", "read", "match"].includes(S.job.state)) {
    $("#results-empty").textContent = "Nothing in this view.";
  }
}

async function decide(pi, ri, approved, candidate) {
  try {
    await api(`/api/jobs/${S.jobId}/decisions`, { method: "POST", body: { items: [{ playlist: pi, index: ri, approved, candidate }] } });
  } catch (e) { toast(e.message); }
  refreshJob();
}

$("#approve-all").addEventListener("click", async () => {
  const items = allResults().filter(({ r }) => r.status === "review" && r.match).map(({ pi, ri }) => ({ playlist: pi, index: ri, approved: true }));
  if (!items.length) return;
  await api(`/api/jobs/${S.jobId}/decisions`, { method: "POST", body: { items } });
  refreshJob();
});

function renderSummary() {
  const s = (S.job && S.job.summary) || {};
  const st = S.job ? S.job.state : "";
  const approved = s.approved || 0;
  const item = (cls, n, text) => h("span", { class: cls }, h("b", {}, n || 0), ` ${text}`);
  $("#summary").replaceChildren(
    item("ok", (s.matched || 0) + (s.added || 0), "matched"),
    item("warn", s.review, "to check"),
    item("bad", s.not_found, "not found"),
    item("", s.already, "already moved"),
  );
  const apply = $("#apply");
  const cancel = $("#cancel");
  if (st === "review") {
    apply.disabled = approved === 0;
    apply.textContent = S.job.dry_run ? `Dry run ${approved} songs` : `Transfer ${approved} songs`;
    cancel.textContent = "Cancel";
  } else if (["done", "error", "cancelled"].includes(st)) {
    $("#actionbar").hidden = true;
  } else {
    apply.disabled = true;
    apply.textContent = st === "write" ? "Transferring..." : "Matching...";
    cancel.textContent = "Stop";
  }
}

$("#apply").addEventListener("click", async () => {
  $("#apply").disabled = true;
  try { await api(`/api/jobs/${S.jobId}/apply`, { method: "POST" }); } catch (e) { toast(e.message); }
  listen();
  refreshJob();
});
$("#cancel").addEventListener("click", async () => {
  const st = S.job ? S.job.state : "";
  if (!confirm(st === "review" ? "Cancel this transfer?" : "Stop after the current song?")) return;
  try { await api(`/api/jobs/${S.jobId}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
  refreshJob();
});

function renderReport() {
  const job = S.job;
  const finished = ["done", "cancelled", "error"].includes(job.state);
  $("#report").hidden = !finished;
  if (!finished) return;
  const s = job.summary || {};
  const added = job.dry_run ? (s.would_add || 0) : (s.added || 0);
  const stat = (cls, n, text) => h("div", { class: `stat ${cls}` }, h("b", {}, n || 0), h("span", {}, text));
  $("#stats").replaceChildren(
    stat("ok", added, job.dry_run ? "would be added" : "added"),
    stat("", s.already, "already there"),
    stat("warn", (s.not_found || 0) + (s.review || 0), "not moved"),
    stat("bad", s.failed, "failed"),
  );
  const dl = $("#download");
  dl.hidden = !job.report;
  dl.href = `/api/jobs/${job.id}/report`;
}

$("#restart").addEventListener("click", goConnect);

/* ---------------------------------------------------------------- boot */

(async function boot() {
  try {
    const meta = await api("/api/meta");
    $("#version").textContent = `v${meta.version}`;
  } catch (_) { /* ignore */ }
  await loadPlatforms();
  const m = location.hash.match(/job=([a-f0-9]+)/);
  if (m) {
    S.jobId = m[1];
    await refreshJob();
    if (S.job) {
      show("run");
      listen();
      return;
    }
  }
  goConnect();
})();
