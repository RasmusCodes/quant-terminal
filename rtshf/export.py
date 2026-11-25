"""Static public build: the same UI over JSON files, built ONLY from a public-mode database, then privacy-scanned.

The scan fails the build (and removes the output) if it finds local paths, e-mail addresses, credential patterns,
broker account markers or the name of any private repository. Private-repo names come from RTS_PRIVATE_DENYLIST
(comma list) and, when run on the owner's machine, from the private database.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .config import REPO
from .db import DB
from .health import health
from .util import now_iso, truncate
from .views import Views

SCAN_PATTERNS = {
    "windows_path": re.compile(r"[A-Za-z]:\\\\?Users\\\\?", re.I),
    "posix_home": re.compile(r"/(?:Users|home)/[a-z0-9_.-]+/", re.I),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "github_token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "broker_account": re.compile(r"(salkkunro|account number|tilinumero)\s*[:#]?\s*\d{6,}", re.I),
}
EMAIL_OK: set[str] = set()
# Reserved documentation domains (RFC 2606 / RFC 6761) belong to nobody, so placeholders such as you@example.com in a
# public README pass; every other address still fails the build.
RESERVED_EMAIL = re.compile(r"@(?:[a-z0-9-]+\.)*(?:example\.(?:com|net|org)|example|test|invalid|localhost)$")


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")), encoding="utf-8")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._-]", "_", name)


def denylist(settings) -> set[str]:
    names = {x.strip().lower() for x in os.environ.get("RTS_PRIVATE_DENYLIST", "").split(",") if x.strip()}
    priv_db = settings.data_dir.parent / "private" / "rtshf.db"
    if priv_db.exists():
        try:
            pdb = DB(priv_db)
            for r in pdb.all("SELECT name FROM sources WHERE kind='github' AND visibility='private'"):
                names.add(r["name"].lower())
            pdb.close()
        except Exception:
            pass
    return {n for n in names if len(n) >= 2}


def distinctive(name: str) -> bool:
    """A private repository name that cannot occur by chance in public text (common words are checked as links only)."""
    return bool(re.search(r"[-_0-9]", name)) or len(name) >= 9


def scan(out: Path, deny: set[str], owner: str = "RasmusCodes") -> list[dict]:
    hits = []
    link_rx = [(n, re.compile(r"(?:github\.com/|gh:)" + re.escape(owner.lower()) + "/" + re.escape(n) + r"(?![a-z0-9_.-])")) for n in deny]
    word_rx = [(n, re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])")) for n in deny if distinctive(n)]
    for p in out.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".json", ".html", ".js", ".css", ".txt", ".md"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for name, rx in SCAN_PATTERNS.items():
            for m in rx.finditer(text):
                val = m.group(0)
                if name == "email" and (val.lower() in EMAIL_OK or val.lower().endswith((".png", ".jpg", ".svg"))
                                        or RESERVED_EMAIL.search(val.lower())):
                    continue
                hits.append({"file": str(p.relative_to(out)), "pattern": name, "match": val[:60]})
                break
        low = text.lower()
        for n, rx in link_rx + word_rx:
            if rx.search(low):
                hits.append({"file": str(p.relative_to(out)), "pattern": "private_repo_name", "match": n})
    return hits


def build_search_index(db: DB) -> dict:
    docs, postings = [], defaultdict(list)
    rows = db.all("""SELECT c.id, c.heading, c.text, c.line_start, d.title, d.url, d.path, d.date, d.doc_type, p.slug
                     FROM chunks c JOIN documents d ON d.id=c.document_id LEFT JOIN projects p ON p.id=d.project_id
                     WHERE d.status='active' AND d.visibility='public' ORDER BY c.id""")
    stop = set("the a an of and or for in on with to is are was were be by at as it that this from which".split())
    for i, r in enumerate(rows):
        docs.append({"title": r["title"], "heading": (r["heading"] or "").split(" > ")[-1][:120], "text": truncate(r["text"].replace("\n", " "), 420),
                     "project": r["slug"], "url": r["url"], "line": r["line_start"], "date": r["date"]})
        tf = Counter(w for w in re.findall(r"[a-z0-9][a-z0-9.\-]*", f"{r['title']} {r['heading']} {r['text']}".lower()) if w not in stop and len(w) > 1)
        for w, n in tf.items():
            postings[w].append([i, n])
    terms = {w: p for w, p in postings.items() if len(p) < 0.4 * max(len(rows), 1)}
    return {"docs": docs, "terms": terms, "built": now_iso()}


def export_static(settings, out_dir: str = "site", preview_private: bool = False) -> dict:
    if not settings.is_public and not preview_private:
        return {"ok": False, "error": "refusing to export a private database; run with --mode public"}
    out = Path(out_dir)
    if not out.is_absolute():
        out = REPO / out
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(REPO / "web", out)
    idx = (out / "index.html").read_text(encoding="utf-8")
    idx = idx.replace("<script>try{", "<script>window.RTS_STATIC=true;</script>\n  <script>try{", 1)
    (out / "index.html").write_text(idx, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    db = DB(settings.db_path)
    v = Views(db, settings)
    data = out / "data"
    projects = v.projects()
    _write(data / "overview.json", v.overview())
    _write(data / "projects.json", projects)
    for p in projects:
        d = v.project(p["slug"])
        if d:
            _write(data / "projects" / f"{_safe(p['slug'])}.json", d)
    for tab in ("research", "quant", "altdata", "markets"):
        _write(data / "tab" / f"{tab}.json", v.tab(tab))
    comps = v.companies()
    _write(data / "companies.json", comps)
    for c in comps:
        d = v.company(c["symbol"])
        if d:
            _write(data / "companies" / f"{_safe(c['symbol'])}.json", d)
    _write(data / "theses.json", v.theses())
    _write(data / "alerts.json", v.alerts())
    for d in v.documents(limit=5000):
        doc = v.document(d["id"])
        if doc:
            doc["chunks"] = doc["chunks"][:80]
            _write(data / "documents" / f"{d['id']}.json", doc)
    h = health(db, settings, include_external=False)
    _write(data / "health.json", {"runs": h["runs"], "counts": h["counts"], "source_summary": h["source_summary"], "mode": "public"})
    _write(data / "search.json", build_search_index(db))
    hits = scan(out, denylist(settings), settings.owner or "RasmusCodes")
    # a private repository's name that already appears in PUBLIC source text (e.g. a public README mentions it) is not
    # a disclosure; anything else blocks the build
    allowed = []
    if hits:
        already_public = set()
        for h in hits:
            if h["pattern"] == "private_repo_name" and h["match"] not in already_public:
                like = f"%{h['match']}%"
                n = db.scalar("""SELECT COUNT(*) FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                                 WHERE d.visibility='public' AND d.status='active'
                                 AND (lower(c.text) LIKE ? OR lower(d.path) LIKE ? OR lower(d.title) LIKE ?)""", (like, like, like), 0)
                if n:
                    already_public.add(h["match"])
        allowed = [h for h in hits if h["pattern"] == "private_repo_name" and h["match"] in already_public]
        hits = [h for h in hits if h not in allowed]
    db.close()
    if hits:
        shutil.rmtree(out)
        return {"ok": False, "error": "privacy scan failed; nothing was written", "hits": hits[:50]}
    files = sum(1 for _ in out.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    return {"ok": True, "out": str(out.relative_to(REPO)) if REPO in out.parents else out.name, "files": files,
            "mb": round(size / 1e6, 2), "projects": len(projects), "privacy_scan": "passed",
            "allowed_already_public": sorted({h["match"] for h in allowed})}
