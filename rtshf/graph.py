"""Relationship graph between projects, companies, themes, theses, models, experiments and signals.

basis: declared (security master / front matter), extracted (stated in a document), inferred (co-occurrence or
similarity). Inferred links are what the research assistant calls speculative connections.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .db import DB
from .util import dumps, now_iso


def _rel(db: DB, src_kind, src_id, pred, dst_kind, dst_id, weight, basis, evidence=None):
    db.execute("""INSERT INTO relations(src_kind,src_id,predicate,dst_kind,dst_id,weight,basis,evidence,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(src_kind,src_id,predicate,dst_kind,dst_id) DO UPDATE SET weight=excluded.weight,
                  basis=excluded.basis, evidence=excluded.evidence, updated_at=excluded.updated_at""",
               (src_kind, src_id, pred, dst_kind, dst_id, weight, basis, dumps(evidence) if evidence else None, now_iso(), now_iso()))


def rebuild(db: DB) -> None:
    db.execute("DELETE FROM relations WHERE basis IN ('extracted','inferred','declared')")   # 'manual' (rts.yaml) is kept
    # project -> company / theme / data source (extracted from mentions)
    for r in db.all("""SELECT pe.project_id, pe.entity_id, pe.mentions, pe.documents, pe.salience, e.kind
                       FROM project_entities pe JOIN entities e ON e.id=pe.entity_id"""):
        if r["kind"] == "company" and (r["mentions"] >= 3 or r["salience"] >= 0.3):
            _rel(db, "project", r["project_id"], "covers", "company", r["entity_id"], round(r["salience"], 3), "extracted",
                 {"mentions": r["mentions"], "documents": r["documents"]})
        elif r["kind"] == "theme" and r["mentions"] >= 4:
            _rel(db, "project", r["project_id"], "researches", "theme", r["entity_id"], round(r["salience"], 3), "extracted",
                 {"mentions": r["mentions"], "documents": r["documents"]})
        elif r["kind"] == "data_source" and r["mentions"] >= 2:
            _rel(db, "project", r["project_id"], "uses_data", "data_source", r["entity_id"], round(r["salience"], 3), "extracted",
                 {"mentions": r["mentions"]})
    # company -> theme (declared in the security master)
    from .util import loads
    for s in db.all("SELECT entity_id, themes FROM securities WHERE entity_id IS NOT NULL"):
        for theme, role in (loads(s["themes"], {}) or {}).items():
            tid = db.scalar("SELECT id FROM entities WHERE key=?", (f"theme:{theme}",))
            if tid:
                _rel(db, "company", s["entity_id"], f"exposed_to:{role}", "theme", tid, 1.0, "declared")
    # thesis -> company, model -> thesis (same project + company)
    for t in db.all("SELECT id, entity_id, project_id FROM theses WHERE status='active'"):
        if t["entity_id"]:
            _rel(db, "thesis", t["id"], "about", "company", t["entity_id"], 1.0, "extracted")
        for m in db.all("SELECT id FROM financial_models WHERE project_id=? AND (entity_id=? OR entity_id IS NULL)",
                        (t["project_id"], t["entity_id"])):
            _rel(db, "model", m["id"], "supports", "thesis", t["id"], 0.8, "inferred", {"why": "same project and company"})
    # alt-data / quant project -> thesis on a covered company (evidence candidates)
    for t in db.all("SELECT id, entity_id, project_id FROM theses WHERE status='active' AND entity_id IS NOT NULL"):
        for r in db.all("""SELECT pe.project_id, pe.salience, p.category FROM project_entities pe JOIN projects p ON p.id=pe.project_id
                           WHERE pe.entity_id=? AND pe.project_id != ? AND pe.salience >= 0.25
                           AND p.relevance='investment'""", (t["entity_id"], t["project_id"] or -1)):
            _rel(db, "project", r["project_id"], "evidence_for", "thesis", t["id"], round(r["salience"], 3), "inferred",
                 {"category": r["category"]})
    # project <-> project similarity on shared companies and themes (inferred)
    vecs = defaultdict(dict)
    for r in db.all("""SELECT pe.project_id, pe.entity_id, pe.salience FROM project_entities pe JOIN entities e ON e.id=pe.entity_id
                       JOIN projects p ON p.id=pe.project_id WHERE e.kind IN ('company','theme') AND p.relevance='investment'"""):
        vecs[r["project_id"]][r["entity_id"]] = r["salience"]
    ids = sorted(vecs)
    norms = {i: math.sqrt(sum(v * v for v in vecs[i].values())) for i in ids}
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            common = set(vecs[a]) & set(vecs[b])
            if not common or not norms[a] or not norms[b]:
                continue
            sim = sum(vecs[a][k] * vecs[b][k] for k in common) / (norms[a] * norms[b])
            if sim >= 0.25:
                top = sorted(common, key=lambda k: -(vecs[a][k] * vecs[b][k]))[:6]
                names = [db.scalar("SELECT name FROM entities WHERE id=?", (k,)) for k in top]
                _rel(db, "project", a, "related_to", "project", b, round(sim, 3), "inferred", {"shared": names})
    db.commit()
