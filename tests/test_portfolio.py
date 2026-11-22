import datetime as dt

import numpy as np
import pandas as pd
import pytest

from rtshf.db import DB
from rtshf.portfolio import analytics as A
from rtshf.portfolio import importers, paper, prices as PX
from rtshf.portfolio.store import ensure_portfolio, sniff_book


@pytest.fixture()
def db(tmp_path):
    d = DB(tmp_path / "t.db")
    for sym, ccy in (("AAA", "USD"), ("BBB", "USD"), ("SPY", "USD")):
        d.insert("securities", {"symbol": sym, "name": sym, "currency": ccy, "sector": "Information Technology", "yahoo": sym, "themes": '{"ai_capex": "spender"}'})
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2024-01-01", dt.date.today())
    for sym, mu in (("AAA", 0.0006), ("BBB", 0.0002), ("SPY", 0.0003)):
        px = 100 * np.cumprod(1 + rng.normal(mu, 0.015, len(days)))
        PX.upsert_series(d, sym, {x.date().isoformat(): float(v) for x, v in zip(days, px)}, "USD", "test")
    d.commit()
    return d


def test_paper_flow_and_live_guard(db):
    r = paper.create_paper(db, "Test book", "USD", 10000)
    o = paper.place_order(db, r["key"], "AAA", "buy", 10)
    assert o["status"] == "filled" and o["fill_price"] > 0
    with pytest.raises(paper.LiveTradingDisabled):
        paper.place_order(db, r["key"], "AAA", "buy", 1, mode="live")
    p = dict(db.one("SELECT * FROM portfolios WHERE key=?", (r["key"],)))
    H = A.holdings(db, p)
    assert H["holdings"][0]["symbol"] == "AAA" and abs(H["holdings"][0]["qty"] - 10) < 1e-9
    assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='order_attempt'") == 2


def test_risk_metrics(db):
    pid = ensure_portfolio(db, "paper:t", "t", "paper", "USD", "SPY")
    for sym, q in (("AAA", 50), ("BBB", 50)):
        db.insert("transactions", {"fingerprint": sym, "portfolio_id": pid, "date": "2024-06-03", "kind": "buy", "symbol": sym, "qty": q,
                                   "price": 100.0, "currency": "USD", "amount": -q * 100.0, "fees": 0})
    p = dict(db.one("SELECT * FROM portfolios WHERE id=?", (pid,)))
    r = A.risk(db, p, None)
    assert r["available"] and 0 < r["volatility_ann"] < 1
    v = r["var"]["0.95"]
    assert 0 < v["hist_var_1d"] <= r["var"]["0.99"]["hist_var_1d"] + 1e-9
    assert abs(sum(x["risk_share"] for x in r["risk_contributions"]) - 1) < 0.05
    assert r["concentration"]["effective_n"] > 1.5
    perf = A.performance(db, p)
    assert perf["available"] and "benchmark" in perf


def test_split_cleaning():
    s = pd.Series([300.0, 303.0, 101.5, 102.0], index=pd.bdate_range("2024-01-01", periods=4))
    c = A.clean_splits(s)
    assert abs(c.iloc[2] / c.iloc[1] - 1) < 0.02


def test_csv_import(db):
    raw = "date,symbol,side,qty,price\n2024-06-03,AAA,buy,10,100\n2024-07-01,AAA,sell,4,110\n"
    r = importers.import_csv_text(db, raw, "imported test")
    assert r["added"] == 2
    again = importers.import_csv_text(db, raw, "imported test")
    assert again["added"] == 0 and again["skipped"] == 2


def test_sniff_book():
    assert sniff_book("ticker,action,weight,shares,ref_price\nNVDA,BUY,0.2,92,217\n")["qty"] == "shares"
    assert sniff_book("a,b\n1,2\n") is None
