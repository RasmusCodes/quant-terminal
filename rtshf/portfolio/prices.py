"""Daily prices and FX: reuse caches first (the website's price file), then the Yahoo chart API, cached in SQLite."""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd

from ..db import DB
from ..util import HttpClient, log


def upsert_series(db: DB, symbol: str, series: dict[str, float], currency: str | None, source: str) -> int:
    rows = [(symbol, d, float(v), currency, source) for d, v in series.items() if v is not None]
    db.executemany("INSERT INTO prices(symbol,date,close,currency,source) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close, source=excluded.source", rows)
    return len(rows)


def upsert_fx(db: DB, currency: str, series: dict[str, float], source: str = "ECB") -> int:
    rows = [(currency, d, float(v)) for d, v in series.items() if v]
    db.executemany("INSERT INTO fx_rates(currency,date,per_eur) VALUES(?,?,?) ON CONFLICT(currency,date) DO UPDATE SET per_eur=excluded.per_eur", rows)
    return len(rows)


def series(db: DB, symbol: str, start: str | None = None) -> pd.Series:
    q = "SELECT date, close FROM prices WHERE symbol=?" + (" AND date>=?" if start else "") + " ORDER BY date"
    rows = db.all(q, (symbol, start) if start else (symbol,))
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([r["close"] for r in rows], index=pd.to_datetime([r["date"] for r in rows]), name=symbol)
    return s[~s.index.duplicated(keep="last")]


def fx_series(db: DB, currency: str) -> pd.Series:
    """Units of `currency` per 1 EUR (EUR itself = 1)."""
    if currency in (None, "EUR"):
        return pd.Series(dtype=float)
    rows = db.all("SELECT date, per_eur FROM fx_rates WHERE currency=? ORDER BY date", (currency,))
    return pd.Series([r["per_eur"] for r in rows], index=pd.to_datetime([r["date"] for r in rows]), name=currency)


def last_price(db: DB, symbol: str) -> tuple[str, float] | None:
    r = db.one("SELECT date, close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,))
    return (r["date"], r["close"]) if r else None


def fetch_yahoo(http: HttpClient, yahoo: str, start: dt.date) -> dict[str, float]:
    """Daily closes (split-adjusted by Yahoo) from the public chart endpoint, as the website already does."""
    params = {"period1": int(dt.datetime.combine(start, dt.time()).timestamp()),
              "period2": int(time.time()) + 86400, "interval": "1d"}
    r = http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}", params=params,
                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    if r.status_code != 200:
        raise RuntimeError(f"Yahoo {yahoo}: HTTP {r.status_code}")
    res = r.json()["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0].get("close") or []
    out = {}
    for ts, c in zip(res.get("timestamp") or [], closes):
        if c is not None:
            out[dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date().isoformat()] = round(float(c), 6)
    live, mt = res["meta"].get("regularMarketPrice"), res["meta"].get("regularMarketTime")
    if live and mt:
        out.setdefault(dt.datetime.fromtimestamp(mt + off, dt.timezone.utc).date().isoformat(), float(live))
    return out


def ensure_prices(db: DB, http: HttpClient, symbols: list[str], start: str = "2019-01-01", max_age_days: int = 1) -> dict:
    """Fetch symbols whose cached series is missing or stale. Polite: sequential with a pause."""
    report = {"fetched": [], "failed": [], "fresh": []}
    today = dt.date.today()
    for sym in symbols:
        sec = db.one("SELECT yahoo, currency FROM securities WHERE symbol=?", (sym,))
        yahoo = (sec["yahoo"] if sec and sec["yahoo"] else sym)
        lp = last_price(db, sym)
        if lp and (today - dt.date.fromisoformat(lp[0])).days <= max_age_days + (2 if today.weekday() >= 5 else 0):
            report["fresh"].append(sym)
            continue
        try:
            first = db.scalar("SELECT MIN(date) FROM prices WHERE symbol=?", (sym,))
            since = dt.date.fromisoformat(lp[0]) - dt.timedelta(days=7) if lp and first and first <= start else dt.date.fromisoformat(start)
            s = fetch_yahoo(http, yahoo, since)
            upsert_series(db, sym, s, sec["currency"] if sec else None, "yahoo")
            report["fetched"].append(sym)
            time.sleep(0.4)
        except Exception as exc:
            log.warning("price fetch %s: %s", sym, exc)
            report["failed"].append(f"{sym}: {exc}")
    db.commit()
    return report


def ensure_fx(db: DB, http: HttpClient, currencies: list[str], start: str = "2019-01-01") -> None:
    for ccy in currencies:
        if ccy in (None, "EUR"):
            continue
        last = db.scalar("SELECT MAX(date) FROM fx_rates WHERE currency=?", (ccy,))
        if last and (dt.date.today() - dt.date.fromisoformat(last)).days <= 3:
            continue
        try:
            r = http.get(f"https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A",
                         params={"startPeriod": last or start, "format": "csvdata"}, timeout=60)
            if r.status_code == 200:
                import csv
                import io
                rows = csv.DictReader(io.StringIO(r.text))
                upsert_fx(db, ccy, {x["TIME_PERIOD"]: float(x["OBS_VALUE"]) for x in rows if x.get("OBS_VALUE")})
        except Exception as exc:
            log.warning("fx %s: %s", ccy, exc)
    db.commit()


def import_website_price_file(db: DB, path: Path, symbol_for_yahoo: dict[str, str]) -> int:
    """The website's private prices.json: {'series': {yahoo: {date: close}}, 'fx': {'USD': {date: usd_per_eur}}}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for yahoo, ser in (data.get("series") or {}).items():
        sym = symbol_for_yahoo.get(yahoo)
        if sym:
            n += upsert_series(db, sym, ser, None, "website-cache")
    for ccy, ser in (data.get("fx") or {}).items():
        upsert_fx(db, ccy, ser, "website-cache")
    db.commit()
    return n
