"""Plain-text summaries for the command line."""
from __future__ import annotations

from collections import Counter

from .db import DB
from .taxonomy import Taxonomy
from .util import loads


def inventory_markdown(settings) -> str:
    """The discovery inventory as markdown (private: it names private repositories and local folders)."""
    db = DB(settings.db_path)
    tax = Taxonomy(settings.taxonomy)
    run = db.one("SELECT * FROM sync_runs WHERE status IN ('ok','partial') ORDER BY id DESC LIMIT 1")
    lines = ["# Project inventory", "",
             f"Generated from sync #{run['id']} ({run['finished_at']}). Every row was found automatically." if run else "No sync yet.", ""]
    src = Counter((r["kind"], r["visibility"]) for r in db.all("SELECT kind, visibility FROM sources"))
    lines.append("Sources: " + ", ".join(f"{k} {v}: {n}" for (k, v), n in sorted(src.items())))
    rel = Counter(r["relevance"] for r in db.all("SELECT relevance FROM projects"))
    lines.append("Projects by relevance: " + ", ".join(f"{k}: {v}" for k, v in rel.most_common()))
    lines.append("")
    order = ["investment", "pending", "game_theory", "publication", "tooling", "career", "reference", "not_relevant", "unknown"]
    for relv in order:
        rows = db.all("""SELECT p.*, (SELECT COUNT(*) FROM documents d WHERE d.project_id=p.id AND d.status='active' AND d.doc_type!='result') docs,
                         (SELECT COUNT(*) FROM experiments e WHERE e.project_id=p.id AND e.status='active') exps,
                         (SELECT COUNT(*) FROM theses t WHERE t.project_id=p.id AND t.status='active') th,
                         (SELECT group_concat(kind || ':' || visibility, ' ') FROM sources s WHERE s.project_id=p.id) src
                         FROM projects p WHERE p.relevance=? ORDER BY p.category, p.slug""", (relv,))
        if not rows:
            continue
        lines += [f"## {relv.replace('_', ' ').title()} ({len(rows)})", "",
                  "| Project | Area | Topics | Sources | Docs | Experiments | Theses | Summary |", "|---|---|---|---|---|---|---|---|"]
        for p in rows:
            subs = ", ".join(s["label"] for s in (loads(p["subtopics"], []) or [])[:3])
            summ = (p["summary"] or "").replace("|", "/").replace("\n", " ")[:160]
            lines.append(f"| {p['name']} | {tax.category_label(p['category'] or '')} | {subs} | {p['src'] or ''} | {p['docs']} | {p['exps']} | {p['th']} | {summ} |")
        lines.append("")
    lines += ["## Theses (active)", "", "| Ticker | Call | Target | Price then | Project | Document |", "|---|---|---|---|---|---|"]
    for t in db.all("""SELECT t.*, p.slug, d.path FROM theses t LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN documents d ON d.id=t.document_id
                       WHERE t.status='active' ORDER BY t.symbol"""):
        lines.append(f"| {t['symbol']} | {t['rating']} | {t['target_price'] or ''} | {t['price_at_thesis'] or ''} | {t['slug']} | {t['path']} |")
    return "\n".join(lines) + "\n"


def status_text(settings) -> str:
    db = DB(settings.db_path)
    tax = Taxonomy(settings.taxonomy)
    out = []
    run = db.one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
    if run:
        out.append(f"Last sync #{run['id']} {run['started_at']} -> {run['finished_at']} [{run['status']}] {run['stats']}")
    rel = Counter(r["relevance"] for r in db.all("SELECT relevance FROM projects"))
    out.append(f"Projects: {sum(rel.values())} " + ", ".join(f"{k}={v}" for k, v in rel.most_common()))
    out.append(f"Documents: {db.scalar('SELECT COUNT(*) FROM documents WHERE status=\"active\"')}  "
               f"chunks: {db.scalar('SELECT COUNT(*) FROM chunks')}  findings: {db.scalar('SELECT COUNT(*) FROM findings WHERE status=\"active\"')}  "
               f"experiments: {db.scalar('SELECT COUNT(*) FROM experiments WHERE status=\"active\"')}  "
               f"theses: {db.scalar('SELECT COUNT(*) FROM theses WHERE status=\"active\"')}  "
               f"models: {db.scalar('SELECT COUNT(*) FROM financial_models')}  signals: {db.scalar('SELECT COUNT(*) FROM signals WHERE active=1')}")
    out.append("")
    for p in db.all("""SELECT p.*, (SELECT COUNT(*) FROM documents d WHERE d.project_id=p.id AND d.status='active') AS docs,
                       (SELECT group_concat(kind || ':' || visibility) FROM sources s WHERE s.project_id=p.id) AS src
                       FROM projects p ORDER BY p.relevance, p.category, p.slug"""):
        subs = ", ".join(s["label"] for s in (loads(p["subtopics"], []) or [])[:3])
        out.append(f"{(p['relevance'] or '?'):<12} {tax.category_label(p['category'] or '?')[:28]:<28} {p['slug']:<30} "
                   f"docs={p['docs']:<4} {p['status'] or '':<8} {p['visibility'] or '':<7} [{p['src']}] {subs}")
    return "\n".join(out)
