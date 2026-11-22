"""Synchronisation orchestrator: discover -> resolve -> ingest changes -> website -> profile -> index -> assistant.

Every stage is isolated: a failing source or stage is logged (sync_errors) and the rest of the run continues.
A lock file prevents two syncs (scheduled + manual) from running at once.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import traceback
from collections import Counter
from pathlib import Path

from . import __version__, classify, resolve
from .config import CONFIG_DIR, Settings
from .connectors import files as FW
from .connectors import github as GH
from .connectors import website as WEB
from .db import DB
from .entities import build_linker, ensure_theme_entities, load_master
from .ingest import Ctx, extract_from_text_doc, process_files, remove_document, store_text_document
from .taxonomy import Taxonomy
from .textproc import Parsed
from .util import HttpClient, dumps, log, now_iso, read_text, sha1, slugify

EXTRACTOR_VERSION = "2026-11-21.5"


class SyncLocked(RuntimeError):
    pass


def _lock(path: Path, stale_after: float = 3 * 3600) -> None:
    if path.exists():
        try:
            pid, ts = path.read_text().split()
            alive = _pid_alive(int(pid))
            if alive and time.time() - float(ts) < stale_after:
                raise SyncLocked(f"another sync is running (pid {pid})")
        except (ValueError, OSError):
            pass
    path.write_text(f"{os.getpid()} {time.time()}")


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def pipeline_version(settings: Settings | None = None) -> str:
    """Changes when extraction rules or reference data change (comments and formatting do not count)."""
    import json
    parts = [EXTRACTOR_VERSION, __version__]
    if settings is not None:
        parts += [json.dumps(settings.taxonomy, sort_keys=True, default=str), json.dumps(settings.securities, sort_keys=True, default=str)]
    return sha1(*parts)[:16]


# ========================================================================================= main entry
def run(settings: Settings, trigger: str = "manual", full: bool = False, only: set[str] | None = None) -> dict:
    lock = settings.data_dir / "sync.lock"
    _lock(lock)
    db = DB(settings.db_path)
    started = time.time()
    run_id = db.insert("sync_runs", {"started_at": now_iso(), "mode": settings.mode, "trigger": trigger, "status": "running"})
    db.commit()
    tax = Taxonomy(settings.taxonomy)
    status, error = "ok", None
    ctx = Ctx(db=db, settings=settings, tax=tax, linker=None, run_id=run_id, http=HttpClient())
    try:
        load_master(db, settings.securities)
        ensure_theme_entities(db, tax)
        ctx.linker = build_linker(db, tax)
        pv = pipeline_version(settings)
        force = full or db.get_meta("pipeline_version") != pv
        if force:
            log.info("full re-extraction (pipeline version %s)", pv)
        only = set(only or {"github", "local", "website", "portfolio"})
        if settings.is_public:
            only -= {"local", "portfolio"}
        gh_repos = _stage(ctx, "github.discover", lambda: discover_github(ctx), []) if "github" in only else []
        local_dirs = _stage(ctx, "local.discover", lambda: discover_local(ctx), []) if "local" in only else []
        resolve.assign_sources(db, settings.owner)
        site_dir = WEB.find_local_source([Path(d["path"]) for d in local_dirs], settings.website_url) if local_dirs else None
        if "local" in only:
            for d in local_dirs:
                _stage(ctx, f"local.ingest:{d['key']}", lambda d=d: ingest_local(ctx, d, force, site_dir))
        if "github" in only:
            for r in gh_repos:
                _stage(ctx, f"github.ingest:{r['key']}", lambda r=r: ingest_github(ctx, r, force))
        if "website" in only:
            _stage(ctx, "website", lambda: sync_website(ctx, site_dir, force))
        # project profiles
        if force:
            ctx.changed_projects |= {r[0] for r in db.all("SELECT id FROM projects")}
        unprofiled = {r[0] for r in db.all("SELECT id FROM projects WHERE relevance IS NULL")}
        roots = {}

        def root_of(s):
            if s["kind"] == "local" and s["path"]:
                return Path(s["path"])
            if s["kind"] == "github":
                c = settings.cache_dir / "repos" / s["key"][3:].replace("/", "__")
                return c if c.exists() else None
            return None
        for pid in sorted(ctx.changed_projects | unprofiled):
            _stage(ctx, f"profile:{pid}", lambda pid=pid: classify.profile_project(db, tax, pid, settings.overrides, root_of))
        _stage(ctx, "theses.status", lambda: refresh_thesis_status(db))
        _stage(ctx, "project_entities", lambda: refresh_project_entities(db))
        _stage(ctx, "relations", lambda: _relations(ctx))
        from . import signals as SG
        _stage(ctx, "signals", lambda: (SG.thesis_signals(db), SG.attach_validation(db)))
        _stage(ctx, "search.index", lambda: _search_index(ctx, force))
        if "portfolio" in only:
            _stage(ctx, "portfolio", lambda: _portfolio(ctx))
        _stage(ctx, "assistant", lambda: _assistant(ctx))
        db.set_meta("pipeline_version", pv)
        if ctx.stats.get("errors"):
            status = "partial"
    except SyncLocked:
        raise
    except Exception as exc:  # unexpected failure of the orchestrator itself
        status, error = "failed", f"{exc}\n{traceback.format_exc()[-2000:]}"
        log.error("sync failed: %s", exc)
    finally:
        stats = dict(ctx.stats)
        stats["seconds"] = round(time.time() - started, 1)
        stats["projects"] = db.scalar("SELECT COUNT(*) FROM projects", default=0)
        db.update("sync_runs", run_id, {"finished_at": now_iso(), "status": status, "stats": dumps(stats), "error": error})
        db.commit()
        db.close()
        try:
            lock.unlink()
        except OSError:
            pass
    return {"run_id": run_id, "status": status, "stats": stats, "error": error}


def _stage(ctx: Ctx, name: str, fn, default=None):
    t0 = time.time()
    try:
        out = fn()
        ctx.db.commit()
        dt_ = time.time() - t0
        if dt_ > 2:
            log.info("%s: %.1fs", name, dt_)
        return out
    except Exception as exc:
        ctx.db.conn.rollback()
        ctx.error(name.split(":", 1)[-1] if ":" in name else name, name.split(":", 1)[0], exc, traceback.format_exc()[-1500:])
        ctx.db.commit()
        return default


# ========================================================================================= GitHub
def discover_github(ctx: Ctx) -> list[dict]:
    s, db = ctx.settings, ctx.db
    repos = GH.list_repos(s.owner, public_only=s.is_public, http=ctx.http)
    if not s.get("github.include_forks", False):
        repos = [r for r in repos if not r.is_fork]
    seen, out = set(), []
    for r in repos:
        key = f"gh:{r.full_name}"
        seen.add(key)
        prev = db.one("SELECT * FROM sources WHERE key=?", (key,))
        row = {"kind": "github", "key": key, "name": r.name, "url": r.url, "visibility": r.visibility, "description": r.description,
               "languages": dumps(r.languages), "topics": dumps(r.topics), "license": r.license, "homepage": r.homepage,
               "default_branch": r.default_branch, "pushed_at": r.pushed_at, "created_at": r.created_at,
               "is_archived": int(r.is_archived), "is_fork": int(r.is_fork), "is_empty": int(r.is_empty),
               "size_bytes": r.disk_kb * 1024, "last_seen": now_iso(),
               "status": "archived" if r.is_archived else "active"}
        if prev:
            changed = prev["pushed_at"] != r.pushed_at
            db.update("sources", prev["id"], row)
            sid = prev["id"]
            if prev["visibility"] != r.visibility:
                ctx.change(key, prev["project_id"], "visibility_changed", "", {"from": prev["visibility"], "to": r.visibility})
            if prev["is_archived"] != int(r.is_archived):
                ctx.change(key, prev["project_id"], "archived" if r.is_archived else "unarchived")
            if prev["status"] == "missing":
                ctx.change(key, prev["project_id"], "source_back")
        else:
            row["first_seen"] = now_iso()
            sid = db.insert("sources", row)
            changed = True
            ctx.change(key, None, "new_source", "", {"kind": "github", "name": r.name, "visibility": r.visibility,
                                                     "description": r.description})
        if changed:
            db.update("sources", sid, {"last_changed": r.pushed_at})
        out.append({"id": sid, "key": key, "info": r, "changed": changed})
    for gone in db.all("SELECT * FROM sources WHERE kind='github' AND status != 'missing'"):
        if gone["key"] not in seen:
            db.update("sources", gone["id"], {"status": "missing"})
            ctx.change(gone["key"], gone["project_id"], "source_missing")
    ctx.stats["github_repos"] = len(out)
    db.commit()
    return out


def ingest_github(ctx: Ctx, r: dict, force: bool) -> None:
    s, db = ctx.settings, ctx.db
    src = db.one("SELECT * FROM sources WHERE id=?", (r["id"],))
    info: GH.RepoInfo = r["info"]
    if info.is_empty:
        return
    linked_local = db.one("SELECT id FROM sources WHERE kind='local' AND project_id=? AND status='active'", (src["project_id"],))
    if linked_local and not s.is_public:
        return                                    # the local working copy is the content source (see ingest_local)
    clone = GH.cached_clone(info, s.cache_dir)
    if r["changed"] or clone is None or force:
        if r["changed"] or clone is None:
            clone, sha = GH.ensure_clone(info, s.cache_dir, int(s.get("github.max_blob_mb", 8)), use_gh=not s.is_public)
            db.update("sources", src["id"], {"head_sha": sha, "path": None})
            if src["head_sha"] and sha and sha != src["head_sha"]:
                ctx.change(src["key"], src["project_id"], "new_commits", "", {"from": src["head_sha"][:10], "to": sha[:10]})
        entries, stats = FW.walk(clone, int(s.get("sync.max_files_per_project", 4000)))
        branch = info.default_branch or "main"
        base = f"https://github.com/{info.full_name}/blob/{branch}/"
        res = process_files(ctx, source=dict(src), project_id=src["project_id"], root=clone, entries=entries,
                            url_for=lambda rel: base + rel, visibility_for=lambda rel: info.visibility, force=force)
        db.update("sources", src["id"], {"fingerprint": FW.fingerprint(entries), "file_count": len(entries),
                                         "last_synced": now_iso(), "last_error": None})
        if any(res.values()):
            ctx.changed_projects.add(src["project_id"])


# ========================================================================================= local workspace
def discover_local(ctx: Ctx) -> list[dict]:
    s, db = ctx.settings, ctx.db
    roots = s.workspace_roots()
    exclude = {e.lower() for e in s.get("workspace.exclude", []) or []}
    seen, out = set(), []
    for root in roots:
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if d.name.startswith(".") or d.name.lower() in exclude or d.resolve() in roots or (d / ".rtsignore").exists():
                continue
            key = f"local:{root.name}/{d.name}"
            seen.add(key)
            remote = FW.read_git_remote(d)
            prev = db.one("SELECT * FROM sources WHERE key=?", (key,))
            row = {"kind": "local", "key": key, "name": d.name, "path": str(d), "visibility": "private", "remote_url": remote,
                   "last_seen": now_iso(), "status": "active"}
            if prev:
                db.update("sources", prev["id"], row)
                sid = prev["id"]
                if prev["status"] == "missing":
                    ctx.change(key, prev["project_id"], "source_back")
            else:
                row["first_seen"] = now_iso()
                sid = db.insert("sources", row)
                ctx.change(key, None, "new_source", "", {"kind": "local", "name": d.name})
            out.append({"id": sid, "key": key, "path": str(d), "name": d.name})
    for gone in db.all("SELECT * FROM sources WHERE kind='local' AND status != 'missing'"):
        if gone["key"] not in seen:
            db.update("sources", gone["id"], {"status": "missing"})
            ctx.change(gone["key"], gone["project_id"], "source_missing")
    ctx.stats["local_folders"] = len(out)
    db.commit()
    return out


def ingest_local(ctx: Ctx, d: dict, force: bool, site_dir: Path | None) -> None:
    s, db = ctx.settings, ctx.db
    src = dict(db.one("SELECT * FROM sources WHERE id=?", (d["id"],)))
    root = Path(d["path"])
    entries, stats = FW.walk(root, int(s.get("sync.max_files_per_project", 4000)))
    if site_dir and root.resolve() == site_dir.resolve():
        entries = [e for e in entries if not e.rel.startswith(("content/", "templates/", "static/", "writer/"))]
    fp = FW.fingerprint(entries)
    latest = max((e.mtime for e in entries), default=None)
    db.update("sources", src["id"], {"file_count": len(entries), "size_bytes": sum(e.size for e in entries),
                                     "last_changed": dt.datetime.fromtimestamp(latest, dt.timezone.utc).replace(microsecond=0).isoformat() if latest else None})
    if fp == src.get("fingerprint") and not force:
        return
    # document libraries (only third-party PDFs) are listed but not parsed unless configured
    n_code = sum(1 for e in entries if e.role == "code")
    pdfs = sum(1 for e in entries if e.rel.lower().endswith(".pdf"))
    is_library = n_code == 0 and pdfs >= 5 and not any(e.rel.lower().startswith("readme") for e in entries)
    parse_docs = not is_library or bool(s.get("workspace.index_document_libraries", False))
    # files tracked in a public GitHub repository get their public URL; everything else stays private
    gh = db.one("SELECT * FROM sources WHERE kind='github' AND project_id=?", (src["project_id"],))
    tracked, base, public = set(), None, False
    if gh and gh["default_branch"]:
        info = GH.RepoInfo(owner=gh["key"][3:].split("/")[0], name=gh["name"], url=gh["url"], visibility=gh["visibility"],
                           default_branch=gh["default_branch"], is_empty=bool(gh["is_empty"]))
        tracked = GH.tracked_paths(info, ctx.http)
        base = f"{gh['url']}/blob/{gh['default_branch']}/"
        public = gh["visibility"] == "public"
    res = process_files(ctx, source=src, project_id=src["project_id"], root=root, entries=entries,
                        url_for=lambda rel: (base + rel) if (base and rel in tracked) else None,
                        visibility_for=lambda rel: "public" if (public and rel in tracked) else "private",
                        force=force, parse_docs=parse_docs)
    db.update("sources", src["id"], {"fingerprint": fp, "last_synced": now_iso(), "last_error": None})
    if src.get("fingerprint") and res.get("real"):
        ctx.change(src["key"], src["project_id"], "updated", "", res)
    ctx.changed_projects.add(src["project_id"])


# ========================================================================================= website
def sync_website(ctx: Ctx, site_dir: Path | None, force: bool) -> None:
    s, db = ctx.settings, ctx.db
    base = s.website_url
    if not base:
        return
    host = base.split("//", 1)[-1]
    key = f"web:{host}"
    src = db.one("SELECT * FROM sources WHERE key=?", (key,))
    live, errors = WEB.crawl(base, ctx.http)
    for e in errors:
        ctx.error(key, "crawl", e)
    live_by_path = {p.path: p for p in live}
    pages = list(live)
    if site_dir and not s.is_public:
        local_pages = WEB.read_local_source(site_dir, base, set(live_by_path))
        by_path = {p.path: p for p in local_pages}
        merged = []
        for p in local_pages:
            lp = live_by_path.get(p.path)
            if lp:
                p.links = sorted(set(p.links) | set(lp.links))
            merged.append(p)
        merged += [p for p in live if p.path not in by_path]      # portfolio/about pages
        pages = merged
    if not src:
        sid = db.insert("sources", {"kind": "website", "key": key, "name": host, "url": base, "visibility": "public",
                                    "first_seen": now_iso(), "last_seen": now_iso(), "status": "active"})
        src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
        ctx.change(key, None, "new_source", "", {"kind": "website"})
    # the site's own project: its local folder, else its GitHub pages repository, else a website project
    site_pid = None
    if site_dir:
        site_pid = db.scalar("SELECT project_id FROM sources WHERE kind='local' AND path=?", (str(site_dir),))
    if not site_pid:
        site_pid = db.scalar("SELECT project_id FROM sources WHERE kind='github' AND lower(name)=?", (host.lower(),))
    if not site_pid:
        site_pid = resolve.ensure_project(db, "website", "Website")
    # the repository that holds the built site belongs to the same project as the site's source
    built = db.one("SELECT id, project_id FROM sources WHERE kind='github' AND (lower(name)=? OR rtrim(homepage,'/')=?)",
                   (host.lower(), base))
    if built and built["project_id"] != site_pid:
        old = built["project_id"]
        db.update("sources", built["id"], {"project_id": site_pid})
        db.execute("UPDATE documents SET project_id=? WHERE source_id=?", (site_pid, built["id"]))
        for t in ("findings", "experiments", "theses", "financial_models", "signal_sources", "strategies"):
            db.execute(f"UPDATE {t} SET project_id=? WHERE project_id=?", (site_pid, old))
        if not db.scalar("SELECT COUNT(*) FROM sources WHERE project_id=?", (old,)):
            db.execute("DELETE FROM projects WHERE id=?", (old,))
    db.update("sources", src["id"], {"project_id": site_pid, "last_seen": now_iso(), "file_count": len(pages)})
    # a profile text per project for matching pages: its README, else its main report/memo
    readmes = {}
    for r in db.all("""SELECT d.project_id, d.id FROM documents d WHERE d.status='active' AND d.source_id != ? AND d.project_id IS NOT NULL
                       AND d.doc_type IN ('readme','report','memo','paper') ORDER BY CASE d.doc_type WHEN 'readme' THEN 0 ELSE 1 END,
                       length(d.path), d.text_len DESC""", (src["id"],)):
        if r["project_id"] not in readmes:
            readmes[r["project_id"]] = classify._doc_text(db, r["id"], 15000)
    sims = resolve.tfidf_similarity([p for p in pages if p.section == "projects"], readmes)
    seen = set()
    for p in pages:
        seen.add(p.path)
        if p.section in ("projects",):
            pid, method, score = resolve.match_page(db, p, site_dir, s.owner, readmes, sims)
            if pid is None and (s.is_public or not site_dir):
                # public build: a published project without a public repository is still a project in the registry
                pid, method = resolve.ensure_project(db, p.slug, p.title[:120] or p.slug), "published_page"
        elif p.section == "research":
            pid, method, score = resolve.match_page(db, p, site_dir, s.owner, readmes, None)
            if method == "similarity":       # research notes without a source path stay with the publication
                pid, method = site_pid, "publication"
        else:
            pid, method, score = site_pid, "publication", 1.0
        pid = pid or site_pid
        prev = db.one("SELECT id, content_hash, project_id FROM documents WHERE source_id=? AND path=?", (src["id"], p.path))
        if prev and prev["content_hash"] == p.content_hash and prev["project_id"] == pid and not force:
            db.execute("UPDATE documents SET last_seen=? WHERE id=?", (now_iso(), prev["id"]))
            continue
        doc_type = {"research": "published_research", "projects": "project_page", "essays": "essay",
                    "portfolio": "portfolio_page"}.get(p.section, "page")
        parsed = Parsed(title=p.title, text=p.text, meta={"call": p.call} if p.call else {}, date=p.date)
        meta = {"section": p.section, "slug": p.slug, "subtitle": p.subtitle, "kind": p.kind, "tags": p.tags, "ticker": p.ticker,
                "tickers": p.tickers, "company": p.company, "facts": p.facts, "status": p.status, "stack": p.stack, "links": p.links,
                "published": p.published, "draft": p.draft, "match": {"method": method, "score": score},
                "category": p.category, "call": p.call or None}
        vis = "public" if p.published else "private"
        ctx.record = not (prev and prev["content_hash"] == p.content_hash)
        doc_id, chunks = store_text_document(ctx, source=dict(src), project_id=pid, rel=p.path, url=p.url if p.published else None,
                                             visibility=vis, parsed=parsed, fmt="html" if not p.local_file else "md",
                                             doc_type=doc_type, superseded=False, size=len(p.text), modified=p.date,
                                             content_hash=p.content_hash, extra_meta=meta)
        sym = None
        from .entities import add_discovered
        for t in p.tickers:
            t = t.split(":")[-1].strip().upper()
            add_discovered(db, t, (p.company if len(p.tickers) == 1 else None) or t, origin="website", exchange=None)
        if p.ticker:
            sym = p.ticker.split(":")[-1].strip().upper()
        extract_from_text_doc(ctx, doc_id=doc_id, project_id=pid, source_key=key, rel=p.path, doc_type=doc_type, parsed=parsed,
                              chunks=chunks, facts=p.facts, symbol_hint=sym)
        ctx.change(key, pid, "changed_page" if prev else "new_page", p.path, {"title": p.title, "published": p.published})
        ctx.record = True
        if p.published and pid:
            db.execute("UPDATE projects SET website_url=COALESCE(website_url, ?) WHERE id=? AND ?='projects'", (p.url, pid, p.section))
    for d in db.all("SELECT id, project_id, path FROM documents WHERE source_id=? AND status='active'", (src["id"],)):
        if d["path"] not in seen:
            remove_document(ctx, dict(d), key)
    db.update("sources", src["id"], {"last_synced": now_iso(), "last_changed": max([p.date or "" for p in pages] + [""]) or None})
    ctx.stats["web_pages"] = len(pages)


# ========================================================================================= global refreshes
def refresh_thesis_status(db: DB) -> None:
    """Per company, the newest thesis of each project is active; older ones are superseded (never deleted)."""
    rows = db.all("""SELECT t.id, t.symbol, t.project_id, t.thesis_date, t.status, d.superseded, t.document_id, d.doc_type
                     FROM theses t LEFT JOIN documents d ON d.id=t.document_id WHERE t.status IN ('active','superseded')""")
    groups = {}
    for r in rows:
        groups.setdefault((r["symbol"], r["project_id"]), []).append(r)
    for _, items in groups.items():
        live = [r for r in items if not r["superseded"] and db.scalar("SELECT status FROM documents WHERE id=?", (r["document_id"],)) == "active"]
        live.sort(key=lambda r: (r["thesis_date"] or "", r["id"]), reverse=True)
        keep = {live[0]["id"]} if live else set()
        # a published note and the memo it came from are the same call: keep both active
        if live:
            top = live[0]
            for r in live[1:]:
                if r["thesis_date"] == top["thesis_date"] or r["doc_type"] != top["doc_type"]:
                    keep.add(r["id"])
        for r in items:
            db.execute("UPDATE theses SET status=? WHERE id=?", ("active" if r["id"] in keep else "superseded", r["id"]))


def refresh_project_entities(db: DB) -> None:
    db.execute("DELETE FROM project_entities")
    db.execute("""INSERT INTO project_entities(project_id, entity_id, mentions, documents, salience, role)
                  SELECT d.project_id, m.entity_id, SUM(m.count), COUNT(DISTINCT m.document_id),
                         SUM(m.count) * 1.0 + 3.0 * SUM(m.in_heading), NULL
                  FROM mentions m JOIN documents d ON d.id=m.document_id
                  WHERE d.status='active' AND d.project_id IS NOT NULL GROUP BY d.project_id, m.entity_id""")
    # normalise salience within each project (0..1) so heavy projects do not dominate
    for p in db.all("SELECT project_id, MAX(salience) AS mx FROM project_entities GROUP BY project_id"):
        if p["mx"]:
            db.execute("UPDATE project_entities SET salience = salience / ? WHERE project_id=?", (p["mx"], p["project_id"]))


def _relations(ctx: Ctx) -> None:
    from . import graph
    graph.rebuild(ctx.db)


def _search_index(ctx: Ctx, force: bool) -> None:
    from . import search
    changed = ctx.stats.get("chunks", 0) > 0 or force
    search.build_dense(ctx.db, ctx.settings, rebuild=changed)


def _portfolio(ctx: Ctx) -> None:
    from .portfolio import store
    store.sync_sources(ctx)


def _assistant(ctx: Ctx) -> None:
    from . import assistant
    assistant.run(ctx)
