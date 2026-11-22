"""Paper portfolios and orders. Live trading is refused: no broker connector exists, and adding one would require a
separate, explicit authorisation mechanism outside this module. Every order attempt is written to the audit log."""
from __future__ import annotations

import datetime as dt

from ..db import DB
from ..util import now_iso, sha1, slugify
from . import prices as PX
from .store import ensure_portfolio


class LiveTradingDisabled(PermissionError):
    pass


def create_paper(db: DB, name: str, base_currency: str = "USD", cash: float = 100_000.0, benchmark: str = "SPY") -> dict:
    key = "paper:" + slugify(name)
    if db.one("SELECT 1 FROM portfolios WHERE key=?", (key,)):
        raise ValueError(f"portfolio {key} already exists")
    pid = ensure_portfolio(db, key, name, "paper", base_currency, benchmark, source="paper (terminal)", read_only=False, meta={})
    if cash:
        db.insert("transactions", {"fingerprint": sha1(key, "deposit", now_iso()), "portfolio_id": pid, "date": dt.date.today().isoformat(),
                                   "kind": "deposit", "symbol": None, "qty": None, "price": None, "currency": base_currency,
                                   "amount": float(cash), "fees": 0.0, "source": "paper", "note": "initial cash"})
    db.audit("user", "create_paper_portfolio", key, {"cash": cash, "currency": base_currency})
    db.commit()
    return {"key": key, "id": pid}


def place_order(db: DB, portfolio_key: str, symbol: str, side: str, qty: float, mode: str = "paper",
                limit_price: float | None = None, note: str = "", signal_ref: str | None = None) -> dict:
    p = db.one("SELECT * FROM portfolios WHERE key=?", (portfolio_key,))
    if not p:
        raise ValueError("unknown portfolio")
    db.audit("user", "order_attempt", portfolio_key, {"symbol": symbol, "side": side, "qty": qty, "mode": mode})
    if mode != "paper" or p["kind"] != "paper":
        raise LiveTradingDisabled("Only paper orders on paper portfolios are possible. The terminal has no broker connection; "
                                  "real trading would need a separate, explicit authorisation mechanism.")
    if p["read_only"]:
        raise ValueError("this portfolio mirrors a file and is read-only")
    side = side.lower()
    if side not in ("buy", "sell", "short", "cover") or qty <= 0:
        raise ValueError("side must be buy/sell/short/cover and qty positive")
    lp = PX.last_price(db, symbol)
    if not lp:
        raise ValueError(f"no price for {symbol}; sync prices first")
    price = lp[1] if limit_price is None else limit_price
    if limit_price is not None and ((side in ("buy", "cover") and lp[1] > limit_price) or (side in ("sell", "short") and lp[1] < limit_price)):
        status, fill = "open", None
    else:
        status, fill = "filled", price
    signed = qty if side in ("buy", "cover") else -qty
    oid = db.insert("orders", {"portfolio_id": p["id"], "created_at": now_iso(), "symbol": symbol, "side": side, "qty": qty,
                               "order_type": "limit" if limit_price is not None else "market", "limit_price": limit_price,
                               "mode": "paper", "status": status, "note": note, "signal_ref": signal_ref})
    tx_id = None
    if status == "filled":
        tx_id = db.insert("transactions", {"fingerprint": sha1(portfolio_key, oid, symbol, signed, fill), "portfolio_id": p["id"],
                                           "date": lp[0], "kind": "buy" if signed > 0 else "sell", "symbol": symbol, "qty": signed,
                                           "price": fill, "currency": db.scalar("SELECT currency FROM securities WHERE symbol=?", (symbol,)) or p["base_currency"],
                                           "amount": -signed * fill, "fees": 0.0, "source": f"paper order {oid}", "note": note})
        db.insert("fills", {"order_id": oid, "ts": now_iso(), "qty": signed, "price": fill, "fees": 0.0, "transaction_id": tx_id})
    db.audit("user", "paper_order", portfolio_key, {"order_id": oid, "status": status, "fill": fill, "price_date": lp[0]})
    db.commit()
    return {"order_id": oid, "status": status, "fill_price": fill, "price_date": lp[0], "note": "Filled at the last cached close." if fill else "Open: limit not reached."}
