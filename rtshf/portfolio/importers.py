"""CSV import: trade lists (date, symbol, side, qty, price) or holdings snapshots (symbol, qty, cost)."""
from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

from ..db import DB
from ..util import parse_date, read_text, sha1, slugify
from .store import ensure_portfolio

COLS = {
    "date": ("date", "trade_date", "trade date", "kauppapäivä", "datetime", "time"),
    "symbol": ("symbol", "ticker", "instrument", "security", "code"),
    "side": ("side", "action", "type", "transaction type", "direction"),
    "qty": ("qty", "quantity", "shares", "units", "amount_shares", "määrä"),
    "price": ("price", "fill_price", "avg_price", "average price", "cost", "avg_cost", "kurssi"),
    "currency": ("currency", "ccy", "valuutta"),
    "fees": ("fees", "commission", "fee", "kulut"),
    "amount": ("amount", "cash", "net amount", "summa"),
}


def _map(header: list[str]) -> dict:
    low = {h.lower().strip(): h for h in header}
    return {k: next((low[c] for c in cands if c in low), None) for k, cands in COLS.items()}


def import_csv_text(db: DB, raw: str, portfolio_key: str, name: str | None = None, kind: str = "paper",
                    base_currency: str = "USD") -> dict:
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
    if not rows:
        raise ValueError("empty CSV")
    m = _map(list(rows[0].keys()))
    if not m["symbol"] or not m["qty"]:
        raise ValueError(f"could not find symbol and quantity columns in {list(rows[0].keys())}")
    key = portfolio_key if ":" in portfolio_key else f"{kind}:{slugify(portfolio_key)}"
    pid = ensure_portfolio(db, key, name or portfolio_key, kind, base_currency, "SPY" if base_currency == "USD" else "VWCE",
                           source="csv import", read_only=False, meta={})
    added = skipped = 0
    today = dt.date.today().isoformat()
    for r in rows:
        sym = (r.get(m["symbol"]) or "").strip().upper()
        try:
            qty = float(str(r.get(m["qty"]) or "0").replace(",", ".").replace(" ", ""))
        except ValueError:
            skipped += 1
            continue
        if not sym or not qty:
            skipped += 1
            continue
        price = None
        if m["price"] and r.get(m["price"]):
            try:
                price = float(str(r[m["price"]]).replace(",", ".").replace(" ", ""))
            except ValueError:
                price = None
        side = (r.get(m["side"]) or "").lower() if m["side"] else ""
        if any(x in side for x in ("sell", "myynti", "short")):
            qty = -abs(qty)
        date = parse_date(r.get(m["date"])) if m["date"] else None
        fp = sha1(key, date or today, sym, qty, price, r.get(m["fees"]) if m["fees"] else None)
        if db.one("SELECT 1 FROM transactions WHERE fingerprint=?", (fp,)):
            skipped += 1
            continue
        fees = 0.0
        if m["fees"] and r.get(m["fees"]):
            try:
                fees = abs(float(str(r[m["fees"]]).replace(",", ".")))
            except ValueError:
                fees = 0.0
        db.insert("transactions", {"fingerprint": fp, "portfolio_id": pid, "date": date or today, "kind": "buy" if qty > 0 else "sell",
                                   "symbol": sym, "qty": qty, "price": price, "currency": (r.get(m["currency"]) or base_currency) if m["currency"] else base_currency,
                                   "amount": -(qty * (price or 0)) - fees, "fees": fees, "source": "csv import", "note": side or None})
        added += 1
    db.audit("user", "import_csv", key, {"added": added, "skipped": skipped})
    db.commit()
    return {"portfolio": key, "added": added, "skipped": skipped, "columns": m}


def import_csv_file(db: DB, path: str, portfolio_key: str, name: str | None = None, kind: str = "paper") -> dict:
    return import_csv_text(db, read_text(Path(path)), portfolio_key, name=name, kind=kind)
