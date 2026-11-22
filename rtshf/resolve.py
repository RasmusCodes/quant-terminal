"""Project identity across sources, and matching website pages to the projects they describe."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from .db import DB
from .util import now_iso, slugify

ALIASES = {"nflx": "netflix", "altdata": "alt data", "alt-data": "alt data", "capstruct": "capital structure",
           "rl": "reinforcement learning", "hft": "high frequency", "vol": "volatility", "cr": "clash royale",
           "gta6": "gta vi", "ttwo": "take two", "celh": "celsius", "ftai": "ftai", "aaoi": "applied optoelectronics",
           "crwv": "coreweave", "statarb": "statistical arbitrage", "pm": "prediction markets"}
STOP = {"the", "a", "an", "of", "and", "for", "in", "on", "with", "to", "lab", "engine", "research", "project", "model", "models",
        "terminal", "data", "platform", "study", "tracker", "report"}


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = set()
    for w in words:
        out.add(w)
        if w in ALIASES:
            out.update(ALIASES[w].split())
    return {w for w in out if w not in STOP and len(w) > 1}


def ensure_project(db: DB, slug: str, name: str) -> int:
    row = db.one("SELECT id FROM projects WHERE slug=?", (slug,))
    if row:
        return row["id"]
    return db.insert("projects", {"slug": slug, "name": name, "first_seen": now_iso(), "status": "new", "category": "unclassified"})


def assign_sources(db: DB, owner: str) -> list[tuple[int, int]]:
    """Give every source a project. Returns (source_id, project_id) pairs that were newly assigned."""
    new = []
    gh = {r["key"][3:].lower(): r for r in db.all("SELECT * FROM sources WHERE kind='github'")}
    for s in db.all("SELECT * FROM sources WHERE kind='github' AND project_id IS NULL"):
        pid = ensure_project(db, slugify(s["name"]), s["name"])
        db.update("sources", s["id"], {"project_id": pid})
        new.append((s["id"], pid))
    for s in db.all("SELECT * FROM sources WHERE kind='local' AND project_id IS NULL"):
        pid = None
        if s["remote_url"]:
            m = re.search(r"github\.com[:/]+([^/]+)/([^/.\s]+?)(?:\.git)?/?$", s["remote_url"])
            if m and f"{m.group(1)}/{m.group(2)}".lower() in gh:
                pid = gh[f"{m.group(1)}/{m.group(2)}".lower()]["project_id"]
        if pid is None:
            same = db.one("SELECT id FROM projects WHERE slug=?", (slugify(s["name"]),))
            pid = same["id"] if same else ensure_project(db, slugify(s["name"]), s["name"])
        db.update("sources", s["id"], {"project_id": pid})
        new.append((s["id"], pid))
    db.commit()
    return new


def tfidf_similarity(pages: list, readme_texts: dict[int, str]) -> dict[tuple[str, int], float]:
    """Cosine similarity of each page with each project's README (TF-IDF over both sets)."""
    pids = [pid for pid, t in readme_texts.items() if t and len(t) > 200]
    if not pages or not pids:
        return {}
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    docs = [p.text[:15000] for p in pages] + [readme_texts[pid][:15000] for pid in pids]
    vec = TfidfVectorizer(stop_words="english", min_df=1, max_df=0.6, ngram_range=(1, 2), sublinear_tf=True)
    m = vec.fit_transform(docs)
    sims = cosine_similarity(m[: len(pages)], m[len(pages):])
    return {(pages[i].path, pids[j]): float(sims[i, j]) for i in range(len(pages)) for j in range(len(pids))}


def match_page(db: DB, page, site_dir: Path | None, owner: str, readme_texts: dict[int, str],
               sims: dict | None = None) -> tuple[int | None, str, float]:
    """Return (project_id, method, score) for a website page."""
    # 1. research notes name their source file
    if page.source_hint and site_dir:
        target = (site_dir / page.source_hint).resolve()
        for s in db.all("SELECT path, project_id FROM sources WHERE kind='local' AND path IS NOT NULL"):
            sp = Path(s["path"]).resolve()
            if sp == target or sp in target.parents:
                return s["project_id"], "source_path", 1.0
    # 2. links to one of the owner's repositories
    repo_links = set()
    for l in page.links:
        m = re.search(r"github\.com/([^/]+)/([^/#?]+)", l)
        if m and m.group(1).lower() == owner.lower():
            repo_links.add(f"gh:{m.group(1)}/{m.group(2)}".lower())
    if repo_links:
        cands = [r for r in db.all("SELECT key, name, project_id FROM sources WHERE kind='github'") if r["key"].lower() in repo_links]
        if len(cands) == 1:
            return cands[0]["project_id"], "repo_link", 0.95
        if cands:
            best = max(cands, key=lambda r: difflib.SequenceMatcher(None, r["name"].lower(), page.slug).ratio())
            return best["project_id"], "repo_link", 0.85
    # 3. name and content similarity
    projects = db.all("SELECT id, slug, name FROM projects")
    pt = tokens(page.slug + " " + page.title)
    best, best_score = None, 0.0
    for p in projects:
        if p["slug"] in ("website",):
            continue
        nt = tokens(p["slug"] + " " + p["name"])
        jac = len(pt & nt) / max(1, len(pt | nt))
        seq = difflib.SequenceMatcher(None, page.slug.replace("-", ""), p["slug"].replace("-", "")).ratio()
        if sims is not None:
            content = sims.get((page.path, p["id"]), 0.0)
        else:
            content = _cosine(page.text[:12000], readme_texts.get(p["id"], "")) if readme_texts.get(p["id"]) else 0.0
        score = 0.3 * jac + 0.2 * seq + 0.8 * content
        if score > best_score:
            best, best_score = p, score
    if best and best_score >= 0.3:
        return best["id"], "similarity", round(best_score, 3)
    return None, "unmatched", round(best_score, 3)


def _cosine(a: str, b: str) -> float:
    import math
    from collections import Counter

    def vec(t):
        return Counter(w for w in re.findall(r"[a-z][a-z0-9]{2,}", t.lower()) if w not in STOP)

    va, vb = vec(a), vec(b)
    if not va or not vb:
        return 0.0
    common = set(va) & set(vb)
    num = sum(va[w] * vb[w] for w in common)
    den = math.sqrt(sum(v * v for v in va.values())) * math.sqrt(sum(v * v for v in vb.values()))
    return num / den if den else 0.0
