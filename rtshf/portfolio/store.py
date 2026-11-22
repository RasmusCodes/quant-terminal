"""Portfolio sources: the website's private ledger (read-only adapter), discovered book files, platform portfolios."""
from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

from ..db import DB
from ..util import dumps, loads, log, now_iso, read_text, sha1
from . import prices as PX

BOOK_NAME = re.compile(r"(book|portfolio|holdings|positions)[^/]*\.csv$", re.I)


def ensure_portfolio(db: DB, key: str, name: str, kind: str, base_currency: str = "EUR", benchmark: str | None = None,
                     source: str = "manual", read_only: bool = False, meta: dict | None = None) -> int:
    row = db.one("SELECT id FROM portfolios WHERE key=?", (key,))
    fields = {"key": key, "name": name, "kind": kind, "base_currency": base_currency, "benchmark": benchmark, "source": source,
              "read_only": int(read_only), "updated_at": now_iso()}
    if meta is not None:
        fields["meta"] = dumps(meta)
    if row:
        db.update("portfolios", row["id"], fields)
        return row["id"]
    fields["created_at"] = now_iso()
    return db.insert("portfolios", fields)


# ----------------------------------------------------------------------------------------- website ledger adapter
def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location("rts_portfolio_history", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rts_portfolio_history"] = mod
    spec.loader.exec_module(mod)
    return mod


def website_ledger(db: DB, site_dir: Path) -> dict | None:
    """Run the website's own portfolio engine on its private ledger, read-only, and keep a snapshot locally."""
    mod_path = site_dir / "portfolio_history.py"
    if not mod_path.exists() or not (site_dir / "private" / "ledger.json").exists():
        return None
    ph = _load_module(mod_path)
    settings = ph.load_settings()
    ledger = ph.load_ledger()
    prices = json.loads(ph.PRICES.read_text(encoding="utf-8")) if ph.PRICES.exists() else {}
    res = ph.compute(ledger, prices, settings)
    reg, _ = ph.registry(settings)
    yahoo_to_key = {v["yahoo"]: k for k, v in reg.items() if v.get("yahoo")}
    holdings = [{"symbol": h["key"], "name": h["name"], "currency": h["ccy"], "qty": h["qty"], "value": round(h["value"], 2),
                 "weight": round(h["weight"], 4), "last": h["last"], "avg_cost": h["avg_cost"], "return_pct": h["return_pct"]}
                for h in res["holdings"]]
    daily = [(d.isoformat(), round(i, 4), b, round(v, 2), round(nd, 2)) for d, i, b, v, nd in res["daily"]]
    series = daily[::7] + ([daily[-1]] if daily and daily[-1] not in daily[::7] else [])
    bench_key = res.get("bench_key")
    snapshot = {"as_of": res["as_of"].isoformat(), "start": res["start"].isoformat(), "holdings": holdings,
                "series": [{"d": d, "index": i, "bench": b, "value": v, "net_deposits": nd} for d, i, b, v, nd in series],
                "daily_index": [(d, i) for d, i, *_ in daily], "bench_key": bench_key,
                "mwr": res.get("mwr"), "notes": res.get("notes"), "cash": {k: round(v, 2) for k, v in (res.get("cash") or {}).items()},
                "excluded": res.get("excluded"), "source": "website/portfolio_history.py (read-only)"}
    # prices for analytics: the site's cache holds actual (split-unadjusted) closes; FX from the ECB
    px_file = site_dir / "private" / "prices.json"
    if px_file.exists():
        data = json.loads(px_file.read_text(encoding="utf-8"))
        for ccy, ser in (data.get("fx") or {}).items():
            PX.upsert_fx(db, ccy, ser, "website-cache")
        for yahoo, ser in (data.get("series") or {}).items():
            key = yahoo_to_key.get(yahoo)
            if key and not db.scalar("SELECT COUNT(*) FROM prices WHERE symbol=? AND source='yahoo'", (key,)):
                PX.upsert_series(db, key, ser, reg[key]["ccy"], "website-cache-unadjusted")
    for k, v in reg.items():
        if not db.one("SELECT 1 FROM securities WHERE symbol=?", (k,)):
            from ..entities import add_discovered
            add_discovered(db, k, v["name"], origin="portfolio", currency=v["ccy"], yahoo=v.get("yahoo"))
    pid = ensure_portfolio(db, "personal", "Personal (Nordnet + Revolut)", "real", "EUR", bench_key or "VWCE",
                           source="website ledger (read-only adapter)", read_only=True, meta=snapshot)
    db.commit()
    return {"portfolio_id": pid, "holdings": len(holdings), "as_of": snapshot["as_of"]}


# ----------------------------------------------------------------------------------------- discovered book files
def sniff_book(raw: str) -> dict | None:
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows or len(rows) > 300:
        return None
    cols = {c.lower().strip(): c for c in rows[0].keys() if c}
    tcol = next((cols[c] for c in ("ticker", "symbol", "instrument") if c in cols), None)
    qcol = next((cols[c] for c in ("shares", "qty", "quantity", "position", "units") if c in cols), None)
    pcol = next((cols[c] for c in ("ref_price", "price", "avg_cost", "cost", "entry_price", "fill_price") if c in cols), None)
    acol = next((cols[c] for c in ("action", "side", "direction") if c in cols), None)
    if not (tcol and qcol and pcol):
        return None
    return {"ticker": tcol, "qty": qcol, "price": pcol, "action": acol}


def discovered_books(db: DB) -> list[dict]:
    out = []
    for r in db.all("""SELECT f.path, s.path AS root, s.key, s.project_id, f.mtime, p.slug FROM file_index f
                       JOIN sources s ON s.id=f.source_id LEFT JOIN projects p ON p.id=s.project_id
                       WHERE f.status='active' AND f.role='result' AND s.kind='local'"""):
        if not BOOK_NAME.search(r["path"]) or re.search(r"(compare|stats|summary|_books)", r["path"], re.I):
            continue
        try:
            raw = read_text(Path(r["root"]) / r["path"])
        except OSError:
            continue
        m = sniff_book(raw)
        if not m:
            continue
        key = f"book:{r['slug']}:{Path(r['path']).stem}"
        date = dt.datetime.fromtimestamp(r["mtime"]).date().isoformat()
        pid = ensure_portfolio(db, key, f"{r['slug']} / {Path(r['path']).name}", "paper", "USD", "SPY",
                               source=f"{r['key']}:{r['path']} (read-only)", read_only=True,
                               meta={"discovered_from": r["path"], "project": r["slug"], "as_of": date})
        db.execute("DELETE FROM transactions WHERE portfolio_id=?", (pid,))
        n = 0
        for row in csv.DictReader(io.StringIO(raw)):
            sym = (row.get(m["ticker"]) or "").strip().upper()
            try:
                qty = float(row.get(m["qty"]) or 0)
                price = float(row.get(m["price"]) or 0)
            except ValueError:
                continue
            if not sym or not qty or not price:
                continue
            side = (row.get(m["action"]) or "BUY").upper() if m["action"] else "BUY"
            signed = -abs(qty) if ("SELL" in side or "SHORT" in side) else abs(qty)
            db.insert("transactions", {"fingerprint": sha1(key, sym, qty, price, side), "portfolio_id": pid, "date": date,
                                       "kind": "buy" if signed > 0 else "sell", "symbol": sym, "qty": signed, "price": price,
                                       "currency": "USD", "amount": -signed * price, "fees": 0.0, "source": r["path"],
                                       "note": side})
            n += 1
        out.append({"portfolio": key, "positions": n, "path": r["path"]})
    db.commit()
    return out


def sync_sources(ctx) -> dict:
    db: DB = ctx.db
    report = {}
    site = db.one("SELECT path FROM sources WHERE kind='local' AND path IS NOT NULL AND project_id IN "
                  "(SELECT project_id FROM sources WHERE kind='website')")
    if site and site["path"]:
        try:
            report["website_ledger"] = website_ledger(db, Path(site["path"]))
        except Exception as exc:
            ctx.error("portfolio:website-ledger", "portfolio", exc)
    try:
        report["books"] = discovered_books(db)
    except Exception as exc:
        ctx.error("portfolio:books", "portfolio", exc)
    # prices for every held or booked symbol plus benchmarks
    syms = set()
    for p in db.all("SELECT * FROM portfolios"):
        meta = loads(p["meta"], {}) or {}
        syms |= {h["symbol"] for h in meta.get("holdings", [])}
        syms |= {r[0] for r in db.all("SELECT DISTINCT symbol FROM transactions WHERE portfolio_id=? AND symbol IS NOT NULL", (p["id"],))}
        if p["benchmark"]:
            syms.add(p["benchmark"])
    for b in ("SPY", "QQQ"):
        syms.add(b)
    for b, (name, yahoo, ccy) in {"SPY": ("SPDR S&P 500 ETF", "SPY", "USD"), "QQQ": ("Invesco QQQ", "QQQ", "USD")}.items():
        if not db.one("SELECT 1 FROM securities WHERE symbol=?", (b,)):
            from ..entities import add_discovered
            add_discovered(db, b, name, origin="benchmark", currency=ccy, yahoo=yahoo)
            db.execute("UPDATE securities SET asset_class='etf', sector='Diversified' WHERE symbol=?", (b,))
    tradable = [s for s in sorted(syms) if db.scalar("SELECT yahoo FROM securities WHERE symbol=?", (s,))]
    report["prices"] = PX.ensure_prices(db, ctx.http, tradable)
    PX.ensure_fx(db, ctx.http, ["USD"])
    ctx.stats["portfolios"] = db.scalar("SELECT COUNT(*) FROM portfolios", default=0)
    return report
