// Small UI helpers: escaping, formatting, markdown-lite, sortable tables, tooltips, badges.
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function fmt(v, kind = "num", d = 2) {
  if (v === null || v === undefined || v === "" || Number.isNaN(v)) return "–";
  if (kind === "pct") return `${(v * 100).toFixed(d === 2 ? 1 : d)}%`;
  if (kind === "pct100") return `${Number(v).toFixed(1)}%`;
  if (kind === "spct") return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
  if (kind === "usd") return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  if (kind === "int") return Number(v).toLocaleString();
  if (kind === "money") return Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const n = Number(v);
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}bn`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}m`;
  return n.toLocaleString(undefined, { maximumFractionDigits: d });
}

export const date = (s) => (s ? String(s).slice(0, 10) : "–");
export function ago(s) {
  if (!s) return "never";
  const t = new Date(s).getTime();
  if (Number.isNaN(t)) return s;
  const m = Math.round((Date.now() - t) / 60000);
  if (m < 2) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}

const CT = { verified_result: "verified", observed_fact: "observed", model_estimate: "model est.", research_claim: "claim" };
export const claimBadge = (ct) => (ct ? `<span class="badge ct-${esc(ct)}" title="${esc(ct.replace("_", " "))}">${esc(CT[ct] || ct)}</span>` : "");
export const stanceBadge = (s, rating) => `<span class="badge ${esc(s || "neutral")}">${esc(rating || s || "?")}</span>`;
export function link(url, text) {
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text);
}
export function srcLine(e) {
  const where = e.url ? link(e.url, e.title || e.path || "source") : esc(e.path || e.title || "");
  const line = e.line ? `:${e.line}` : "";
  return `${e.project ? `<b>${esc(e.project)}</b> · ` : ""}${where}${line}${e.date ? ` · ${esc(date(e.date))}` : ""}`;
}

// markdown-lite: **bold**, _italic_, lists, links, [n] citations
export function md(text) {
  const lines = String(text || "").split("\n");
  let html = "", inList = false;
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|\s)_(.+?)_(?=\s|$|[.,;:)])/g, "$1<i>$2</i>")
    .replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1">[$1]</span>');
  for (const raw of lines) {
    const l = raw.trimEnd();
    const m = l.match(/^\s*[-*]\s+(.*)$/);
    if (m) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(m[1])}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (l.startsWith("> ")) html += `<blockquote class="muted">${inline(l.slice(2))}</blockquote>`;
    else if (l.trim()) html += `<p>${inline(l)}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
}

// sortable table: columns [{key, label, num, render, sort}]
export function table(rows, cols, { onRow, empty = "Nothing here yet.", max = 2000, id = "" } = {}) {
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const tid = id || `t${Math.random().toString(36).slice(2, 8)}`;
  const head = cols.map((c, i) => `<th class="${c.num ? "num" : ""}" data-i="${i}">${esc(c.label)}</th>`).join("");
  const body = rows.slice(0, max).map((r, ri) => `<tr data-ri="${ri}" class="${onRow ? "click" : ""}">${cols.map((c) => {
    const v = c.render ? c.render(r) : esc(r[c.key] ?? "–");
    return `<td class="${c.num ? "num" : ""}">${v}</td>`;
  }).join("")}</tr>`).join("");
  setTimeout(() => wireTable(tid, rows, cols, onRow), 0);
  return `<table id="${tid}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>` +
    (rows.length > max ? `<div class="note">${rows.length - max} more rows not shown.</div>` : "");
}

function wireTable(tid, rows, cols, onRow) {
  const t = document.getElementById(tid);
  if (!t) return;
  let order = rows.map((_, i) => i);
  const draw = () => {
    const tb = t.tBodies[0];
    const trs = [...tb.rows];
    const byRi = new Map(trs.map((tr) => [Number(tr.dataset.ri), tr]));
    order.forEach((ri) => { const tr = byRi.get(ri); if (tr) tb.appendChild(tr); });
  };
  t.tHead.addEventListener("click", (ev) => {
    const th = ev.target.closest("th");
    if (!th) return;
    const c = cols[Number(th.dataset.i)];
    const key = c.sort || ((r) => r[c.key]);
    const dir = th.dataset.dir === "asc" ? -1 : 1;
    [...t.tHead.rows[0].cells].forEach((x) => delete x.dataset.dir);
    th.dataset.dir = dir === 1 ? "asc" : "desc";
    order.sort((a, b) => {
      const va = key(rows[a]), vb = key(rows[b]);
      if (va === vb) return 0;
      if (va === null || va === undefined || va === "") return 1;
      if (vb === null || vb === undefined || vb === "") return -1;
      return (typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb))) * dir;
    });
    draw();
  });
  if (onRow) t.tBodies[0].addEventListener("click", (ev) => {
    if (ev.target.closest("a, button")) return;
    const tr = ev.target.closest("tr");
    if (tr) onRow(rows[Number(tr.dataset.ri)]);
  });
}

const tip = () => document.getElementById("tooltip");
export function showTip(html, x, y) {
  const t = tip();
  t.innerHTML = html;
  t.classList.add("on");
  const w = t.offsetWidth, h = t.offsetHeight;
  t.style.left = `${Math.min(window.innerWidth - w - 8, x + 14)}px`;
  t.style.top = `${Math.max(8, y - h - 10)}px`;
}
export function hideTip() { tip().classList.remove("on"); }

export function panel(title, body, { tools = "", cls = "c12", flush = false, scroll = false, id = "" } = {}) {
  return `<section class="panel ${cls}" ${id ? `id="${id}"` : ""}><div class="panel-h"><h2>${esc(title)}</h2><div class="tools">${tools}</div></div>
    <div class="panel-b ${flush ? "flush" : ""} ${scroll ? "scroll" : ""}">${body}</div></section>`;
}
export const kpi = (k, v, d = "") => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${v}</div>${d ? `<div class="d">${d}</div>` : ""}</div>`;
