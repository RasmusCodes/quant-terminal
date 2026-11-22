"""Optional integration SDK for the RTS quant terminal (copy this single file into any project; no dependencies).

Discovery never needs it: every repository and folder is found and indexed anyway. Use it when you want a project's
outputs read exactly as you mean them instead of inferred:

    from rts_sdk import write_manifest, publish_signals, log_experiment
    write_manifest(name="My strategy", category="quant_trading", companies=["NVDA"])
    publish_signals([{"ticker": "NVDA", "signal": 1, "expected_return": 0.04, "confidence": 0.6}], horizon="20d")
    log_experiment("momentum 12-1", {"sharpe": 0.8, "t_stat": 2.4, "alpha": 3.1}, oos=True, costs="stated",
                   result_kind="historical_backtest", period="2011-2024", hypothesis="H1")

Files written: rts.yaml (manifest), rts_signals.csv, results/rts_experiments.json. The terminal picks them up on its
next sync. Categories: quant_trading, derivatives, alt_data, equity_research, portfolio, game_theory, research_tools.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
from pathlib import Path

__all__ = ["write_manifest", "publish_signals", "log_experiment"]


def write_manifest(path: str | Path = ".", name: str | None = None, category: str | None = None, summary: str | None = None,
                   companies: list[str] | None = None, data_sources: list[str] | None = None, outputs: list[str] | None = None) -> Path:
    """Write rts.yaml at the project root (simple YAML, no dependency)."""
    lines = ["# RTS quant terminal manifest (optional). See sdk/rts_sdk.py."]
    for key, val in (("name", name), ("category", category), ("summary", summary)):
        if val:
            lines.append(f"{key}: {json.dumps(val)}")
    for key, vals in (("companies", companies), ("data_sources", data_sources), ("outputs", outputs)):
        if vals:
            lines.append(f"{key}: [{', '.join(json.dumps(v) for v in vals)}]")
    p = Path(path) / "rts.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def publish_signals(rows: list[dict], path: str | Path = "rts_signals.csv", strategy: str | None = None,
                    horizon: str | None = None, as_of: str | None = None) -> Path:
    """rows: dicts with ticker and at least one of signal (-1..1), expected_return, score; optional confidence."""
    as_of = as_of or _dt.date.today().isoformat()
    fields = ["ticker", "signal", "expected_return", "confidence", "horizon", "as_of", "strategy"]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({"ticker": str(r["ticker"]).upper(), "signal": r.get("signal", r.get("score")),
                        "expected_return": r.get("expected_return"), "confidence": r.get("confidence"),
                        "horizon": r.get("horizon", horizon), "as_of": r.get("as_of", as_of), "strategy": r.get("strategy", strategy)})
    return p


def log_experiment(name: str, metrics: dict, path: str | Path = "results/rts_experiments.json", oos: bool | None = None,
                   costs: str | None = None, result_kind: str | None = None, period: str | None = None,
                   hypothesis: str | None = None, verdict: str | None = None, dataset: str | None = None) -> Path:
    """Append (or replace by name) one experiment. metrics keys: sharpe, alpha, t_stat, p_value, ic, hit_rate, ..."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    items = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    items = [x for x in items if x.get("name") != name]
    item = {"name": name, "metrics": metrics, "logged": _dt.datetime.now().isoformat(timespec="seconds")}
    for k, v in (("oos", oos), ("costs", costs), ("result_kind", result_kind), ("period", period), ("hypothesis", hypothesis),
                 ("verdict", verdict), ("dataset", dataset)):
        if v is not None:
            item[k] = v
    items.append(item)
    p.write_text(json.dumps(items, indent=1), encoding="utf-8")
    return p
