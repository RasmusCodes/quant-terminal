# Optional integration SDK

The terminal finds and indexes every project without any setup. The SDK exists for the cases where inference is not
good enough: a strategy's live signals, an experiment whose out-of-sample status should be stated rather than guessed,
or a project that should be classified a particular way.

Copy `sdk/rts_sdk.py` into the project (one file, no dependencies):

```python
from rts_sdk import write_manifest, publish_signals, log_experiment

write_manifest(name="Momentum book", category="quant_trading", companies=["NVDA", "AMD"],
               summary="12-1 momentum on US large caps with a volatility target.")

publish_signals([
    {"ticker": "NVDA", "signal": 1, "expected_return": 0.04, "confidence": 0.6},
    {"ticker": "AMD", "signal": -1, "expected_return": -0.02, "confidence": 0.5},
], horizon="20d")

log_experiment("12-1 momentum, vol-targeted", {"sharpe": 0.8, "t_stat": 2.4, "alpha": 3.1},
               oos=True, costs="stated", result_kind="historical_backtest", period="2011-2024", hypothesis="H1")
```

| File | Read as |
|---|---|
| `rts.yaml` | manifest (name, category, summary, companies, data sources, outputs) |
| `rts_signals.csv` | an active signal adapter (ticker, signal, expected_return, confidence, horizon, as_of, strategy) |
| `results/rts_experiments.json` | experiments with explicit out-of-sample, cost, period and result-kind fields (`source_kind = sdk`) |

Categories: `quant_trading`, `derivatives`, `alt_data`, `equity_research`, `portfolio`, `game_theory`, `research_tools`.
Game-theory projects never produce investment signals, whatever the files say.
