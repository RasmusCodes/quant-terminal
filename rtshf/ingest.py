"""Document ingestion: change detection per file, parsing, chunking, entity mentions and per-document extraction."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .connectors.files import FileEntry
from .db import DB
from .entities import EntityLinker, add_discovered, entity_id
from .extract import experiments as X
from .extract import findings as F
from .extract import finmodels as M
from .extract import signals_sniffer as S
from .extract import theses as T
from .extract.common import section_role
from .taxonomy import Taxonomy
from .textproc import FORMATS, Parsed, chunk_text, parse_file
from .util import dumps, file_sha1, in_superseded_folder, loads, log, now_iso, read_text, sha1

TEXT_DOC_TYPES_WITH_THESES = {"published_research", "memo", "report", "readme", "deck", "note", "paper", "essay", "doc", "notebook"}


@dataclass
class Ctx:
    db: DB
    settings: object
    tax: Taxonomy
    linker: EntityLinker
    run_id: int
    http: object = None
    changed_projects: set = field(default_factory=set)
    stats: Counter = field(default_factory=Counter)
    record: bool = True        # False while re-extracting unchanged content (new rules are not research updates)

    CONTENT_KINDS = {"thesis_version", "experiment_metrics_changed", "model_outputs_changed", "changed_doc", "new_doc",
                     "changed_result", "changed_page", "new_page"}

    def change(self, source_key: str, project_id: int | None, kind: str, path: str = "", detail=None):
        if project_id:
            self.changed_projects.add(project_id)
        if not self.record and kind in self.CONTENT_KINDS:
            self.stats["reextracted"] += 1
            return
        self.db.execute("INSERT INTO changes(run_id,source_key,project_id,kind,path,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                        (self.run_id, source_key, project_id, kind, path, dumps(detail) if detail is not None else None, now_iso()))
        self.stats[f"change:{kind}"] += 1

    def error(self, source_key: str, stage: str, exc: Exception | str, detail: str | None = None):
        msg = str(exc)[:500]
        log.warning("%s [%s]: %s", source_key, stage, msg)
        self.db.execute("INSERT INTO sync_errors(run_id,source_key,stage,message,detail,created_at) VALUES(?,?,?,?,?,?)",
                        (self.run_id, source_key, stage, msg, detail, now_iso()))
        self.stats["errors"] += 1


# ----------------------------------------------------------------------------------------- doc typing
def classify_doc(rel: str, fmt: str, title: str = "") -> tuple[str, bool]:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    parts = low.split("/")[:-1]
    superseded = in_superseded_folder(low)
    t = (title or "").lower()
    if fmt == "xlsx":
        return "model", superseded
    if fmt == "pptx":
        return "deck", superseded
    if fmt == "ipynb":
        return "notebook", superseded
    if name.startswith("readme"):
        return "readme", superseded
    if re.search(r"(memo|thesis|pitch|investment[_ -]case|deep[_ -]?dive|stock[_ -]report|one-?pager|strategy\.md$)", name) or re.search(r"(investment memo|deep dive|stock report|short thesis|long thesis)", t):
        return "memo", superseded
    if "paper" in parts or name.startswith("paper") or re.search(r"_paper\b|paper\.", name):
        return "paper", superseded
    if re.search(r"(plan|roadmap|todo|milestone|architecture|spec|design|decision|adr[-_]|changelog|contributing|setup|install|"
                 r"testing|deployment|data_dictionary|schema|risks\.md$)", name) or "adr" in parts:
        return "design_doc", superseded
    if re.search(r"(sources|source_map|data_quality|coverage|survey|landscape|glossary|methodology)", name):
        return "technical_doc", superseded
    if re.search(r"(report|study|analysis|results|findings|note|summary|update|review|research|tables|weekly|daily|brief)", name) \
            or any(p in ("reports", "report", "out", "output", "outputs", "results") for p in parts):
        return "report", superseded
    if "notes" in parts or "note" in parts:
        return "note", superseded
    return "doc", superseded


FILENAME_TICKER = re.compile(r"(?:^|[_\-\s/(])([A-Z]{2,5})(?=[_\-\s.)]|$)")


def filename_symbol(rel: str, known: set[str]) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    for m in FILENAME_TICKER.finditer(name):
        if m.group(1) in known:
            return m.group(1)
    return None


# ----------------------------------------------------------------------------------------- storage
def store_text_document(ctx: Ctx, *, source: dict, project_id: int | None, rel: str, url: str | None, visibility: str,
                        parsed: Parsed, fmt: str, doc_type: str, superseded: bool, size: int, modified: str | None,
                        content_hash: str, extra_meta: dict | None = None) -> tuple[int, list]:
    db = ctx.db
    meta = dict(parsed.meta or {})
    meta.update(extra_meta or {})
    meta = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool, list, dict)) or v is None}
    existing = db.one("SELECT id, first_seen FROM documents WHERE source_id=? AND path=?", (source["id"], rel))
    row = {"source_id": source["id"], "project_id": project_id, "path": rel, "url": url, "title": (parsed.title or rel.rsplit("/", 1)[-1])[:300],
           "doc_type": doc_type, "fmt": fmt, "date": parsed.date, "modified": modified, "size": size, "content_hash": content_hash,
           "visibility": visibility, "meta": dumps(meta), "text_len": len(parsed.text or ""), "superseded": int(superseded),
           "last_seen": now_iso(), "status": "active"}
    if existing:
        doc_id = existing["id"]
        db.update("documents", doc_id, row)
        purge_document_content(db, doc_id)
    else:
        row["first_seen"] = now_iso()
        doc_id = db.insert("documents", row)
    chunks = chunk_text(parsed.text or "", max_chars=int(ctx.settings.get("search.max_chunk_chars", 1400)))
    title = row["title"]
    stored = []
    for ch in chunks:
        cid = db.insert("chunks", {"document_id": doc_id, "seq": ch.seq, "heading": ch.heading[:300], "text": ch.text,
                                   "line_start": ch.line_start, "line_end": ch.line_end})
        db.execute("INSERT INTO chunk_fts(rowid, title, heading, text) VALUES(?,?,?,?)", (cid, title, ch.heading, ch.text))
        ch.id = cid
        stored.append(ch)
    # entity mentions
    company_counts: Counter = Counter()
    for ch in stored:
        found = ctx.linker.companies(ch.text)
        head_found = ctx.linker.companies(ch.heading) if ch.heading else Counter()
        for sym, n in (found + head_found).items():
            eid = entity_id(db, f"company:{sym}")
            if eid:
                db.execute("INSERT OR REPLACE INTO mentions(entity_id,chunk_id,document_id,count,in_heading) VALUES(?,?,?,?,?)",
                           (eid, ch.id, doc_id, n, 1 if sym in head_found else 0))
                company_counts[sym] += n + (2 if sym in head_found else 0)
        for key, score in ctx.linker.themes(ch.text).items():
            eid = entity_id(db, f"theme:{key}")
            if eid:
                db.execute("INSERT OR REPLACE INTO mentions(entity_id,chunk_id,document_id,count,in_heading) VALUES(?,?,?,?,?)",
                           (eid, ch.id, doc_id, max(1, int(round(score))), 0))
        for key, n in ctx.linker.data_sources(ch.text).items():
            eid = entity_id(db, f"data:{key}")
            if eid:
                db.execute("INSERT OR REPLACE INTO mentions(entity_id,chunk_id,document_id,count,in_heading) VALUES(?,?,?,?,?)",
                           (eid, ch.id, doc_id, n, 0))
    # securities written as "Name (NASDAQ: SYM)" that the master does not know yet
    for sym, name in ctx.linker.discover(parsed.text[:300000] if parsed.text else ""):
        add_discovered(db, sym, name)
        ctx.stats["securities_discovered"] += 1
    title_syms = ctx.linker.companies(title)
    for sym in title_syms:
        company_counts[sym] += 5
    db.update("documents", doc_id, {"meta": dumps({**meta, "companies": dict(company_counts.most_common(12))})})
    ctx.stats["documents"] += 1
    ctx.stats["chunks"] += len(stored)
    return doc_id, stored


def purge_document_content(db: DB, doc_id: int) -> None:
    ids = [r[0] for r in db.all("SELECT id FROM chunks WHERE document_id=?", (doc_id,))]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        db.execute(f"DELETE FROM chunk_fts WHERE rowid IN ({','.join('?' * len(part))})", part)
    db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
    db.execute("DELETE FROM mentions WHERE document_id=?", (doc_id,))
    db.execute("UPDATE findings SET status='stale' WHERE document_id=? AND status='active'", (doc_id,))
    db.execute("UPDATE experiments SET status='stale' WHERE document_id=? AND status IN ('active','superseded')", (doc_id,))


def refresh_superseded(db: DB) -> dict:
    """Old versions (archive/, *_v1/, *_pre_fix/ folders) keep their history but stop counting as current evidence:
    the document gets the flag and its experiments the status 'superseded'. Runs every sync, both directions."""
    flipped = 0
    for d in db.all("""SELECT d.id, d.path, d.superseded FROM documents d JOIN sources s ON s.id=d.source_id
                       WHERE s.kind != 'website' AND d.status='active'"""):
        want = int(in_superseded_folder(d["path"] or ""))
        if want != (d["superseded"] or 0):
            db.execute("UPDATE documents SET superseded=? WHERE id=?", (want, d["id"]))
            flipped += 1
    old = "document_id IN (SELECT id FROM documents WHERE superseded=1)"
    n_old = db.scalar(f"SELECT COUNT(*) FROM experiments WHERE status='active' AND {old}", default=0)
    n_back = db.scalar(f"SELECT COUNT(*) FROM experiments WHERE status='superseded' AND NOT {old}", default=0)
    db.execute(f"UPDATE experiments SET status='superseded' WHERE status='active' AND {old}")
    db.execute(f"UPDATE experiments SET status='active' WHERE status='superseded' AND NOT {old}")
    db.commit()
    return {"documents_flipped": flipped, "experiments_superseded": n_old, "experiments_restored": n_back}


def finalize_document(db: DB, doc_id: int) -> None:
    """Rows not re-confirmed by the latest extraction of this document are marked removed (history is kept)."""
    db.execute("UPDATE findings SET status='removed', last_seen=? WHERE document_id=? AND status='stale'", (now_iso(), doc_id))
    db.execute("UPDATE experiments SET status='removed', last_seen=? WHERE document_id=? AND status='stale'", (now_iso(), doc_id))


def remove_document(ctx: Ctx, doc: dict, source_key: str) -> None:
    db = ctx.db
    purge_document_content(db, doc["id"])
    finalize_document(db, doc["id"])
    db.execute("UPDATE documents SET status='removed', last_seen=? WHERE id=?", (now_iso(), doc["id"]))
    db.execute("UPDATE theses SET status='removed' WHERE document_id=? AND status IN ('active','superseded')", (doc["id"],))
    ctx.change(source_key, doc["project_id"], "removed_doc", doc["path"])


# ----------------------------------------------------------------------------------------- extraction per document
def dominant_symbol(counts: dict, min_mentions: int = 3, share: float = 0.4) -> str | None:
    if not counts:
        return None
    total = sum(counts.values())
    sym, n = max(counts.items(), key=lambda kv: kv[1])
    return sym if n >= min_mentions and n / max(total, 1) >= share else None


def extract_from_text_doc(ctx: Ctx, *, doc_id: int, project_id: int | None, source_key: str, rel: str, doc_type: str,
                          parsed: Parsed, chunks: list, facts: list | None = None, symbol_hint: str | None = None,
                          superseded: bool = False) -> None:
    db = ctx.db
    doc_key = f"{source_key}|{rel}"
    counts = (loads(db.scalar("SELECT meta FROM documents WHERE id=?", (doc_id,)), {}) or {}).get("companies") or {}
    # findings (a statement that names no company inherits the document's main company, marked in the rule)
    doc_company = symbol_hint or dominant_symbol(counts, min_mentions=3, share=0.5)
    for f in F.extract(chunks, doc_type, facts=facts):
        chunk_id = next((c.id for c in chunks if c.seq == f.chunk_seq), None) if f.chunk_seq is not None else None
        comps = list(ctx.linker.companies(f.text).keys())
        if not comps and doc_company:
            comps = [doc_company]
            f.rule += "|doc-company"
        themes = list(ctx.linker.themes(f.text).keys())
        fp = sha1(doc_key, re.sub(r"\W+", " ", f.text.lower()))
        row = {"fingerprint": fp, "project_id": project_id, "document_id": doc_id, "chunk_id": chunk_id, "text": f.text,
               "claim_type": f.claim_type, "evidence_kind": f.evidence_kind, "section": f.section, "polarity": f.polarity,
               "numbers": dumps(f.numbers), "entities": dumps(comps), "themes": dumps(themes), "confidence": f.confidence,
               "rule": f.rule, "line": f.line, "importance": f.importance * (0.6 if superseded else 1.0),
               "last_seen": now_iso(), "status": "active"}
        if db.one("SELECT 1 FROM findings WHERE fingerprint=?", (fp,)):
            db.upsert("findings", row, ["fingerprint"])
        else:
            row["first_seen"] = now_iso()
            db.insert("findings", row)
            ctx.stats["findings_new"] += 1
    # theses: a structured call (front matter / research sheet) first, else an explicit call in the owner's own outputs
    th = None
    meta = parsed.meta or {}
    call = meta.get("call") if isinstance(meta.get("call"), dict) else (
        {k: meta.get(k) for k in ("rating", "rating_label", "conviction", "horizon", "currency", "price", "price_date", "target",
                                  "target_label", "weighted", "weighted_label", "scenarios") if meta.get(k) not in (None, "")}
        if meta.get("rating") else None)
    symbol = symbol_hint or dominant_symbol(counts, min_mentions=5, share=0.5)
    if call and symbol:
        th = T.from_structured(call, symbol, chunks, parsed.title or "")
    elif doc_type in TEXT_DOC_TYPES_WITH_THESES and T.eligible(rel, doc_type, parsed.title or ""):
        th = T.extract(parsed.text or "", chunks, parsed.title or "", symbol_hint, dominant_symbol(counts, min_mentions=5, share=0.5))
    if th is None or not th.symbol:
        # this document no longer states a call (edited, or extracted under older rules): retire, keep history
        db.execute("UPDATE theses SET status='removed', last_seen=? WHERE document_id=? AND status IN ('active','superseded')",
                   (now_iso(), doc_id))
    if th is not None:
        if th and th.symbol:
            fp = sha1(doc_key, th.stance, th.target, th.symbol)
            eid = entity_id(db, f"company:{th.symbol}")
            row = {"fingerprint": fp, "entity_id": eid, "symbol": th.symbol, "project_id": project_id, "document_id": doc_id,
                   "stance": th.stance, "rating": th.rating, "target_price": th.target, "price_at_thesis": th.spot,
                   "currency": (th.extra.get("currency") or "USD"), "upside": th.upside, "scenarios": dumps(th.scenarios),
                   "horizon": th.horizon, "thesis_date": parsed.date, "summary": th.summary, "arguments": dumps(th.arguments),
                   "risks": dumps(th.risks), "catalysts": dumps(th.catalysts), "assumptions": dumps(th.assumptions),
                   "method": th.method, "meta": dumps({**th.extra, "line": th.line}),
                   "status": "superseded" if superseded else "active", "last_seen": now_iso()}
            prev = db.one("SELECT id FROM theses WHERE fingerprint=?", (fp,))
            if prev:
                db.update("theses", prev["id"], row)
                db.execute("UPDATE theses SET status='superseded' WHERE document_id=? AND id != ? AND status='active'", (doc_id, prev["id"]))
            else:
                older = db.all("SELECT id, version FROM theses WHERE document_id=? AND status IN ('active','removed')", (doc_id,))
                for o in older:
                    db.update("theses", o["id"], {"status": "superseded"})
                row["version"] = (max([o["version"] or 1 for o in older]) + 1) if older else 1
                row["first_seen"] = now_iso()
                db.insert("theses", row)
                ctx.stats["theses_new"] += 1
                ctx.change(source_key, project_id, "thesis_version", rel, {"symbol": th.symbol, "stance": th.stance, "target": th.target,
                                                                           "previous": [o["id"] for o in older]})
    # experiments: tables + text results, with document context
    exps = []
    if parsed.text and doc_type not in ("design_doc",):
        if (rel.lower().endswith((".md", ".markdown")) or doc_type in ("published_research", "project_page")):
            exps += X.from_markdown_tables(parsed.text, rel)
        exps += X.from_text(chunks, rel)
    if exps:
        ctxd = X.doc_context(parsed.text or "")
        for e in exps:
            store_experiment(ctx, e, project_id, doc_id, doc_key, ctxd, parsed.date)
    finalize_document(db, doc_id)


def store_experiment(ctx: Ctx, e: X.Experiment, project_id, doc_id, doc_key: str, ctxd: dict, run_date: str | None) -> None:
    db = ctx.db
    fp = sha1(doc_key, e.name, e.source_kind, e.line if e.source_kind != "results_file" else "")
    # identical names inside one file (repeated table labels, list items) get an ordinal so they never overwrite each other
    seen = ctx.__dict__.setdefault("_exp_fps", {})
    n = seen.get(fp, 0)
    seen[fp] = n + 1
    if n:
        fp = sha1(fp, n)
    flags = []
    if ctxd.get("result_kind") == "synthetic_demo":
        flags.append({"flag": "synthetic_data", "status": "detected", "detail": "Results come from synthetic data."})
    has_return = any(k in e.metrics for k in ("sharpe", "alpha", "cagr", "ann_return", "total_return", "pnl", "sortino"))
    if has_return and not ctxd.get("costs"):
        flags.append({"flag": "costs_not_stated", "status": "possible", "detail": "Return metrics with no transaction costs mentioned."})
    # out-of-sample is claimed for THIS result only when its own row/heading/sentence says so (or an SDK file states it)
    own_text = f"{e.name} {(e.extra or {}).get('statement', '')} {(e.variant_of or '')}"
    explicit_oos = bool(((e.extra or {}).get("explicit") or {}).get("oos")) or bool(X.OOS.search(own_text) and not X.IN_SAMPLE_ONLY.search(own_text))
    oos_scope = "explicit" if explicit_oos else ("document" if ctxd.get("oos") else None)
    if has_return and not oos_scope:
        flags.append({"flag": "in_sample_only", "status": "possible", "detail": "No out-of-sample/walk-forward/holdout language found."})
    sig = {k: e.metrics[k] for k in ("t_stat", "p_value") if k in e.metrics}
    row = {"fingerprint": fp, "project_id": project_id, "document_id": doc_id, "name": e.name[:300], "hypothesis": e.hypothesis,
           "result_kind": ctxd.get("result_kind"), "datasets": None, "periods": dumps(ctxd.get("periods") or []),
           "oos": int(explicit_oos), "costs": ctxd.get("costs"), "significance": dumps(sig), "verdict": e.verdict,
           "metrics": dumps(e.metrics), "limitations": ctxd.get("limitations"),
           "reproducibility": dumps({"preregistered": ctxd.get("preregistered"), "oos_scope": oos_scope}), "flags": dumps(flags),
           "source_kind": e.source_kind, "location": e.location, "line": e.line, "run_date": run_date,
           "last_seen": now_iso(), "status": "active"}
    existing = db.one("SELECT id, metrics, last_seen FROM experiments WHERE fingerprint=?", (fp,))
    if existing:
        eid = existing["id"]
        before = loads(existing["metrics"], {}) or {}
        run_start = ctx.__dict__.setdefault("_run_start", db.scalar("SELECT started_at FROM sync_runs WHERE id=?", (ctx.run_id,)) or "")
        if before and before != e.metrics and (existing["last_seen"] or "") < run_start:
            ctx.change(doc_key.split("|", 1)[0], project_id, "experiment_metrics_changed", e.location,
                       {"name": e.name[:200], "before": before, "after": e.metrics})
        db.update("experiments", eid, row)
    else:
        row["first_seen"] = now_iso()
        eid = db.insert("experiments", row)
        ctx.stats["experiments_new"] += 1
    db.execute("DELETE FROM experiment_metrics WHERE experiment_id=?", (eid,))
    for k, v in e.metrics.items():
        if isinstance(v, (int, float)):
            db.execute("INSERT OR REPLACE INTO experiment_metrics(experiment_id,name,value,unit,raw) VALUES(?,?,?,?,?)",
                       (eid, k, float(v), None, None))


# ----------------------------------------------------------------------------------------- per-source processing
def process_files(ctx: Ctx, *, source: dict, project_id: int | None, root: Path, entries: list[FileEntry],
                  url_for, visibility_for, force: bool = False, parse_docs: bool = True) -> dict:
    """Ingest changed files of one source. url_for(rel)->url|None, visibility_for(rel)->'public'|'private'."""
    db = ctx.db
    key = source["key"]
    known = {r[0]: r for r in db.all("SELECT path, size, mtime, hash, role FROM file_index WHERE source_id=?", (source["id"],))}
    seen = set()
    out = Counter()
    known_syms = {r[0] for r in db.all("SELECT symbol FROM securities")}
    for e in entries:
        seen.add(e.rel)
        prev = known.get(e.rel)
        if prev and not force and prev["size"] == e.size and abs((prev["mtime"] or 0) - e.mtime) < 1e-3:
            continue
        p = root / e.rel
        try:
            h = file_sha1(p)
        except OSError as exc:
            ctx.error(key, "hash", exc, e.rel)
            continue
        db.execute("INSERT INTO file_index(source_id,path,size,mtime,hash,role,status,seen_at) VALUES(?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(source_id,path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, hash=excluded.hash, "
                   "role=excluded.role, status='active', seen_at=excluded.seen_at",
                   (source["id"], e.rel, e.size, e.mtime, h, e.role, "active", now_iso()))
        if prev and prev["hash"] == h and not force:
            continue
        ctx.record = (prev is None) or (prev["hash"] != h)      # same bytes under new rules = re-extraction, not an update
        if ctx.record and e.role in ("doc", "model", "result"):
            out["real"] += 1
        try:
            if e.role in ("doc", "model") and parse_docs:
                _ingest_doc(ctx, source, project_id, root, e, h, url_for(e.rel), visibility_for(e.rel), known_syms)
                ctx.change(key, project_id, "changed_doc" if prev else "new_doc", e.rel)
                out["docs"] += 1
            elif e.role == "result":
                _ingest_result(ctx, source, project_id, root, e, h, url_for(e.rel), visibility_for(e.rel))
                if prev:
                    ctx.change(key, project_id, "changed_result", e.rel)
                out["results"] += 1
            else:
                if project_id:
                    ctx.changed_projects.add(project_id)
                out[e.role] += 1
        except Exception as exc:  # one bad file never stops the sync
            ctx.error(key, f"ingest:{e.role}", exc, e.rel)
        finally:
            ctx.record = True
        if out["docs"] % 25 == 0:
            db.commit()
    # removed files
    for rel, prev in known.items():
        if rel in seen:
            continue
        db.execute("UPDATE file_index SET status='removed' WHERE source_id=? AND path=?", (source["id"], rel))
        doc = db.one("SELECT id, project_id, path FROM documents WHERE source_id=? AND path=? AND status='active'", (source["id"], rel))
        if doc:
            remove_document(ctx, dict(doc), key)
            out["removed"] += 1
            if (root / rel).exists() is False:      # the file itself is gone (not just out of scope under new rules)
                out["real"] += 1
        db.execute("DELETE FROM file_index WHERE source_id=? AND path=?", (source["id"], rel))
    db.commit()
    return dict(out)


def _ingest_doc(ctx: Ctx, source, project_id, root: Path, e: FileEntry, h: str, url, visibility, known_syms) -> None:
    import datetime as dt

    p = root / e.rel
    parsed, structure = parse_file(p, e.fmt)
    doc_type, superseded = classify_doc(e.rel, e.fmt, parsed.title)
    modified = dt.datetime.fromtimestamp(e.mtime, dt.timezone.utc).replace(microsecond=0).isoformat()
    if not parsed.date:
        parsed.date = modified[:10]
    doc_id, chunks = store_text_document(ctx, source=source, project_id=project_id, rel=e.rel, url=url, visibility=visibility,
                                         parsed=parsed, fmt=e.fmt, doc_type=doc_type, superseded=superseded, size=e.size,
                                         modified=modified, content_hash=h)
    sym_hint = filename_symbol(e.rel, known_syms)
    if e.fmt == "xlsx" and structure is not None:
        info = M.analyse(structure, p.name)
        sym = sym_hint or dominant_symbol((loads(ctx.db.scalar("SELECT meta FROM documents WHERE id=?", (doc_id,)), {}) or {}).get("companies") or {})
        prev = ctx.db.one("SELECT id, outputs FROM financial_models WHERE document_id=?", (doc_id,))
        row = {"document_id": doc_id, "project_id": project_id, "entity_id": entity_id(ctx.db, f"company:{sym}") if sym else None,
               "symbol": sym, "name": p.name, "path": e.rel, "model_type": info["model_type"], "sheets": dumps(info["sheets"]),
               "outputs": dumps(info["outputs"]), "assumptions": dumps(info["assumptions"][:60]), "content_hash": h,
               "modified": modified, "last_seen": now_iso()}
        if prev:
            if prev["outputs"] != row["outputs"]:
                row["previous_outputs"] = prev["outputs"]
                ctx.change(source["key"], project_id, "model_outputs_changed", e.rel,
                           {"before": loads(prev["outputs"], []), "after": info["outputs"]})
            ctx.db.update("financial_models", prev["id"], row)
        else:
            row["first_seen"] = now_iso()
            ctx.db.insert("financial_models", row)
        ctx.stats["models"] += 1
        return
    facts = parsed.meta.get("_facts") if parsed.meta else None
    extract_from_text_doc(ctx, doc_id=doc_id, project_id=project_id, source_key=source["key"], rel=e.rel, doc_type=doc_type,
                          parsed=parsed, chunks=chunks, facts=facts, symbol_hint=sym_hint, superseded=superseded)


def _ingest_result(ctx: Ctx, source, project_id, root: Path, e: FileEntry, h: str, url, visibility) -> None:
    db = ctx.db
    p = root / e.rel
    raw = read_text(p)
    exps, strategies = X.from_results_file(e.rel, e.fmt, raw)
    doc_key = f"{source['key']}|{e.rel}"
    if exps or strategies:
        doc = db.one("SELECT id FROM documents WHERE source_id=? AND path=?", (source["id"], e.rel))
        row = {"source_id": source["id"], "project_id": project_id, "path": e.rel, "url": url, "title": e.rel.rsplit("/", 1)[-1],
               "doc_type": "result", "fmt": e.fmt, "size": e.size, "content_hash": h, "visibility": visibility,
               "superseded": int(in_superseded_folder(e.rel)), "last_seen": now_iso(), "status": "active", "text_len": 0}
        if doc:
            doc_id = doc["id"]
            db.update("documents", doc_id, row)
            db.execute("UPDATE experiments SET status='stale' WHERE document_id=? AND status IN ('active','superseded')", (doc_id,))
        else:
            row["first_seen"] = now_iso()
            doc_id = db.insert("documents", row)
        sdk_file = e.rel.rsplit("/", 1)[-1].lower() == "rts_experiments.json"
        for ex in exps:
            # context from the project's README/report language is attached later (project pass); SDK files state it
            explicit = (ex.extra or {}).get("explicit") or {}
            ctxd = {"result_kind": explicit.get("result_kind"), "oos": bool(explicit.get("oos")), "costs": explicit.get("costs"),
                    "periods": [explicit["period"]] if explicit.get("period") else []}
            if sdk_file:
                ex.source_kind = "sdk"
            store_experiment(ctx, ex, project_id, doc_id, doc_key, ctxd, None)
        for s in strategies:
            skey = f"{source['key']}:{s['key']}"
            db.upsert("strategies", {"key": skey, "project_id": project_id, "name": s["name"], "family": s.get("family"),
                                     "description": s.get("description"), "costs_modeled": 1 if s.get("costs_modeled") else 0,
                                     "first_date": s.get("first"), "last_date": s.get("last"), "source": e.rel,
                                     "meta": dumps({}), "first_seen": now_iso(), "last_seen": now_iso()},
                      ["key"], update=["project_id", "name", "family", "description", "costs_modeled", "first_date", "last_date", "source", "last_seen"])
        finalize_document(db, doc_id)
    if e.fmt == "csv":
        m = S.sniff_csv(e.rel, raw)
        if m:
            skey = f"{source['key']}:{e.rel}"
            status = "active" if S.auto_activate(m, e.rel) else "proposed"
            prev = db.one("SELECT id, status, note FROM signal_sources WHERE key=?", (skey,))
            user_set = bool(prev and prev["note"] == "user")
            if user_set:
                status = prev["status"]               # the owner's decision always stands
            if status == "disabled":
                return
            db.upsert("signal_sources", {"key": skey, "project_id": project_id, "path": e.rel, "kind": "csv", "mapping": dumps(m),
                                         "status": status, "confidence": m["confidence"], "rows": m["rows"], "last_loaded": now_iso(),
                                         "note": "user" if user_set else "auto-detected", "created_at": now_iso()},
                      ["key"], update=["project_id", "path", "mapping", "status", "confidence", "rows", "last_loaded"])
            ctx.stats["signal_sources"] += 1
            if status != "active":
                db.execute("UPDATE signals SET active=0 WHERE strategy_id=?", (skey,))
            else:
                from . import signals as SG
                SG.ingest_table(ctx, skey, project_id, m, raw, {"source": source["key"], "path": e.rel, "url": url, "hash": h})
