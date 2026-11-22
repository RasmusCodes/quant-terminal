"""The optional SDK: files it writes are read exactly as stated."""
import sys
from pathlib import Path

from rtshf import config, sync
from rtshf.db import DB

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import rts_sdk  # noqa: E402


def test_sdk_roundtrip(tmp_path, monkeypatch):
    proj = tmp_path / "ws" / "momentum-book"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("# Momentum book\n\nA small research project.\n", encoding="utf-8")
    rts_sdk.write_manifest(proj, name="Momentum book", category="quant_trading", summary="12-1 momentum with a vol target.",
                           companies=["NVDA"])
    rts_sdk.publish_signals([{"ticker": "NVDA", "signal": 1, "expected_return": 0.04, "confidence": 0.6},
                             {"ticker": "AMD", "signal": -1, "expected_return": -0.02, "confidence": 0.5}],
                            path=proj / "rts_signals.csv", horizon="20d", strategy="mom")
    rts_sdk.log_experiment("12-1 momentum", {"sharpe": 0.8, "t_stat": 2.4}, path=proj / "results" / "rts_experiments.json",
                           oos=True, costs="stated", result_kind="historical_backtest", period="2011-2024")
    monkeypatch.setenv("RTS_WORKSPACE_ROOTS", str(tmp_path / "ws"))
    s = config.load("private", tmp_path / "data")
    r = sync.run(s, trigger="test", only={"local"})
    assert r["status"] in ("ok", "partial"), r
    db = DB(s.db_path)
    p = db.one("SELECT * FROM projects WHERE slug='momentum-book'")
    assert p["category"] == "quant_trading" and p["summary"].startswith("12-1 momentum")
    e = db.one("SELECT * FROM experiments WHERE source_kind='sdk'")
    assert e and e["oos"] == 1 and e["costs"] == "stated" and e["result_kind"] == "historical_backtest"
    sigs = {r["instrument"]: r["direction"] for r in db.all("SELECT instrument, direction FROM signals WHERE active=1 AND source_kind='project_output'")}
    assert sigs == {"NVDA": 1, "AMD": -1}
