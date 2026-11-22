"""End to end on a temporary workspace: first sync, then a new project folder and a new report appear and are picked up
automatically on the next sync, with assistant alerts and cited answers."""
import time

from rtshf import config, sync
from rtshf.db import DB
from rtshf.qa import answer
from rtshf.views import Views


def _w(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_discovery_and_incremental(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _w(ws / "alpha-lab" / "README.md", "# Alpha lab\n\nStatistical arbitrage research: pairs trading and cointegration on S&P 500 stocks, "
                                        "walk-forward backtests net of costs.\n\n## Results\n\n- Correlation pairs earn Sharpe 0.45 net of costs (t 2.1), out-of-sample 2011-2024.\n")
    _w(ws / "alpha-lab" / "src" / "pairs.py", "import pandas as pd\nimport statsmodels\n")
    _w(ws / "alpha-lab" / "src" / "b.py", "x=1\n")
    _w(ws / "alpha-lab" / "src" / "c.py", "y=2\n")
    _w(ws / "nflx-notes" / "README.md", "# Netflix (NASDAQ: NFLX) research\n\n**Call:** BUY, 12-month price target $71 (+17.6% vs $60.75).\n\n"
                                         "## Thesis\n\n- Only ~34% of broadband homes outside the US pay for Netflix, so price increases have room.\n\n"
                                         "## Risks\n\n- Competition for viewing time could slow growth and weaken pricing power.\n")
    monkeypatch.setenv("RTS_WORKSPACE_ROOTS", str(ws))
    s = config.load("private", tmp_path / "data")
    r1 = sync.run(s, trigger="test", only={"local"})
    assert r1["status"] in ("ok", "partial"), r1
    db = DB(s.db_path)
    slugs = {r[0] for r in db.all("SELECT slug FROM projects")}
    assert {"alpha-lab", "nflx-notes"} <= slugs
    assert db.one("SELECT category FROM projects WHERE slug='alpha-lab'")["category"] == "quant_trading"
    t = db.one("SELECT * FROM theses WHERE symbol='NFLX'")
    assert t and t["stance"] == "long" and t["target_price"] == 71
    assert db.scalar("SELECT COUNT(*) FROM experiments WHERE status='active'") >= 1
    db.close()

    # a new project and a new report arrive; nothing is registered by hand
    time.sleep(1.1)
    _w(ws / "nflx-altdata" / "README.md", "# Netflix alternative data\n\nAlternative data tracker for Netflix (NFLX): app downloads and Google Trends.\n\n"
                                           "## Findings\n\n- App downloads fell 12% in October, a risk to subscriber growth and pricing power.\n")
    _w(ws / "nflx-altdata" / "scripts" / "fetch.py", "import requests\n")
    _w(ws / "nflx-altdata" / "scripts" / "parse.py", "import pandas\n")
    _w(ws / "nflx-altdata" / "scripts" / "util.py", "z=3\n")
    _w(ws / "alpha-lab" / "reports" / "02_note.md", "# Note 2\n\n## Results\n\n- H2 is rejected: momentum pairs Sharpe 0.05 (t 0.3).\n")
    r2 = sync.run(s, trigger="test", only={"local"})
    assert r2["status"] in ("ok", "partial"), r2
    db = DB(s.db_path)
    assert db.one("SELECT 1 FROM projects WHERE slug='nflx-altdata'")
    assert db.one("SELECT 1 FROM documents WHERE path='reports/02_note.md' AND status='active'")
    kinds = {r[0] for r in db.all("SELECT kind FROM alerts")}
    assert "new_project" in kinds
    assert "impact" in kinds or "connection" in kinds
    changes = {r[0] for r in db.all("SELECT kind FROM changes WHERE run_id=?", (r2["run_id"],))}
    assert {"new_source", "new_doc"} <= changes

    res = answer(db, s, "What evidence contradicts my current Netflix thesis?", use_llm=False)
    assert res["intent"] == "thesis_contra" and res["evidence"]
    assert any("App downloads fell" in (e.get("text") or "") for e in res["evidence"])
    res2 = answer(db, s, "What research have I done on NFLX?", use_llm=False)
    assert res2["intent"] == "company" and "NFLX" in res2["answer"]
    v = Views(db, s)
    assert v.overview()["counts"]["projects"] >= 3

    # unchanged third sync touches nothing
    r3 = sync.run(s, trigger="test", only={"local"})
    assert not any(k.startswith("change:new") for k in r3["stats"])
