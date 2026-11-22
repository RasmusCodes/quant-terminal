"""FastAPI server for the private terminal: JSON API + the single-page UI. Binds to 127.0.0.1 by default."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import REPO
from .db import DB
from .views import Views

WEB = REPO / "web"


def create_app(settings) -> FastAPI:
    app = FastAPI(title="RTS HF quant terminal", docs_url="/api/docs", redoc_url=None)
    state = {"sync": None}

    def db() -> DB:
        return DB(settings.db_path)

    def views() -> Views:
        return Views(db(), settings)

    def need(x):
        if x is None:
            raise HTTPException(404, "not found")
        return x

    @app.get("/api/overview")
    def overview():
        return views().overview()

    @app.get("/api/projects")
    def projects():
        return views().projects()

    @app.get("/api/projects/{slug}")
    def project(slug: str):
        return need(views().project(slug))

    @app.get("/api/tab/{name}")
    def tab(name: str):
        return views().tab(name)

    @app.get("/api/companies")
    def companies():
        return views().companies()

    @app.get("/api/companies/{symbol}")
    def company(symbol: str):
        return need(views().company(symbol.upper()))

    @app.get("/api/theses")
    def theses(all: bool = False):
        return views().theses(include_superseded=all)

    @app.get("/api/findings")
    def findings(symbol: str | None = None, claim_type: str | None = None, limit: int = 100):
        return views().findings(symbol=symbol, claim_type=claim_type, limit=limit)

    @app.get("/api/experiments")
    def experiments(oos: int | None = None):
        return views().experiments(oos=oos, limit=5000)

    @app.get("/api/models")
    def models():
        return views().models()

    @app.get("/api/signals")
    def signals():
        return views().signals()

    @app.get("/api/documents")
    def documents(project: str | None = None, doc_type: str | None = None):
        return views().documents(project, doc_type)

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: int):
        return need(views().document(doc_id))

    @app.get("/api/search")
    def search(q: str = Query(..., min_length=1), limit: int = 20, project: str | None = None):
        from .search import Searcher
        d = db()
        pids = None
        if project:
            pid = d.scalar("SELECT id FROM projects WHERE slug=?", (project,))
            pids = {pid} if pid else None
        return Searcher(d, settings).search(q, limit=limit, project_ids=pids)

    @app.post("/api/ask")
    def ask(payload: dict = Body(...)):
        from .qa import answer
        q = (payload.get("question") or "").strip()
        if not q:
            raise HTTPException(400, "question required")
        return answer(db(), settings, q, payload.get("use_llm"))

    @app.get("/api/alerts")
    def alerts(status: str | None = None):
        return views().alerts(status=status)

    @app.post("/api/alerts/{alert_id}")
    def set_alert(alert_id: int, payload: dict = Body(...)):
        st = payload.get("status")
        if st not in ("new", "seen", "dismissed", "resolved"):
            raise HTTPException(400, "bad status")
        d = db()
        d.execute("UPDATE alerts SET status=? WHERE id=?", (st, alert_id))
        d.audit("user", "alert_status", str(alert_id), {"status": st})
        return {"ok": True}

    @app.get("/api/portfolios")
    def portfolios():
        return views().portfolios()

    @app.get("/api/portfolios/{key:path}/risk")
    def risk(key: str):
        from .portfolio import analytics as A
        d = db()
        p = need(d.one("SELECT * FROM portfolios WHERE key=?", (key,)))
        return A.risk(d, dict(p), settings)

    @app.post("/api/portfolios/{key:path}/optimize")
    def optimize(key: str, payload: dict = Body(default={})):
        from .portfolio import optimize as O
        d = db()
        p = need(d.one("SELECT * FROM portfolios WHERE key=?", (key,)))
        return O.optimise(d, dict(p), payload.get("method", "min_variance"), float(payload.get("max_weight", 0.25)), settings,
                          payload.get("extra_symbols"))

    @app.get("/api/portfolios/{key:path}/size/{symbol}")
    def size(key: str, symbol: str, target_risk: float = 0.02):
        from .portfolio import optimize as O
        d = db()
        p = need(d.one("SELECT * FROM portfolios WHERE key=?", (key,)))
        return O.size_position(d, dict(p), symbol.upper(), target_risk=target_risk, settings=settings)

    @app.post("/api/portfolios/{key:path}/orders")
    def order(key: str, payload: dict = Body(...)):
        from .portfolio import paper
        try:
            return paper.place_order(db(), key, str(payload["symbol"]).upper(), payload["side"], float(payload["qty"]),
                                     mode=payload.get("mode", "paper"), limit_price=payload.get("limit_price"), note=payload.get("note", ""))
        except paper.LiveTradingDisabled as exc:
            raise HTTPException(403, str(exc))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/portfolios/{key:path}")
    def portfolio(key: str):
        return need(views().portfolio(key))

    @app.post("/api/paper")
    def create_paper(payload: dict = Body(...)):
        from .portfolio import paper
        try:
            return paper.create_paper(db(), payload["name"], payload.get("currency", "USD"), float(payload.get("cash", 100000)))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/import")
    def import_csv(payload: dict = Body(...)):
        from .portfolio import importers
        try:
            return importers.import_csv_text(db(), payload["csv"], payload["portfolio"], payload.get("name"), payload.get("kind", "paper"),
                                             payload.get("currency", "USD"))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/health")
    def health():
        from .health import health as H
        return H(db(), settings)

    @app.get("/api/settings")
    def settings_view():
        d = db()
        return {"mode": settings.mode, "owner": settings.owner, "website": settings.website_url,
                "workspace_roots": [str(p) for p in settings.workspace_roots()],
                "sync": settings.get("sync"), "llm": {**(settings.get("llm") or {}), "credentials": __import__("rtshf.llm", fromlist=["x"]).credentials_available()},
                "portfolio": settings.get("portfolio"), "signal_sources": [dict(r) for r in d.all("SELECT key, path, status, confidence, rows FROM signal_sources")],
                "audit": [dict(r) for r in d.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50")]}

    @app.post("/api/signal-sources/{key:path}")
    def set_signal_source(key: str, payload: dict = Body(...)):
        st = payload.get("status")
        if st not in ("active", "proposed", "disabled"):
            raise HTTPException(400, "bad status")
        d = db()
        d.execute("UPDATE signal_sources SET status=?, note='user' WHERE key=?", (st, key))
        if st == "disabled":
            d.execute("UPDATE signals SET active=0 WHERE strategy_id=?", (key,))
        d.audit("user", "signal_source_status", key, {"status": st})
        return {"ok": True}

    @app.post("/api/sync")
    def trigger_sync(payload: dict = Body(default={})):
        if state["sync"] and state["sync"].is_alive():
            return {"started": False, "reason": "a sync is already running"}

        def work():
            from . import sync
            try:
                sync.run(settings, trigger="ui", full=bool(payload.get("full")))
            except Exception:
                pass
        state["sync"] = threading.Thread(target=work, daemon=True)
        state["sync"].start()
        return {"started": True}

    @app.get("/api/graph")
    def graph(kind: str = "project", id: int | None = None):
        d = db()
        rows = d.all("SELECT * FROM relations" + (" WHERE (src_kind=? AND src_id=?) OR (dst_kind=? AND dst_id=?)" if id else "") + " LIMIT 2000",
                     (kind, id, kind, id) if id else ())
        return [dict(r) for r in rows]

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    if WEB.exists():
        app.mount("/", StaticFiles(directory=str(WEB)), name="web")
    return app
