"""Project profiling: category, relevance, subtopics, themes, technologies, data sources, capabilities, status.

Scores come from taxonomy terms found in the project's own name, description, README, document headings,
website page and code imports. The evidence (top terms per category) is stored so a classification can be checked.
User overrides (config/overrides.yaml or local.yaml) always win.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from pathlib import Path

from .db import DB
from .extract import experiments as X
from .taxonomy import Taxonomy
from .util import dumps, in_superseded_folder, loads, now_iso, read_text

INVEST_MIN = 7.0          # minimum investment-category score to count as investment research
GAME_MIN = 4.0
TOOLS_MIN = 4.0
TECH = {
    "pandas": "pandas", "numpy": "NumPy", "scipy": "SciPy", "sklearn": "scikit-learn", "torch": "PyTorch", "tensorflow": "TensorFlow",
    "statsmodels": "statsmodels", "cvxpy": "cvxpy", "QuantLib": "QuantLib", "fastapi": "FastAPI", "flask": "Flask",
    "streamlit": "Streamlit", "duckdb": "DuckDB", "sqlite3": "SQLite", "pyarrow": "Parquet/Arrow", "polars": "Polars",
    "matplotlib": "matplotlib", "plotly": "Plotly", "openpyxl": "openpyxl", "requests": "requests", "bs4": "BeautifulSoup",
    "lightgbm": "LightGBM", "xgboost": "XGBoost", "catboost": "CatBoost", "gymnasium": "Gymnasium", "stable_baselines3": "Stable-Baselines3",
    "arch": "arch", "numba": "Numba", "pyo3": "PyO3", "jinja2": "Jinja2", "yaml": "PyYAML", "networkx": "NetworkX",
    "hmmlearn": "hmmlearn", "pypdf": "pypdf", "anthropic": "Anthropic SDK", "pydantic": "Pydantic", "sqlalchemy": "SQLAlchemy",
}
LANG_EXT = {".py": "Python", ".rs": "Rust", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".mjs": "JavaScript",
            ".r": "R", ".jl": "Julia", ".sql": "SQL", ".cpp": "C++", ".go": "Go", ".ipynb": "Jupyter", ".xlsx": "Excel"}
IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)|import\s+([A-Za-z_][\w]*))", re.M)


def profile_project(db: DB, tax: Taxonomy, project_id: int, overrides: dict, root_of_source) -> dict:
    """Recompute and store the project's profile. root_of_source(source_row) -> Path|None for code reading."""
    proj = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if proj is None:                      # merged into another project during this run
        return {}
    sources = db.all("SELECT * FROM sources WHERE project_id=?", (project_id,))
    docs = db.all("SELECT id, title, doc_type, path, url, visibility, meta, date, modified, superseded, source_id FROM documents "
                  "WHERE project_id=? AND status='active'", (project_id,))
    name = proj["name"]
    descs = [s["description"] for s in sources if s["description"]]
    readme = next((d for d in docs if d["doc_type"] == "readme" and "/" not in d["path"]), None) or \
        next((d for d in docs if d["doc_type"] == "readme"), None)
    readme_text = _doc_text(db, readme["id"], 30000) if readme else ""
    page = next((d for d in docs if d["doc_type"] in ("project_page",)), None)
    page_text = _doc_text(db, page["id"], 12000) if page else ""
    headings = " ".join(r[0] for r in db.all(
        "SELECT DISTINCT c.heading FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.project_id=? AND d.status='active' "
        "AND c.heading != '' LIMIT 400", (project_id,)) if r[0])
    titles = " ".join(d["title"] or "" for d in docs)
    report_docs = [d for d in docs if d["doc_type"] in ("report", "memo", "paper", "published_research")][:8]
    report_text = " ".join(_doc_text(db, d["id"], 8000) for d in report_docs)
    files = db.all("SELECT f.path, f.role FROM file_index f JOIN sources s ON s.id=f.source_id WHERE s.project_id=? AND f.status='active'",
                   (project_id,))
    file_names = " ".join(re.sub(r"[_\-/.]", " ", f["path"]) for f in files[:3000])

    # code: imports + research-practice patterns
    code_texts, imports = [], Counter()
    for s in sources:
        root = root_of_source(s)
        if not root:
            continue
        for f in db.all("SELECT path FROM file_index WHERE source_id=? AND role='code' AND status='active' LIMIT 250", (s["id"],)):
            try:
                txt = read_text(Path(root) / f["path"], limit=200_000)
            except OSError:
                continue
            if f["path"].endswith(".py"):
                for m in IMPORT_RE.finditer(txt):
                    imports[(m.group(1) or m.group(2))] += 1
            code_texts.append((f["path"], txt))
    weighted = [(name.replace("-", " ").replace("_", " "), 3.0), (" ".join(descs), 3.0), (readme_text, 1.0), (page_text, 1.5),
                (headings, 1.0), (titles, 1.5), (report_text, 0.6), (file_names, 0.3)]
    cats = tax.score_categories(weighted)
    for mod, n in imports.items():
        if mod in tax.imports:
            ck, w = tax.imports[mod]
            c = cats.setdefault(ck, {"score": 0.0, "subtopics": Counter(), "terms": Counter()})
            c["score"] += float(w)
            c["terms"][f"import {mod}"] += n
    scores = {k: round(v["score"], 2) for k, v in cats.items()}
    kind = _kind(files, docs)
    if any(s["kind"] == "website" for s in sources):
        kind = "publication"
    invest = {k: v for k, v in scores.items() if tax.is_investment(k)}
    top_inv = max(invest.items(), key=lambda kv: kv[1]) if invest else (None, 0.0)
    game = scores.get("game_theory", 0.0)
    tools = scores.get("research_tools", 0.0)
    career = scores.get("career", 0.0)
    text_docs = [d for d in docs if d["doc_type"] not in ("result",)]
    last_activity = max([s["pushed_at"] or "" for s in sources] + [s["last_changed"] or "" for s in sources] +
                        [d["modified"] or "" for d in docs] + [""])
    recent = _days_since(last_activity) is not None and _days_since(last_activity) <= 21
    if kind == "document_library":
        relevance, category = "reference", "reference"
    elif kind == "publication":
        relevance, category = "publication", "research_tools"
    elif (kind == "empty" or not text_docs) and (recent or kind == "empty") and not any(len(d or "") > 25 for d in descs):
        # nothing written yet (often a project another session is creating right now): provisional, re-classified on change
        relevance = "pending"
        category = top_inv[0] if top_inv[1] > 0 else ("game_theory" if game > 0 else "unclassified")
    elif career >= 8 and career >= top_inv[1] * 0.6:
        relevance, category = "career", "career"
    elif game >= GAME_MIN and game >= top_inv[1] * 0.8:
        relevance, category = "game_theory", "game_theory"
    elif top_inv[1] >= INVEST_MIN:
        relevance, category = "investment", top_inv[0]
    elif recent and top_inv[1] >= 2.5 and len(text_docs) <= 3:
        # a young project with a real but thin signal: provisional until more is written
        relevance, category = "pending", top_inv[0]
    elif tools >= TOOLS_MIN or game >= GAME_MIN:
        relevance, category = ("game_theory", "game_theory") if game >= GAME_MIN else ("tooling", "research_tools")
    else:
        relevance, category = "not_relevant", "not_relevant"
    secondary = [k for k, v in sorted(invest.items(), key=lambda kv: -kv[1]) if k != category and v >= max(INVEST_MIN * 0.6, 0.35 * top_inv[1])]
    subtopics = []
    for ck in [category] + secondary:
        if ck in cats:
            for sk, sv in sorted(cats[ck]["subtopics"].items(), key=lambda kv: -kv[1])[:4]:
                if sv >= 2.0:
                    subtopics.append({"category": ck, "key": sk, "label": tax.subtopic_label(ck, sk), "score": round(sv, 2)})
    evidence = {k: [t for t, _ in v["terms"].most_common(8)] for k, v in cats.items()}
    # themes from mentions across the project's documents
    themes = []
    for r in db.all("""SELECT e.key, e.name, SUM(m.count) AS n, COUNT(DISTINCT m.document_id) AS d FROM mentions m
                       JOIN entities e ON e.id=m.entity_id JOIN documents doc ON doc.id=m.document_id
                       WHERE doc.project_id=? AND e.kind='theme' GROUP BY e.id ORDER BY n DESC""", (project_id,)):
        if r["n"] >= 4:
            themes.append({"key": r["key"].split(":", 1)[1], "label": r["name"], "score": int(r["n"]), "documents": r["d"]})
    data_sources = [{"key": r["key"].split(":", 1)[1], "label": r["name"], "mentions": int(r["n"])} for r in db.all(
        """SELECT e.key, e.name, SUM(m.count) AS n FROM mentions m JOIN entities e ON e.id=m.entity_id
           JOIN documents doc ON doc.id=m.document_id WHERE doc.project_id=? AND e.kind='data_source'
           GROUP BY e.id HAVING n >= 2 ORDER BY n DESC LIMIT 15""", (project_id,))]
    tech = Counter()
    for s in sources:
        for l in loads(s["languages"], []) or []:
            tech[l] += 5
    for f in files:
        ext = Path(f["path"]).suffix.lower()
        if ext in LANG_EXT:
            tech[LANG_EXT[ext]] += 1
    for mod, n in imports.items():
        if mod in TECH:
            tech[TECH[mod]] += n
    technologies = [t for t, _ in tech.most_common(14)]
    # capabilities
    paths = [f["path"].lower() for f in files]
    caps = {
        "tests": any(p.startswith("tests/") or "/tests/" in p or re.search(r"(^|/)test_[^/]+\.py$", p) for p in paths),
        "dashboard": any("dashboard" in p or p.endswith("app.py") for p in paths),
        "financial_models": bool(db.scalar("SELECT COUNT(*) FROM financial_models WHERE project_id=?", (project_id,), 0)),
        "model_artifacts": sum(1 for f in files if f["role"] == "artifact"),
        "reports": sum(1 for d in docs if d["doc_type"] in ("report", "memo", "paper", "published_research")),
        "experiments": int(db.scalar("SELECT COUNT(*) FROM experiments WHERE project_id=? AND status='active'", (project_id,), 0)),
        "theses": int(db.scalar("SELECT COUNT(*) FROM theses WHERE project_id=? AND status='active'", (project_id,), 0)),
        "signal_sources": int(db.scalar("SELECT COUNT(*) FROM signal_sources WHERE project_id=? AND status='active'", (project_id,), 0)),
        "preregistered": bool(re.search(r"pre-?registered|preregistered", readme_text + report_text, re.I)),
        "manifest": any(p.endswith(("rts.yaml", "rts.yml")) for p in paths),
        "pinned_requirements": any(p.endswith(("requirements.txt", "requirements-lock.txt", "cargo.lock", "pyproject.toml")) for p in paths),
    }
    # research-practice flags at project level
    hyps = int(db.scalar("SELECT COUNT(DISTINCT COALESCE(hypothesis, name)) FROM experiments WHERE project_id=? AND status='active'",
                         (project_id,), 0))
    flags = X.practice_flags(readme_text + "\n" + report_text + "\n" + page_text, code_texts, hyps) if relevance == "investment" else []
    # status from activity
    status = _status(last_activity, len(files), proj["first_seen"])
    if not files and docs:
        status = "published"            # known only from its published page (no repository or folder visible)
    if relevance == "pending" and status not in ("empty",):
        status = "in_progress"
    summary = _summary(page, page_text, descs, readme_text, db, project_id)
    vis = "public" if any(s["visibility"] == "public" for s in sources) or any(d["visibility"] == "public" for d in docs) else "private"
    repo = next((s for s in sources if s["kind"] == "github"), None)
    fields = {"summary": summary, "category": category, "secondary": dumps(secondary), "scores": dumps(scores),
              "subtopics": dumps(subtopics[:10]), "themes": dumps(themes[:10]), "relevance": relevance,
              "relevance_score": round(top_inv[1] if relevance == "investment" else max(game, tools, top_inv[1]), 2),
              "evidence": dumps({"terms": evidence, "flags": flags, "imports": dict(imports.most_common(20))}), "kind": kind,
              "technologies": dumps(technologies), "data_sources": dumps(data_sources), "capabilities": dumps(caps),
              "visibility": vis, "repo_url": repo["url"] if repo else None, "status": status,
              "last_activity": last_activity or None, "last_synced": now_iso()}
    # optional SDK manifest (rts.yaml at the project root) states what the project is; config overrides win over it
    manifest = _manifest(db, sources, root_of_source)
    if manifest:
        if manifest.get("category") in tax.categories:
            fields["category"] = manifest["category"]
            fields["relevance"] = "investment" if tax.is_investment(manifest["category"]) else (
                "game_theory" if manifest["category"] == "game_theory" else "tooling")
        if manifest.get("summary"):
            fields["summary"] = str(manifest["summary"])[:500]
        if manifest.get("name"):
            fields["name"] = str(manifest["name"])[:120]
        caps["manifest"] = True
        fields["capabilities"] = dumps(caps)
        for sym in manifest.get("companies") or []:
            eid = db.scalar("SELECT id FROM entities WHERE key=?", (f"company:{str(sym).upper()}",))
            if eid:
                db.execute("""INSERT INTO relations(src_kind,src_id,predicate,dst_kind,dst_id,weight,basis,evidence,created_at,updated_at)
                              VALUES('project',?,'covers','company',?,1.0,'manual',?,?,?) ON CONFLICT(src_kind,src_id,predicate,dst_kind,dst_id)
                              DO UPDATE SET basis='manual', updated_at=excluded.updated_at""",
                           (project_id, eid, dumps({"from": "rts.yaml"}), now_iso(), now_iso()))
    ov = (overrides.get("projects") or {}).get(proj["slug"]) or {}
    for k in ("category", "relevance", "summary", "name", "status"):
        if ov.get(k):
            fields[k] = ov[k]
    if ov or manifest:
        fields["override"] = dumps({**({"manifest": manifest} if manifest else {}), **ov})
    db.update("projects", project_id, fields)
    _contextualise_result_experiments(db, project_id, readme_text + "\n" + report_text, flags)
    db.commit()
    return fields


ARTIFACT_KIND = {".pt": "PyTorch checkpoint", ".pth": "PyTorch checkpoint", ".onnx": "ONNX model", ".cbm": "CatBoost model",
                 ".lgb": "LightGBM model", ".pkl": "pickled model", ".joblib": "joblib model", ".npz": "NumPy model arrays",
                 ".h5": "HDF5/Keras model"}
WEIGHT_FORMATS = {".pt", ".pth", ".onnx", ".cbm", ".lgb"}          # formats that only ever hold model weights
# .pkl/.joblib/.npz/.h5 hold data caches and backtest result objects at least as often as fitted models, so they count
# only when the path itself says "model". ("weights" is left out: in this workspace it usually means portfolio weights.)
MODEL_WORD = re.compile(r"(?<![a-z])(models?|checkpoints?|ckpts?|estimators?|fitted|trained|classifiers?|clf|regressors?|"
                        r"polic(?:y|ies)|agents?|boosters?|scalers?|encoders?|vectori[sz]ers?|tokeni[sz]ers?)(?![a-z])")
DATA_WORD = re.compile(r"(?<![a-z])(inputs?|data(?:set)?|panel|features?|cache|chunks?|prices|returns|events|filings|vintages|"
                       r"quotes|trades|logs?|results?|selections?|state)(?![a-z])")
RUN_FAMILY_MIN = 3        # this many sibling folders with the same kind of file = one experiment's runs, not separate models


def is_model_artifact(path: str) -> bool:
    ext = Path(path).suffix.lower()
    if ext not in ARTIFACT_KIND:
        return False
    if ext in WEIGHT_FORMATS:
        return True
    low = path.lower().replace("\\", "/")
    return bool(MODEL_WORD.search(low)) and not DATA_WORD.search(Path(low).stem)


def run_facets(names: list[str]) -> list[dict]:
    """'a2c__wf1_2018__s0' style run names -> the distinct values at each position (agents, windows, seeds, ...)."""
    parts = [n.split("__") for n in names]
    width = Counter(len(p) for p in parts).most_common(1)[0][0] if parts else 0
    if width < 2:
        return []
    cols = [sorted({p[i] for p in parts if len(p) == width}) for i in range(width)]
    return [{"n": len(c), "values": c[:8]} for c in cols]


def refresh_model_registry(db: DB, project_id: int, slug: str) -> int:
    """Saved model files -> model-registry entries: one per folder and file type, with the run folders of one
    experiment (3+ siblings, e.g. runs/controls/<agent>__<window>__<seed>/model.pt) collapsed into one entry."""
    def blank():
        return {"n": 0, "size": 0, "mtime": 0.0, "examples": [], "runs": []}

    leaves: dict = {}
    for f in db.all("""SELECT f.path, f.size, f.mtime FROM file_index f JOIN sources s ON s.id=f.source_id
                       WHERE s.project_id=? AND f.role='artifact' AND f.status='active' ORDER BY f.path""", (project_id,)):
        if not is_model_artifact(f["path"]):
            continue
        folder, _, name = f["path"].rpartition("/")
        g = leaves.setdefault((folder, Path(name).suffix.lower()), blank())
        g["n"] += 1
        g["size"] += f["size"] or 0
        g["mtime"] = max(g["mtime"], f["mtime"] or 0)
        g["examples"].append(f["path"])
    siblings = Counter((folder.rpartition("/")[0], ext) for folder, ext in leaves if "/" in folder)
    groups: dict = {}
    for (folder, ext), g in leaves.items():
        parent, _, leaf = folder.rpartition("/")
        if parent and siblings[(parent, ext)] >= RUN_FAMILY_MIN:
            tgt = groups.setdefault((parent, ext), blank())
            tgt["runs"].append(leaf)
        else:
            tgt = groups.setdefault((folder or ".", ext), blank())
        tgt["n"] += g["n"]
        tgt["size"] += g["size"]
        tgt["mtime"] = max(tgt["mtime"], g["mtime"])
        tgt["examples"] += g["examples"][: max(0, 3 - len(tgt["examples"]))]
    first_seen = {r["key"]: r["first_seen"] for r in db.all("SELECT key, first_seen FROM models WHERE project_id=?", (project_id,))}
    db.execute("DELETE FROM models WHERE project_id=?", (project_id,))
    for (folder, ext), g in groups.items():
        key = f"{slug}:{folder}:{ext}"
        meta = {"bytes": g["size"], "examples": g["examples"]}
        if in_superseded_folder(folder + "/"):
            meta["archived"] = True
        if g["runs"]:
            meta |= {"runs": len(g["runs"]), "facets": run_facets(g["runs"])}
        db.insert("models", {"key": key, "project_id": project_id, "kind": ARTIFACT_KIND[ext], "name": folder, "path": folder,
                             "framework": ext.lstrip("."), "artifacts": g["n"],
                             "modified": dt.datetime.fromtimestamp(g["mtime"], dt.timezone.utc).replace(microsecond=0).isoformat() if g["mtime"] else None,
                             "meta": dumps(meta), "first_seen": first_seen.get(key) or now_iso(), "last_seen": now_iso()})
    return len(groups)


def refresh_model_registry_all(db: DB) -> int:
    """Cheap (one query per project), so it runs on every sync: rule changes apply everywhere without re-extraction."""
    n = sum(refresh_model_registry(db, p["id"], p["slug"]) for p in db.all("SELECT id, slug FROM projects"))
    db.commit()
    return n


def _manifest(db: DB, sources, root_of_source) -> dict:
    import yaml
    for s in sources:
        root = root_of_source(s)
        if not root:
            continue
        for name in ("rts.yaml", "rts.yml"):
            p = Path(root) / name
            if p.exists():
                try:
                    data = yaml.safe_load(read_text(p)) or {}
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}
    return {}


def _doc_text(db: DB, doc_id: int, limit: int) -> str:
    out, n = [], 0
    for r in db.all("SELECT heading, text FROM chunks WHERE document_id=? ORDER BY seq", (doc_id,)):
        piece = (("# " + r["heading"].split(" > ")[-1] + "\n") if r["heading"] else "") + r["text"]
        out.append(piece)
        n += len(piece)
        if n >= limit:
            break
    return "\n".join(out)[:limit]


def _kind(files, docs) -> str:
    if not files and not docs:
        return "empty"
    roles = Counter(f["role"] for f in files)
    pdfs = sum(1 for f in files if f["path"].lower().endswith(".pdf"))
    has_readme = any(f["path"].lower().startswith("readme") for f in files)
    if not has_readme and roles.get("code", 0) == 0 and pdfs >= 5 and pdfs >= 0.8 * max(len(files), 1):
        return "document_library"
    if roles.get("code", 0) >= 3:
        return "code_project"
    return "research_folder"


def _days_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


def _status(last_activity: str, n_files: int, first_seen: str | None) -> str:
    if n_files == 0:
        return "empty"
    try:
        last = dt.datetime.fromisoformat(last_activity.replace("Z", "+00:00")) if last_activity else None
    except ValueError:
        last = None
    now = dt.datetime.now(dt.timezone.utc)
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    if first_seen:
        try:
            fs = dt.datetime.fromisoformat(first_seen)
            if (now - fs).days < 2:
                return "new"
        except ValueError:
            pass
    if last is None:
        return "unknown"
    days = (now - last).days
    if days <= 14:
        return "active"
    if days <= 90:
        return "recent"
    return "dormant"


def _summary(page, page_text, descs, readme_text, db, project_id) -> str:
    if page:
        meta = loads(db.scalar("SELECT meta FROM documents WHERE id=?", (page["id"],)), {}) or {}
        if meta.get("subtitle"):
            return str(meta["subtitle"])[:500]
    for d in descs:
        if d and len(d) > 25:
            return d[:500]
    for para in re.split(r"\n\s*\n", readme_text):
        p = para.strip()
        if re.match(r"^(?:[-*•]|\d{1,2}[.)])\s", p):      # a list is not a summary
            continue
        if len(p) > 70 and not p.startswith(("#", "|", "```", "!", "<", "*Status", "> ")):
            p = re.sub(r"[*_`]", "", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", p))
            return re.sub(r"\s+", " ", p)[:500]
    return descs[0] if descs else ""


def _contextualise_result_experiments(db: DB, project_id: int, text: str, flags: list) -> None:
    """Results-file experiments carry no prose; give them the project's documented context (marked as such)."""
    ctxd = X.doc_context(text)
    for e in db.all("SELECT id, flags, metrics FROM experiments WHERE project_id=? AND source_kind='results_file' AND status='active'",
                    (project_id,)):
        fl = [f for f in (loads(e["flags"], []) or []) if f.get("flag") not in ("costs_not_stated", "in_sample_only")]
        metrics = loads(e["metrics"], {}) or {}
        has_return = any(k in metrics for k in ("sharpe", "alpha", "cagr", "ann_return", "total_return", "pnl"))
        if has_return and not ctxd["costs"]:
            fl.append({"flag": "costs_not_stated", "status": "possible", "detail": "No cost language in the project's documents."})
        if has_return and not ctxd["oos"]:
            fl.append({"flag": "in_sample_only", "status": "possible", "detail": "No out-of-sample language in the project's documents."})
        repro = {"oos_scope": "document" if ctxd["oos"] else None, "context": "project documents"}
        db.execute("UPDATE experiments SET result_kind=?, costs=?, periods=?, flags=?, reproducibility=? WHERE id=?",
                   (ctxd["result_kind"], ctxd["costs"], dumps(ctxd["periods"]), dumps(fl), dumps(repro), e["id"]))
    proj_flags = [f for f in flags if f.get("status") in ("possible", "review")]
    if proj_flags:
        for e in db.all("SELECT id, flags FROM experiments WHERE project_id=? AND status='active'", (project_id,)):
            fl = loads(e["flags"], []) or []
            have = {f.get("flag") for f in fl}
            fl += [dict(f, scope="project") for f in proj_flags if f["flag"] not in have]
            db.execute("UPDATE experiments SET flags=? WHERE id=?", (dumps(fl), e["id"]))
