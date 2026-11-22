"""Research assistant: after every sync, look at what actually changed and point out connections, affected theses,
stale numbers, decaying strategies and gaps.

Each alert has a basis:
  actual_update            something verifiably changed (a file, a model output, a metric, a new repository)
  speculative_connection   a link inferred from shared companies/themes/terms; worth a look, not a fact
  gap                      something missing (a holding with no research, a thesis with no model, ...)
  system                   integration health
The assistant only writes alerts. It never edits a thesis, a model or a document.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from .db import DB
from .util import dumps, loads, now_iso, sha1

DECAY_TERMS = re.compile(r"(decay\w*|flat since|stopped working|no longer (?:works|earns|pays)|lost [\d.]+%|post-publication|"
                         r"regime (?:change|shift)|edge (?:has )?(?:faded|disappeared|gone)|~?0 (?:net|alpha)|earns nothing|"
                         r"did not survive|fails out-of-sample|failed out-of-sample)", re.I)


def _alert(db: DB, run_id: int, key: str, kind: str, basis: str, severity: str, title: str, detail: str,
           evidence: list | None = None, subjects: dict | None = None) -> bool:
    prev = db.one("SELECT id, status FROM alerts WHERE dedupe_key=?", (key,))
    row = {"dedupe_key": key, "kind": kind, "basis": basis, "severity": severity, "title": title[:300], "detail": detail[:4000],
           "evidence": dumps(evidence or []), "subjects": dumps(subjects or {}), "updated_at": now_iso(), "run_id": run_id}
    if prev:
        db.update("alerts", prev["id"], row)        # keep the user's status (seen/dismissed/resolved)
        return False
    row.update(created_at=now_iso(), status="new")
    db.insert("alerts", row)
    return True


def run(ctx) -> dict:
    db: DB = ctx.db
    rid = ctx.run_id
    counts = defaultdict(int)
    changes = db.all("SELECT * FROM changes WHERE run_id=?", (rid,))
    by_project = defaultdict(list)
    for c in changes:
        if c["project_id"]:
            by_project[c["project_id"]].append(c)
    first_run = db.scalar("SELECT COUNT(*) FROM sync_runs WHERE status IN ('ok','partial') AND id < ?", (rid,), 0) == 0
    proj = {r["id"]: r for r in db.all("SELECT * FROM projects")}

    # 1. new repositories / folders -------------------------------------------------------------
    for c in changes:
        if c["kind"] != "new_source" or first_run:
            continue
        src = db.one("SELECT * FROM sources WHERE key=?", (c["source_key"],))
        if not src or not src["project_id"]:
            continue
        p = proj.get(src["project_id"])
        related = _related(db, src["project_id"])
        detail = (f"{src['kind'].title()} source {src['name']} appeared and was indexed automatically "
                  f"(category: {p['category'] if p else '?'}, visibility: {src['visibility']}).")
        if related:
            detail += " Possibly related (shared companies/themes, speculative): " + "; ".join(
                f"{r['slug']} ({', '.join(r['shared'][:4])})" for r in related[:5])
        counts["new"] += _alert(db, rid, f"new:{c['source_key']}", "new_project", "actual_update", "notice",
                                f"New project discovered: {src['name']}", detail,
                                [{"kind": "source", "key": c["source_key"], "url": src["url"]}] + [{"kind": "project", "slug": r["slug"]} for r in related[:5]],
                                {"project_id": src["project_id"]})

    # 2. content updates -> connections and thesis impact ------------------------------------------
    for pid, chs in by_project.items():
        p = proj.get(pid)
        if not p or p["relevance"] not in ("investment",) or first_run:
            continue
        doc_changes = [c for c in chs if c["kind"] in ("new_doc", "changed_doc", "new_page", "changed_page", "changed_result",
                                                       "model_outputs_changed", "new_commits", "updated")]
        if not doc_changes:
            continue
        paths = sorted({c["path"] for c in doc_changes if c["path"]})[:12]
        companies = _changed_companies(db, pid, paths)
        # thesis impact: theses on these companies in this or other projects
        for t in db.all(f"""SELECT t.*, p.slug, d.title AS doc_title, d.url, d.path FROM theses t
                            LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN documents d ON d.id=t.document_id
                            WHERE t.status='active' AND t.symbol IN ({','.join('?' * len(companies)) or "''"})""", list(companies)):
            terms = _terms_of_changes(db, pid, paths)
            affected = _affected_items(t, terms)
            same = t["project_id"] == pid
            title = (f"{p['name']} changed: {t['symbol']} thesis ({t['rating']}"
                     + (f", target ${t['target_price']:g}" if t["target_price"] else "") + f") may be affected")
            detail = (f"{len(doc_changes)} change(s) in {p['name']} touch {t['symbol']} "
                      f"({', '.join(paths[:5])}{'…' if len(paths) > 5 else ''}). "
                      + ("The thesis lives in the same project." if same else f"The thesis is in {t['slug']}.")
                      + (" Assumptions/arguments sharing terms with the changed content (speculative mapping): "
                         + " | ".join(a[:160] for a in affected[:4]) if affected else " No thesis assumption shares terms with the change."))
            counts["impact"] += _alert(db, rid, f"impact:{pid}:{t['id']}:{sha1(*paths)[:10]}", "impact", "actual_update",
                                       "warning" if affected else "notice", title, detail,
                                       [{"kind": "change", "project": p["slug"], "path": x} for x in paths[:8]] +
                                       [{"kind": "thesis", "id": t["id"], "url": t["url"], "path": t["path"]}],
                                       {"project_id": pid, "thesis_id": t["id"], "symbol": t["symbol"]})
        # speculative connections to other projects on the same companies
        for sym in list(companies)[:6]:
            others = db.all("""SELECT p.id, p.slug, p.name, p.category, pe.salience FROM project_entities pe
                               JOIN projects p ON p.id=pe.project_id JOIN entities e ON e.id=pe.entity_id
                               WHERE e.key=? AND pe.project_id != ? AND pe.salience >= 0.2 AND p.relevance='investment'
                               ORDER BY pe.salience DESC LIMIT 4""", (f"company:{sym}", pid))
            for o in others:
                counts["connection"] += _alert(
                    db, rid, f"conn:{pid}:{o['id']}:{sym}:{dt.date.today().isoformat()[:7]}", "connection",
                    "speculative_connection", "info",
                    f"{p['name']} and {o['name']} both cover {sym}",
                    f"New or changed material in {p['name']} mentions {sym}; {o['name']} ({o['category']}) also covers it "
                    f"(salience {o['salience']:.2f}). Its data may inform this work, or the reverse. Inferred from shared "
                    f"company mentions, not from a stated link.",
                    [{"kind": "project", "slug": p["slug"]}, {"kind": "project", "slug": o["slug"]}],
                    {"project_id": pid, "other_project_id": o["id"], "symbol": sym})

    # 3. model outputs changed -> stale published numbers ------------------------------------------
    for c in changes:
        if c["kind"] != "model_outputs_changed":
            continue
        d = loads(c["detail"], {}) or {}
        before = {o["key"]: o["value"] for o in d.get("before") or []}
        after = {o["key"]: o["value"] for o in d.get("after") or []}
        moved = {k: (before.get(k), v) for k, v in after.items() if before.get(k) is not None and v is not None
                 and abs(v - before[k]) > 1e-9}
        if not moved:
            continue
        fm = db.one("SELECT * FROM financial_models WHERE project_id=? AND path=?", (c["project_id"], c["path"]))
        detail = "Changed outputs: " + "; ".join(f"{k}: {a:g} -> {b:g}" for k, (a, b) in list(moved.items())[:8])
        theses = db.all("SELECT t.*, d.title AS doc_title, d.url FROM theses t LEFT JOIN documents d ON d.id=t.document_id "
                        "WHERE t.status='active' AND (t.project_id=? OR (t.symbol IS NOT NULL AND t.symbol=?))",
                        (c["project_id"], fm["symbol"] if fm else None))
        new_val = after.get("price_target") or after.get("value_per_share")
        for t in theses:
            if new_val and t["target_price"] and abs(new_val / t["target_price"] - 1) > 0.02:
                detail += (f". Thesis '{t['doc_title']}' states ${t['target_price']:g}; the model now gives ${new_val:g} "
                           f"({(new_val / t['target_price'] - 1) * 100:+.1f}%). The published memo may be outdated.")
        counts["model"] += _alert(db, rid, f"model:{c['project_id']}:{c['path']}:{sha1(dumps(after))[:10]}", "stale_thesis" if theses else "model_change",
                                  "actual_update", "warning" if theses else "notice",
                                  f"Model outputs changed: {c['path'].rsplit('/', 1)[-1]}", detail,
                                  [{"kind": "model", "path": c["path"], "project_id": c["project_id"]}] +
                                  [{"kind": "thesis", "id": t["id"], "url": t["url"]} for t in theses],
                                  {"project_id": c["project_id"]})

    # 4. thesis versions ---------------------------------------------------------------------------
    for c in changes:
        if c["kind"] != "thesis_version" or first_run:
            continue
        d = loads(c["detail"], {}) or {}
        if not d.get("previous"):
            continue
        old = db.one("SELECT rating, target_price FROM theses WHERE id=?", (d["previous"][0],))
        counts["thesis"] += _alert(db, rid, f"thesisv:{c['path']}:{d.get('target')}:{d.get('stance')}", "thesis_changed", "actual_update",
                                   "notice", f"{d.get('symbol')} thesis document changed ({c['path'].rsplit('/', 1)[-1]})",
                                   f"Previous: {old['rating'] if old else '?'} {('$%g' % old['target_price']) if old and old['target_price'] else ''}; "
                                   f"now: {d.get('stance')} {('$%g' % d['target']) if d.get('target') else ''}. Recorded as a new version; "
                                   "the old one is kept as superseded.",
                                   [{"kind": "document", "path": c["path"]}], {"symbol": d.get("symbol")})

    # 5. strategy decay: metrics that fell, and decay statements in research -----------------------
    for c in changes:
        if c["kind"] != "experiment_metrics_changed":
            continue
        d = loads(c["detail"], {}) or {}
        b, a = d.get("before") or {}, d.get("after") or {}
        drops = []
        for m in ("sharpe", "alpha", "ic", "t_stat", "ann_return", "hit_rate"):
            if m in b and m in a and b[m] is not None and a[m] is not None and a[m] < b[m] - max(0.3 * abs(b[m]), 0.1):
                drops.append(f"{m} {b[m]:g} -> {a[m]:g}")
        if not drops:
            continue
        related = _related(db, c["project_id"])
        counts["decay"] += _alert(db, rid, f"decay:{c['project_id']}:{c['path']}:{sha1(dumps(a))[:10]}", "strategy_decay", "actual_update",
                                  "warning", f"Weaker results: {d.get('name', c['path'])[:120]}",
                                  "Metrics fell between syncs: " + ", ".join(drops) + ". Projects that share companies/themes "
                                  "(possibly affected, speculative): " + (", ".join(r["slug"] for r in related[:6]) or "none found"),
                                  [{"kind": "experiment", "path": c["path"]}] + [{"kind": "project", "slug": r["slug"]} for r in related[:6]],
                                  {"project_id": c["project_id"]})
    run_start = db.scalar("SELECT started_at FROM sync_runs WHERE id=?", (rid,))
    for f in db.all("""SELECT f.id, f.text, f.project_id, f.document_id, f.first_seen, f.line, p.slug, p.name, d.path, d.url
                       FROM findings f JOIN projects p ON p.id=f.project_id JOIN documents d ON d.id=f.document_id
                       WHERE f.status='active' AND p.relevance='investment' AND p.category IN ('quant_trading','derivatives','alt_data')
                       AND f.polarity < 0""" ):
        if not DECAY_TERMS.search(f["text"]):
            continue
        is_new = (f["first_seen"] or "") >= (run_start or "")
        if not is_new and not first_run:
            continue
        related = _related(db, f["project_id"])
        basis = "baseline" if first_run else "actual_update"
        counts["decay_text"] += _alert(db, rid, f"decaytext:{f['id']}", "strategy_decay", basis, "info" if first_run else "notice",
                                       f"Decay/regime evidence in {f['name']}" + (" (existing research)" if first_run else " (new)"),
                                       f"\"{f['text'][:400]}\" — related projects that may share the exposure (speculative): "
                                       + (", ".join(r["slug"] for r in related[:5]) or "none found"),
                                       [{"kind": "finding", "id": f["id"], "path": f["path"], "url": f["url"], "line": f["line"]}],
                                       {"project_id": f["project_id"]})

    # 6. gaps --------------------------------------------------------------------------------------
    counts["gaps"] += _gaps(db, rid)
    # 7. integration health ------------------------------------------------------------------------
    counts["system"] += _system(db, rid, ctx.settings)
    db.commit()
    ctx.stats["alerts_new"] = sum(counts.values())
    return dict(counts)


# ----------------------------------------------------------------------------------------- helpers
def _related(db: DB, pid: int) -> list[dict]:
    out = []
    for r in db.all("""SELECT CASE WHEN src_id=? THEN dst_id ELSE src_id END AS other, weight, evidence FROM relations
                       WHERE predicate='related_to' AND (src_id=? OR dst_id=?) ORDER BY weight DESC LIMIT 8""", (pid, pid, pid)):
        p = db.one("SELECT slug, name FROM projects WHERE id=?", (r["other"],))
        if p:
            out.append({"slug": p["slug"], "name": p["name"], "weight": r["weight"], "shared": (loads(r["evidence"], {}) or {}).get("shared", [])})
    return out


def _changed_companies(db: DB, pid: int, paths: list[str]) -> set[str]:
    syms = set()
    for path in paths:
        for r in db.all("""SELECT e.symbol, SUM(m.count) n FROM mentions m JOIN entities e ON e.id=m.entity_id JOIN documents d ON d.id=m.document_id
                           WHERE d.project_id=? AND d.path=? AND e.kind='company' GROUP BY e.id HAVING n >= 2""", (pid, path)):
            syms.add(r["symbol"])
    if not syms:   # result files carry no text: fall back to the project's main companies
        for r in db.all("""SELECT e.symbol FROM project_entities pe JOIN entities e ON e.id=pe.entity_id
                           WHERE pe.project_id=? AND e.kind='company' AND pe.salience >= 0.5""", (pid,)):
            syms.add(r["symbol"])
    return {s for s in syms if s}


def _terms_of_changes(db: DB, pid: int, paths: list[str]) -> set[str]:
    words = set()
    for path in paths[:8]:
        for r in db.all("SELECT c.heading FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.project_id=? AND d.path=? LIMIT 60",
                        (pid, path)):
            words |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", r["heading"] or "")}
        words |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", path)}
    return words - {"report", "notes", "data", "table", "summary", "results", "readme", "html", "csv", "json", "xlsx", "with", "from", "this"}


def _affected_items(thesis, terms: set[str]) -> list[str]:
    items = (loads(thesis["assumptions"], []) or []) + (loads(thesis["arguments"], []) or []) + (loads(thesis["risks"], []) or [])
    scored = []
    for it in items:
        toks = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", it)}
        n = len(toks & terms)
        if n >= 2:
            scored.append((n, it))
    return [it for _, it in sorted(scored, key=lambda x: -x[0])]


def _gaps(db: DB, rid: int) -> int:
    n = 0
    # "covered" = a main subject somewhere (not a passing mention in someone else's research), or a thesis/model on it
    covered = {r[0] for r in db.all("""SELECT e.symbol FROM project_entities pe JOIN entities e ON e.id=pe.entity_id
                                       JOIN projects p ON p.id=pe.project_id WHERE e.kind='company' AND pe.salience >= 0.5
                                       AND pe.documents >= 2 AND p.relevance='investment'""")}
    thesis_syms = {r[0] for r in db.all("SELECT symbol FROM theses WHERE status='active'")}
    thesis_syms |= {r[0] for r in db.all("SELECT symbol FROM financial_models WHERE symbol IS NOT NULL")}
    for p in db.all("SELECT * FROM portfolios"):
        hs = (loads(p["meta"], {}) or {}).get("holdings") or []
        if not hs:
            hs = [{"symbol": r[0], "weight": None} for r in db.all("SELECT DISTINCT symbol FROM transactions WHERE portfolio_id=? AND symbol IS NOT NULL", (p["id"],))]
        for h in hs:
            sym = h["symbol"]
            if sym in covered or sym in thesis_syms:
                continue
            w = h.get("weight")
            n += _alert(db, rid, f"gap:holding:{p['key']}:{sym}", "gap", "gap", "notice" if (w or 0) >= 5 else "info",
                        f"No research found on {sym} ({p['name']})",
                        f"{sym} is held in {p['name']}" + (f" at {w:.1f}% weight" if w else "") + " but no project or thesis covers it.",
                        [{"kind": "portfolio", "key": p["key"]}], {"symbol": sym, "portfolio": p["key"]})
    for t in db.all("""SELECT t.symbol, MIN(t.id) id, p.name FROM theses t JOIN projects p ON p.id=t.project_id
                       WHERE t.status='active' AND t.target_price IS NOT NULL AND NOT EXISTS
                       (SELECT 1 FROM financial_models m WHERE m.symbol=t.symbol) GROUP BY t.symbol, p.name"""):
        n += _alert(db, rid, f"gap:nomodel:{t['symbol']}:{t['name']}", "gap", "gap", "info", f"{t['symbol']} call has a target but no model file",
                    f"{t['name']} states a price target for {t['symbol']}, but no spreadsheet model for {t['symbol']} was found in any "
                    "project (it may live outside the workspace).", [{"kind": "thesis", "id": t["id"]}], {"thesis_id": t["id"], "symbol": t["symbol"]})
    for t in db.all("SELECT t.*, d.title FROM theses t LEFT JOIN documents d ON d.id=t.document_id WHERE t.status='active' AND t.price_at_thesis IS NOT NULL"):
        px = db.one("SELECT date, close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (t["symbol"],))
        if px and px["date"] > (t["thesis_date"] or "") and abs(px["close"] / t["price_at_thesis"] - 1) > 0.15:
            move = px["close"] / t["price_at_thesis"] - 1
            n += _alert(db, rid, f"gap:moved:{t['id']}:{px['date'][:7]}", "stale_thesis", "actual_update", "notice",
                        f"{t['symbol']} moved {move * 100:+.0f}% since the thesis",
                        f"'{t['title']}' used ${t['price_at_thesis']:g} on {t['thesis_date']}; last close ${px['close']:g} on {px['date']}. "
                        "The upside and the call may need a refresh.", [{"kind": "thesis", "id": t["id"]}], {"thesis_id": t["id"]})
    return n


def _system(db: DB, rid: int, settings) -> int:
    n = 0
    recent = [r[0] for r in db.all("SELECT id FROM sync_runs ORDER BY id DESC LIMIT 3")]
    if recent:
        for r in db.all(f"""SELECT source_key, COUNT(DISTINCT run_id) runs, MAX(message) msg FROM sync_errors
                            WHERE run_id IN ({','.join('?' * len(recent))}) GROUP BY source_key HAVING runs >= 2""", recent):
            n += _alert(db, rid, f"sys:err:{r['source_key']}", "integration", "system", "warning",
                        f"Repeated sync errors: {r['source_key']}", f"Failed in {r['runs']} of the last {len(recent)} runs. Last error: {r['msg']}",
                        [], {"source": r["source_key"]})
    for s in db.all("SELECT * FROM signal_sources WHERE status='proposed'"):
        n += _alert(db, rid, f"sys:signal:{s['key']}", "integration", "system", "info", f"Signal table found, not activated: {s['path']}",
                    f"A ticker + score table was detected (confidence {s['confidence']}); it is kept as a proposal because the "
                    "mapping is uncertain. Activate it in Settings if it is a real signal.", [], {"signal_source": s["key"]})
    return n
