"""JSON views shared by the API server and the static (public) export, so both show the same thing."""
from __future__ import annotations

from collections import Counter, defaultdict

from .db import DB, rows_to_dicts
from .taxonomy import Taxonomy
from .util import loads, truncate

PJSON = ["secondary", "scores", "subtopics", "themes", "evidence", "technologies", "data_sources", "capabilities", "override"]
TAB_CATEGORIES = {"research": ["equity_research"], "quant": ["quant_trading", "game_theory"], "altdata": ["alt_data"],
                  "markets": ["derivatives"], "portfolio": ["portfolio"]}


class Views:
    def __init__(self, db: DB, settings):
        self.db = db
        self.settings = settings
        self.tax = Taxonomy(settings.taxonomy)
        self.public = settings.is_public

    def _vis(self, alias: str = "d") -> str:
        return f" AND {alias}.visibility='public'" if self.public else ""

    # ------------------------------------------------------------------ projects
    def projects(self) -> list[dict]:
        rows = self.db.all(f"""SELECT p.*, COALESCE(dc.n,0) AS documents, COALESCE(ec.n,0) AS experiments, COALESCE(tc.n,0) AS theses,
               COALESCE(fc.n,0) AS findings, sc.src AS src
               FROM projects p
               LEFT JOIN (SELECT d.project_id, COUNT(*) n FROM documents d WHERE d.status='active'{self._vis()} GROUP BY d.project_id) dc ON dc.project_id=p.id
               LEFT JOIN (SELECT project_id, COUNT(*) n FROM experiments WHERE status='active' GROUP BY project_id) ec ON ec.project_id=p.id
               LEFT JOIN (SELECT project_id, COUNT(*) n FROM theses WHERE status='active' GROUP BY project_id) tc ON tc.project_id=p.id
               LEFT JOIN (SELECT project_id, COUNT(*) n FROM findings WHERE status='active' GROUP BY project_id) fc ON fc.project_id=p.id
               LEFT JOIN (SELECT project_id, group_concat(kind || '|' || visibility || '|' || COALESCE(url,'') || '|' || status, ';;') src
                          FROM sources GROUP BY project_id) sc ON sc.project_id=p.id
               {'WHERE p.visibility=\'public\'' if self.public else ''} ORDER BY p.last_activity DESC""")
        out = []
        for r in rows_to_dicts(rows, PJSON):
            r["sources"] = [dict(zip(("kind", "visibility", "url", "status"), s.split("|"))) for s in (r.pop("src") or "").split(";;") if s]
            if self.public:
                r["sources"] = [s for s in r["sources"] if s["visibility"] == "public"]
            r["category_label"] = self.tax.category_label(r["category"] or "")
            ev = r.get("evidence") or {}
            r["flags"] = ev.get("flags", []) if isinstance(ev, dict) else []
            r["top_terms"] = (ev.get("terms", {}) or {}).get(r["category"], [])[:8] if isinstance(ev, dict) else []
            r.pop("evidence", None)
            r.pop("override", None)
            out.append(r)
        return out

    def project(self, slug: str) -> dict | None:
        p = self.db.one("SELECT * FROM projects WHERE slug=?", (slug,))
        if not p or (self.public and p["visibility"] != "public"):
            return None
        d = rows_to_dicts([p], PJSON)[0]
        d["category_label"] = self.tax.category_label(d["category"] or "")
        d["sources"] = rows_to_dicts(self.db.all(
            "SELECT kind, key, name, url, visibility, status, pushed_at, last_synced, last_changed, file_count, description FROM sources WHERE project_id=?"
            + (" AND visibility='public'" if self.public else ""), (p["id"],)))
        if not self.public:
            for s in d["sources"]:
                pass
        d["documents"] = rows_to_dicts(self.db.all(
            f"SELECT id, title, doc_type, path, url, date, visibility, superseded FROM documents d WHERE project_id=? AND status='active' "
            f"AND doc_type != 'result'{self._vis()} ORDER BY CASE doc_type WHEN 'published_research' THEN 0 WHEN 'memo' THEN 1 WHEN 'report' THEN 2 "
            f"WHEN 'readme' THEN 3 ELSE 4 END, date DESC LIMIT 200", (p["id"],)))
        d["findings"] = self.findings(project_id=p["id"], limit=40)
        d["experiments"] = self.experiments(project_id=p["id"], limit=200)
        d["theses"] = self.theses(project_id=p["id"])
        d["models"] = rows_to_dicts(self.db.all("SELECT id, name, path, model_type, symbol, outputs, modified FROM financial_models WHERE project_id=?",
                                                (p["id"],)), ["outputs"])
        d["strategies"] = rows_to_dicts(self.db.all("SELECT key, name, family, description, costs_modeled, first_date, last_date FROM strategies WHERE project_id=?", (p["id"],)))
        d["ml_models"] = rows_to_dicts(self.db.all("SELECT name, kind, framework, artifacts, modified FROM models WHERE project_id=? ORDER BY artifacts DESC",
                                                   (p["id"],)))
        d["companies"] = rows_to_dicts(self.db.all("""SELECT e.symbol, e.name, pe.mentions, pe.documents, round(pe.salience,3) AS salience
                                                     FROM project_entities pe JOIN entities e ON e.id=pe.entity_id WHERE pe.project_id=? AND e.kind='company'
                                                     ORDER BY pe.salience DESC LIMIT 25""", (p["id"],)))
        d["related"] = rows_to_dicts(self.db.all("""SELECT CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END AS pid, r.weight, r.evidence
                                                   FROM relations r WHERE r.predicate='related_to' AND (r.src_id=? OR r.dst_id=?) ORDER BY r.weight DESC LIMIT 10""",
                                                (p["id"], p["id"], p["id"])), ["evidence"])
        for r in d["related"]:
            o = self.db.one("SELECT slug, name, visibility FROM projects WHERE id=?", (r["pid"],))
            r["slug"], r["name"] = (o["slug"], o["name"]) if o else (None, None)
            if self.public and o and o["visibility"] != "public":
                r["slug"] = None
        d["related"] = [r for r in d["related"] if r["slug"]]
        d["signals"] = rows_to_dicts(self.db.all("SELECT * FROM signals WHERE project_id=? AND active=1 LIMIT 100", (p["id"],)), ["provenance", "meta"])
        d["alerts"] = self.alerts(project_id=p["id"], limit=20)
        ev = d.pop("evidence", {}) or {}
        d["classification"] = {"scores": d.get("scores"), "terms": ev.get("terms", {}) if isinstance(ev, dict) else {},
                               "flags": ev.get("flags", []) if isinstance(ev, dict) else [], "imports": ev.get("imports", {}) if isinstance(ev, dict) else {}}
        return d

    # ------------------------------------------------------------------ research objects
    def findings(self, project_id=None, symbol=None, claim_type=None, limit=50) -> list[dict]:
        q = f"""SELECT f.id, f.text, f.claim_type, f.evidence_kind, f.section, f.polarity, f.entities, f.themes, f.line, f.importance,
                f.first_seen, p.slug AS project, d.title, d.url, d.path, d.doc_type, d.date FROM findings f
                JOIN documents d ON d.id=f.document_id LEFT JOIN projects p ON p.id=f.project_id
                WHERE f.status='active' AND d.status='active'{self._vis()}"""
        args = []
        if project_id:
            q += " AND f.project_id=?"
            args.append(project_id)
        if symbol:
            q += " AND f.entities LIKE ?"
            args.append(f'%"{symbol}"%')
        if claim_type:
            q += " AND f.claim_type=?"
            args.append(claim_type)
        q += " ORDER BY f.importance DESC LIMIT ?"
        args.append(limit)
        return rows_to_dicts(self.db.all(q, args), ["entities", "themes"])

    def theses(self, project_id=None, symbol=None, include_superseded=False) -> list[dict]:
        q = f"""SELECT t.*, p.slug AS project, p.name AS project_name, d.title, d.url, d.path, d.doc_type, d.visibility FROM theses t
                LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN documents d ON d.id=t.document_id WHERE 1=1{self._vis()}"""
        args = []
        if not include_superseded:
            q += " AND t.status='active'"
        if project_id:
            q += " AND t.project_id=?"
            args.append(project_id)
        if symbol:
            q += " AND t.symbol=?"
            args.append(symbol)
        q += " ORDER BY t.thesis_date DESC"
        rows = rows_to_dicts(self.db.all(q, args), ["scenarios", "arguments", "risks", "catalysts", "assumptions", "meta"])
        # the same call often appears in several documents (memo, README, published note): pick one primary per call
        pref = {"published_research": 0, "memo": 1, "readme": 2, "report": 3, "deck": 4}
        groups: dict = {}
        for r in rows:
            groups.setdefault((r["symbol"], r["project"], r["stance"], r["status"]), []).append(r)
        for items in groups.values():
            items.sort(key=lambda r: (0 if r.get("method") == "structured" else 1,
                                      -((1 if r.get("target_price") else 0) + (1 if r.get("price_at_thesis") else 0)),
                                      pref.get(r.get("doc_type"), 9)))
            for i, r in enumerate(items):
                r["primary"] = i == 0
                r["n_sources"] = len(items)
        return rows

    def experiments(self, project_id=None, oos=None, limit=500) -> list[dict]:
        q = """SELECT e.id, e.name, e.hypothesis, e.result_kind, e.oos, e.costs, e.verdict, e.metrics, e.flags, e.periods, e.source_kind,
               e.location, e.line, e.run_date, e.limitations, p.slug AS project, p.name AS project_name, p.category, d.url, d.path, d.visibility
               FROM experiments e LEFT JOIN projects p ON p.id=e.project_id LEFT JOIN documents d ON d.id=e.document_id
               WHERE e.status='active'""" + (" AND (d.visibility='public' OR d.visibility IS NULL)" if self.public else "")
        args = []
        if project_id:
            q += " AND e.project_id=?"
            args.append(project_id)
        if oos is not None:
            q += " AND e.oos=?"
            args.append(int(oos))
        q += " ORDER BY p.slug, e.id LIMIT ?"
        args.append(limit)
        return rows_to_dicts(self.db.all(q, args), ["metrics", "flags", "periods"])

    def companies(self) -> list[dict]:
        rows = self.db.all("""SELECT s.symbol, s.name, s.sector, s.industry, s.country, s.currency, s.exchange, s.asset_class, s.themes, s.origin,
                              (SELECT COUNT(DISTINCT pe.project_id) FROM project_entities pe WHERE pe.entity_id=s.entity_id AND pe.mentions >= 2) AS projects,
                              (SELECT SUM(pe.mentions) FROM project_entities pe WHERE pe.entity_id=s.entity_id) AS mentions,
                              (SELECT COUNT(*) FROM theses t WHERE t.symbol=s.symbol AND t.status='active') AS theses,
                              (SELECT COUNT(*) FROM financial_models m WHERE m.symbol=s.symbol) AS models,
                              (SELECT COUNT(*) FROM signals g WHERE g.instrument=s.symbol AND g.active=1) AS signals
                              FROM securities s ORDER BY mentions DESC""")
        out = rows_to_dicts(rows, ["themes"])
        return [c for c in out if (c["mentions"] or 0) > 0 or c["theses"] or c["signals"] or c["origin"] in ("seed", "portfolio")]

    def company(self, symbol: str) -> dict | None:
        s = self.db.one("SELECT * FROM securities WHERE symbol=?", (symbol,))
        if not s:
            return None
        d = rows_to_dicts([s], ["themes"])[0]
        d["projects"] = rows_to_dicts(self.db.all(f"""SELECT p.slug, p.name, p.category, pe.mentions, pe.documents, round(pe.salience,3) salience
                                                    FROM project_entities pe JOIN projects p ON p.id=pe.project_id WHERE pe.entity_id=?
                                                    {"AND p.visibility='public'" if self.public else ""} ORDER BY pe.salience DESC""", (s["entity_id"],)))
        d["theses"] = self.theses(symbol=symbol, include_superseded=True)
        d["findings"] = self.findings(symbol=symbol, limit=40)
        d["models"] = rows_to_dicts(self.db.all("SELECT m.id, m.name, m.path, m.model_type, m.outputs, m.modified, p.slug AS project FROM financial_models m "
                                                "LEFT JOIN projects p ON p.id=m.project_id WHERE m.symbol=?", (symbol,)), ["outputs"])
        d["signals"] = rows_to_dicts(self.db.all("SELECT * FROM signals WHERE instrument=? AND active=1", (symbol,)), ["provenance", "meta"])
        d["documents"] = rows_to_dicts(self.db.all(f"""SELECT d.id, d.title, d.doc_type, d.path, d.url, d.date, p.slug AS project, SUM(m.count) AS mentions
                                                     FROM mentions m JOIN documents d ON d.id=m.document_id LEFT JOIN projects p ON p.id=d.project_id
                                                     WHERE m.entity_id=? AND d.status='active'{self._vis()} GROUP BY d.id ORDER BY mentions DESC LIMIT 30""",
                                                  (s["entity_id"],)))
        if not self.public:
            from .portfolio import prices as PX
            lp = PX.last_price(self.db, symbol)
            d["last_price"] = {"date": lp[0], "close": lp[1]} if lp else None
        return d

    def models(self) -> dict:
        fin = rows_to_dicts(self.db.all("""SELECT m.id, m.name, m.path, m.model_type, m.symbol, m.outputs, m.assumptions, m.modified, m.previous_outputs,
                                           p.slug AS project, d.url, d.visibility FROM financial_models m LEFT JOIN projects p ON p.id=m.project_id
                                           LEFT JOIN documents d ON d.id=m.document_id""" + (" WHERE d.visibility='public'" if self.public else "")),
                            ["outputs", "assumptions", "previous_outputs"])
        ml = rows_to_dicts(self.db.all("""SELECT m.key, m.kind, m.name, m.framework, m.artifacts, m.modified, m.meta, p.slug AS project, p.visibility
                                          FROM models m LEFT JOIN projects p ON p.id=m.project_id ORDER BY m.modified DESC"""), ["meta"])
        if self.public:
            ml = [m for m in ml if m.get("visibility") == "public"]
        return {"financial": fin, "ml": ml,
                "strategies": rows_to_dicts(self.db.all("SELECT s.*, p.slug AS project FROM strategies s LEFT JOIN projects p ON p.id=s.project_id"))}

    def signals(self) -> dict:
        from . import signals as SG
        return {"registry": rows_to_dicts(self.db.all("""SELECT s.*, p.slug AS project FROM signals s LEFT JOIN projects p ON p.id=s.project_id
                                                        WHERE s.active=1 ORDER BY s.instrument"""), ["provenance", "meta"]),
                "aggregate": SG.aggregate(self.db),
                "sources": rows_to_dicts(self.db.all("SELECT ss.*, p.slug AS project FROM signal_sources ss LEFT JOIN projects p ON p.id=ss.project_id"), ["mapping"])}

    def alerts(self, status=None, project_id=None, limit=200) -> list[dict]:
        q = "SELECT * FROM alerts WHERE 1=1"
        args = []
        if status:
            q += " AND status=?"
            args.append(status)
        if project_id:
            q += " AND subjects LIKE ?"
            args.append(f'%"project_id": {project_id}%')
        q += " ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, CASE severity WHEN 'warning' THEN 0 WHEN 'notice' THEN 1 ELSE 2 END, id DESC LIMIT ?"
        args.append(limit)
        out = rows_to_dicts(self.db.all(q, args), ["evidence", "subjects"])
        if self.public:
            out = [a for a in out if a["kind"] not in ("gap", "integration")]
        return out

    def documents(self, project=None, doc_type=None, limit=500) -> list[dict]:
        q = f"""SELECT d.id, d.title, d.doc_type, d.fmt, d.path, d.url, d.date, d.visibility, d.superseded, p.slug AS project
                FROM documents d LEFT JOIN projects p ON p.id=d.project_id WHERE d.status='active' AND d.doc_type != 'result'{self._vis()}"""
        args = []
        if project:
            q += " AND p.slug=?"
            args.append(project)
        if doc_type:
            q += " AND d.doc_type=?"
            args.append(doc_type)
        q += " ORDER BY d.date DESC LIMIT ?"
        args.append(limit)
        return rows_to_dicts(self.db.all(q, args))

    def document(self, doc_id: int) -> dict | None:
        d = self.db.one("SELECT d.*, p.slug AS project, p.name AS project_name, s.path AS root FROM documents d LEFT JOIN projects p ON p.id=d.project_id "
                        "LEFT JOIN sources s ON s.id=d.source_id WHERE d.id=?", (doc_id,))
        if not d or (self.public and d["visibility"] != "public"):
            return None
        out = rows_to_dicts([d], ["meta"])[0]
        out.pop("root", None)
        out["chunks"] = rows_to_dicts(self.db.all("SELECT seq, heading, text, line_start, line_end FROM chunks WHERE document_id=? ORDER BY seq", (doc_id,)))
        out["findings"] = rows_to_dicts(self.db.all("SELECT id, text, claim_type, evidence_kind, line, rule FROM findings WHERE document_id=? AND status='active' ORDER BY line", (doc_id,)))
        return out

    # ------------------------------------------------------------------ tabs
    def overview(self) -> dict:
        db = self.db
        projs = self.projects()
        rel = Counter(p["relevance"] for p in projs)
        cats = Counter(p["category"] for p in projs if p["relevance"] in ("investment", "game_theory"))
        recent_docs = self.documents(limit=12)
        last = db.one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
        out = {
            "counts": {"projects": len(projs), "investment_projects": rel.get("investment", 0), "game_theory": rel.get("game_theory", 0),
                       "documents": db.scalar("SELECT COUNT(*) FROM documents d WHERE status='active' AND doc_type != 'result'" + self._vis(), default=0),
                       "findings": db.scalar("SELECT COUNT(*) FROM findings WHERE status='active'", default=0),
                       "experiments": db.scalar("SELECT COUNT(*) FROM experiments WHERE status='active'", default=0),
                       "theses": db.scalar("SELECT COUNT(*) FROM theses WHERE status='active'", default=0),
                       "models": db.scalar("SELECT COUNT(*) FROM financial_models", default=0),
                       "signals": db.scalar("SELECT COUNT(*) FROM signals WHERE active=1", default=0),
                       "companies": db.scalar("SELECT COUNT(DISTINCT entity_id) FROM project_entities pe JOIN entities e ON e.id=pe.entity_id WHERE e.kind='company' AND pe.salience>=0.25", default=0),
                       "sources": db.scalar("SELECT COUNT(*) FROM sources" + (" WHERE visibility='public'" if self.public else ""), default=0)},
            "categories": [{"key": k, "label": self.tax.category_label(k), "n": v} for k, v in cats.most_common()],
            "recent_projects": [{k: p[k] for k in ("slug", "name", "category_label", "status", "last_activity", "summary", "relevance")}
                                for p in projs[:10]],
            "recent_documents": recent_docs,
            "theses": self.theses()[:12],
            "alerts": self.alerts(limit=12),
            "last_sync": dict(last) | {"stats": loads(last["stats"], {})} if last else None,
            "mode": self.settings.mode,
        }
        from . import signals as SG
        out["signals_top"] = SG.aggregate(db)[:10]
        if not self.public:
            out["portfolios"] = self.portfolios()
        return out

    def tab(self, name: str) -> dict:
        cats = TAB_CATEGORIES.get(name, [])
        projs = [p for p in self.projects() if p["category"] in cats or any(c in (p.get("secondary") or []) for c in cats)]
        out = {"projects": projs}
        ids = [p["id"] for p in projs]
        if name == "research":
            out["theses"] = self.theses(include_superseded=True)
            out["models"] = self.models()["financial"]
            out["companies"] = self.companies()[:80]
            out["documents"] = [d for d in self.documents(limit=400) if d["doc_type"] in ("published_research", "memo", "report", "deck", "paper")][:150]
        elif name == "quant":
            allx = [e for e in self.experiments(limit=100000) if e["category"] in ("quant_trading", "derivatives", "alt_data", "game_theory")]
            out["experiment_summary"] = _exp_summary(allx)          # over every result

            def rank(e):
                m = e.get("metrics") or {}
                return (-int(bool(e["oos"])), -abs(m.get("t_stat", 0) or 0), (m.get("p_value") if m.get("p_value") is not None else 1))
            out["experiments"] = sorted(allx, key=rank)[:2500]       # the most informative results for the table
            out["experiments_total"] = len(allx)
            reg = self.models()
            out["strategies"] = reg["strategies"]
            out["ml_models"] = reg["ml"]
            out["signals"] = self.signals()
        elif name == "altdata":
            out["data_sources"] = rows_to_dicts(self.db.all(f"""SELECT e.key, e.name, COUNT(DISTINCT pe.project_id) projects, SUM(pe.mentions) mentions,
                                                              group_concat(DISTINCT p.slug) AS project_slugs FROM project_entities pe
                                                              JOIN entities e ON e.id=pe.entity_id JOIN projects p ON p.id=pe.project_id
                                                              WHERE e.kind='data_source' AND pe.mentions >= 2 {"AND p.visibility='public'" if self.public else ""}
                                                              GROUP BY e.id ORDER BY projects DESC, mentions DESC"""))
            out["company_links"] = rows_to_dicts(self.db.all(f"""SELECT e.symbol, e.name, p.slug, round(pe.salience,3) salience, pe.mentions FROM project_entities pe
                                                               JOIN entities e ON e.id=pe.entity_id JOIN projects p ON p.id=pe.project_id
                                                               WHERE e.kind='company' AND pe.salience >= 0.2 AND (p.category='alt_data' OR p.secondary LIKE '%alt_data%')
                                                               {"AND p.visibility='public'" if self.public else ""} ORDER BY pe.salience DESC"""))
            out["signals"] = self.signals()
        elif name == "markets":
            out["experiments"] = [e for e in self.experiments(limit=3000) if e["category"] == "derivatives"]
            out["securities"] = self.companies()
            out["theses"] = [t for t in self.theses() if True]
        return out

    # ------------------------------------------------------------------ portfolio
    def portfolios(self) -> list[dict]:
        if self.public:
            return []
        from .portfolio import analytics as A
        out = []
        for p in self.db.all("SELECT * FROM portfolios ORDER BY CASE kind WHEN 'real' THEN 0 WHEN 'paper' THEN 1 ELSE 2 END, name"):
            H = A.holdings(self.db, dict(p))
            meta = loads(p["meta"], {}) or {}
            out.append({"key": p["key"], "name": p["name"], "kind": p["kind"], "base_currency": p["base_currency"], "benchmark": p["benchmark"],
                        "source": p["source"], "read_only": bool(p["read_only"]), "as_of": H["as_of"], "positions": len(H["holdings"]),
                        "gross": round(H["gross"], 2), "top": [h["symbol"] for h in H["holdings"][:5]], "discovered_from": meta.get("discovered_from")})
        return out

    def portfolio(self, key: str) -> dict | None:
        if self.public:
            return None
        from .portfolio import analytics as A
        p = self.db.one("SELECT * FROM portfolios WHERE key=?", (key,))
        if not p:
            return None
        p = dict(p)
        H = A.holdings(self.db, p)
        perf = A.performance(self.db, p)
        meta = loads(p["meta"], {}) or {}
        txs = rows_to_dicts(self.db.all("SELECT date, kind, symbol, qty, price, currency, amount, source, note FROM transactions WHERE portfolio_id=? ORDER BY date DESC LIMIT 200", (p["id"],)))
        orders = rows_to_dicts(self.db.all("SELECT * FROM orders WHERE portfolio_id=? ORDER BY id DESC LIMIT 50", (p["id"],)))
        for h in H["holdings"]:
            h["theses"] = [{"rating": t["rating"], "target": t["target_price"], "project": t["slug"]} for t in self.db.all(
                "SELECT t.rating, t.target_price, p.slug FROM theses t LEFT JOIN projects p ON p.id=t.project_id WHERE t.symbol=? AND t.status='active'", (h["symbol"],))]
            sec = self.db.one("SELECT sector, industry, themes FROM securities WHERE symbol=?", (h["symbol"],))
            h["sector"] = sec["sector"] if sec else None
            h["themes"] = list((loads(sec["themes"], {}) or {}).keys()) if sec else []
        return {"portfolio": {k: p[k] for k in ("key", "name", "kind", "base_currency", "benchmark", "source", "read_only")},
                "holdings": H, "performance": perf, "transactions": txs, "orders": orders, "notes": meta.get("notes"), "mwr": meta.get("mwr")}


def _exp_summary(exps: list[dict]) -> dict:
    by = defaultdict(lambda: {"n": 0, "oos": 0, "significant": 0, "flags": 0})
    kinds = Counter()
    for e in exps:
        m = e.get("metrics") or {}
        b = by[e["project"]]
        b["n"] += 1
        b["oos"] += int(bool(e["oos"]))
        t = abs(m.get("t_stat", 0) or 0)
        p = m.get("p_value")
        b["significant"] += int(t >= 2 or (p is not None and p < 0.05))
        b["flags"] += int(any(f.get("status") in ("possible", "detected") for f in (e.get("flags") or [])))
        kinds[e["result_kind"] or "unknown"] += 1
    return {"by_project": dict(by), "result_kinds": dict(kinds)}
