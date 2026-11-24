// RTS Quant Terminal - single-page app (no build step). Views render from the API or the static public bundle.
import { get, post, search, STATIC, invalidate } from "./api.js";
import { esc, $, $$, fmt, date, ago, claimBadge, stanceBadge, link, srcLine, md, table, panel, kpi } from "./ui.js";
import { line, bars, heat, spark } from "./charts.js";

const main = $("#main");
const CAT_LABEL = {};
const routes = { overview, research, quant, altdata, markets, portfolio, risk, registry, search: searchView, settings, project: projectPage, company: companyPage };

// ------------------------------------------------------------------ router & chrome
async function route() {
  const h = location.hash.replace(/^#\/?/, "") || "overview";
  const [tab, ...rest] = h.split("/");
  $$("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  closeDrawer();
  main.innerHTML = '<div class="loading">Loading…</div>';
  try {
    await (routes[tab] || overview)(rest.map(decodeURIComponent));
  } catch (err) {
    main.innerHTML = `<div class="empty">Could not load this view: ${esc(err.message)}</div>`;
  }
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

$("#cmd").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("#cmd-input").value.trim();
  if (q) location.hash = `#/search/${encodeURIComponent(q)}`;
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) { ev.preventDefault(); $("#cmd-input").focus(); }
  if (ev.key === "Escape") closeDrawer();
});
$("#theme-btn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("rts-theme", next); } catch (e) { /* storage unavailable */ }
  route();
});
$("#alerts-btn").addEventListener("click", () => openAlerts());
$("#scrim").addEventListener("click", closeDrawer);

function openDrawer(html) {
  $("#drawer-inner").innerHTML = `<button class="btn ghost close" id="dclose">Close ✕</button>` + html;
  $("#drawer").classList.add("open"); $("#scrim").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false");
  $("#dclose").addEventListener("click", closeDrawer);
  $("#drawer").scrollTop = 0;
}
function closeDrawer() { $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); }

async function statusbar() {
  try {
    const o = await get("/overview");
    const ls = o.last_sync || {};
    const st = ls.status === "ok" ? "sd-ok" : ls.status === "partial" ? "sd-warn" : ls.status === "failed" ? "sd-bad" : "sd-idle";
    const when = ls.status === "running" ? `started ${ago(ls.started_at)}` : ago(ls.finished_at);
    const nNew = (o.alerts || []).filter((a) => a.status === "new").length;
    $("#alerts-count").textContent = nNew;
    $("#alerts-dot").classList.toggle("on", nNew > 0);
    $("#mode-badge").textContent = STATIC ? "public" : o.mode;
    $("#statusbar").innerHTML = `<span><span class="status-dot ${st}"></span>sync ${esc(ls.status || "never")} · ${esc(when)}</span>
      <span>${fmt(o.counts.projects, "int")} projects</span><span>${fmt(o.counts.documents, "int")} documents</span>
      <span>${fmt(o.counts.findings, "int")} findings</span><span>${fmt(o.counts.experiments, "int")} experiments</span>
      <span>${fmt(o.counts.theses, "int")} theses</span><span>${fmt(o.counts.signals, "int")} signals</span>
      <span>${STATIC ? "read-only public build · public sources only" : "private · local"}</span>`;
  } catch (e) { $("#statusbar").textContent = "status unavailable"; }
}

// ------------------------------------------------------------------ shared renderers
const relLabel = { investment: "investment", game_theory: "game theory", tooling: "tooling", reference: "reference", not_relevant: "not relevant", unknown: "unknown" };
function projectCards(projects) {
  if (!projects.length) return '<div class="empty">No projects in this area yet.</div>';
  return `<div class="cards">${projects.map((p) => `<div class="card" data-slug="${esc(p.slug)}">
    <div class="flex space"><span class="nm">${esc(p.name)}</span><span class="badge">${esc(p.status || "")}</span></div>
    <div class="meta">${esc(p.category_label || "")} · ${fmt(p.documents, "int")} docs · ${fmt(p.experiments, "int")} exp${p.theses ? ` · ${p.theses} thesis` : ""} · ${esc(p.visibility)}</div>
    <div class="sm">${esc(p.summary || "")}</div>
    <div style="margin-top:6px">${(p.subtopics || []).slice(0, 3).map((s) => `<span class="pill">${esc(s.label)}</span>`).join("")}</div></div>`).join("")}</div>`;
}
function wireCards(root = main) { $$(".card[data-slug]", root).forEach((c) => c.addEventListener("click", () => openProject(c.dataset.slug))); }

const thesisCols = [
  { key: "symbol", label: "Ticker", render: (t) => `<span class="tkr">${esc(t.symbol)}</span>` },
  { key: "rating", label: "Call", render: (t) => stanceBadge(t.stance, t.rating) },
  { key: "target_price", label: "Target", num: true, render: (t) => fmt(t.target_price, "usd") },
  { key: "price_at_thesis", label: "Price then", num: true, render: (t) => fmt(t.price_at_thesis, "usd") },
  { key: "upside", label: "Upside", num: true, render: (t) => fmt(t.upside, "spct") },
  { key: "thesis_date", label: "Date", render: (t) => date(t.thesis_date) },
  { key: "project", label: "Project" },
  { key: "status", label: "Status", render: (t) => `<span class="badge ${t.status === "active" ? "good" : ""}">${esc(t.status)}</span>` },
  { key: "title", label: "Document", render: (t) => `<span class="clip">${t.url ? link(t.url, t.title) : esc(t.title)}</span>` },
];
function openThesis(t) {
  const sc = t.scenarios || {};
  const list = (xs) => xs && xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : '<p class="muted">None extracted.</p>';
  openDrawer(`<h1>${esc(t.symbol)} · ${stanceBadge(t.stance, t.rating)}</h1><p class="sub">${esc(t.title || "")} — ${esc(t.project || "")}</p>
    <dl class="kv"><dt>Target</dt><dd class="num">${fmt(t.target_price, "usd")}</dd><dt>Price at thesis</dt><dd class="num">${fmt(t.price_at_thesis, "usd")}</dd>
    <dt>Upside</dt><dd class="num">${fmt(t.upside, "spct")}</dd><dt>Scenarios</dt><dd class="num">${Object.entries(sc).map(([k, v]) => `${esc(k)} ${fmt(v, "usd")}`).join(" · ") || "–"}</dd>
    <dt>Horizon</dt><dd>${esc(t.horizon || "–")}</dd><dt>Date</dt><dd>${esc(date(t.thesis_date))}</dd><dt>Version</dt><dd>v${esc(t.version || 1)} (${esc(t.status)})</dd>
    <dt>Source</dt><dd>${t.url ? link(t.url, t.path) : esc(t.path || "")}</dd></dl>
    <p class="note">Extracted from the document as written. The terminal never edits a thesis; changes appear as new versions and assistant alerts.</p>
    <h3>Evidence line</h3><div class="chunk">${esc(t.summary || "")}</div>
    <h3>Arguments</h3>${list(t.arguments)}<h3>Risks</h3>${list(t.risks)}<h3>Catalysts</h3>${list(t.catalysts)}<h3>Assumptions</h3>${list(t.assumptions)}
    <p><a href="#/company/${encodeURIComponent(t.symbol)}">All research on ${esc(t.symbol)} →</a></p>`);
}

const findingCols = [
  { key: "claim_type", label: "Type", render: (f) => claimBadge(f.claim_type) },
  { key: "text", label: "Finding", render: (f) => `<span class="clip">${esc(f.text)}</span>` },
  { key: "project", label: "Project" },
  { key: "title", label: "Source", render: (f) => `<span class="clip">${srcLine(f)}</span>` },
];

function metricsCell(m) {
  const keys = ["sharpe", "alpha", "t_stat", "p_value", "ic", "ann_return", "hit_rate", "max_drawdown", "brier"];
  return keys.filter((k) => m && m[k] !== undefined).map((k) => `<span class="pill mono">${k} ${fmt(m[k], "num", 3)}</span>`).join("");
}
const expCols = [
  { key: "project", label: "Project" },
  { key: "name", label: "Experiment", render: (e) => `<span class="clip">${esc(e.name)}</span>` },
  { key: "result_kind", label: "Kind", render: (e) => `<span class="badge">${esc((e.result_kind || "?").replace("_", " "))}</span>` },
  { key: "oos", label: "OOS", render: (e) => (e.oos ? '<span class="badge good">oos</span>' : '<span class="muted">–</span>'), sort: (e) => e.oos },
  { key: "metrics", label: "Metrics", render: (e) => metricsCell(e.metrics), sort: (e) => Math.abs((e.metrics || {}).t_stat || 0) },
  { key: "verdict", label: "Verdict", render: (e) => esc(e.verdict || "") },
  { key: "flags", label: "Flags", render: (e) => (e.flags || []).filter((f) => ["possible", "detected"].includes(f.status)).map((f) => `<span class="badge warn" title="${esc(f.detail)}">${esc(f.flag.replace(/_/g, " "))}</span>`).join(" "), sort: (e) => (e.flags || []).length },
];
function openExperiment(e) {
  openDrawer(`<h1>${esc(e.name)}</h1><p class="sub">${esc(e.project_name || e.project)} · ${esc(e.source_kind)} · ${e.url ? link(e.url, e.location) : esc(e.location || "")}${e.line ? `:${e.line}` : ""}</p>
    <dl class="kv"><dt>Result kind</dt><dd>${esc(e.result_kind || "?")}</dd><dt>Out-of-sample</dt><dd>${e.oos ? "yes (language found)" : "not stated"}</dd>
    <dt>Costs</dt><dd>${esc(e.costs || "not stated")}</dd><dt>Periods</dt><dd>${esc((e.periods || []).join(", ") || "–")}</dd>
    <dt>Hypothesis</dt><dd>${esc(e.hypothesis || "–")}</dd><dt>Verdict</dt><dd>${esc(e.verdict || "–")}</dd></dl>
    <h3>Metrics (as stated in the source)</h3>${table(Object.entries(e.metrics || {}).map(([k, v]) => ({ k, v })), [{ key: "k", label: "Metric" }, { key: "v", label: "Value", num: true, render: (r) => fmt(r.v, "num", 4) }])}
    <h3>Research-practice checks</h3>${(e.flags || []).length ? (e.flags || []).map((f) => `<div class="alert ${f.status === "acknowledged" ? "info" : "warning"}"><div class="t">${esc(f.flag.replace(/_/g, " "))} · ${esc(f.status)}${f.scope ? " · project-level" : ""}</div><div class="dtl">${esc(f.detail || "")}</div>${f.evidence ? `<div class="m">${esc(typeof f.evidence === "string" ? f.evidence : JSON.stringify(f.evidence).slice(0, 400))}</div>` : ""}</div>`).join("") : '<p class="muted">No flags.</p>'}
    ${e.limitations ? `<h3>Limitations (from the document)</h3><div class="chunk">${esc(e.limitations)}</div>` : ""}
    <p class="note">Heuristic flags are prompts to check, not verdicts. Nothing is computed that the source does not state.</p>`);
}

function alertsList(alerts, { actions = !STATIC } = {}) {
  if (!alerts.length) return '<div class="empty">No alerts.</div>';
  return alerts.map((a) => `<div class="alert ${esc(a.severity)}" data-id="${a.id}">
    <div class="flex space"><span class="t">${esc(a.title)}</span><span class="badge">${esc(a.basis.replace("_", " "))}</span></div>
    <div class="m">${esc(a.kind.replace("_", " "))} · ${esc(ago(a.created_at))} · ${esc(a.status)}</div>
    <div class="dtl">${esc(a.detail)}</div>
    ${actions ? `<div class="acts"><button class="btn ghost" data-st="seen">Seen</button><button class="btn ghost" data-st="dismissed">Dismiss</button><button class="btn ghost" data-st="resolved">Resolved</button></div>` : ""}</div>`).join("");
}
function wireAlerts(root) {
  $$(".alert[data-id] button[data-st]", root).forEach((b) => b.addEventListener("click", async () => {
    const id = b.closest(".alert").dataset.id;
    await post(`/alerts/${id}`, { status: b.dataset.st });
    b.closest(".alert").style.opacity = 0.45;
    invalidate("/overview"); invalidate("/alerts"); statusbar();
  }));
}
async function openAlerts() {
  const alerts = await get("/alerts", { fresh: true });
  openDrawer(`<h1>Research assistant</h1><p class="sub">Connections, affected theses, stale numbers, decaying strategies and gaps found after each sync.
    <b>Actual update</b> = something verifiably changed; <b>speculative connection</b> = inferred from shared companies or themes; <b>gap</b> = something missing.
    The assistant never edits a thesis.</p>${alertsList(alerts)}`);
  wireAlerts($("#drawer"));
}

// ------------------------------------------------------------------ overview
async function overview() {
  const o = await get("/overview", { fresh: true });
  const c = o.counts;
  let html = `<h1>Overview</h1><p class="sub">Every repository, local project and published page, re-indexed automatically. ${STATIC ? "This public build shows public sources only." : ""}</p>
    <div class="kpis">${kpi("Projects", fmt(c.projects, "int"), `${c.investment_projects} investment · ${c.game_theory} game theory`)}${kpi("Documents", fmt(c.documents, "int"))}
    ${kpi("Findings", fmt(c.findings, "int"), "typed by claim status")}${kpi("Experiments", fmt(c.experiments, "int"))}${kpi("Theses", fmt(c.theses, "int"), "active calls")}
    ${kpi("Signals", fmt(c.signals, "int"))}${kpi("Companies", fmt(c.companies, "int"), "main subject somewhere")}${kpi("Sources", fmt(c.sources, "int"), "repos · folders · site")}</div><div class="grid">`;
  html += panel("Research assistant", `<div id="ov-alerts">${alertsList((o.alerts || []).slice(0, 8))}</div>`, { cls: "c6", scroll: true, tools: `<a href="#" id="all-alerts">all alerts</a>` });
  html += panel("Active theses", table(o.theses, thesisCols.slice(0, 7), { onRow: openThesis, empty: "No thesis found yet." }), { cls: "c6", flush: true, scroll: true });
  html += panel("Recently active projects", table(o.recent_projects, [
    { key: "name", label: "Project", render: (p) => `<b>${esc(p.name)}</b>` }, { key: "category_label", label: "Area" },
    { key: "status", label: "Status", render: (p) => `<span class="badge">${esc(p.status || "")}</span>` }, { key: "last_activity", label: "Last activity", render: (p) => esc(ago(p.last_activity)) }],
    { onRow: (p) => openProject(p.slug) }), { cls: "c6", flush: true, scroll: true });
  html += panel("Signals by instrument", table(o.signals_top, [
    { key: "instrument", label: "Instrument", render: (s) => `<span class="tkr">${esc(s.instrument)}</span>` },
    { key: "score", label: "Net score", num: true, render: (s) => `<span style="color:${s.score > 0 ? "var(--good-ink)" : s.score < 0 ? "var(--critical)" : "inherit"}">${fmt(s.score, "num", 2)}</span>` },
    { key: "n", label: "Signals", num: true }, { key: "conflict", label: "", render: (s) => (s.conflict ? '<span class="badge warn">conflict</span>' : "") },
    { key: "contributors", label: "From", render: (s) => esc(s.contributors.slice(0, 3).map((x) => x.project || x.strategy).join(", ")) }],
    { onRow: (s) => (location.hash = `#/company/${encodeURIComponent(s.instrument)}`), empty: "No signals yet." }), { cls: "c6", flush: true, scroll: true });
  html += panel("Recent documents", table(o.recent_documents, [
    { key: "title", label: "Document", render: (d) => `<span class="clip">${esc(d.title)}</span>` }, { key: "doc_type", label: "Type" },
    { key: "project", label: "Project" }, { key: "date", label: "Date", render: (d) => date(d.date) }], { onRow: (d) => openDocument(d.id) }), { cls: "c6", flush: true, scroll: true });
  if (o.portfolios && o.portfolios.length) {
    html += panel("Portfolios", table(o.portfolios, [{ key: "name", label: "Portfolio" }, { key: "kind", label: "Kind" }, { key: "positions", label: "Positions", num: true },
      { key: "top", label: "Largest", render: (p) => esc(p.top.join(", ")) }, { key: "source", label: "Source", render: (p) => `<span class="muted">${esc(p.source)}</span>` }],
      { onRow: (p) => (location.hash = `#/portfolio/${encodeURIComponent(p.key)}`) }), { cls: "c6", flush: true });
  }
  html += panel("Areas", `<div id="ov-cats"></div>`, { cls: "c6" });
  main.innerHTML = html + "</div>";
  bars($("#ov-cats"), o.categories.map((x) => ({ label: x.label, value: x.n })), { valueFmt: (v) => fmt(v, "int") });
  wireAlerts($("#ov-alerts"));
  $("#all-alerts").addEventListener("click", (ev) => { ev.preventDefault(); openAlerts(); });
}

// ------------------------------------------------------------------ research
async function research() {
  const d = await get("/tab/research");
  let html = `<h1>Research</h1><p class="sub">Companies, investment theses, reports and financial models across projects. Theses are read from the documents as written; the same call stated in several documents is shown once (primary source first).</p><div class="grid">`;
  html += panel("Theses", `<div id="rs-theses"></div>`, { cls: "c12", flush: true, scroll: true,
    tools: `<label><input type="checkbox" id="th-all"> every source &amp; superseded versions</label>` });
  html += panel("Companies", table(d.companies, [
    { key: "symbol", label: "Ticker", render: (c) => `<span class="tkr">${esc(c.symbol)}</span>` }, { key: "name", label: "Name" },
    { key: "sector", label: "Sector" }, { key: "projects", label: "Projects", num: true }, { key: "mentions", label: "Mentions", num: true },
    { key: "theses", label: "Theses", num: true }, { key: "models", label: "Models", num: true }, { key: "signals", label: "Signals", num: true }],
    { onRow: (c) => (location.hash = `#/company/${encodeURIComponent(c.symbol)}`) }), { cls: "c7", flush: true, scroll: true });
  html += panel("Financial models", table(d.models, [
    { key: "name", label: "Model", render: (m) => `<span class="clip">${m.url ? link(m.url, m.name) : esc(m.name)}</span>` }, { key: "symbol", label: "Ticker" },
    { key: "model_type", label: "Type" }, { key: "project", label: "Project" },
    { key: "outputs", label: "Key outputs", render: (m) => (m.outputs || []).slice(0, 3).map((o) => `<span class="pill mono">${esc(o.key)} ${fmt(o.value, "num", 2)}</span>`).join("") }],
    { empty: "No spreadsheet models found." }), { cls: "c5", flush: true, scroll: true });
  html += panel("Research documents", table(d.documents, [
    { key: "title", label: "Title", render: (x) => `<span class="clip">${esc(x.title)}</span>` }, { key: "doc_type", label: "Type" },
    { key: "project", label: "Project" }, { key: "date", label: "Date", render: (x) => date(x.date) },
    { key: "visibility", label: "", render: (x) => `<span class="badge">${esc(x.visibility)}</span>` }], { onRow: (x) => openDocument(x.id) }), { cls: "c12", flush: true, scroll: true });
  html += panel("Research projects", projectCards(d.projects), { cls: "c12" });
  main.innerHTML = html + "</div>";
  const drawTheses = () => {
    const all = $("#th-all").checked;
    const rows = d.theses.filter((t) => all || (t.primary && t.status === "active"));
    const cols = [...thesisCols];
    cols.splice(8, 0, { key: "n_sources", label: "Sources", num: true });
    $("#rs-theses").innerHTML = table(rows, cols, { onRow: openThesis, empty: "No document states a call yet." });
  };
  $("#th-all").addEventListener("change", drawTheses);
  drawTheses();
  wireCards();
}

// ------------------------------------------------------------------ quant
async function quant() {
  const d = await get("/tab/quant");
  const exps = d.experiments || [];
  let html = `<h1>Quant</h1><p class="sub">Strategies, models, experiments and backtests. Metrics are as stated in each project's reports and result files; flags are heuristic prompts.</p><div class="grid">`;
  const byProj = Object.entries(d.experiment_summary.by_project || {}).map(([k, v]) => ({ project: k, ...v })).sort((a, b) => b.n - a.n);
  html += panel("Experiments by project", table(byProj, [{ key: "project", label: "Project" }, { key: "n", label: "Results", num: true },
    { key: "oos", label: "Out-of-sample", num: true }, { key: "significant", label: "t≥2 or p<.05", num: true }, { key: "flags", label: "Flagged", num: true }]), { cls: "c6", flush: true, scroll: true });
  html += panel("Result kinds", `<div id="q-kinds"></div><p class="note">historical backtest · simulation · statistical study · forecast evaluation · synthetic demo · live · paper</p>`, { cls: "c6" });
  html += panel(`Experiments (${exps.length}${d.experiments_total > exps.length ? ` most informative of ${d.experiments_total}` : ""})`, `<div id="q-exps"></div>`, { cls: "c12", flush: true, scroll: true,
    tools: `<label><input type="checkbox" id="f-oos"> OOS only</label><label><input type="checkbox" id="f-sig"> significant</label><label><input type="checkbox" id="f-clean"> no flags</label><input id="f-q" placeholder="filter…">` });
  html += panel("Strategy registry", table(d.strategies, [{ key: "name", label: "Strategy" }, { key: "family", label: "Family" }, { key: "project", label: "Project" },
    { key: "costs_modeled", label: "Costs", render: (s) => (s.costs_modeled ? "modelled" : "–") }, { key: "first_date", label: "From", render: (s) => date(s.first_date) },
    { key: "last_date", label: "To", render: (s) => date(s.last_date) }, { key: "description", label: "Description", render: (s) => `<span class="clip">${esc(s.description || "")}</span>` }],
    { empty: "No strategy registry files found." }), { cls: "c12", flush: true, scroll: true });
  html += panel("Model registry (saved artefacts)", table(d.ml_models || [], [{ key: "project", label: "Project" }, { key: "name", label: "Folder", render: (m) => `<span class="mono">${esc(m.name)}</span>` },
    { key: "kind", label: "Kind" }, { key: "artifacts", label: "Files", num: true }, { key: "modified", label: "Updated", render: (m) => date(m.modified) }],
    { empty: "No saved model files found." }), { cls: "c12", flush: true, scroll: true });
  html += panel("Signal registry", signalTable(d.signals.registry), { cls: "c12", flush: true, scroll: true });
  html += panel("Projects", projectCards(d.projects), { cls: "c12" });
  main.innerHTML = html + "</div>";
  bars($("#q-kinds"), Object.entries(d.experiment_summary.result_kinds || {}).map(([k, v]) => ({ label: k.replace(/_/g, " "), value: v })), { valueFmt: (v) => fmt(v, "int") });
  const draw = () => {
    const q = $("#f-q").value.toLowerCase();
    const rows = exps.filter((e) => (!$("#f-oos").checked || e.oos) &&
      (!$("#f-sig").checked || Math.abs((e.metrics || {}).t_stat || 0) >= 2 || ((e.metrics || {}).p_value ?? 1) < 0.05) &&
      (!$("#f-clean").checked || !(e.flags || []).some((f) => ["possible", "detected"].includes(f.status))) &&
      (!q || `${e.name} ${e.project}`.toLowerCase().includes(q)));
    $("#q-exps").innerHTML = table(rows, expCols, { onRow: openExperiment, max: 600 });
  };
  ["f-oos", "f-sig", "f-clean"].forEach((id) => $(`#${id}`).addEventListener("change", draw));
  $("#f-q").addEventListener("input", draw);
  draw();
  wireCards();
}

function signalTable(rows) {
  return table(rows, [
    { key: "instrument", label: "Instrument", render: (s) => `<a class="tkr" href="#/company/${encodeURIComponent(s.instrument)}">${esc(s.instrument)}</a>` },
    { key: "direction", label: "Dir", num: true, render: (s) => (s.direction > 0 ? "▲ long" : s.direction < 0 ? "▼ short" : "• flat") },
    { key: "expected_return", label: "Exp. return", num: true, render: (s) => fmt(s.expected_return, "spct") },
    { key: "confidence", label: "Conf.", num: true, render: (s) => fmt(s.confidence, "num", 2) },
    { key: "horizon", label: "Horizon" }, { key: "strategy_id", label: "Strategy", render: (s) => `<span class="clip mono">${esc(s.strategy_id)}</span>` },
    { key: "validation_status", label: "Validation", render: (s) => `<span class="badge ${s.validation_status === "validated_oos" ? "good" : s.validation_status === "unvalidated" ? "warn" : ""}">${esc(s.validation_status)}</span>` },
    { key: "available_at", label: "Known at", render: (s) => date(s.available_at) }], { empty: "No signals." });
}

// ------------------------------------------------------------------ alt data
async function altdata() {
  const d = await get("/tab/altdata");
  let html = `<h1>Alternative data</h1><p class="sub">Operational datasets, the sources behind them, and which companies each alt-data project informs.</p><div class="grid">`;
  html += panel("Alt-data projects", projectCards(d.projects), { cls: "c12" });
  html += panel("Data sources in use", table(d.data_sources, [{ key: "name", label: "Source" }, { key: "projects", label: "Projects", num: true },
    { key: "mentions", label: "Mentions", num: true }, { key: "project_slugs", label: "Used by", render: (r) => `<span class="clip">${esc((r.project_slugs || "").replace(/,/g, ", "))}</span>` }]), { cls: "c6", flush: true, scroll: true });
  html += panel("Company ↔ project links", table(d.company_links, [{ key: "symbol", label: "Ticker", render: (r) => `<a class="tkr" href="#/company/${encodeURIComponent(r.symbol)}">${esc(r.symbol)}</a>` },
    { key: "name", label: "Company" }, { key: "slug", label: "Project" }, { key: "salience", label: "Salience", num: true }, { key: "mentions", label: "Mentions", num: true }]), { cls: "c6", flush: true, scroll: true });
  html += panel("Signal tables found in project outputs", table(d.signals.sources, [{ key: "path", label: "File", render: (r) => `<span class="mono clip">${esc(r.path)}</span>` },
    { key: "project", label: "Project" }, { key: "status", label: "Status", render: (r) => `<span class="badge ${r.status === "active" ? "good" : "warn"}">${esc(r.status)}</span>` },
    { key: "confidence", label: "Mapping conf.", num: true }, { key: "rows", label: "Rows", num: true },
    { key: "mapping", label: "Mapping", render: (r) => `<span class="mono">${esc(r.mapping.ticker_col)} → ${esc(r.mapping.value_col || r.mapping.action_col || "")}</span>` }],
    { empty: "No signal-like tables detected." }), { cls: "c12", flush: true, scroll: true });
  main.innerHTML = html + "</div>";
  wireCards();
}

// ------------------------------------------------------------------ markets
async function markets() {
  const d = await get("/tab/markets");
  let html = `<h1>Markets</h1><p class="sub">Derivatives, volatility, credit, rates and relative-value research, plus the security master the terminal links everything to.</p><div class="grid">`;
  html += panel("Derivatives, rates & credit projects", projectCards(d.projects), { cls: "c12" });
  html += panel("Results in these projects", table(d.experiments, expCols, { onRow: openExperiment, max: 400, empty: "No extracted results." }), { cls: "c12", flush: true, scroll: true });
  html += panel("Security master", table(d.securities, [{ key: "symbol", label: "Symbol", render: (s) => `<a class="tkr" href="#/company/${encodeURIComponent(s.symbol)}">${esc(s.symbol)}</a>` },
    { key: "name", label: "Name" }, { key: "sector", label: "Sector" }, { key: "industry", label: "Industry" }, { key: "currency", label: "Ccy" },
    { key: "themes", label: "Themes", render: (s) => Object.entries(s.themes || {}).map(([k, v]) => `<span class="pill" title="${esc(v)}">${esc(k.replace(/_/g, " "))}</span>`).join("") },
    { key: "mentions", label: "Mentions", num: true }, { key: "origin", label: "Origin" }]), { cls: "c12", flush: true, scroll: true });
  main.innerHTML = html + "</div>";
  wireCards();
}

// ------------------------------------------------------------------ portfolio
async function portfolio([key]) {
  if (STATIC) {
    main.innerHTML = `<h1>Portfolio</h1><div class="panel"><div class="panel-b">The public build does not publish holdings or amounts. The portfolio by weight is on the
      <a href="https://rasmuscodes.github.io/portfolio/">website's portfolio page</a>. Holdings, performance, exposures and risk run in the private terminal.</div></div>`;
    return;
  }
  const list = await get("/portfolios", { fresh: true });
  key = key || (list[0] && list[0].key);
  let html = `<h1>Portfolio</h1><p class="sub">Holdings, performance and allocation. The personal portfolio is read (never written) from the website's private ledger; books found in projects are mirrored read-only; paper portfolios live here.</p>
    <div class="tools" style="margin-bottom:12px"><select id="pf-sel">${list.map((p) => `<option value="${esc(p.key)}" ${p.key === key ? "selected" : ""}>${esc(p.name)} (${esc(p.kind)})</option>`).join("")}</select>
    <a href="#/risk/${encodeURIComponent(key || "")}">Risk for this portfolio →</a></div>`;
  if (!key) { main.innerHTML = html + '<div class="empty">No portfolios yet. Create a paper portfolio or import a CSV below.</div>' + paperForms(); wirePaper(); return; }
  const d = await get(`/portfolios/${encodeURIComponent(key)}`, { fresh: true });
  const H = d.holdings, perf = d.performance || {};
  const cash = Object.entries(H.cash || {}).filter(([, v]) => v).map(([k, v]) => `${esc(k)} ${fmt(v, "money")}`).join(" · ");
  html += `<div class="kpis">${kpi("Positions", fmt(H.holdings.length, "int"), cash ? `cash: ${cash}` : "")}${kpi("Total return", fmt(perf.total_return, "spct"), perf.start ? `since ${perf.start}` : "")}
    ${kpi("CAGR", fmt(perf.cagr, "spct"))}${kpi("Volatility", fmt(perf.volatility, "pct"))}${kpi("Max drawdown", fmt(perf.max_drawdown, "spct"), perf.max_drawdown_from ? `${perf.max_drawdown_from} → ${perf.max_drawdown_to}` : "")}
    ${perf.benchmark ? kpi(`Benchmark (${esc(perf.benchmark.name || "")})`, fmt(perf.benchmark.total_return, "spct"), perf.beta !== undefined ? `beta ${fmt(perf.beta, "num", 2)}` : "") : ""}</div><div class="grid">`;
  html += panel("Performance", `<div id="pf-perf"></div><p class="note">${esc(perf.method || perf.reason || "")}</p>`, { cls: "c8" });
  html += panel("Allocation by sector", `<div id="pf-sector"></div>`, { cls: "c4" });
  html += panel("Holdings", table(H.holdings, [
    { key: "symbol", label: "Symbol", render: (h) => `<a class="tkr" href="#/company/${encodeURIComponent(h.symbol)}">${esc(h.symbol)}</a>` }, { key: "name", label: "Name" },
    { key: "weight", label: "Weight", num: true, render: (h) => fmt(h.weight, "pct") }, { key: "qty", label: "Qty", num: true, render: (h) => fmt(h.qty, "num", 4) },
    { key: "last", label: "Last", num: true, render: (h) => fmt(h.last, "num", 2) }, { key: "avg_cost", label: "Avg cost", num: true, render: (h) => fmt(h.avg_cost, "num", 2) },
    { key: "return_pct", label: "Return", num: true, render: (h) => (h.return_pct === null || h.return_pct === undefined ? "–" : fmt(h.return_pct, "pct100")) },
    { key: "sector", label: "Sector" }, { key: "theses", label: "Your calls", render: (h) => (h.theses || []).map((t) => `${esc(t.rating)}${t.target ? ` $${fmt(t.target, "num", 0)}` : ""} (${esc(t.project)})`).join("; ") || '<span class="muted">none</span>' }]),
    { cls: "c12", flush: true });
  html += panel("Transactions", table(d.transactions, [{ key: "date", label: "Date" }, { key: "kind", label: "Kind" }, { key: "symbol", label: "Symbol" },
    { key: "qty", label: "Qty", num: true, render: (t) => fmt(t.qty, "num", 4) }, { key: "price", label: "Price", num: true, render: (t) => fmt(t.price, "num", 2) },
    { key: "source", label: "Source" }], { empty: d.portfolio.read_only ? "This portfolio is a read-only snapshot (no transaction list)." : "No transactions." }), { cls: "c6", flush: true, scroll: true });
  html += panel("Paper trading & import", paperForms(d.portfolio), { cls: "c6" });
  main.innerHTML = html + "</div>";
  $("#pf-sel").addEventListener("change", (ev) => (location.hash = `#/portfolio/${encodeURIComponent(ev.target.value)}`));
  if (perf.series) {
    const s = [{ name: d.portfolio.name.split(" (")[0], points: perf.series.map((p) => ({ x: p.d, y: p.p })) }];
    if (perf.benchmark) s.push({ name: perf.benchmark.name || "Benchmark", points: perf.benchmark.series.map((p) => ({ x: p.d, y: p.b })) });
    line($("#pf-perf"), s, { yFmt: (v) => fmt(v, "num", 0) });
  } else $("#pf-perf").innerHTML = `<div class="empty">${esc(perf.reason || "No performance history.")}</div>`;
  const sec = {};
  H.holdings.forEach((h) => { sec[h.sector || "Unclassified"] = (sec[h.sector || "Unclassified"] || 0) + (h.weight || 0); });
  bars($("#pf-sector"), Object.entries(sec).map(([k, v]) => ({ label: k, value: v })).sort((a, b) => b.value - a.value));
  wirePaper(d.portfolio);
}

function paperForms(p) {
  const canTrade = p && p.kind === "paper" && !p.read_only;
  return `${canTrade ? `<h3>Paper order (${esc(p.name)})</h3><div class="flex"><input id="po-sym" placeholder="Symbol" size="8"><select id="po-side"><option>buy</option><option>sell</option><option>short</option><option>cover</option></select>
      <input id="po-qty" type="number" min="0" step="any" placeholder="Qty" style="width:90px"><button class="btn" id="po-go">Place paper order</button></div><div class="note" id="po-msg">Fills at the last cached close. Live orders are refused: there is no broker connection.</div>` : ""}
    <h3>New paper portfolio</h3><div class="flex"><input id="pp-name" placeholder="Name"><input id="pp-cash" type="number" value="100000" style="width:110px"><select id="pp-ccy"><option>USD</option><option>EUR</option></select><button class="btn ghost" id="pp-go">Create</button></div>
    <h3>Import CSV (trades or holdings)</h3><div class="field"><textarea id="ci-csv" placeholder="date,symbol,side,qty,price&#10;2026-11-01,NVDA,buy,10,190.5"></textarea></div>
    <div class="flex"><input id="ci-key" placeholder="Portfolio name" value="${esc(p && p.kind === "paper" && !p.read_only ? p.key : "")}"><button class="btn ghost" id="ci-go">Import</button></div><div class="note" id="ci-msg"></div>`;
}
function wirePaper(p) {
  const go = $("#po-go");
  if (go) go.addEventListener("click", async () => {
    try {
      const r = await post(`/portfolios/${encodeURIComponent(p.key)}/orders`, { symbol: $("#po-sym").value, side: $("#po-side").value, qty: Number($("#po-qty").value) });
      $("#po-msg").textContent = `Order ${r.order_id}: ${r.status}${r.fill_price ? ` at ${r.fill_price} (${r.price_date})` : ""}.`;
      invalidate("/portfolios"); setTimeout(route, 600);
    } catch (e) { $("#po-msg").textContent = e.message; }
  });
  $("#pp-go").addEventListener("click", async () => {
    try { const r = await post("/paper", { name: $("#pp-name").value, cash: Number($("#pp-cash").value), currency: $("#pp-ccy").value }); location.hash = `#/portfolio/${encodeURIComponent(r.key)}`; }
    catch (e) { alert(e.message); }
  });
  $("#ci-go").addEventListener("click", async () => {
    try { const r = await post("/import", { csv: $("#ci-csv").value, portfolio: $("#ci-key").value || "imported" }); $("#ci-msg").textContent = `Imported ${r.added} rows into ${r.portfolio} (${r.skipped} skipped).`; invalidate("/portfolios"); }
    catch (e) { $("#ci-msg").textContent = e.message; }
  });
}

// ------------------------------------------------------------------ risk
async function risk([key]) {
  if (STATIC) { main.innerHTML = `<h1>Risk</h1><div class="panel"><div class="panel-b">Risk analytics need the private holdings and run in the local terminal only.</div></div>`; return; }
  const list = await get("/portfolios", { fresh: true });
  key = key || (list[0] && list[0].key);
  if (!key) { main.innerHTML = '<h1>Risk</h1><div class="empty">No portfolio yet.</div>'; return; }
  main.innerHTML = `<h1>Risk</h1><p class="sub">Computed on today's holdings and their daily history in the portfolio's base currency.</p><div class="loading">Computing risk…</div>`;
  const r = await get(`/portfolios/${encodeURIComponent(key)}/risk`, { fresh: true });
  let html = `<h1>Risk</h1><p class="sub">Computed on today's holdings and their daily return history (${r.lookback_days || 0} days, ${esc(r.base_currency || "")}). Coverage = share of the book with price history.</p>
    <div class="tools" style="margin-bottom:12px"><select id="rk-sel">${list.map((p) => `<option value="${esc(p.key)}" ${p.key === key ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div>`;
  if (!r.available) { main.innerHTML = html + `<div class="empty">${esc(r.reason || "Not enough price history.")}</div>`; wireRiskSel(); return; }
  const v95 = (r.var || {})["0.95"] || {}, v99 = (r.var || {})["0.99"] || {};
  html += `<div class="kpis">${kpi("Volatility (ann.)", fmt(r.volatility_ann, "pct"), `historical ${fmt(r.volatility_hist_ann, "pct")}`)}${kpi("VaR 95% 1d", fmt(v95.hist_var_1d, "pct"), `ES ${fmt(v95.hist_es_1d, "pct")}`)}
    ${kpi("VaR 99% 1d", fmt(v99.hist_var_1d, "pct"), `ES ${fmt(v99.hist_es_1d, "pct")}`)}${kpi("Beta", fmt(r.beta, "num", 2), r.beta_benchmark ? `vs ${esc(r.beta_benchmark)}` : "")}
    ${kpi("Drawdown (hist.)", fmt(r.drawdown_hist, "spct"))}${kpi("Coverage", fmt(r.coverage, "pct"), r.missing_history && r.missing_history.length ? `missing: ${esc(r.missing_history.join(", "))}` : "all priced")}
    ${kpi("Effective N", fmt(r.concentration.effective_n, "num", 1), `top 5 = ${fmt(r.concentration.top5, "pct")}`)}</div><div class="grid">`;
  html += panel("Risk contribution", `<div id="rk-rc"></div><p class="note">Share of portfolio variance (${esc(r.covariance || "")}).</p>`, { cls: "c6" });
  html += panel("Factor exposure", r.factors && r.factors.available ? table(r.factors.betas, [{ key: "factor", label: "Factor" }, { key: "beta", label: "Beta", num: true, render: (b) => fmt(b.beta, "num", 3) }, { key: "t", label: "t", num: true, render: (b) => fmt(b.t, "num", 2) }])
    + `<p class="note">${esc(r.factors.model)} · R² ${fmt(r.factors.r2, "num", 2)} · alpha ${fmt(r.factors.alpha_ann, "spct")} (t ${fmt(r.factors.alpha_t, "num", 2)}) · ${esc(r.factors.note)}</p>` : `<div class="empty">${esc((r.factors || {}).reason || "Not available.")}</div>`, { cls: "c6" });
  html += panel("Sector exposure", `<div id="rk-sector"></div>`, { cls: "c4" });
  html += panel("Theme exposure (direct)", `<div id="rk-theme"></div><p class="note">From security-master roles; edit config/securities.yaml.</p>`, { cls: "c4" });
  html += panel("Currency exposure", `<div id="rk-ccy"></div>`, { cls: "c4" });
  html += panel("Scenarios", table([...(r.scenarios.historical || []).map((s) => ({ ...s, type: "historical" })), ...(r.scenarios.hypothetical || []).map((s) => ({ ...s, type: "hypothetical" }))], [
    { key: "label", label: "Scenario" }, { key: "type", label: "Type" }, { key: "window", label: "Window", render: (s) => esc(s.window || s.method || "") },
    { key: "pnl_pct", label: "P&L", num: true, render: (s) => `<span style="color:${s.pnl_pct < 0 ? "var(--critical)" : "var(--good-ink)"}">${fmt(s.pnl_pct, "spct")}</span>` },
    { key: "beta_proxied", label: "Proxied", render: (s) => esc((s.beta_proxied || []).join(", ")) }]) + `<p class="note">${esc(r.scenarios.note)}</p>`, { cls: "c6", flush: false });
  html += panel("Correlation (largest holdings)", `<div id="rk-corr" style="overflow:auto"></div>`, { cls: "c6" });
  html += panel("Optimiser (suggestion only)", `<div class="flex"><select id="op-m"><option value="min_variance">Minimum variance</option><option value="risk_parity">Risk parity</option><option value="max_sharpe">Max Sharpe (uses your signals)</option></select>
      <label>max weight <input id="op-w" type="number" value="0.25" step="0.05" min="0.05" max="1" style="width:70px"></label><button class="btn" id="op-go">Run</button></div><div id="op-out"></div>`, { cls: "c6" });
  html += panel("Position sizing", `<div class="flex"><input id="sz-sym" placeholder="Symbol" size="8"><label>target risk <input id="sz-r" type="number" value="0.02" step="0.005" style="width:70px"></label><button class="btn ghost" id="sz-go">Size</button></div><div id="sz-out"></div>`, { cls: "c6" });
  main.innerHTML = html + "</div>";
  wireRiskSel();
  bars($("#rk-rc"), r.risk_contributions.map((x) => ({ label: x.symbol, value: x.risk_share, tip: `weight ${fmt(x.weight, "pct")}, vol ${fmt(x.vol_ann, "pct")}` })), { signed: true });
  const ex = r.exposures || {};
  bars($("#rk-sector"), Object.entries(ex.sector || {}).map(([k, v]) => ({ label: k, value: v })));
  bars($("#rk-theme"), Object.entries(ex.theme_direct || {}).map(([k, v]) => ({ label: k.replace(/_/g, " "), value: v })));
  bars($("#rk-ccy"), Object.entries(ex.currency || {}).map(([k, v]) => ({ label: k, value: v })));
  heat($("#rk-corr"), r.correlation.symbols, r.correlation.matrix);
  $("#op-go").addEventListener("click", async () => {
    $("#op-out").innerHTML = '<div class="loading">Optimising…</div>';
    const o = await post(`/portfolios/${encodeURIComponent(key)}/optimize`, { method: $("#op-m").value, max_weight: Number($("#op-w").value) });
    $("#op-out").innerHTML = o.available ? table(o.weights, [{ key: "symbol", label: "Symbol" }, { key: "current", label: "Current", num: true, render: (w) => fmt(w.current, "pct") },
      { key: "proposed", label: "Proposed", num: true, render: (w) => fmt(w.proposed, "pct") }, { key: "mu", label: "Exp. ret.", num: true, render: (w) => fmt(w.mu, "spct") }])
      + `<p class="note">Vol ${fmt(o.current_vol, "pct")} → ${fmt(o.expected_vol, "pct")} · turnover ${fmt(o.turnover, "pct")} · ${esc(o.covariance)}. ${esc(o.note || "")} ${esc(o.disclaimer)}</p>` : `<div class="empty">${esc(o.reason)}</div>`;
  });
  $("#sz-go").addEventListener("click", async () => {
    const s = await get(`/portfolios/${encodeURIComponent(key)}/size/${encodeURIComponent($("#sz-sym").value || "")}?target_risk=${$("#sz-r").value}`, { fresh: true });
    $("#sz-out").innerHTML = s.available ? `<dl class="kv"><dt>Volatility</dt><dd>${fmt(s.vol_ann, "pct")}</dd><dt>Vol-target weight</dt><dd>${fmt(s.vol_target_weight, "pct")}</dd>
      <dt>Expected return</dt><dd>${fmt(s.expected_return, "spct")} ${s.signal_sources ? `<span class="muted">(${esc(s.signal_sources.join(", "))})</span>` : '<span class="muted">(no signal)</span>'}</dd>
      <dt>Fractional Kelly</dt><dd>${fmt(s.fractional_kelly_weight, "pct")}</dd></dl><p class="note">${esc(s.note)}</p>` : `<div class="empty">${esc(s.reason)}</div>`;
  });
}
function wireRiskSel() { const s = $("#rk-sel"); if (s) s.addEventListener("change", (ev) => (location.hash = `#/risk/${encodeURIComponent(ev.target.value)}`)); }

// ------------------------------------------------------------------ registry
async function registry() {
  const ps = await get("/projects");
  const cats = [...new Set(ps.map((p) => p.category_label))].sort();
  let html = `<h1>Project registry</h1><p class="sub">Every repository and project folder found automatically, with its classification and the evidence for it. New repositories and folders appear on the next sync with no registration.</p>
    <div class="kpis">${kpi("Total", ps.length)}${kpi("Investment", ps.filter((p) => p.relevance === "investment").length)}${kpi("Game theory", ps.filter((p) => p.relevance === "game_theory").length)}
    ${kpi("Public", ps.filter((p) => p.visibility === "public").length)}${kpi("Not relevant / reference", ps.filter((p) => ["not_relevant", "reference", "tooling"].includes(p.relevance)).length)}</div>`;
  html += panel("Projects", `<div id="rg-t"></div>`, { flush: true, tools: `<select id="rg-rel"><option value="">all relevance</option>${Object.keys(relLabel).map((k) => `<option value="${k}">${relLabel[k]}</option>`).join("")}</select>
    <select id="rg-cat"><option value="">all areas</option>${cats.map((c) => `<option>${esc(c)}</option>`).join("")}</select><select id="rg-src"><option value="">any source</option><option>github</option><option>local</option><option>website</option></select><input id="rg-q" placeholder="filter…">` });
  main.innerHTML = html;
  const draw = () => {
    const rel = $("#rg-rel").value, cat = $("#rg-cat").value, src = $("#rg-src").value, q = $("#rg-q").value.toLowerCase();
    const rows = ps.filter((p) => (!rel || p.relevance === rel) && (!cat || p.category_label === cat) && (!src || p.sources.some((s) => s.kind === src)) && (!q || `${p.name} ${p.summary}`.toLowerCase().includes(q)));
    $("#rg-t").innerHTML = table(rows, [
      { key: "name", label: "Project", render: (p) => `<b>${esc(p.name)}</b><div class="muted clip">${esc(p.summary || "")}</div>` },
      { key: "relevance", label: "Relevance", render: (p) => `<span class="badge">${esc(relLabel[p.relevance] || p.relevance || "?")}</span>` },
      { key: "category_label", label: "Area" },
      { key: "subtopics", label: "Topics", render: (p) => (p.subtopics || []).slice(0, 3).map((s) => `<span class="pill">${esc(s.label)}</span>`).join(""), sort: (p) => (p.subtopics || []).length },
      { key: "sources", label: "Sources", render: (p) => p.sources.map((s) => `<span class="badge" title="${esc(s.url || "")}">${esc(s.kind)}${s.visibility === "public" ? "·pub" : ""}</span>`).join(" "), sort: (p) => p.sources.length },
      { key: "documents", label: "Docs", num: true }, { key: "experiments", label: "Exp.", num: true }, { key: "findings", label: "Findings", num: true },
      { key: "status", label: "Status" }, { key: "last_activity", label: "Activity", render: (p) => esc(ago(p.last_activity)), sort: (p) => p.last_activity || "" }],
      { onRow: (p) => openProject(p.slug), max: 1000 });
  };
  ["rg-rel", "rg-cat", "rg-src"].forEach((id) => $(`#${id}`).addEventListener("change", draw));
  $("#rg-q").addEventListener("input", draw);
  draw();
}

async function openProject(slug) {
  openDrawer('<div class="loading">Loading project…</div>');
  const p = await get(`/projects/${encodeURIComponent(slug)}`);
  const cls = p.classification || {};
  const scores = Object.entries(cls.scores || {}).sort((a, b) => b[1] - a[1]);
  openDrawer(`<h1>${esc(p.name)}</h1><p class="sub">${esc(p.summary || "")}</p>
    <dl class="kv"><dt>Area</dt><dd>${esc(p.category_label)} <span class="badge">${esc(relLabel[p.relevance] || p.relevance)}</span></dd>
    <dt>Kind / status</dt><dd>${esc(p.kind || "")} · ${esc(p.status || "")}</dd><dt>Visibility</dt><dd>${esc(p.visibility)}</dd>
    <dt>Sources</dt><dd>${p.sources.map((s) => `${esc(s.kind)}: ${s.url ? link(s.url, s.name) : esc(s.name)} <span class="muted">(${esc(s.visibility)}, ${esc(s.status)})</span>`).join("<br>")}</dd>
    ${p.website_url ? `<dt>Website</dt><dd>${link(p.website_url, p.website_url)}</dd>` : ""}
    <dt>Technologies</dt><dd>${(p.technologies || []).map((t) => `<span class="pill">${esc(t)}</span>`).join("")}</dd>
    <dt>Data sources</dt><dd>${(p.data_sources || []).map((d) => `<span class="pill">${esc(d.label)}</span>`).join("") || "–"}</dd>
    <dt>Themes</dt><dd>${(p.themes || []).map((t) => `<span class="pill">${esc(t.label)}</span>`).join("") || "–"}</dd>
    <dt>Capabilities</dt><dd>${Object.entries(p.capabilities || {}).filter(([, v]) => v).map(([k, v]) => `<span class="pill">${esc(k.replace(/_/g, " "))}${v === true ? "" : ` ${esc(v)}`}</span>`).join("")}</dd>
    <dt>Last activity</dt><dd>${esc(ago(p.last_activity))}</dd></dl>
    <details><summary>Why this classification</summary><div class="note">${scores.map(([k, v]) => `${esc(k)} ${v}`).join(" · ")}</div>
    ${Object.entries(cls.terms || {}).map(([k, ts]) => `<div class="note"><b>${esc(k)}</b>: ${esc(ts.join(", "))}</div>`).join("")}</details>
    ${(cls.flags || []).length ? `<h3>Research-practice checks</h3>${cls.flags.map((f) => `<div class="alert ${f.status === "acknowledged" ? "info" : "warning"}"><div class="t">${esc(f.flag.replace(/_/g, " "))} · ${esc(f.status)}</div><div class="dtl">${esc(f.detail)}</div></div>`).join("")}` : ""}
    ${p.theses.length ? `<h3>Theses</h3>${table(p.theses, thesisCols.slice(0, 6), { onRow: openThesis })}` : ""}
    ${p.models.length ? `<h3>Models</h3>${table(p.models, [{ key: "name", label: "Model" }, { key: "model_type", label: "Type" }, { key: "outputs", label: "Outputs", render: (m) => (m.outputs || []).slice(0, 4).map((o) => `<span class="pill mono">${esc(o.key)} ${fmt(o.value, "num", 2)}</span>`).join("") }])}` : ""}
    ${(p.ml_models || []).length ? `<h3>Saved model artefacts</h3>${table(p.ml_models, [{ key: "name", label: "Folder", render: (m) => `<span class="mono">${esc(m.name)}</span>` }, { key: "kind", label: "Kind" }, { key: "artifacts", label: "Files", num: true }, { key: "modified", label: "Updated", render: (m) => date(m.modified) }])}` : ""}
    <h3>Key findings</h3>${table(p.findings.slice(0, 25), findingCols, { empty: "No findings extracted." })}
    ${p.experiments.length ? `<h3>Experiments (${p.experiments.length})</h3>${table(p.experiments, expCols.slice(1), { onRow: openExperiment, max: 80 })}` : ""}
    ${p.companies.length ? `<h3>Companies</h3><div>${p.companies.map((c) => `<a class="pill" href="#/company/${encodeURIComponent(c.symbol)}">${esc(c.symbol)} · ${c.mentions}</a>`).join("")}</div>` : ""}
    ${p.related.length ? `<h3>Related projects (inferred)</h3>${p.related.map((r) => `<div><a href="#" data-rel="${esc(r.slug)}">${esc(r.name)}</a> <span class="muted">${fmt(r.weight, "num", 2)} · shared: ${esc(((r.evidence || {}).shared || []).join(", "))}</span></div>`).join("")}` : ""}
    ${p.alerts.length ? `<h3>Assistant</h3>${alertsList(p.alerts, { actions: false })}` : ""}
    <h3>Documents (${p.documents.length})</h3>${table(p.documents, [{ key: "title", label: "Title", render: (d) => `<span class="clip">${esc(d.title)}</span>` }, { key: "doc_type", label: "Type" }, { key: "date", label: "Date", render: (d) => date(d.date) }, { key: "visibility", label: "Vis." }], { onRow: (d) => openDocument(d.id), max: 200 })}`);
  $$("[data-rel]", $("#drawer")).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); openProject(a.dataset.rel); }));
}

async function projectPage([slug]) { await registry(); if (slug) openProject(slug); }

async function openDocument(id) {
  if (STATIC) { const d = await get(`/documents/${id}`).catch(() => null); if (!d) return; }
  openDrawer('<div class="loading">Loading document…</div>');
  const d = await get(`/documents/${id}`);
  openDrawer(`<h1>${esc(d.title)}</h1><p class="sub">${esc(d.project_name || d.project || "")} · ${esc(d.doc_type)} · ${esc(date(d.date))} · ${d.url ? link(d.url, "open source") : esc(d.path)}</p>
    ${d.findings.length ? `<h3>Findings</h3>${table(d.findings, [{ key: "claim_type", label: "Type", render: (f) => claimBadge(f.claim_type) }, { key: "text", label: "Finding" }, { key: "line", label: "Line", num: true }, { key: "rule", label: "Rule", render: (f) => `<span class="muted mono">${esc(f.rule)}</span>` }])}` : ""}
    <h3>Text</h3>${d.chunks.slice(0, 60).map((c) => `<div class="note">${esc(c.heading || "")} <span class="mono">L${c.line_start}–${c.line_end}</span></div><div class="chunk">${esc(c.text)}</div>`).join("")}`);
}

// ------------------------------------------------------------------ company page
async function companyPage([sym]) {
  const c = await get(`/companies/${encodeURIComponent(sym)}`);
  let html = `<h1><span class="tkr">${esc(c.symbol)}</span> ${esc(c.name || "")}</h1><p class="sub">${esc(c.sector || "")}${c.industry ? ` · ${esc(c.industry)}` : ""}${c.exchange ? ` · ${esc(c.exchange)}` : ""}
    ${c.last_price ? ` · last ${fmt(c.last_price.close, "num", 2)} (${esc(c.last_price.date)})` : ""} · themes: ${Object.entries(c.themes || {}).map(([k, v]) => `${esc(k.replace(/_/g, " "))} (${esc(v)})`).join(", ") || "none"}</p>
    <div class="flex" style="margin-bottom:12px"><a class="btn ghost" href="#/search/${encodeURIComponent(`What research have I done on ${c.symbol}?`)}">Ask about ${esc(c.symbol)}</a>
    <a class="btn ghost" href="#/search/${encodeURIComponent(`What evidence contradicts my ${c.symbol} thesis?`)}">Contradicting evidence</a></div><div class="grid">`;
  html += panel("Theses", table(c.theses, thesisCols, { onRow: openThesis, empty: "No call on this company." }), { cls: "c12", flush: true });
  html += panel("Projects", table(c.projects, [{ key: "name", label: "Project" }, { key: "category", label: "Area" }, { key: "mentions", label: "Mentions", num: true }, { key: "documents", label: "Docs", num: true }, { key: "salience", label: "Salience", num: true }], { onRow: (p) => openProject(p.slug), empty: "No project mentions it." }), { cls: "c6", flush: true, scroll: true });
  html += panel("Signals", signalTable(c.signals), { cls: "c6", flush: true, scroll: true });
  html += panel("Findings", table(c.findings, findingCols, { empty: "No findings mention it." }), { cls: "c12", flush: true, scroll: true });
  html += panel("Models", table(c.models, [{ key: "name", label: "Model" }, { key: "model_type", label: "Type" }, { key: "project", label: "Project" }, { key: "outputs", label: "Outputs", render: (m) => (m.outputs || []).slice(0, 5).map((o) => `<span class="pill mono">${esc(o.label)} ${fmt(o.value, "num", 2)}</span>`).join("") }], { empty: "No model file for it." }), { cls: "c6", flush: true });
  html += panel("Documents", table(c.documents, [{ key: "title", label: "Title", render: (d) => `<span class="clip">${esc(d.title)}</span>` }, { key: "project", label: "Project" }, { key: "mentions", label: "Mentions", num: true }], { onRow: (d) => openDocument(d.id) }), { cls: "c6", flush: true, scroll: true });
  main.innerHTML = html + "</div>";
}

// ------------------------------------------------------------------ search / ask
const EXAMPLES = ["What research have I done on NVIDIA?", "What do all my models currently indicate about AI infrastructure?", "Which projects have researched data center power demand?",
  "What are the strongest arguments in my FTAI thesis?", "What evidence contradicts my current Netflix thesis?", "Which of my backtests have actually demonstrated out-of-sample alpha?",
  "What are my exposures to semiconductor capex?", "Which companies have appeared in multiple alternative-data projects?", "Which research projects have produced the most useful predictive results?"];

async function searchView([q]) {
  q = q || "";
  $("#cmd-input").value = q;
  let html = `<h1>Search & ask</h1><p class="sub">Natural-language questions are routed to structured queries over theses, findings, experiments, signals and holdings, then backed by hybrid search. Every statement cites its source; missing evidence is reported as missing.</p>
    <div class="flex" style="margin-bottom:12px">${EXAMPLES.map((e) => `<a class="pill" href="#/search/${encodeURIComponent(e)}">${esc(e)}</a>`).join("")}</div>`;
  if (!q) { main.innerHTML = html; return; }
  main.innerHTML = html + '<div class="loading">Thinking with your research…</div>';
  if (STATIC) {
    const hits = await search(q, 25);
    main.innerHTML = html + panel(`Results for “${q}”`, hits.length ? `<ol class="evidence">${hits.map((h, i) => `<li><span class="n">${i + 1}</span><div><b>${esc(h.title)}</b>${h.heading ? ` › ${esc(h.heading)}` : ""}<div>${esc(h.text)}</div><div class="src">${srcLine(h)}</div></div></li>`).join("")}</ol>` : '<div class="empty">No matches.</div>')
      + `<p class="note">The public build searches public material in your browser. Structured answers and the full archive run in the private terminal.</p>`;
    return;
  }
  const [ans, hits] = await Promise.all([post("/ask", { question: q }), search(q, 12)]);
  let out = "";
  if (ans.synthesis) out += `<div class="synth answer">${md(ans.synthesis.text)}<div class="note">Written by ${esc(ans.synthesis.model || "Claude")} from the numbered evidence only.</div></div>`;
  out += `<div class="answer">${md(ans.answer)}</div>${(ans.notes || []).map((n) => `<p class="note">${esc(n)}</p>`).join("")}<p class="note">Method: ${esc(ans.method)} · intent: ${esc(ans.intent)}</p>`;
  const ev = `<ol class="evidence">${ans.evidence.map((e, i) => `<li id="ev-${i + 1}"><span class="n">[${i + 1}]</span><div>${claimBadge(e.claim_type)} ${esc(e.text)}<div class="src">${srcLine(e)}</div></div></li>`).join("")}</ol>`;
  main.innerHTML = html + `<div class="grid">${panel("Answer", out, { cls: "c7" })}${panel(`Sources (${ans.evidence.length})`, ev || '<div class="empty">None.</div>', { cls: "c5", scroll: true })}
    ${panel("Related passages", `<ol class="evidence">${hits.map((h, i) => `<li><span class="n">${i + 1}</span><div><b>${esc(h.project || "")}</b> · ${esc(h.title)}${h.heading ? ` › ${esc(h.heading.split(" > ").pop())}` : ""}<div>${esc((h.text || "").slice(0, 420))}</div><div class="src">${srcLine({ ...h, line: h.line_start })}</div></div></li>`).join("")}</ol>`, { cls: "c12" })}</div>`;
  $$(".cite").forEach((c) => c.addEventListener("click", () => {
    const li = $(`#ev-${c.dataset.cite}`);
    if (li) { $$(".evidence li.hl").forEach((x) => x.classList.remove("hl")); li.classList.add("hl"); li.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }));
}

// ------------------------------------------------------------------ settings & health
async function settings() {
  if (STATIC) {
    const h = await get("/health");
    main.innerHTML = `<h1>Settings</h1><p class="sub">Public build: rebuilt on a schedule by GitHub Actions from public repositories and the live website only.</p>` +
      panel("Last builds", table(h.runs || [], [{ key: "started_at", label: "Started", render: (r) => esc(ago(r.started_at)) }, { key: "status", label: "Status" }, { key: "trigger", label: "Trigger" }]));
    return;
  }
  const [h, s] = await Promise.all([get("/health", { fresh: true }), get("/settings", { fresh: true })]);
  const sched = h.scheduler || {}, gha = h.github_actions || {};
  let html = `<h1>Settings & health</h1><p class="sub">Sources, schedules, integrations and permissions. Credentials are never stored by the terminal: GitHub access uses the GitHub CLI's own login.</p>
    <div class="kpis">${kpi("Last success", esc(ago(h.last_run && h.last_run.finished_at)), h.stale ? "STALE" : "fresh")}${kpi("Sources", h.source_summary.total, `${h.source_summary.stale} stale · ${h.source_summary.missing} missing`)}
    ${kpi("Errors (3 runs)", h.source_summary.with_errors)}${kpi("Database", `${h.db_mb} MB`)}${kpi("Scheduler", sched.installed ? esc(sched.status || "installed") : "not installed", sched.next_run ? `next ${esc(sched.next_run)}` : "")}
    ${kpi("GitHub Actions", gha.configured ? esc(((gha.runs || [])[0] || {}).conclusion || "configured") : "not configured")}</div>
    <div class="flex" style="margin-bottom:12px"><button class="btn" id="sync-now">Sync now</button><button class="btn ghost" id="sync-full">Full re-extract</button><span class="note" id="sync-msg"></span></div><div class="grid">`;
  html += panel("Manual steps remaining", h.manual_steps.length ? `<ul>${h.manual_steps.map((m) => `<li><span class="badge">${esc(m.level)}</span> ${esc(m.step)}</li>`).join("")}</ul>` : '<div class="empty">None.</div>', { cls: "c6" });
  html += panel("Configuration", `<dl class="kv"><dt>Mode</dt><dd>${esc(s.mode)}</dd><dt>GitHub owner</dt><dd>${esc(s.owner)}</dd><dt>Website</dt><dd>${link(s.website, s.website)}</dd>
    <dt>Workspace roots</dt><dd class="mono">${s.workspace_roots.map(esc).join("<br>")}</dd><dt>Sync</dt><dd>every ${esc(s.sync.interval_hours)} h locally · public build every ${esc(s.sync.public_interval_hours)} h</dd>
    <dt>Claude synthesis</dt><dd>${s.llm.credentials ? "credentials found" : "off (no credentials)"} · ${esc(s.llm.model)}</dd><dt>Live trading</dt><dd>disabled (no broker connector)</dd></dl>`, { cls: "c6" });
  html += panel("Sync runs", table(h.runs, [{ key: "id", label: "#", num: true }, { key: "started_at", label: "Started", render: (r) => esc(ago(r.started_at)) }, { key: "trigger", label: "Trigger" },
    { key: "status", label: "Status" }, { key: "stats", label: "Changes", render: (r) => esc(Object.entries(r.stats || {}).filter(([k]) => k.startsWith("change:")).map(([k, v]) => `${k.slice(7)} ${v}`).join(", ")) },
    { key: "secs", label: "Seconds", num: true, render: (r) => fmt((r.stats || {}).seconds, "num", 0) }]), { cls: "c12", flush: true, scroll: true });
  html += panel("Sources", table(h.sources, [{ key: "kind", label: "Kind" }, { key: "name", label: "Name" }, { key: "project", label: "Project" }, { key: "visibility", label: "Vis." },
    { key: "status", label: "Status", render: (x) => `<span class="status-dot ${x.status === "active" ? (x.recent_errors ? "sd-warn" : "sd-ok") : "sd-bad"}"></span>${esc(x.status)}` },
    { key: "last_synced", label: "Synced", render: (x) => esc(ago(x.last_synced || x.last_seen)), sort: (x) => x.age_hours }, { key: "recent_errors", label: "Errors", num: true }]), { cls: "c7", flush: true, scroll: true });
  html += panel("Recent errors", table(h.errors, [{ key: "created_at", label: "When", render: (e) => esc(ago(e.created_at)) }, { key: "source_key", label: "Source" }, { key: "stage", label: "Stage" }, { key: "message", label: "Message", render: (e) => `<span class="clip">${esc(e.message)}</span>` }], { empty: "No errors." }), { cls: "c5", flush: true, scroll: true });
  html += panel("Signal adapters", table(s.signal_sources, [{ key: "path", label: "File", render: (x) => `<span class="mono clip">${esc(x.path)}</span>` }, { key: "confidence", label: "Conf.", num: true }, { key: "rows", label: "Rows", num: true },
    { key: "status", label: "Status", render: (x) => `<select data-ss="${esc(x.key)}">${["active", "proposed", "disabled"].map((o) => `<option ${o === x.status ? "selected" : ""}>${o}</option>`).join("")}</select>` }], { empty: "None detected." }), { cls: "c6", flush: true, scroll: true });
  html += panel("Audit log", table(s.audit, [{ key: "ts", label: "When", render: (a) => esc(ago(a.ts)) }, { key: "action", label: "Action" }, { key: "target", label: "Target" }], { empty: "No consequential actions yet." }), { cls: "c6", flush: true, scroll: true });
  main.innerHTML = html + "</div>";
  const trigger = async (full) => { const r = await post("/sync", { full }); $("#sync-msg").textContent = r.started ? "Sync started in the background; refresh in a minute." : r.reason; };
  $("#sync-now").addEventListener("click", () => trigger(false));
  $("#sync-full").addEventListener("click", () => trigger(true));
  $$("[data-ss]").forEach((sel) => sel.addEventListener("change", async () => { await post(`/signal-sources/${encodeURIComponent(sel.dataset.ss)}`, { status: sel.value }); invalidate(); }));
}

// ------------------------------------------------------------------ boot
statusbar();
route();
