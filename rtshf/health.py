"""System health: sync runs, per-source freshness and errors, schedulers, index state, remaining manual steps."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess

from .db import DB
from .util import loads

TASK_NAME = "rts-quant-terminal sync"


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return round((dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600, 1)


def scheduled_task() -> dict:
    if os.name != "nt":
        return {"available": False}
    try:
        p = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"], capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    if p.returncode != 0:
        return {"available": True, "installed": False}
    info = {}
    for line in p.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return {"available": True, "installed": True, "status": info.get("Status"), "last_run": info.get("Last Run Time"),
            "last_result": info.get("Last Result"), "next_run": info.get("Next Run Time"),
            "repeat": info.get("Repeat: Every")}


def github_actions(owner: str, repo: str = "quant-terminal") -> dict:
    if not shutil.which("gh"):
        return {"available": False}
    try:
        p = subprocess.run(["gh", "api", f"repos/{owner}/{repo}/actions/runs?per_page=3"], capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return {"available": True, "configured": False}
        runs = json.loads(p.stdout).get("workflow_runs", [])
        return {"available": True, "configured": True,
                "runs": [{"name": r["name"], "status": r["status"], "conclusion": r["conclusion"], "event": r["event"],
                          "created_at": r["created_at"], "url": r["html_url"]} for r in runs]}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def health(db: DB, settings, include_external: bool = True) -> dict:
    stale_h = float(settings.get("sync.stale_after_hours", 26))
    runs = [dict(r) | {"stats": loads(r["stats"], {})} for r in db.all("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 8")]
    sources = []
    for s in db.all("SELECT s.*, p.slug FROM sources s LEFT JOIN projects p ON p.id=s.project_id ORDER BY s.kind, s.name"):
        errs = db.scalar("SELECT COUNT(*) FROM sync_errors WHERE source_key=? AND run_id IN (SELECT id FROM sync_runs ORDER BY id DESC LIMIT 3)",
                         (s["key"],), 0)
        age = _age_hours(s["last_synced"] or s["last_seen"])
        sources.append({"key": s["key"], "kind": s["kind"], "name": s["name"], "project": s["slug"], "status": s["status"],
                        "visibility": s["visibility"], "last_synced": s["last_synced"], "last_seen": s["last_seen"],
                        "age_hours": age, "stale": age is None or age > stale_h, "recent_errors": errs, "last_error": s["last_error"]})
    errors = [dict(r) for r in db.all("SELECT * FROM sync_errors ORDER BY id DESC LIMIT 40")]
    last_ok = db.one("SELECT * FROM sync_runs WHERE status IN ('ok','partial') ORDER BY id DESC LIMIT 1")
    dense = settings.data_dir / "dense.npy"
    out = {
        "mode": settings.mode,
        "last_run": runs[0] if runs else None,
        "last_success_age_hours": _age_hours(last_ok["finished_at"]) if last_ok else None,
        "stale": (last_ok is None) or ((_age_hours(last_ok["finished_at"]) or 1e9) > stale_h),
        "runs": runs,
        "sources": sources,
        "source_summary": {"total": len(sources), "stale": sum(1 for s in sources if s["stale"] and s["status"] == "active"),
                           "missing": sum(1 for s in sources if s["status"] == "missing"),
                           "with_errors": sum(1 for s in sources if s["recent_errors"])},
        "errors": errors,
        "db_mb": round(settings.db_path.stat().st_size / 1e6, 1) if settings.db_path.exists() else 0,
        "dense_index": {"exists": dense.exists(),
                        "updated": dt.datetime.fromtimestamp(dense.stat().st_mtime).isoformat(timespec="minutes") if dense.exists() else None},
        "counts": {t: db.scalar(f"SELECT COUNT(*) FROM {t}" + (" WHERE status='active'" if t in ("documents", "findings", "experiments") else ""), default=0)
                   for t in ("projects", "documents", "chunks", "findings", "experiments", "theses", "financial_models", "signals", "alerts")},
    }
    if include_external and not settings.is_public:
        out["scheduler"] = scheduled_task()
        out["github_actions"] = github_actions(settings.owner)
    out["manual_steps"] = manual_steps(db, settings, out)
    return out


def manual_steps(db: DB, settings, h: dict) -> list[dict]:
    """What still needs a human. The goal is an empty 'required' list."""
    steps = []
    sched = h.get("scheduler") or {}
    if sched.get("available") and not sched.get("installed"):
        steps.append({"level": "required", "step": "Install the scheduled sync once: scripts\\install_task.ps1"})
    proposed = db.scalar("SELECT COUNT(*) FROM signal_sources WHERE status='proposed'", default=0)
    if proposed:
        steps.append({"level": "optional", "step": f"Review {proposed} auto-detected signal table(s) in Settings (kept inactive until approved)"})
    from . import llm
    if not llm.credentials_available():
        steps.append({"level": "optional", "step": "Set ANTHROPIC_API_KEY to get written-out answers (search and extractive answers work without it)"})
    new_alerts = db.scalar("SELECT COUNT(*) FROM alerts WHERE status='new'", default=0)
    if new_alerts:
        steps.append({"level": "review", "step": f"{new_alerts} new research-assistant alert(s) to read"})
    return steps
