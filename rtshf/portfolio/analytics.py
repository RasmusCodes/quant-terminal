"""Holdings, performance, risk, exposures, factor betas and scenarios for any portfolio.

Risk uses the CURRENT holdings and their daily return history (so it describes the book you hold now), with returns
converted to the portfolio's base currency. Every number says how it was computed; nothing is filled in silently.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from ..db import DB
from ..util import loads
from . import prices as PX

SPLIT_FACTORS = [2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50]
SCENARIOS = [
    ("covid_crash", "COVID crash", "2020-02-19", "2020-03-23"),
    ("rate_shock_2022", "2022 rate shock", "2022-01-03", "2022-10-12"),
    ("carry_unwind_2024", "Aug 2024 carry unwind", "2024-07-16", "2024-08-05"),
    ("tariff_shock_2025", "2025 tariff shock", "2025-02-19", "2025-04-08"),
]


# ----------------------------------------------------------------------------------------- returns
def clean_splits(s: pd.Series) -> pd.Series:
    """Undo unadjusted share splits: a one-day move close to an integer split ratio is treated as a split."""
    if len(s) < 3:
        return s
    s = s.copy()
    ratio = s / s.shift(1)
    for date, r in ratio.dropna().items():
        if r <= 0:
            continue
        inv = 1 / r
        for f in SPLIT_FACTORS:
            if abs(inv - f) / f < 0.04:                       # price dropped ~f-fold: forward split
                s.loc[:date - pd.Timedelta(days=1)] /= f
                break
            if abs(r - f) / f < 0.04:                         # reverse split
                s.loc[:date - pd.Timedelta(days=1)] *= f
                break
    return s


def base_ccy_prices(db: DB, symbol: str, base: str, start: str) -> pd.Series:
    s = PX.series(db, symbol, start)
    if s.empty:
        return s
    if db.scalar("SELECT COUNT(*) FROM prices WHERE symbol=? AND source LIKE 'website-cache%'", (symbol,)):
        s = clean_splits(s)
    ccy = db.scalar("SELECT currency FROM securities WHERE symbol=?", (symbol,)) or "USD"
    if ccy == base:
        return s
    if base == "EUR":
        fx = PX.fx_series(db, ccy)                # ccy per EUR
        if fx.empty:
            return pd.Series(dtype=float)
        fx = fx.reindex(s.index.union(fx.index)).ffill().reindex(s.index)
        return (s / fx).dropna()
    if ccy == "EUR":                               # base USD, asset EUR
        fx = PX.fx_series(db, base)
        fx = fx.reindex(s.index.union(fx.index)).ffill().reindex(s.index)
        return (s * fx).dropna()
    return s


def returns_matrix(db: DB, symbols: list[str], base: str, lookback_days: int = 504) -> tuple[pd.DataFrame, dict]:
    start = (dt.date.today() - dt.timedelta(days=int(lookback_days * 1.6))).isoformat()
    cols, info = {}, {}
    for sym in symbols:
        p = base_ccy_prices(db, sym, base, start)
        if len(p) < 40:
            info[sym] = "no price history"
            continue
        r = p.pct_change(fill_method=None).dropna()
        r = r[(r > -0.9) & (r < 3.0)]
        cols[sym] = r
        info[sym] = f"{len(r)} daily returns"
    if not cols:
        return pd.DataFrame(), info
    df = pd.DataFrame(cols).sort_index()
    df = df[df.index.day_of_week < 5].tail(lookback_days)
    return df, info


# ----------------------------------------------------------------------------------------- holdings
def holdings(db: DB, portfolio: dict) -> dict:
    """Current holdings with market values in the base currency, from a snapshot (read-only adapters) or transactions."""
    meta = loads(portfolio.get("meta"), {}) or {}
    base = portfolio.get("base_currency") or "EUR"
    if meta.get("holdings"):
        hs = [dict(h) for h in meta["holdings"]]
        total = sum(h.get("value") or 0 for h in hs) or 1.0
        for h in hs:
            h["weight"] = (h.get("value") or 0) / total
        return {"as_of": meta.get("as_of"), "base_currency": base, "holdings": hs, "cash": meta.get("cash") or {},
                "gross": total, "net": total, "method": meta.get("source", "snapshot")}
    pos = defaultdict(float)
    cost = defaultdict(float)
    cash = 0.0
    for t in db.all("SELECT * FROM transactions WHERE portfolio_id=? ORDER BY date, id", (portfolio["id"],)):
        if t["kind"] in ("buy", "sell") and t["symbol"]:
            q = t["qty"] or 0.0
            if pos[t["symbol"]] * q >= 0:                 # adding to a position
                cost[t["symbol"]] += q * (t["price"] or 0)
            else:                                          # reducing: keep average cost
                if pos[t["symbol"]]:
                    cost[t["symbol"]] *= (pos[t["symbol"]] + q) / pos[t["symbol"]]
            pos[t["symbol"]] += q
            cash -= q * (t["price"] or 0) + (t["fees"] or 0)
        elif t["kind"] in ("deposit", "withdrawal", "dividend", "fee"):
            cash += t["amount"] or 0.0
    hs = []
    for sym, q in pos.items():
        if abs(q) < 1e-9:
            continue
        lp = PX.last_price(db, sym)
        px_base = base_ccy_prices(db, sym, base, (dt.date.today() - dt.timedelta(days=15)).isoformat())
        last_base = float(px_base.iloc[-1]) if not px_base.empty else None
        sec = db.one("SELECT name, currency FROM securities WHERE symbol=?", (sym,))
        avg = cost[sym] / q if q else None
        value = q * last_base if last_base is not None else q * (avg or 0)
        hs.append({"symbol": sym, "name": sec["name"] if sec else sym, "currency": sec["currency"] if sec else base, "qty": q,
                   "last": lp[1] if lp else None, "last_date": lp[0] if lp else None, "avg_cost": avg, "value": round(value, 2),
                   "return_pct": ((lp[1] / avg - 1) * 100 * (1 if q > 0 else -1)) if (lp and avg) else None,
                   "priced": lp is not None})
    gross = sum(abs(h["value"]) for h in hs) or 1.0
    net = sum(h["value"] for h in hs)
    for h in hs:
        h["weight"] = h["value"] / gross
    hs.sort(key=lambda h: -abs(h["value"]))
    return {"as_of": dt.date.today().isoformat(), "base_currency": base, "holdings": hs, "cash": {base: round(cash, 2)},
            "gross": gross, "net": net, "method": "transactions"}


# ----------------------------------------------------------------------------------------- performance
def performance(db: DB, portfolio: dict) -> dict:
    meta = loads(portfolio.get("meta"), {}) or {}
    base = portfolio.get("base_currency") or "EUR"
    if meta.get("daily_index"):
        idx = pd.Series([v for _, v in meta["daily_index"]], index=pd.to_datetime([d for d, _ in meta["daily_index"]]))
        bench = None
        if meta.get("series") and any(x.get("bench") for x in meta["series"]):
            b = pd.Series([x["bench"] for x in meta["series"] if x.get("bench")],
                          index=pd.to_datetime([x["d"] for x in meta["series"] if x.get("bench")]))
            bench = b / b.iloc[0] * 100
        return _perf_stats(idx, bench, portfolio.get("benchmark"), "website ledger time-weighted index")
    # transactions: value the book daily since its first trade
    txs = db.all("SELECT * FROM transactions WHERE portfolio_id=? ORDER BY date", (portfolio["id"],))
    if not txs:
        return {"available": False, "reason": "no transactions"}
    start = txs[0]["date"]
    syms = sorted({t["symbol"] for t in txs if t["symbol"]})
    px = {s: base_ccy_prices(db, s, base, start) for s in syms}
    days = pd.bdate_range(start, dt.date.today())
    pos = defaultdict(float)
    cash = 0.0
    by_day = defaultdict(list)
    for t in txs:
        by_day[pd.Timestamp(t["date"])].append(t)
    values, flows = [], []
    for d in days:
        flow = 0.0
        for t in by_day.get(d, []):
            if t["kind"] in ("buy", "sell") and t["symbol"]:
                pos[t["symbol"]] += t["qty"] or 0
                cash -= (t["qty"] or 0) * (t["price"] or 0) + (t["fees"] or 0)
            elif t["kind"] in ("deposit", "withdrawal"):
                cash += t["amount"] or 0
                flow += t["amount"] or 0
        v = cash
        for s, q in pos.items():
            ser = px.get(s)
            if ser is not None and not ser.empty:
                p = ser[:d]
                if not p.empty:
                    v += q * float(p.iloc[-1])
        values.append(v)
        flows.append(flow)
    vals = pd.Series(values, index=days)
    # books without deposits: measure against the gross amount invested on day one
    first_gross = sum(abs((t["qty"] or 0) * (t["price"] or 0)) for t in by_day.get(pd.Timestamp(start), []))
    capital = first_gross if not any(flows) and first_gross else None
    if capital:
        idx = 100 * (1 + (vals - vals.iloc[0]) / capital)
        method = "P&L on the book's day-one gross exposure (no cash ledger)"
    else:
        rets = (vals - pd.Series(flows, index=days)) / vals.shift(1) - 1
        idx = 100 * (1 + rets.fillna(0)).cumprod()
        method = "time-weighted from daily valuations"
    bench = None
    if portfolio.get("benchmark"):
        b = base_ccy_prices(db, portfolio["benchmark"], base, start)
        if not b.empty:
            b = b.reindex(days, method="ffill").dropna()
            if not b.empty:
                bench = b / b.iloc[0] * 100
    return _perf_stats(idx, bench, portfolio.get("benchmark"), method)


def _perf_stats(idx: pd.Series, bench: pd.Series | None, bench_name: str | None, method: str) -> dict:
    idx = idx.dropna()
    if len(idx) < 2:
        return {"available": False, "reason": "not enough history"}
    years = max((idx.index[-1] - idx.index[0]).days / 365.25, 1 / 365)
    total = idx.iloc[-1] / idx.iloc[0] - 1
    daily = idx.resample("B").last().ffill().pct_change().dropna()
    vol = float(daily.std() * math.sqrt(252)) if len(daily) > 5 else None
    dd = idx / idx.cummax() - 1
    trough = dd.idxmin()
    peak = idx[:trough].idxmax() if len(idx[:trough]) else idx.index[0]
    out = {"available": True, "method": method, "start": idx.index[0].date().isoformat(), "end": idx.index[-1].date().isoformat(),
           "total_return": round(float(total), 4), "cagr": round(float((1 + total) ** (1 / years) - 1), 4) if years > 0.25 else None,
           "volatility": round(vol, 4) if vol else None, "max_drawdown": round(float(dd.min()), 4),
           "max_drawdown_from": peak.date().isoformat(), "max_drawdown_to": trough.date().isoformat(),
           "sharpe_0rf": round(float(daily.mean() * 252 / (daily.std() * math.sqrt(252))), 2) if vol else None,
           "series": [{"d": d.date().isoformat(), "p": round(float(v), 3)} for d, v in idx.resample("W-FRI").last().dropna().items()]}
    if bench is not None and len(bench.dropna()) > 2:
        b = bench.dropna()
        bt = b.iloc[-1] / b.iloc[0] - 1
        out["benchmark"] = {"name": bench_name, "total_return": round(float(bt), 4),
                            "cagr": round(float((1 + bt) ** (1 / years) - 1), 4) if years > 0.25 else None,
                            "series": [{"d": d.date().isoformat(), "b": round(float(v), 3)} for d, v in b.resample("W-FRI").last().dropna().items()]}
        bd = b.resample("B").last().ffill().pct_change().dropna()
        j = pd.concat([daily, bd], axis=1, join="inner").dropna()
        if len(j) > 30:
            cov = np.cov(j.iloc[:, 0], j.iloc[:, 1])
            out["beta"] = round(float(cov[0, 1] / cov[1, 1]), 3)
            te = (j.iloc[:, 0] - j.iloc[:, 1]).std() * math.sqrt(252)
            out["tracking_error"] = round(float(te), 4)
            out["information_ratio"] = round(float((j.iloc[:, 0] - j.iloc[:, 1]).mean() * 252 / te), 2) if te > 0 else None
    return out


# ----------------------------------------------------------------------------------------- risk
def _lw_cov(R: pd.DataFrame) -> tuple[np.ndarray, str]:
    X = R.fillna(0.0).values
    if X.shape[0] > X.shape[1] + 5:
        try:
            from sklearn.covariance import LedoitWolf
            return LedoitWolf().fit(X).covariance_, "Ledoit-Wolf shrinkage"
        except Exception:
            pass
    return np.cov(X, rowvar=False), "sample covariance"


def risk(db: DB, portfolio: dict, settings=None) -> dict:
    base = portfolio.get("base_currency") or "EUR"
    H = holdings(db, portfolio)
    hs = [h for h in H["holdings"] if abs(h.get("weight") or 0) > 1e-6]
    if not hs:
        return {"available": False, "reason": "no holdings"}
    lookback = int(settings.get("portfolio.risk_lookback_days", 504)) if settings else 504
    syms = [h["symbol"] for h in hs]
    R, info = returns_matrix(db, syms, base, lookback)
    w_all = pd.Series({h["symbol"]: h["weight"] for h in hs})
    covered = [s for s in syms if s in R.columns]
    missing = [s for s in syms if s not in R.columns]
    out = {"available": bool(covered), "as_of": H["as_of"], "base_currency": base, "lookback_days": len(R),
           "coverage": round(float(w_all[covered].abs().sum() / w_all.abs().sum()), 4) if covered else 0.0,
           "missing_history": missing, "history": info}
    out["concentration"] = _concentration(w_all)
    out["exposures"] = exposures(db, hs)
    if not covered:
        return out
    R = R[covered].dropna(how="all").fillna(0.0)
    w = w_all[covered]          # weights of the whole book: holdings without history add no risk (coverage is reported)
    port = R.values @ w.values
    port = pd.Series(port, index=R.index)
    cov, cov_method = _lw_cov(R)
    sigma_d = float(math.sqrt(max(w.values @ cov @ w.values, 0)))
    out["volatility_ann"] = round(sigma_d * math.sqrt(252), 4)
    out["volatility_hist_ann"] = round(float(port.std() * math.sqrt(252)), 4)
    out["covariance"] = cov_method
    levels = (settings.get("portfolio.var_levels", [0.95, 0.99]) if settings else [0.95, 0.99])
    var = {}
    for lv in levels:
        q = float(np.quantile(port, 1 - lv))
        es = float(port[port <= q].mean()) if (port <= q).any() else q
        z = {0.95: 1.6449, 0.99: 2.3263}.get(lv, 1.6449)
        var[str(lv)] = {"hist_var_1d": round(-q, 4), "hist_es_1d": round(-es, 4), "param_var_1d": round(z * sigma_d, 4),
                        "hist_var_10d_sqrt": round(-q * math.sqrt(10), 4)}
    out["var"] = var
    out["var_note"] = "Historical simulation of today's weights over the lookback; 10-day by square-root scaling."
    mrc = cov @ w.values
    contrib = w.values * mrc / (sigma_d ** 2) if sigma_d > 0 else np.zeros(len(w))
    out["risk_contributions"] = sorted([{"symbol": s, "weight": round(float(w[s]), 4), "risk_share": round(float(c), 4),
                                         "vol_ann": round(float(R[s].std() * math.sqrt(252)), 4)}
                                        for s, c in zip(w.index, contrib)], key=lambda x: -x["risk_share"])
    top = list(w.abs().sort_values(ascending=False).index[:15])
    corr = R[top].corr().round(3)
    out["correlation"] = {"symbols": top, "matrix": corr.values.tolist()}
    dd = (1 + port).cumprod()
    out["drawdown_hist"] = round(float((dd / dd.cummax() - 1).min()), 4)
    bench = portfolio.get("benchmark")
    if bench:
        b, _ = returns_matrix(db, [bench], base, lookback)
        if not b.empty:
            j = pd.concat([port, b.iloc[:, 0]], axis=1, join="inner").dropna()
            if len(j) > 30:
                c = np.cov(j.iloc[:, 0], j.iloc[:, 1])
                out["beta"] = round(float(c[0, 1] / c[1, 1]), 3)
                out["beta_benchmark"] = bench
    out["factors"] = factor_exposure(db, port, base, settings)
    out["scenarios"] = scenarios(db, hs, w_all, base, out.get("beta"))
    return out


def _concentration(w: pd.Series) -> dict:
    a = w.abs() / w.abs().sum()
    hhi = float((a ** 2).sum())
    s = a.sort_values(ascending=False)
    return {"hhi": round(hhi, 4), "effective_n": round(1 / hhi, 2) if hhi else None, "top1": round(float(s.iloc[0]), 4),
            "top5": round(float(s.iloc[:5].sum()), 4), "n": int(len(s)),
            "gross": round(float(w.abs().sum()), 4), "net": round(float(w.sum()), 4)}


def exposures(db: DB, hs: list[dict]) -> dict:
    out = {k: defaultdict(float) for k in ("sector", "industry", "country", "currency", "asset_class", "theme_direct", "theme_any")}
    for h in hs:
        w = h.get("weight") or 0.0
        sec = db.one("SELECT * FROM securities WHERE symbol=?", (h["symbol"],))
        if not sec:
            for k in ("sector", "industry", "country", "asset_class"):
                out[k]["Unclassified"] += w
            out["currency"][h.get("currency") or "?"] += w
            continue
        out["sector"][sec["sector"] or "Unclassified"] += w
        out["industry"][sec["industry"] or "Unclassified"] += w
        out["country"][sec["country"] or "Unclassified"] += w
        out["currency"][sec["currency"] or h.get("currency") or "?"] += w
        out["asset_class"][sec["asset_class"] or "equity"] += w
        for theme, role in (loads(sec["themes"], {}) or {}).items():
            out["theme_any"][theme] += w
            if role in ("subject", "supplier", "operator", "beneficiary"):
                out["theme_direct"][theme] += w
    return {k: dict(sorted(((kk, round(vv, 4)) for kk, vv in v.items()), key=lambda kv: -abs(kv[1]))) for k, v in out.items()}


# ----------------------------------------------------------------------------------------- factors
def factor_exposure(db: DB, port: pd.Series, base: str, settings=None) -> dict:
    from . import factors as FF
    try:
        F = FF.load(settings)
    except Exception as exc:
        return {"available": False, "reason": f"factor data unavailable: {exc}"}
    if F is None or F.empty:
        return {"available": False, "reason": "factor data unavailable"}
    r = port.copy()
    if base == "EUR":                     # factors are USD returns: convert the portfolio's EUR returns to USD
        fx = PX.fx_series(db, "USD")
        if not fx.empty:
            fx = fx.reindex(r.index.union(fx.index)).ffill().reindex(r.index)
            r = (1 + r) * (fx / fx.shift(1)) - 1
    j = pd.concat([r.rename("p"), F], axis=1, join="inner").dropna()
    if len(j) < 60:
        return {"available": False, "reason": "too few overlapping days"}
    y = j["p"] - j["RF"]
    cols = [c for c in ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "MOM") if c in j.columns]
    X = np.column_stack([np.ones(len(j))] + [j[c].values for c in cols])
    beta, *_ = np.linalg.lstsq(X, y.values, rcond=None)
    resid = y.values - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    s2 = resid @ resid / dof
    try:
        se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    except np.linalg.LinAlgError:
        se = np.full(len(beta), np.nan)
    r2 = 1 - (resid @ resid) / (((y - y.mean()) ** 2).sum())
    return {"available": True, "model": FF.MODEL_NAME, "days": int(len(j)), "r2": round(float(r2), 3),
            "alpha_ann": round(float(beta[0] * 252), 4), "alpha_t": round(float(beta[0] / se[0]), 2) if se[0] else None,
            "betas": [{"factor": c, "beta": round(float(b), 3), "t": round(float(b / s), 2) if s else None}
                      for c, b, s in zip(cols, beta[1:], se[1:])],
            "note": "Daily OLS of portfolio excess returns (in USD) on Ken French factors; current weights held fixed."}


# ----------------------------------------------------------------------------------------- scenarios
HYPOTHETICAL = [
    ("equity_-10", "Equity market -10%", "beta"),
    ("equity_-20", "Equity market -20%", "beta"),
    ("usd_-10", "USD -10% against the base currency", "fx"),
    ("ai_capex_cut", "AI capex cut (themes)", "theme"),
    ("rates_up_100", "Rates +100 bp (sector rules of thumb)", "sector"),
]
AI_SHOCK = {"supplier": -0.30, "operator": -0.30, "beneficiary": -0.20, "subject": -0.20, "spender": -0.08}
RATES_SHOCK = {"Information Technology": -0.08, "Communication Services": -0.07, "Consumer Discretionary": -0.07,
               "Financials": 0.02, "Utilities": -0.04, "Real Estate": -0.09, "Consumer Staples": -0.02, "Industrials": -0.04,
               "Energy": 0.0, "Health Care": -0.03, "Materials": -0.03, "Diversified": -0.05}


def scenarios(db: DB, hs: list[dict], w: pd.Series, base: str, port_beta: float | None) -> dict:
    out = {"historical": [], "hypothetical": []}
    bench_sym = "SPY"
    for key, label, a, b in SCENARIOS:
        total, used, proxied = 0.0, [], []
        bench = base_ccy_prices(db, bench_sym, base, (pd.Timestamp(a) - pd.Timedelta(days=10)).date().isoformat())
        b_ret = None
        if not bench.empty and bench.index[0] <= pd.Timestamp(a):
            b0, b1 = bench[:a], bench[:b]
            if not b0.empty and not b1.empty:
                b_ret = float(b1.iloc[-1] / b0.iloc[-1] - 1)
        for h in hs:
            p = base_ccy_prices(db, h["symbol"], base, (pd.Timestamp(a) - pd.Timedelta(days=10)).date().isoformat())
            wt = float(w.get(h["symbol"], 0.0))
            if not p.empty and p.index[0] <= pd.Timestamp(a) and not p[:b].empty:
                r = float(p[:b].iloc[-1] / p[:a].iloc[-1] - 1)
                used.append(h["symbol"])
            elif b_ret is not None:
                r = b_ret * (port_beta or 1.0)
                proxied.append(h["symbol"])
            else:
                continue
            total += wt * r
        if used or proxied:
            out["historical"].append({"key": key, "label": label, "window": f"{a} to {b}", "pnl_pct": round(total, 4),
                                      "actual_history": used, "beta_proxied": proxied,
                                      "benchmark_return": None if b_ret is None else round(b_ret, 4)})
    beta = port_beta or 1.0
    for key, label, kind in HYPOTHETICAL:
        pnl, detail = 0.0, []
        for h in hs:
            wt = float(w.get(h["symbol"], 0.0))
            sec = db.one("SELECT * FROM securities WHERE symbol=?", (h["symbol"],))
            if kind == "beta":
                shock = (-0.10 if key == "equity_-10" else -0.20) * beta
            elif kind == "fx":
                shock = -0.10 if (sec and sec["currency"] == "USD" and base != "USD") else 0.0
            elif kind == "theme":
                themes = loads(sec["themes"], {}) if sec else {}
                roles = [themes[t] for t in ("ai_capex", "ai_infrastructure", "semiconductor_capex") if t in themes]
                shock = min([AI_SHOCK.get(r, -0.05) for r in roles], default=-0.05 * beta)
            else:
                shock = RATES_SHOCK.get(sec["sector"] if sec else "", -0.04)
            pnl += wt * shock
            detail.append({"symbol": h["symbol"], "shock": round(shock, 4)})
        out["hypothetical"].append({"key": key, "label": label, "pnl_pct": round(pnl, 4), "method": kind, "detail": detail})
    out["note"] = ("Historical: each holding's actual base-currency return over the window (holdings without history use the "
                   "benchmark times portfolio beta). Hypothetical shocks are rules of thumb, not forecasts.")
    return out
