"""Position sizing and portfolio optimisation suggestions. Nothing here trades; outputs are proposals with inputs shown."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..db import DB
from . import analytics as A


def expected_returns(db: DB, symbols: list[str]) -> dict[str, dict]:
    """Annualised expected returns from active signals (thesis upside over its horizon, return forecasts)."""
    out = {}
    for s in symbols:
        rows = db.all("SELECT expected_return, horizon, confidence, validation_status, strategy_id FROM signals "
                      "WHERE instrument=? AND active=1 AND expected_return IS NOT NULL", (s,))
        vals, ws, srcs = [], [], []
        for r in rows:
            h = (r["horizon"] or "12m").lower()
            months = float(h[:-1]) if h.endswith("m") and h[:-1].replace(".", "").isdigit() else 12.0
            ann = (1 + r["expected_return"]) ** (12 / months) - 1 if r["expected_return"] > -1 else r["expected_return"]
            w = (r["confidence"] or 0.4)
            vals.append(ann)
            ws.append(w)
            srcs.append(r["strategy_id"])
        if vals:
            out[s] = {"mu": float(np.average(vals, weights=ws)), "sources": srcs}
    return out


def optimise(db: DB, portfolio: dict, method: str = "min_variance", max_weight: float = 0.25, settings=None,
             extra_symbols: list[str] | None = None, long_only: bool = True) -> dict:
    from scipy.optimize import minimize

    H = A.holdings(db, portfolio)
    cur = {h["symbol"]: h["weight"] for h in H["holdings"]}
    syms = list(dict.fromkeys(list(cur) + list(extra_symbols or [])))
    R, info = A.returns_matrix(db, syms, portfolio.get("base_currency") or "EUR",
                               int(settings.get("portfolio.risk_lookback_days", 504)) if settings else 504)
    syms = [s for s in syms if s in R.columns]
    if len(syms) < 2:
        return {"available": False, "reason": "need price history for at least two holdings", "history": info}
    R = R[syms].fillna(0.0)
    cov, cov_method = A._lw_cov(R)
    cov = cov * 252
    mus = expected_returns(db, syms)
    mu = np.array([mus.get(s, {}).get("mu", np.nan) for s in syms])
    n = len(syms)
    bounds = [(0.0 if long_only else -max_weight, max_weight)] * n
    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    x0 = np.array([max(cur.get(s, 0.0), 0.0) for s in syms])
    x0 = x0 / x0.sum() if x0.sum() > 0 else np.full(n, 1.0 / n)
    note = ""
    if method == "max_sharpe":
        if np.isnan(mu).all():
            return {"available": False, "reason": "no expected returns: no active thesis or forecast signals for these holdings"}
        mu_f = np.where(np.isnan(mu), np.nanmean(mu) * 0.0, mu)
        note = "Holdings without an expected-return signal are given 0% expected return."
        obj = lambda w: -(w @ mu_f) / math.sqrt(max(w @ cov @ w, 1e-12))
    elif method == "risk_parity":
        def obj(w):
            sig = math.sqrt(max(w @ cov @ w, 1e-12))
            rc = w * (cov @ w) / sig
            return float(((rc - sig / n) ** 2).sum()) * 1e4
    else:
        method = "min_variance"
        obj = lambda w: w @ cov @ w
    res = minimize(obj, x0, method="SLSQP", bounds=bounds, constraints=cons, options={"maxiter": 500, "ftol": 1e-10})
    w = np.clip(res.x, -max_weight, max_weight)
    w = w / w.sum()
    vol = math.sqrt(max(w @ cov @ w, 0))
    cur_w = np.array([cur.get(s, 0.0) for s in syms])
    cur_w_n = cur_w / cur_w.sum() if cur_w.sum() > 0 else cur_w
    return {"available": True, "method": method, "converged": bool(res.success), "covariance": cov_method,
            "max_weight": max_weight, "note": note, "expected_vol": round(vol, 4),
            "current_vol": round(math.sqrt(max(cur_w_n @ cov @ cur_w_n, 0)), 4),
            "expected_return": round(float(w @ np.nan_to_num(mu)), 4) if not np.isnan(mu).all() else None,
            "turnover": round(float(np.abs(w - cur_w_n).sum() / 2), 4),
            "weights": [{"symbol": s, "current": round(float(c), 4), "proposed": round(float(p), 4),
                         "mu": None if np.isnan(m) else round(float(m), 4)} for s, c, p, m in zip(syms, cur_w_n, w, mu)],
            "disclaimer": "A mechanical suggestion from historical covariance and your own signals. Not advice; nothing is traded."}


def size_position(db: DB, portfolio: dict, symbol: str, target_risk: float = 0.02, kelly_fraction: float = 0.25,
                  max_weight: float = 0.15, settings=None) -> dict:
    """Vol-target and fractional-Kelly weights for one name, from its volatility and your expected-return signals."""
    base = portfolio.get("base_currency") or "EUR"
    R, info = A.returns_matrix(db, [symbol], base, 504)
    if R.empty:
        return {"available": False, "reason": info.get(symbol, "no price history")}
    vol = float(R.iloc[:, 0].std() * math.sqrt(252))
    mu = expected_returns(db, [symbol]).get(symbol)
    vt = min(target_risk / vol, max_weight) if vol > 0 else None
    kelly = None
    if mu and vol > 0:
        kelly = max(min(kelly_fraction * mu["mu"] / vol ** 2, max_weight), -max_weight)
    return {"available": True, "symbol": symbol, "vol_ann": round(vol, 4), "vol_target_weight": None if vt is None else round(vt, 4),
            "target_risk_contribution": target_risk, "expected_return": None if not mu else round(mu["mu"], 4),
            "signal_sources": None if not mu else mu["sources"], "kelly_fraction": kelly_fraction,
            "fractional_kelly_weight": None if kelly is None else round(kelly, 4), "max_weight": max_weight,
            "note": "Vol target: weight = target annual risk / asset volatility. Kelly: fraction x mu / sigma^2, capped."}
