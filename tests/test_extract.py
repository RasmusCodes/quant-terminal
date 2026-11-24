from rtshf.extract import common as C
from rtshf.extract import experiments as X
from rtshf.extract import findings as F
from rtshf.extract import signals_sniffer as S
from rtshf.extract import theses as T
from rtshf.textproc import chunk_text, parse_markdown


def test_text_metrics():
    m = C.text_metrics("Capped quality book: alpha +4.3% a year (t 2.4), Sharpe 0.50 net of costs")
    assert m["alpha"] == 4.3 and m["t_stat"] == 2.4 and m["sharpe"] == 0.5
    assert C.text_metrics("H18 p 0.063, narrowly rejected")["p_value"] == 0.063
    assert "t_stat" not in C.text_metrics("at 3 pm the market opened")


def test_metric_names():
    assert C.metric_name("Sharpe ratio") == "sharpe"
    assert C.metric_name("t-stat") == "t_stat"
    assert C.metric_name("max_dd") == "max_drawdown"
    assert C.metric_name("ticker") is None
    # bare one-letter names: a sample length or a time index in files, a statistic only as a lower-case table header
    assert C.metric_name("T") == "n_obs" and C.metric_name("t") is None and C.metric_name("t", table=True) == "t_stat"
    assert C.metric_name("p") is None and C.metric_name("p", table=True) == "p_value"


def test_implausible_metrics_rejected():
    exps, _ = X.from_results_file("results/summary.json", "json", '{"name": "x", "t": 3916, "T": 240, "sharpe": 0.4, "t_stat": 2.2}')
    assert exps[0].metrics == {"sharpe": 0.4, "t_stat": 2.2, "n_obs": 240.0}
    exps2, _ = X.from_results_file("results/summary.json", "json", '{"name": "x", "t_stat": 188, "sharpe": 0.4, "alpha": 0.05}')
    assert "t_stat" not in exps2[0].metrics


def test_source_tag_statements_are_observed():
    ct, rule, _ = F.classify_statement("NVIDIA also bought 7.79m new COHR shares at $256.80 for $2.0bn [D-COHR-2026-02].")
    assert ct == "observed_fact" and rule.startswith("source-tag")
    assert F.classify_statement("Our base case estimate is $120 [F: 10-K Note 30].")[0] == "model_estimate"


def test_claim_types():
    assert F.classify_statement("The replay produced 0 false fills on 172k orders, verified against real Bitstamp data.")[0] == "verified_result"
    assert F.classify_statement("The 10-K reported revenue of $1.2bn in 2025.")[0] == "observed_fact"
    assert F.classify_statement("Our DCF implies a fair value of $71 per share.")[0] == "model_estimate"
    assert F.classify_statement("Management is underappreciated by the market.")[0] == "research_claim"


def test_findings_from_doc():
    text = "# Report\n\n## Executive summary\n\n- Earnings days carry 28% of the variance (95% CI 15-39%).\n- Neither half produced an edge after costs.\n\n## Method\n\nWe used daily data.\n"
    chunks = chunk_text(text)
    out = F.extract(chunks, "report")
    texts = [f.text for f in out]
    assert any("28% of the variance" in t for t in texts)
    assert all(f.line for f in out)


def test_thesis_extraction():
    text = ("# NFLX equity research\n\n**Call:** BUY, 12-month price target $71 (+17.6% vs $60.75 at the 16-Nov-2026 close). "
            "Bear $43 / base $73 / bull $97, each the DCF value of its case.\n\n## Thesis\n\n- Only ~34% of broadband homes outside the US pay for Netflix.\n\n"
            "## Risks\n\n- Competition for viewing time could slow growth.\n")
    th = T.extract(text, chunk_text(text), "NFLX equity research", "NFLX", None)
    assert th.stance == "long" and th.target == 71 and th.spot == 60.75
    assert th.scenarios == {"bear": 43, "base": 73, "bull": 97}
    assert th.risks and th.arguments
    short = ("FTAI memo. Short at $167.03, moderate conviction.\n\n**The call.** We are short FTAI. On our cash-flow DCF the base case "
             "is worth **$120 a share (-28%)**. Weighting the bear, base and bull cases 30/50/20 gives **$138 (-17%)**.")
    th2 = T.extract(short, chunk_text(short), "FTAI memo", "FTAI", None)
    assert th2.stance == "short" and th2.target == 138 and th2.spot == 167.03 and th2.scenarios.get("base") == 120
    assert T.find_stance("Built to be re-run monthly. Nothing here is a buy/sell call.") is None
    assert T.find_stance("**Recommendation: WATCHLIST. No position at $74.96.**")[1] == "neutral"
    assert T.find_stance("We hold a short call option on SPX as a hedge.") is None
    assert T.eligible("README.md", "readme") and not T.eligible("docs/README.md", "readme")
    assert not T.eligible("notes/work/engines/lseg_2026_09_15_CELH_OQ_Morgan_Stanley.txt", "doc")
    assert T.eligible("reports/memo/FTAI_Investment_Memo_2026-10-04.md", "memo")


def test_structured_thesis():
    call = {"rating": "short", "price": 167.03, "target": 120, "target_label": "Base-case value (DCF)", "weighted": 138,
            "scenarios": [{"label": "Bear", "value": 42, "prob": 30}, {"label": "Base", "value": 120, "prob": 50}, {"label": "Bull", "value": 328, "prob": 20}],
            "horizon": "12 months", "conviction": "Moderate"}
    th = T.from_structured(call, "FTAI", [], "FTAI memo")
    assert th.stance == "short" and th.target == 138 and th.spot == 167.03 and th.scenarios == {"bear": 42, "base": 120, "bull": 328}
    assert round(th.upside, 3) == round(138 / 167.03 - 1, 3) and th.extra["probabilities"]["bear"] == 30
    pair = T.from_structured({"rating": "pair", "rating_label": "Long notes + puts"}, "CRWV", [], "")
    assert pair.stance == "relative_value" and pair.rating == "LONG NOTES + PUTS"


def test_no_thesis_without_call():
    text = "Verdict: Partly supported. Earnings days carry 28% of the variance."
    assert T.extract(text, chunk_text(text), "study", "NFLX", None) is None


def test_markdown_tables_and_hypotheses():
    md = "## Results\n\n| Book | Sharpe | Alpha (%) | t |\n|---|---|---|---|\n| Quality | 0.80 | 4.1 | 3.4 |\n| Value | 0.10 | 0.5 | 0.6 |\n"
    exps = X.from_markdown_tables(md, "r.md")
    assert len(exps) == 2 and exps[0].metrics["sharpe"] == 0.8 and exps[0].metrics["t_stat"] == 3.4
    text = "H14 is supported: capped quality alpha +4.3% a year (t 2.4)."
    hx = X.from_text(chunk_text(text), "r.md")
    assert hx and hx[0].hypothesis == "H14" and hx[0].verdict == "supported"


def test_results_file_json():
    raw = '[{"name": "momentum", "metrics": {"sharpe": 0.8, "t_stat": 2.4}, "oos": true, "costs": "stated"}]'
    exps, strategies = X.from_results_file("results/rts_experiments.json", "json", raw)
    assert exps[0].metrics == {"sharpe": 0.8, "t_stat": 2.4}
    assert exps[0].extra["explicit"]["oos"] is True


def test_doc_context():
    c = X.doc_context("Walk-forward backtest net of costs, 2011-2024 out-of-sample.")
    assert c["result_kind"] == "historical_backtest" and c["oos"] and c["costs"] == "stated" and "2011-2024" in c["periods"]
    assert X.doc_context("All results come from a synthetic toy world.")["result_kind"] == "synthetic_demo"


def test_signal_sniffer():
    raw = "ticker,period_end,pred_gm_chg_next,rev\nAAOI,2026-09-30,0.02,1\nCOHR,2026-09-30,-0.01,2\nLITE,2026-09-30,0.005,3\n"
    m = S.sniff_csv("out/screen.csv", raw)
    assert m and m["value_col"] == "pred_gm_chg_next" and m["date_col"] == "period_end"
    sigs = S.load_signals(raw, m)
    assert {s["instrument"] for s in sigs} == {"AAOI", "COHR", "LITE"}
    book = "ticker,action,weight,shares,ref_price\nAMD,SELL SHORT,0.15,27,560\nNVDA,BUY,0.2,92,217\n"
    mb = S.sniff_csv("data/book.csv", book)
    out = {s["instrument"]: s["direction"] for s in S.load_signals(book, mb)}
    assert out == {"AMD": -1, "NVDA": 1}


def test_front_matter_lines_preserved():
    p = parse_markdown("---\ntitle: X\nticker: NFLX\n---\n\n# Body\nline\n")
    assert p.meta["ticker"] == "NFLX" and p.title == "X"
    assert p.text.splitlines()[5] == "# Body"
