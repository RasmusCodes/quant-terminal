// Minimal SVG charts following the dataviz reference rules: thin marks, 2px lines, recessive grid,
// one axis, legend for >= 2 series plus direct end labels, crosshair/hover tooltips, colours by role.
import { esc, fmt, showTip, hideTip } from "./ui.js";

const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];

function ticks(min, max, n = 5) {
  const span = max - min || 1;
  const step = Math.pow(10, Math.floor(Math.log10(span / n)));
  const err = (span / n) / step;
  const s = step * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
  const out = [];
  for (let v = Math.ceil(min / s) * s; v <= max + 1e-9; v += s) out.push(+v.toFixed(10));
  return out;
}

// series: [{name, points: [{x: Date|string, y: number}]}]
export function line(el, series, { height = 260, yFmt = (v) => fmt(v, "num", 1), xFmt = (d) => d.toISOString().slice(0, 7) } = {}) {
  const W = Math.max(el.clientWidth || 600, 320), H = height, m = { l: 48, r: 92, t: 12, b: 26 };
  const S = series.filter((s) => s.points && s.points.length).map((s, i) => ({ ...s, color: s.color || SERIES[i], pts: s.points.map((p) => ({ x: new Date(p.x), y: p.y })).filter((p) => !Number.isNaN(p.x) && p.y !== null) }));
  if (!S.length) { el.innerHTML = '<div class="empty">No series to plot.</div>'; return; }
  const xs = S.flatMap((s) => s.pts.map((p) => +p.x)), ys = S.flatMap((s) => s.pts.map((p) => p.y));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.08 || 1; y0 -= pad; y1 += pad;
  const X = (v) => m.l + ((v - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b);
  const yt = ticks(y0, y1, 5);
  const xt = ticks(x0, x1, 6).map((v) => new Date(v));
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(S.map((s) => s.name).join(" vs "))}">`;
  svg += yt.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--grid)" stroke-width="1"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${esc(yFmt(v))}</text>`).join("");
  svg += xt.map((d) => `<text x="${X(+d)}" y="${H - 8}" text-anchor="middle">${esc(xFmt(d))}</text>`).join("");
  svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}" stroke="var(--axis)"/>`;
  const labels = [];
  for (const s of S) {
    const d = s.pts.map((p, i) => `${i ? "L" : "M"}${X(+p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const last = s.pts[s.pts.length - 1];
    labels.push({ y: Y(last.y), x: X(+last.x), text: `${s.name} ${yFmt(last.y)}`, color: s.color });
  }
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 13) labels[i].y = labels[i - 1].y + 13;
  svg += labels.map((l) => `<circle cx="${l.x}" cy="${l.y}" r="3" fill="${l.color}" stroke="var(--surface)" stroke-width="2"/><text class="lbl" x="${l.x + 8}" y="${l.y + 4}">${esc(l.text)}</text>`).join("");
  svg += `<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--axis)" stroke-dasharray="3 3" visibility="hidden"/>`;
  svg += `<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" class="hit"/></svg>`;
  const legend = S.length > 1 ? `<div class="legend">${S.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
  el.innerHTML = legend + `<div class="chart">${svg}</div>`;
  const root = el.querySelector("svg"), hit = root.querySelector(".hit"), xh = root.querySelector(".xh");
  hit.addEventListener("mousemove", (ev) => {
    const r = root.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    const t = x0 + ((px - m.l) / (W - m.l - m.r)) * (x1 - x0);
    const rows = S.map((s) => {
      let best = s.pts[0];
      for (const p of s.pts) if (Math.abs(+p.x - t) < Math.abs(+best.x - t)) best = p;
      return { s, p: best };
    });
    xh.setAttribute("x1", X(+rows[0].p.x)); xh.setAttribute("x2", X(+rows[0].p.x)); xh.setAttribute("visibility", "visible");
    showTip(`<b>${esc(rows[0].p.x.toISOString().slice(0, 10))}</b><br>` + rows.map(({ s, p }) => `<span style="color:${s.color}">■</span> ${esc(s.name)}: ${esc(yFmt(p.y))}`).join("<br>"), ev.clientX, ev.clientY);
  });
  hit.addEventListener("mouseleave", () => { hideTip(); xh.setAttribute("visibility", "hidden"); });
}

// horizontal bars, one hue (magnitude) or signed (diverging around zero)
export function bars(el, items, { valueFmt = (v) => fmt(v, "pct"), max = 14, signed = false, color = "var(--s1)" } = {}) {
  const rows = items.slice(0, max);
  if (!rows.length) { el.innerHTML = '<div class="empty">No data.</div>'; return; }
  const absMax = Math.max(...rows.map((r) => Math.abs(r.value)), 1e-9);
  const hasNeg = signed || rows.some((r) => r.value < 0);
  el.innerHTML = rows.map((r, i) => {
    const w = (Math.abs(r.value) / absMax) * (hasNeg ? 50 : 100);
    const left = hasNeg ? (r.value >= 0 ? 50 : 50 - w) : 0;
    return `<div class="bar-row" data-i="${i}"><div class="clip" title="${esc(r.label)}">${esc(r.label)}</div>
      <div class="track">${hasNeg ? '<span class="zero" style="left:50%"></span>' : ""}<span class="fill ${r.value < 0 ? "neg" : ""}" style="left:${left}%;width:${Math.max(w, 0.6)}%;${r.value >= 0 && !hasNeg ? `background:${color}` : ""}"></span></div>
      <div class="v">${esc(valueFmt(r.value))}</div></div>`;
  }).join("") + (items.length > max ? `<div class="note">${items.length - max} smaller items not shown.</div>` : "");
  el.querySelectorAll(".bar-row").forEach((row) => {
    row.addEventListener("mousemove", (ev) => { const r = rows[Number(row.dataset.i)]; showTip(`${esc(r.label)}: <b>${esc(valueFmt(r.value))}</b>${r.tip ? "<br>" + esc(r.tip) : ""}`, ev.clientX, ev.clientY); });
    row.addEventListener("mouseleave", hideTip);
  });
}

// correlation heatmap: diverging red (-1) - gray (0) - blue (+1)
export function heat(el, labels, matrix) {
  if (!labels || !labels.length) { el.innerHTML = '<div class="empty">No data.</div>'; return; }
  const cell = (v) => {
    const a = Math.min(Math.abs(v), 1);
    const c = v >= 0 ? "var(--div-pos)" : "var(--div-neg)";
    return `background: color-mix(in oklab, ${c} ${Math.round(a * 85)}%, var(--div-mid));color:${a > 0.55 ? "#fff" : "var(--ink)"}`;
  };
  el.innerHTML = `<table class="heat"><thead><tr><th></th>${labels.map((l) => `<th class="num">${esc(l)}</th>`).join("")}</tr></thead><tbody>` +
    matrix.map((row, i) => `<tr><th>${esc(labels[i])}</th>${row.map((v, j) => `<td style="${cell(v)}" title="${esc(labels[i])} / ${esc(labels[j])}: ${v.toFixed(2)}">${v.toFixed(2)}</td>`).join("")}</tr>`).join("") +
    `</tbody></table><div class="note">Blue = positive correlation, red = negative, gray = none.</div>`;
}

export function spark(values, { w = 120, h = 28 } = {}) {
  const v = values.filter((x) => x !== null && x !== undefined);
  if (v.length < 2) return "";
  const mn = Math.min(...v), mx = Math.max(...v);
  const pts = v.map((y, i) => `${(i / (v.length - 1)) * w},${h - 2 - ((y - mn) / (mx - mn || 1)) * (h - 4)}`).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--s1)" stroke-width="1.5"/></svg>`;
}
