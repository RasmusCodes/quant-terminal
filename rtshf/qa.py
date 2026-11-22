"""Question answering over the research database.

Questions are routed to a structured handler (company dossier, theme view, thesis arguments, contradicting evidence,
out-of-sample results, exposures, cross-project companies, predictive results) or to hybrid search. Answers are built
only from stored records and passages, each cited with its source. Missing evidence is reported as missing.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import llm
from .db import DB
from .entities import build_linker
from .search import Searcher
from .taxonomy import Taxonomy
from .util import loads, truncate

CLAIM_WEIGHT = {"verified_result": 1.0, "observed_fact": 0.9, "model_estimate": 0.8, "research_claim": 0.55}
CLAIM_LABEL = {"verified_result": "verified result", "observed_fact": "observed fact", "model_estimate": "model estimate",
               "research_claim": "research claim"}
THEME_EXPAND = {"ai_infrastructure": ["ai_infrastructure", "ai_capex", "data_center_power", "optical_networking", "semiconductors"],
                "semiconductor_capex": ["semiconductor_capex", "semiconductors"],
                "data_center_power": ["data_center_power"]}
NAME_THEMES = {r"\bai infra\w*|\bai infrastructure|\bai data cent": "ai_infrastructure", r"data[- ]cent\w* power|power demand": "data_center_power",
               r"semi\w* capex|wafer fab|semicap|chip equipment": "semiconductor_capex", r"\bai capex": "ai_capex",
               r"optical|transceiver": "optical_networking", r"\bsemiconductor|\bchips?\b": "semiconductors",
               r"streaming": "streaming_media", r"aviation|aero[- ]?engine": "aviation_aerospace", r"crypto|bitcoin": "crypto_digital_assets",
               r"volatil": "volatility_regimes", r"credit": "credit_markets", r"rates|yield curve": "rates_macro",
               r"prediction market": "prediction_markets", r"casino|gaming": "gaming_entertainment"}
INTENTS = [
    ("thesis_contra", re.compile(r"(contradict|against|undermin|challenge|disagree|counter|bear case|risks? to|weak(?:ness|en)|wrong about)", re.I)),
    ("thesis_args", re.compile(r"(strongest|best|key|main|core)\s+(argument|point|reason|case)|arguments? (?:for|in)|why (?:am i|are we|is it) (?:long|short)|bull case", re.I)),
    ("oos_alpha", re.compile(r"(out[- ]of[- ]sample|\boos\b|actually (?:work|demonstrat|show|produc)|real alpha|survive[sd]? (?:costs|out))", re.I)),
    ("exposure", re.compile(r"(exposure|exposed|how much .*(?:hold|own)|my (?:holdings|portfolio|book)s?\b.*\b(?:in|to)\b)", re.I)),
    ("cross_projects", re.compile(r"(companies|names|tickers|stocks).*(multiple|several|more than one|across|both).*(project|alt)", re.I)),
    ("predictive", re.compile(r"(predictive|forecast\w*|predict\w*).*(result|power|skill|useful)|most useful .*result", re.I)),
    ("projects_on", re.compile(r"\b(which|what) (?:of my )?projects?\b", re.I)),
    ("theme_view", re.compile(r"(indicate|say|suggest|think|view|tell|models?|signals?)\b", re.I)),
    ("company", re.compile(r"(research|work|done|know|analysis|covered|written|built|have i|did i)", re.I)),
]


def _ev(kind, text, **kw) -> dict:
    d = {"kind": kind, "text": text}
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


class Answerer:
    def __init__(self, db: DB, settings):
        self.db = db
        self.settings = settings
        self.tax = Taxonomy(settings.taxonomy)
        self.linker = build_linker(db, self.tax)
        self.searcher = Searcher(db, settings)

    # ------------------------------------------------------------------ entity detection
    def detect(self, q: str) -> dict:
        companies = [s for s, _ in self.linker.companies(q).most_common()]
        for tok in re.findall(r"\b[A-Z]{2,5}\b", q):                # bare tickers the linker treats as ambiguous
            if tok not in companies and self.db.one("SELECT 1 FROM securities WHERE symbol=?", (tok,)) and tok not in ("AI", "US"):
                companies.append(tok)
        themes = []
        for rx, key in NAME_THEMES.items():
            if re.search(rx, q, re.I) and key not in themes:
                themes.append(key)
        for k, (s, _) in sorted(self.tax.score_themes(q).items(), key=lambda kv: -kv[1][0]):
            if k not in themes and s >= 1.0:
                themes.append(k)
        projects = []
        for p in self.db.all("SELECT id, slug, name FROM projects"):
            if re.search(r"\b" + re.escape(p["slug"].replace("-", " ")) + r"\b", q.lower().replace("-", " ")):
                projects.append(p["id"])
        return {"companies": companies, "themes": themes, "projects": projects}

    def route(self, q: str, ents: dict) -> str:
        for name, rx in INTENTS:
            if not rx.search(q):
                continue
            if name in ("thesis_contra", "thesis_args") and not ents["companies"]:
                continue
            if name == "theme_view" and not ents["themes"]:
                continue
            if name == "company" and not ents["companies"]:
                continue
            if name == "projects_on" and not (ents["themes"] or ents["companies"]):
                continue
            if name == "exposure" and not (ents["themes"] or ents["companies"]):
                continue
            return name
        if ents["companies"]:
            return "company"
        if ents["themes"]:
            return "theme_view"
        return "search"

    # ------------------------------------------------------------------ main
    def answer(self, question: str, use_llm: bool | None = None) -> dict:
        ents = self.detect(question)
        intent = self.route(question, ents)
        handler = getattr(self, f"h_{intent}")
        res = handler(question, ents)
        res.setdefault("notes", [])
        res["intent"] = intent
        res["entities"] = ents
        res["question"] = question
        if not res["evidence"]:
            res["answer"] += "\n\nNo stored evidence answers this. Nothing has been inferred to fill the gap."
        if (use_llm if use_llm is not None else llm.enabled(self.settings)) and res["evidence"]:
            syn = llm.synthesize(self.settings, question, res["evidence"], res["answer"])
            if syn:
                res["synthesis"] = syn
        res["method"] = "structured query + hybrid retrieval (extractive)" + (" + Claude synthesis" if res.get("synthesis") else "")
        return res

    # ------------------------------------------------------------------ helpers
    def _findings(self, where: str, params: list, limit: int = 30) -> list[dict]:
        rows = self.db.all(f"""SELECT f.*, p.slug, p.name AS pname, d.title, d.url, d.path, d.doc_type, d.date, d.visibility
                               FROM findings f JOIN documents d ON d.id=f.document_id LEFT JOIN projects p ON p.id=f.project_id
                               WHERE f.status='active' AND d.status='active' AND {where}
                               ORDER BY f.importance DESC LIMIT ?""", params + [limit])
        return [_ev("finding", r["text"], claim_type=r["claim_type"], evidence_kind=r["evidence_kind"], project=r["slug"],
                    title=r["title"], url=r["url"], path=r["path"], line=r["line"], date=r["date"], polarity=r["polarity"],
                    importance=r["importance"], id=r["id"], visibility=r["visibility"]) for r in rows]

    def _passages(self, q: str, limit: int = 8, **kw) -> list[dict]:
        out = []
        for h in self.searcher.search(q, limit=limit, **kw):
            out.append(_ev("passage", truncate(h["text"].replace("\n", " "), 600), project=h["project"], title=h["title"],
                           heading=h["heading"], url=h["url"], path=h["path"], line=h["line_start"], date=h["date"],
                           score=h["score"], visibility=h["visibility"]))
        return out

    def _entity_ids(self, keys: list[str]) -> set[int]:
        return {r[0] for r in self.db.all(f"SELECT id FROM entities WHERE key IN ({','.join('?' * len(keys))})", keys)} if keys else set()

    def _theses(self, sym: str) -> list:
        return self.db.all("""SELECT t.*, p.slug, p.name AS pname, d.title, d.url, d.path, d.doc_type FROM theses t
                              LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN documents d ON d.id=t.document_id
                              WHERE t.symbol=? ORDER BY CASE t.status WHEN 'active' THEN 0 ELSE 1 END, t.thesis_date DESC""", (sym,))

    @staticmethod
    def _thesis_line(t) -> str:
        bits = [f"{t['rating']}"]
        if t["target_price"]:
            bits.append(f"target ${t['target_price']:g}")
        if t["price_at_thesis"]:
            bits.append(f"vs ${t['price_at_thesis']:g}")
        if t["upside"] is not None:
            bits.append(f"({t['upside'] * 100:+.1f}%)")
        sc = loads(t["scenarios"], {}) or {}
        if sc:
            bits.append("scenarios " + " / ".join(f"{k} ${v:g}" for k, v in sc.items()))
        return " ".join(bits) + f" — {t['title']} ({t['slug']}, {t['thesis_date'] or 'undated'}, {t['status']})"

    def _cite(self, evidence: list[dict], item: dict) -> str:
        evidence.append(item)
        return f"[{len(evidence)}]"

    # ------------------------------------------------------------------ handlers
    def h_company(self, q, ents):
        sym = ents["companies"][0]
        sec = self.db.one("SELECT * FROM securities WHERE symbol=?", (sym,))
        name = sec["name"] if sec else sym
        eid = self.db.scalar("SELECT id FROM entities WHERE key=?", (f"company:{sym}",))
        ev, lines = [], []
        projects = self.db.all("""SELECT p.slug, p.name, p.category, pe.mentions, pe.documents, pe.salience FROM project_entities pe
                                  JOIN projects p ON p.id=pe.project_id WHERE pe.entity_id=? ORDER BY pe.salience DESC, pe.mentions DESC""",
                               (eid,)) if eid else []
        main = [p for p in projects if p["salience"] >= 0.25]
        minor = [p for p in projects if p["salience"] < 0.25]
        if not projects:
            lines.append(f"No project in the registry mentions {name} ({sym}).")
        else:
            lines.append(f"**{name} ({sym})** appears in {len(projects)} project(s); it is a main subject in {len(main)}.")
            for p in main[:8]:
                lines.append(f"- {p['name']} ({self.tax.category_label(p['category'])}): {p['mentions']} mentions in {p['documents']} documents "
                             + self._cite(ev, _ev("project", f"{p['name']} covers {sym} ({p['mentions']} mentions)", project=p["slug"])))
            if minor:
                lines.append("- Mentioned in passing: " + ", ".join(p["slug"] for p in minor[:10]))
        theses = self._theses(sym)
        if theses:
            lines.append("\n**Theses**")
            for t in theses[:4]:
                lines.append("- " + self._thesis_line(t) + " " + self._cite(ev, _ev("thesis", self._thesis_line(t), project=t["slug"],
                             title=t["title"], url=t["url"], path=t["path"], id=t["id"], claim_type="research_claim")))
        models = self.db.all("SELECT m.*, p.slug FROM financial_models m LEFT JOIN projects p ON p.id=m.project_id WHERE m.symbol=?", (sym,))
        if models:
            lines.append("\n**Models**")
            for m in models[:5]:
                outs = ", ".join(f"{o['label']}: {o['value']:g}" for o in (loads(m["outputs"], []) or [])[:4])
                lines.append(f"- {m['name']} ({m['model_type']}, {m['slug']}) {outs} " + self._cite(ev, _ev("model", f"{m['name']}: {outs}",
                             project=m["slug"], path=m["path"], claim_type="model_estimate")))
        fnd = self._findings("f.entities LIKE ?", [f'%"{sym}"%'], 12)
        if fnd:
            lines.append("\n**Key findings**")
            for f in fnd[:8]:
                lines.append(f"- {truncate(f['text'], 240)} _({CLAIM_LABEL[f['claim_type']]})_ " + self._cite(ev, f))
        sigs = self.db.all("SELECT * FROM signals WHERE instrument=? AND active=1", (sym,))
        if sigs:
            lines.append("\n**Signals**: " + "; ".join(f"{s['strategy_id']} dir {s['direction']:+d}" + (f" exp {s['expected_return'] * 100:+.1f}%" if s["expected_return"] is not None else "")
                                                       + f" ({s['validation_status']})" for s in sigs[:6]))
        held = []
        for p in self.db.all("SELECT * FROM portfolios"):
            for h in (loads(p["meta"], {}) or {}).get("holdings", []) or []:
                if h["symbol"] == sym:
                    held.append(f"{p['name']} ({h.get('weight', 0):.1f}%)")
            if self.db.one("SELECT 1 FROM transactions WHERE portfolio_id=? AND symbol=?", (p["id"], sym)):
                held.append(p["name"])
        if held:
            lines.append("\n**Held in**: " + ", ".join(sorted(set(held))))
        if len(ev) < 6:
            for psg in self._passages(f"{name} {sym}", 6, entity_ids={eid} if eid else None):
                lines.append(f"- {truncate(psg['text'], 200)} " + self._cite(ev, psg))
        return {"answer": "\n".join(lines), "evidence": ev}

    def h_theme_view(self, q, ents):
        theme = ents["themes"][0]
        keys = THEME_EXPAND.get(theme, [theme])
        label = self.tax.theme_label(theme)
        ev, lines = [], [f"**{label}** across your work (themes searched: {', '.join(self.tax.theme_label(k) for k in keys)})."]
        tids = self._entity_ids([f"theme:{k}" for k in keys])
        projs = self.db.all(f"""SELECT p.slug, p.name, p.category, SUM(pe.mentions) m FROM project_entities pe JOIN projects p ON p.id=pe.project_id
                                WHERE pe.entity_id IN ({','.join('?' * len(tids)) or "''"}) AND p.relevance='investment'
                                GROUP BY p.id HAVING m >= 4 ORDER BY m DESC""", list(tids))
        if projs:
            lines.append(f"\n**Projects that research it** ({len(projs)}): " + ", ".join(f"{p['name']} ({p['m']})" for p in projs[:12]))
        syms = [r[0] for r in self.db.all("SELECT symbol, themes FROM securities") if any(k in (loads(r[1], {}) or {}) for k in keys)]
        theses = [t for s in syms for t in self.db.all("""SELECT t.*, p.slug, d.title, d.url, d.path FROM theses t LEFT JOIN projects p ON p.id=t.project_id
                                                           LEFT JOIN documents d ON d.id=t.document_id WHERE t.symbol=? AND t.status='active'""", (s,))]
        if theses:
            lines.append("\n**Your active calls on exposed companies**")
            for t in theses[:10]:
                lines.append(f"- {t['symbol']}: " + self._thesis_line(t) + " " + self._cite(ev, _ev("thesis", f"{t['symbol']} " + self._thesis_line(t),
                             project=t["slug"], title=t["title"], url=t["url"], path=t["path"], id=t["id"], claim_type="research_claim")))
        sigs = self.db.all(f"""SELECT instrument, strategy_id, direction, expected_return, validation_status FROM signals
                               WHERE active=1 AND instrument IN ({','.join('?' * len(syms)) or "''"})""", syms)
        if sigs:
            agg = defaultdict(list)
            for s in sigs:
                agg[s["instrument"]].append(s)
            lines.append("\n**Signals on exposed names**: " + "; ".join(
                f"{k} " + "/".join(f"{x['direction']:+d}" for x in v) + f" ({len(v)} signal{'s' if len(v) > 1 else ''})" for k, v in list(agg.items())[:12]))
        like = " OR ".join(["f.themes LIKE ?"] * len(keys))
        fnd = self._findings(f"({like}) AND f.claim_type IN ('model_estimate','verified_result','observed_fact')", [f'%"{k}"%' for k in keys], 14)
        if fnd:
            lines.append("\n**What the models and data say** (model estimates, observations, verified results)")
            for f in fnd[:10]:
                lines.append(f"- {truncate(f['text'], 230)} _({CLAIM_LABEL[f['claim_type']]}, {f['project']})_ " + self._cite(ev, f))
        claims = self._findings(f"({like}) AND f.claim_type='research_claim'", [f'%"{k}"%' for k in keys], 6)
        if claims:
            lines.append("\n**Arguments (research claims, not results)**")
            for f in claims[:5]:
                lines.append(f"- {truncate(f['text'], 200)} _({f['project']})_ " + self._cite(ev, f))
        if len(ev) < 5:
            for psg in self._passages(q, 6, entity_ids=tids):
                lines.append(f"- {truncate(psg['text'], 200)} " + self._cite(ev, psg))
        return {"answer": "\n".join(lines), "evidence": ev}

    def h_projects_on(self, q, ents):
        ev, lines = [], []
        if ents["themes"]:
            keys = THEME_EXPAND.get(ents["themes"][0], [ents["themes"][0]])
            tids = self._entity_ids([f"theme:{k}" for k in keys])
            rows = self.db.all(f"""SELECT p.id, p.slug, p.name, p.category, SUM(pe.mentions) m, SUM(pe.documents) d FROM project_entities pe
                                   JOIN projects p ON p.id=pe.project_id WHERE pe.entity_id IN ({','.join('?' * len(tids)) or "''"})
                                   GROUP BY p.id HAVING m >= 3 ORDER BY m DESC""", list(tids))
            lines.append(f"Projects whose documents discuss **{self.tax.theme_label(ents['themes'][0])}**, by number of mentions:")
            for r in rows[:15]:
                psg = self._passages(q, 1, project_ids={r["id"]}, entity_ids=tids)
                cite = self._cite(ev, psg[0]) if psg else ""
                lines.append(f"- **{r['name']}** ({self.tax.category_label(r['category'])}): {r['m']} mentions in {r['d']} documents {cite}")
                if psg:
                    lines.append(f"  > {truncate(psg[0]['text'], 220)}")
            if not rows:
                lines.append("None found.")
        else:
            return self.h_company(q, ents)
        return {"answer": "\n".join(lines), "evidence": ev}

    def h_thesis_args(self, q, ents):
        sym = ents["companies"][0]
        theses = [t for t in self._theses(sym) if t["status"] == "active"] or self._theses(sym)
        ev, lines = [], []
        if not theses:
            return {"answer": f"No thesis on {sym} was found (no document states a call on it). Research mentioning {sym}:",
                    "evidence": self._passages(f"{sym} thesis", 8)}
        t = theses[0]
        lines.append(f"**{sym} thesis**: " + self._thesis_line(t) + " " + self._cite(ev, _ev("thesis", self._thesis_line(t), project=t["slug"],
                     title=t["title"], url=t["url"], path=t["path"], id=t["id"], claim_type="research_claim")))
        own = self._findings("f.document_id=? AND f.claim_type != 'research_claim'", [t["document_id"]], 20)
        args = loads(t["arguments"], []) or []
        # rank the memo's own statements by evidence type and numbers
        ranked = sorted(own, key=lambda f: -(CLAIM_WEIGHT[f["claim_type"]] * (f["importance"] or 0.5)))
        lines.append("\n**Best-supported points in the thesis document** (ranked: verified > observed > model estimate)")
        for f in ranked[:7]:
            lines.append(f"- {truncate(f['text'], 240)} _({CLAIM_LABEL[f['claim_type']]})_ " + self._cite(ev, f))
        if args and len(ranked) < 4:
            for a in args[:5]:
                lines.append(f"- {truncate(a, 220)} _(argument as written)_ " + self._cite(ev, _ev("argument", a, project=t["slug"], title=t["title"],
                             url=t["url"], path=t["path"], claim_type="research_claim")))
        sign = -1 if t["stance"] == "short" else 1
        corr = [f for f in self._findings("f.entities LIKE ? AND f.document_id != ? AND f.claim_type != 'research_claim'",
                                          [f'%"{sym}"%', t["document_id"]], 40) if (f.get("polarity") or 0) * sign > 0.2]
        if corr:
            lines.append("\n**Corroborating evidence from other documents**")
            for f in corr[:6]:
                lines.append(f"- {truncate(f['text'], 220)} _({CLAIM_LABEL[f['claim_type']]}, {f['project']})_ " + self._cite(ev, f))
        res = {"answer": "\n".join(lines), "evidence": ev}
        res["notes"] = ["Ranking is mechanical: statement type x section importance; it does not judge the economics."]
        return res

    def h_thesis_contra(self, q, ents):
        sym = ents["companies"][0]
        theses = [t for t in self._theses(sym) if t["status"] == "active"] or self._theses(sym)
        ev, lines = [], []
        if not theses:
            return {"answer": f"No thesis on {sym} was found, so there is nothing to contradict.", "evidence": []}
        t = theses[0]
        sign = -1 if t["stance"] == "short" else 1
        lines.append(f"**Current {sym} thesis**: " + self._thesis_line(t) + " " + self._cite(ev, _ev("thesis", self._thesis_line(t), project=t["slug"],
                     title=t["title"], url=t["url"], path=t["path"], id=t["id"], claim_type="research_claim")))
        risks = loads(t["risks"], []) or []
        if risks:
            lines.append("\n**Risks the thesis itself lists**")
            for r in risks[:6]:
                lines.append(f"- {truncate(r, 220)} " + self._cite(ev, _ev("risk", r, project=t["slug"], title=t["title"], url=t["url"],
                             path=t["path"], claim_type="research_claim")))
        contra = [f for f in self._findings("f.entities LIKE ?", [f'%"{sym}"%'], 80) if (f.get("polarity") or 0) * sign < -0.2]
        other = [f for f in contra if f.get("path") != t["path"]]
        same = [f for f in contra if f.get("path") == t["path"]]
        if other:
            lines.append("\n**Findings elsewhere that point the other way**")
            for f in sorted(other, key=lambda f: -CLAIM_WEIGHT[f["claim_type"]])[:8]:
                lines.append(f"- {truncate(f['text'], 230)} _({CLAIM_LABEL[f['claim_type']]}, {f['project']}, {f.get('date') or ''})_ " + self._cite(ev, f))
        if same:
            lines.append("\n**Counter-points inside the thesis document**")
            for f in same[:5]:
                lines.append(f"- {truncate(f['text'], 220)} _({CLAIM_LABEL[f['claim_type']]})_ " + self._cite(ev, f))
        alerts = self.db.all("SELECT title, detail FROM alerts WHERE subjects LIKE ? AND status != 'dismissed' ORDER BY id DESC LIMIT 4",
                             (f'%"{sym}"%',))
        for a in alerts:
            lines.append(f"\n_Assistant alert:_ {a['title']}")
        res = {"answer": "\n".join(lines), "evidence": ev}
        res["notes"] = ["'Points the other way' uses a finance sentiment lexicon on each statement relative to the stance; read the sources."]
        return res

    def h_oos_alpha(self, q, ents):
        rows = self.db.all("""SELECT e.*, p.slug, p.name AS pname, d.url, d.path, d.title FROM experiments e
                              LEFT JOIN projects p ON p.id=e.project_id LEFT JOIN documents d ON d.id=e.document_id
                              WHERE e.status='active' AND p.relevance='investment'""")
        passing, near, failing, untested = [], [], [], 0
        for r in rows:
            m = loads(r["metrics"], {}) or {}
            if not any(k in m for k in ("alpha", "sharpe", "ann_return", "cagr", "ic", "ir")):
                continue
            flags = {f["flag"] for f in (loads(r["flags"], []) or []) if f.get("status") in ("possible", "detected")}
            t = abs(m.get("t_stat", 0) or 0)
            p = m.get("p_value")
            sig = t >= 2 or (p is not None and p < 0.05)
            weak = (1.5 <= t < 2) or (p is not None and 0.05 <= p < 0.10)
            item = (r, m, flags)
            if not r["oos"]:
                untested += 1
                continue
            positive = (m.get("alpha", 0) or 0) > 0 or (m.get("sharpe", 0) or 0) > 0 or (m.get("ic", 0) or 0) > 0 or (m.get("ann_return", 0) or 0) > 0
            if sig and positive and not ({"synthetic_data", "holdout_reuse"} & flags) and r["costs"] in ("stated", "mixed"):
                passing.append(item)
            elif (sig or weak) and positive:
                near.append(item)
            elif r["verdict"] == "rejected" or not positive:
                failing.append(item)
        ev, lines = [], []
        lines.append(f"Out of {len(rows)} extracted experiments, {len(passing)} show positive, statistically significant "
                     "out-of-sample results with costs stated and no red flag; "
                     f"{len(near)} are borderline or lack a cost statement; {len(failing)} out-of-sample tests were negative or rejected; "
                     f"{untested} return results have no out-of-sample language.")

        def fmt(r, m, flags):
            ms = ", ".join(f"{k} {v:g}" for k, v in m.items() if k in ("alpha", "sharpe", "t_stat", "p_value", "ic", "ann_return"))
            return f"{r['pname']}: {truncate(r['name'], 140)} [{ms}] costs {r['costs'] or 'not stated'}" + (f"; flags: {', '.join(sorted(flags))}" if flags else "")
        if passing:
            lines.append("\n**Passing**")
            for r, m, fl in sorted(passing, key=lambda x: -abs(x[1].get("t_stat", 0) or 0))[:10]:
                lines.append("- " + fmt(r, m, fl) + " " + self._cite(ev, _ev("experiment", fmt(r, m, fl), project=r["slug"], title=r["title"],
                             url=r["url"], path=r["path"] or r["location"], line=r["line"], claim_type="model_estimate", id=r["id"])))
        if near:
            lines.append("\n**Borderline / incomplete**")
            for r, m, fl in near[:8]:
                lines.append("- " + fmt(r, m, fl) + " " + self._cite(ev, _ev("experiment", fmt(r, m, fl), project=r["slug"], title=r["title"],
                             url=r["url"], path=r["path"] or r["location"], line=r["line"], claim_type="model_estimate", id=r["id"])))
        if failing:
            lines.append("\n**Tested out-of-sample and did not hold**: " + ", ".join(sorted({r['pname'] for r, _, _ in failing})))
        res = {"answer": "\n".join(lines), "evidence": ev}
        res["notes"] = ["Experiments and their out-of-sample/cost labels are extracted automatically from reports and result files; "
                        "a 'passing' label means the text and numbers meet the rule, not that the result is independently replicated."]
        return res

    def h_exposure(self, q, ents):
        from .portfolio import analytics as A
        ev, lines = [], []
        keys = THEME_EXPAND.get(ents["themes"][0], [ents["themes"][0]]) if ents["themes"] else []
        syms = set(ents["companies"])
        for p in self.db.all("SELECT * FROM portfolios"):
            H = A.holdings(self.db, dict(p))
            rows = []
            for h in H["holdings"]:
                sec = self.db.one("SELECT themes, name FROM securities WHERE symbol=?", (h["symbol"],))
                th = loads(sec["themes"], {}) if sec else {}
                hit = [f"{k}:{th[k]}" for k in keys if k in th] if keys else ([h["symbol"]] if h["symbol"] in syms else [])
                if hit:
                    rows.append((h, hit))
            total = sum((h.get("weight") or 0) for h, _ in rows)
            label = ", ".join(self.tax.theme_label(k) for k in keys) if keys else ", ".join(syms)
            if rows:
                lines.append(f"\n**{p['name']}**: {total * 100:.1f}% of the book is exposed to {label}")
                for h, hit in sorted(rows, key=lambda x: -abs(x[0].get("weight") or 0)):
                    lines.append(f"- {h['symbol']} {(h.get('weight') or 0) * 100:.1f}% ({'; '.join(hit)}) "
                                 + self._cite(ev, _ev("position", f"{p['name']} holds {h['symbol']} at {(h.get('weight') or 0) * 100:.1f}% ({'; '.join(hit)})",
                                                      title=p["name"], claim_type="observed_fact")))
            else:
                lines.append(f"\n**{p['name']}**: no holding is tagged with {label}.")
        lines.insert(0, "Exposure = portfolio weight of holdings whose security-master entry carries the theme (role in brackets: "
                        "supplier/operator/subject/beneficiary/spender). Roles are declared reference data, editable in config/securities.yaml.")
        return {"answer": "\n".join(lines), "evidence": ev}

    def h_cross_projects(self, q, ents):
        cat = "alt_data" if re.search(r"alt", q, re.I) else None
        rows = self.db.all("""SELECT e.symbol, e.name, p.slug, p.category, pe.salience, pe.mentions FROM project_entities pe
                              JOIN entities e ON e.id=pe.entity_id JOIN projects p ON p.id=pe.project_id
                              WHERE e.kind='company' AND pe.salience >= 0.15 AND pe.mentions >= 3 AND p.relevance='investment'""" +
                           (" AND (p.category='alt_data' OR p.secondary LIKE '%alt_data%')" if cat else ""))
        by = defaultdict(list)
        for r in rows:
            by[(r["symbol"], r["name"])].append(r)
        multi = sorted([(k, v) for k, v in by.items() if len({x["slug"] for x in v}) >= 2], key=lambda kv: -len(kv[1]))
        ev, lines = [], [f"Companies that are a substantive subject (salience >= 0.15) in two or more "
                         f"{'alternative-data ' if cat else ''}projects: {len(multi)}."]
        for (sym, name), v in multi[:20]:
            lines.append(f"- **{sym}** ({name}): " + ", ".join(f"{x['slug']} ({x['mentions']})" for x in sorted(v, key=lambda x: -x["mentions"]))
                         + " " + self._cite(ev, _ev("company", f"{sym} appears in " + ", ".join(x["slug"] for x in v), claim_type="observed_fact")))
        return {"answer": "\n".join(lines), "evidence": ev}

    def h_predictive(self, q, ents):
        rows = self.db.all("""SELECT e.*, p.slug, p.name AS pname, d.url, d.path, d.title FROM experiments e LEFT JOIN projects p ON p.id=e.project_id
                              LEFT JOIN documents d ON d.id=e.document_id WHERE e.status='active' AND p.relevance='investment'""")
        scored = []
        for r in rows:
            m = loads(r["metrics"], {}) or {}
            t = abs(m.get("t_stat", 0) or 0)
            p = m.get("p_value")
            strength = max(t, 2.0 if (p is not None and p < 0.05) else 0, (m.get("ic", 0) or 0) * 30)
            if strength <= 0:
                continue
            scored.append((strength * (1.3 if r["oos"] else 0.8) * (0.5 if r["result_kind"] == "synthetic_demo" else 1.0), r, m))
        by_proj = defaultdict(list)
        for s, r, m in scored:
            by_proj[r["pname"]].append((s, r, m))
        ranked = sorted(by_proj.items(), key=lambda kv: -max(x[0] for x in kv[1]))
        ev, lines = [], ["Projects ranked by their strongest extracted statistical result (t-stat / p-value / IC; out-of-sample weighted up, "
                         "synthetic data down):"]
        for pname, items in ranked[:10]:
            s, r, m = max(items, key=lambda x: x[0])
            ms = ", ".join(f"{k} {v:g}" for k, v in m.items() if k in ("t_stat", "p_value", "ic", "sharpe", "alpha", "hit_rate", "brier"))
            txt = f"{pname}: {truncate(r['name'], 150)} [{ms}] {'OOS' if r['oos'] else 'in-sample/unclear'}; {len(items)} significant-ish results"
            lines.append("- " + txt + " " + self._cite(ev, _ev("experiment", txt, project=r["slug"], title=r["title"], url=r["url"],
                                                                path=r["path"] or r["location"], line=r["line"], claim_type="model_estimate", id=r["id"])))
        return {"answer": "\n".join(lines), "evidence": ev,
                "notes": ["Usefulness is proxied by statistical strength; it is not a measure of economic value."]}

    def h_search(self, q, ents):
        ps = self._passages(q, 10)
        ev, lines = [], [f"Most relevant passages for: _{q}_"]
        for psg in ps:
            lines.append(f"- **{psg['project']}** / {psg['title']}" + (f" › {psg['heading'].split(' > ')[-1]}" if psg.get("heading") else "")
                         + f": {truncate(psg['text'], 220)} " + self._cite(ev, psg))
        return {"answer": "\n".join(lines), "evidence": ev}


def answer(db: DB, settings, question: str, use_llm: bool | None = None) -> dict:
    return Answerer(db, settings).answer(question, use_llm)
