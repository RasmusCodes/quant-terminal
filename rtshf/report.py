"""Plain-text summaries for the command line."""
from __future__ import annotations

from collections import Counter

from .db import DB
from .taxonomy import Taxonomy
from .util import loads


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
