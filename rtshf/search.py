"""Hybrid search: BM25 (SQLite FTS5) + dense latent-semantic vectors (TF-IDF -> truncated SVD), fused by reciprocal rank.

The dense half needs no model download or API: it is fitted on the corpus itself at sync time. If a sentence-embedding
model is installed later, `encode` is the single place to swap it in.
"""
from __future__ import annotations

import pickle
import re
from dataclasses import dataclass

import numpy as np

from .db import DB
from .util import log, loads

FTS_SAFE = re.compile(r"[^\w\s]")
DOC_TYPE_BOOST = {"published_research": 1.25, "memo": 1.25, "report": 1.15, "paper": 1.15, "project_page": 1.1, "readme": 1.05,
                  "essay": 1.0, "deck": 1.0, "note": 0.95, "notebook": 0.9, "doc": 0.9, "model": 0.85, "technical_doc": 0.75,
                  "design_doc": 0.6, "portfolio_page": 0.9}


@dataclass
class Hit:
    chunk_id: int
    score: float
    bm25: float | None = None
    dense: float | None = None


def _paths(settings):
    return settings.data_dir / "dense.npy", settings.data_dir / "dense_model.pkl"


def build_dense(db: DB, settings, rebuild: bool = True) -> int:
    vec_path, model_path = _paths(settings)
    if not rebuild and vec_path.exists() and model_path.exists():
        return 0
    rows = db.all("""SELECT c.id, c.heading, c.text, d.title FROM chunks c JOIN documents d ON d.id=c.document_id
                     WHERE d.status='active' ORDER BY c.id""")
    if len(rows) < 20:
        return 0
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    texts = [f"{r['title']} {r['heading']} {r['text']}" for r in rows]
    tfidf = TfidfVectorizer(stop_words="english", min_df=3, max_df=0.5, sublinear_tf=True, ngram_range=(1, 2), max_features=40_000,
                            dtype=np.float32)
    X = tfidf.fit_transform(texts)
    dims = int(min(settings.get("search.dense_dims", 192), X.shape[1] - 1, X.shape[0] - 1))
    svd = TruncatedSVD(n_components=max(dims, 2), random_state=7)
    Z = svd.fit_transform(X).astype(np.float32)
    Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    np.save(vec_path, Z)
    tfidf.stop_words_ = None                      # the pruned-term set is only diagnostic and dominates the pickle
    svd.components_ = svd.components_.astype(np.float32)
    with open(model_path, "wb") as f:
        pickle.dump({"tfidf": tfidf, "svd": svd, "ids": ids}, f)
    log.info("dense index: %d chunks x %d dims", Z.shape[0], Z.shape[1])
    return Z.shape[0]


_CACHE: dict = {}     # model file -> (mtime, model, vectors); one load per process, refreshed when the index is rebuilt


class Searcher:
    def __init__(self, db: DB, settings):
        self.db = db
        self.settings = settings
        self._model = None
        self._Z = None

    def _load(self):
        if self._model is None:
            vec_path, model_path = _paths(self.settings)
            if vec_path.exists() and model_path.exists():
                mtime = model_path.stat().st_mtime
                hit = _CACHE.get(str(model_path))
                if not hit or hit[0] != mtime:
                    with open(model_path, "rb") as f:
                        model = pickle.load(f)
                    hit = (mtime, model, np.load(vec_path))
                    _CACHE[str(model_path)] = hit
                self._model, self._Z = hit[1], hit[2]
        return self._model is not None

    def encode(self, text: str) -> np.ndarray | None:
        if not self._load():
            return None
        v = self._model["svd"].transform(self._model["tfidf"].transform([text])).astype(np.float32)[0]
        n = np.linalg.norm(v)
        return v / n if n > 0 else None

    def bm25(self, query: str, limit: int = 200) -> list[tuple[int, float]]:
        terms = [t for t in FTS_SAFE.sub(" ", query).split() if len(t) > 1]
        if not terms:
            return []
        q = " OR ".join(f'"{t}"' for t in terms[:20])
        try:
            rows = self.db.all("SELECT rowid, bm25(chunk_fts, 2.0, 1.5, 1.0) AS s FROM chunk_fts WHERE chunk_fts MATCH ? "
                               "ORDER BY s LIMIT ?", (q, limit))
        except Exception as exc:
            log.warning("fts query failed: %s", exc)
            return []
        return [(r["rowid"], -r["s"]) for r in rows]

    def dense(self, query: str, limit: int = 200) -> list[tuple[int, float]]:
        v = self.encode(query)
        if v is None:
            return []
        sims = self._Z @ v
        idx = np.argpartition(-sims, min(limit, len(sims) - 1))[:limit]
        idx = idx[np.argsort(-sims[idx])]
        ids = self._model["ids"]
        return [(int(ids[i]), float(sims[i])) for i in idx if sims[i] > 0.05]

    def search(self, query: str, limit: int = 20, project_ids: set[int] | None = None, doc_types: set[str] | None = None,
               entity_ids: set[int] | None = None, visibility: str | None = None, include_superseded: bool = False) -> list[dict]:
        bm = self.bm25(query)
        de = self.dense(query)
        k = 60.0
        fused: dict[int, Hit] = {}
        for rank, (cid, s) in enumerate(bm):
            h = fused.setdefault(cid, Hit(cid, 0.0))
            h.score += 1.0 / (k + rank)
            h.bm25 = s
        for rank, (cid, s) in enumerate(de):
            h = fused.setdefault(cid, Hit(cid, 0.0))
            h.score += 1.0 / (k + rank)
            h.dense = s
        if not fused:
            return []
        ids = list(fused)
        rows = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.db.all(f"""SELECT c.id, c.document_id, c.heading, c.text, c.line_start, c.line_end, d.title, d.doc_type,
                                     d.path, d.url, d.visibility, d.project_id, d.superseded, d.date, p.slug, p.name AS project_name,
                                     p.category, p.relevance
                                     FROM chunks c JOIN documents d ON d.id=c.document_id LEFT JOIN projects p ON p.id=d.project_id
                                     WHERE c.id IN ({','.join('?' * len(part))}) AND d.status='active'""", part):
                rows[r["id"]] = r
        ent_chunks = None
        if entity_ids:
            ent_chunks = {r[0] for r in self.db.all(f"SELECT chunk_id FROM mentions WHERE entity_id IN ({','.join('?' * len(entity_ids))})",
                                                    list(entity_ids))}
        out = []
        for cid, h in fused.items():
            r = rows.get(cid)
            if not r:
                continue
            if project_ids and r["project_id"] not in project_ids:
                continue
            if doc_types and r["doc_type"] not in doc_types:
                continue
            if visibility and r["visibility"] != visibility:
                continue
            if r["superseded"] and not include_superseded:
                h.score *= 0.6
            boost = DOC_TYPE_BOOST.get(r["doc_type"], 0.9)
            if ent_chunks is not None:
                boost *= 1.6 if cid in ent_chunks else 0.7
            if r["relevance"] in ("not_relevant", "reference"):
                boost *= 0.5
            out.append({"chunk_id": cid, "score": round(h.score * boost * 1000, 3), "bm25": h.bm25, "dense": h.dense,
                        "document_id": r["document_id"], "title": r["title"], "heading": r["heading"], "text": r["text"],
                        "line_start": r["line_start"], "line_end": r["line_end"], "doc_type": r["doc_type"], "path": r["path"],
                        "url": r["url"], "visibility": r["visibility"], "project_id": r["project_id"], "project": r["slug"],
                        "project_name": r["project_name"], "category": r["category"], "date": r["date"],
                        "superseded": bool(r["superseded"])})
        out.sort(key=lambda x: -x["score"])
        # at most 3 chunks per document so one long report does not crowd out the rest
        per_doc, final = {}, []
        for x in out:
            n = per_doc.get(x["document_id"], 0)
            if n >= 3:
                continue
            per_doc[x["document_id"]] = n + 1
            final.append(x)
            if len(final) >= limit:
                break
        return final
