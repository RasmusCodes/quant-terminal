"""Experiments and backtests: from result files, markdown result tables, hypothesis verdicts and result sentences.

Nothing is computed or filled in: a metric exists only if a file or a sentence states it. Context (result kind,
out-of-sample, costs, periods, datasets) comes from the containing document and is labelled as detected.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

import yaml

from . import common as C

SYNTHETIC = re.compile(r"(synthetic|toy world|toy data|fictional|made-up data|simulated data|no real data)", re.I)
SIMULATION = re.compile(r"(simulat\w+|agent-based|monte carlo|discrete-event)", re.I)
LIVE = re.compile(r"(live trading|live results?|real money|real-money|in production)", re.I)
PAPER = re.compile(r"(paper[- ]trad\w+|forward test\w*|shadow trad\w+)", re.I)
BACKTEST = re.compile(r"(backtest\w*|historical (?:test|simulation)|walk-forward|net of costs|annual return|sharpe)", re.I)
FORECAST_EVAL = re.compile(r"(forecast\w*|brier|log loss|mae\b|rmse|calibrat\w+|beat rate|hit rate)", re.I)
OOS = re.compile(r"(out-of-sample|out of sample|\bOOS\b|walk-forward|walk forward|hold-?out|test set|test period|post-publication|"
                 r"pre-registered|preregistered|forward test|leave-one-out|unseen|after the training)", re.I)
IN_SAMPLE_ONLY = re.compile(r"(in-sample only|in sample only|full-sample fit|no holdout)", re.I)
COSTS_YES = re.compile(r"(net of (?:\w+ )?costs|after (?:\w+ )?costs|transaction costs?|cost model|commission\w*|slippage|fees|"
                       r"half-spread|market impact|borrow cost|\bbps\b|costs modell?ed|costs_modeled)", re.I)
COSTS_NO = re.compile(r"(before costs|gross of costs|ignor\w+ costs|no transaction costs|costs? (?:are|were) not (?:included|modell?ed))", re.I)
HYPOTHESIS = re.compile(r"\b(H\d{1,2}[a-z]?)\b[^.\n]{0,220}?\b(not supported|supported|narrowly rejected|rejected|confirmed|failed|passed|"
                        r"inconclusive|holds|does not hold)\b", re.I)
VERDICT = re.compile(r"\bverdict\b\s*[:\-–]\s*([^.\n]{3,120})", re.I)


@dataclass
class Experiment:
    name: str
    source_kind: str
    metrics: dict = field(default_factory=dict)
    hypothesis: str | None = None
    verdict: str | None = None
    location: str = ""
    line: int | None = None
    variant_of: str | None = None
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------------------- document context
def doc_context(text: str) -> dict:
    t = text[:200_000]
    kind = "statistical_study"
    if SYNTHETIC.search(t):
        kind = "synthetic_demo"
    elif LIVE.search(t) and not re.search(r"no live|not live|never (?:trade|traded) live|nothing here .* order", t, re.I):
        kind = "live"
    elif PAPER.search(t):
        kind = "paper"
    elif BACKTEST.search(t):
        kind = "historical_backtest"
    elif SIMULATION.search(t):
        kind = "simulation"
    elif FORECAST_EVAL.search(t):
        kind = "forecast_evaluation"
    costs = "stated" if COSTS_YES.search(t) else None
    if COSTS_NO.search(t):
        costs = "excluded" if not costs else "mixed"
    lim = None
    m = re.search(r"^#{1,4}\s*(?:limitations?|caveats?|what this (?:does not|doesn't) show|threats to validity)[^\n]*\n(.{40,1500}?)(?=^#{1,4}\s|\Z)",
                  t, re.I | re.M | re.S)
    if m:
        lim = C._clean_md(m.group(1))[:900]
    return {"result_kind": kind, "oos": bool(OOS.search(t)) and not IN_SAMPLE_ONLY.search(t), "costs": costs,
            "periods": C.periods(t), "limitations": lim,
            "preregistered": bool(re.search(r"pre-?registered|preregistered", t, re.I))}


# ----------------------------------------------------------------------------------------- results files
def _flatten(d, prefix="", depth=0, out=None):
    out = {} if out is None else out
    if depth > 3:
        return out
    if isinstance(d, dict):
        for k, v in d.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), depth + 1, out)
    elif isinstance(d, (int, float)) and not isinstance(d, bool):
        out[prefix] = float(d)
    return out


def _metrics_from_flat(flat: dict) -> dict:
    metrics = {}
    for k, v in flat.items():
        name = C.metric_name(k.split(".")[-1])
        if name and name not in metrics:
            metrics[name] = v
    return metrics


def from_results_file(rel: str, fmt: str, raw: str) -> tuple[list[Experiment], list[dict]]:
    """Return (experiments, strategy registry entries)."""
    exps: list[Experiment] = []
    strategies: list[dict] = []
    stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    try:
        if fmt in ("json", "yaml", "yml"):
            data = json.loads(raw) if fmt == "json" else yaml.safe_load(raw)
        else:
            data = None
    except Exception:
        return exps, strategies
    if isinstance(data, dict):
        m = _metrics_from_flat(_flatten(data))
        if len([k for k in m if k not in ("n_obs", "volatility", "corr", "beta")]) >= 2:
            exps.append(Experiment(name=str(data.get("name") or data.get("label") or data.get("id") or stem), source_kind="results_file",
                                   metrics=m, location=rel))
        # dict of variants: {"variant": {"sharpe": ..}}
        elif all(isinstance(v, dict) for v in data.values()) and 1 < len(data) <= 60:
            for k, v in data.items():
                mm = _metrics_from_flat(_flatten(v))
                if len([x for x in mm if x not in ("n_obs", "volatility", "corr", "beta")]) >= 2:
                    exps.append(Experiment(name=f"{stem}: {k}", source_kind="results_file", metrics=mm, location=rel, variant_of=stem))
    elif isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:50]):
        keys = set().union(*[set(x) for x in data[:50]])
        if {"id", "label"} & keys and ({"description", "family", "note"} & keys):
            for x in data[:200]:
                strategies.append({"key": str(x.get("id") or x.get("label")), "name": str(x.get("label") or x.get("id")),
                                   "family": x.get("family"), "description": x.get("description"),
                                   "costs_modeled": x.get("costs_modeled"), "first": x.get("first"), "last": x.get("last")})
        for x in data[:60]:
            mm = _metrics_from_flat(_flatten(x.get("metrics", x) if isinstance(x.get("metrics"), dict) else x))
            if len([k for k in mm if k not in ("n_obs", "volatility", "corr", "beta")]) >= 1 and (len(mm) >= 2 or "metrics" in x):
                label = x.get("label") or x.get("name") or x.get("id") or x.get("strategy") or x.get("model")
                explicit = {k: x[k] for k in ("oos", "costs", "result_kind", "hypothesis", "verdict", "period", "dataset") if k in x}
                exps.append(Experiment(name=f"{stem}: {label}" if label else stem, source_kind="results_file", metrics=mm,
                                       location=rel, variant_of=stem, hypothesis=explicit.get("hypothesis"),
                                       verdict=explicit.get("verdict"), extra={"explicit": explicit} if explicit else {}))
    if fmt in ("csv", "tsv"):
        exps += _from_csv(rel, raw, stem, "\t" if fmt == "tsv" else ",")
    return exps[:80], strategies


def _from_csv(rel: str, raw: str, stem: str, delim: str) -> list[Experiment]:
    try:
        rows = list(csv.reader(io.StringIO(raw), delimiter=delim))
    except csv.Error:
        return []
    if len(rows) < 2 or len(rows) > 202:
        return []
    header = rows[0]
    mcols = {i: C.metric_name(h) for i, h in enumerate(header) if C.metric_name(h)}
    strong = [i for i, n in mcols.items() if n not in ("n_obs", "volatility", "corr", "beta")]
    if not strong:
        return []
    label_col = next((i for i, h in enumerate(header) if i not in mcols and re.search(r"(name|label|strategy|model|variant|book|id|signal|method|spec|test)", h, re.I)), 0)
    out = []
    for r in rows[1:61]:
        if len(r) != len(header):
            continue
        metrics = {}
        for i, n in mcols.items():
            v = C.to_float(r[i])
            if v is not None and n not in metrics:
                metrics[n] = v
        if len([k for k in metrics if k not in ("n_obs", "volatility", "corr", "beta")]) >= 1 and len(metrics) >= 2:
            out.append(Experiment(name=f"{stem}: {r[label_col]}", source_kind="results_file", metrics=metrics, location=rel, variant_of=stem))
    return out


# ----------------------------------------------------------------------------------------- markdown tables
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")


def from_markdown_tables(text: str, rel: str, max_tables: int = 40, max_rows: int = 25) -> list[Experiment]:
    lines = text.splitlines()
    out: list[Experiment] = []
    heading = ""
    i, tables = 0, 0
    while i < len(lines) and tables < max_tables:
        line = lines[i]
        hm = re.match(r"^#{1,5}\s+(.+)", line)
        if hm:
            heading = hm.group(1).strip()
        if TABLE_ROW.match(line) and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            header = [c.strip() for c in TABLE_ROW.match(line).group(1).split("|")]
            mcols = {j: C.metric_name(_strip_unit(h)) for j, h in enumerate(header) if C.metric_name(_strip_unit(h))}
            j = i + 2
            body = []
            while j < len(lines) and TABLE_ROW.match(lines[j]):
                body.append((j + 1, [c.strip() for c in TABLE_ROW.match(lines[j]).group(1).split("|")]))
                j += 1
            strong = [k for k, n in mcols.items() if n not in ("n_obs", "volatility", "corr", "beta")]
            if strong:
                tables += 1
                for ln, cells in body[:max_rows]:
                    if len(cells) != len(header):
                        continue
                    metrics = {}
                    for k, n in mcols.items():
                        v = C.to_float(re.sub(r"[*_`]", "", cells[k]).split(" ")[0])
                        if v is not None and n not in metrics:
                            metrics[n] = v
                    label = re.sub(r"[*_`]", "", cells[0]) or f"row {ln}"
                    if metrics:
                        out.append(Experiment(name=f"{heading or 'Table'} — {label}"[:200], source_kind="report_table",
                                              metrics=metrics, location=rel, line=ln, variant_of=heading or None,
                                              extra={"columns": header[:12]}))
            i = j
            continue
        i += 1
    return out


def _strip_unit(h: str) -> str:
    return re.sub(r"\(.*?\)|\[.*?\]|%|\*", "", h).strip()


# ----------------------------------------------------------------------------------------- text results
def from_text(chunks: list, rel: str) -> list[Experiment]:
    out: list[Experiment] = []
    seen = set()
    for ch in chunks:
        role = C.section_role(ch.heading or "")
        for off, stmt in C.statements(ch.text):
            if len(stmt) > 700:
                continue
            hm = HYPOTHESIS.search(stmt)
            metrics = C.text_metrics(stmt)
            strong = [k for k in metrics if k in C.RETURN_METRICS or k in ("t_stat", "p_value", "ic", "hit_rate", "brier", "r2")]
            if not hm and len(strong) < 2 and not (strong and role in ("summary", "findings", "verdict")):
                continue
            key = (hm.group(1).upper() if hm else None, tuple(sorted(metrics.items())))
            if key in seen:
                continue
            seen.add(key)
            name = (hm.group(1).upper() + ": " if hm else "") + C._clean_md(stmt)[:140]
            out.append(Experiment(name=name, source_kind="hypothesis" if hm else "text", metrics=metrics,
                                  hypothesis=hm.group(1).upper() if hm else None,
                                  verdict=_norm_verdict(hm.group(2)) if hm else None, location=rel,
                                  line=(ch.line_start or 1) + off, extra={"statement": C._clean_md(stmt)[:600]}))
    return out[:60]


def _norm_verdict(v: str) -> str:
    v = v.lower()
    if v in ("supported", "confirmed", "passed", "holds"):
        return "supported"
    if v in ("narrowly rejected",):
        return "rejected (narrowly)"
    if v in ("rejected", "not supported", "failed", "does not hold"):
        return "rejected"
    return "inconclusive"


# ----------------------------------------------------------------------------------------- research-practice flags
def practice_flags(doc_text: str, code_texts: list[tuple[str, str]], n_hypotheses: int) -> list[dict]:
    """Heuristic warnings. 'acknowledged' means the author discusses and handles the issue."""
    t = doc_text
    flags = []

    def add(key, status, detail, evidence=None):
        flags.append({"flag": key, "status": status, "detail": detail, "evidence": evidence})

    if re.search(r"survivor(?:ship)?", t, re.I):
        add("survivorship_bias", "acknowledged", "Survivorship is discussed in the documents.",
            _snippet(t, r"survivor(?:ship)?"))
    elif re.search(r"(current (?:s&p|index) (?:500 )?(?:constituents|members)|today's (?:constituents|members)|current constituents)", t, re.I):
        add("survivorship_bias", "possible", "Universe may be built from today's index members.",
            _snippet(t, r"current (?:s&p|index)|today's (?:constituents|members)|current constituents"))
    if re.search(r"look-?ahead|leakage|point-in-time", t, re.I):
        add("look_ahead", "acknowledged", "Look-ahead/leakage controls are discussed.", _snippet(t, r"look-?ahead|leakage|point-in-time"))
    if re.search(r"(re-?ran (?:it )?on the hold-?out|tuned on the (?:hold-?out|test)|hold-?out (?:again|twice|reused)|after seeing the (?:hold-?out|test))", t, re.I):
        add("holdout_reuse", "possible", "Text suggests the holdout/test set was used more than once.",
            _snippet(t, r"hold-?out|test set"))
    if n_hypotheses >= 10 and not re.search(r"(bonferroni|holm|false discovery|\bfdr\b|multiple[- ]testing|multiple comparisons|"
                                             r"deflated sharpe|reality check|data[- ]snooping|pre-?regist)", t, re.I):
        add("multiple_testing", "possible", f"{n_hypotheses} tests/variants found with no multiple-testing control mentioned.")
    elif n_hypotheses >= 10:
        add("multiple_testing", "acknowledged", "Multiple testing is controlled or tests are pre-registered.",
            _snippet(t, r"bonferroni|holm|false discovery|multiple[- ]testing|deflated sharpe|pre-?regist"))
    code_hits = []
    for path, code in code_texts:
        for rx, label in ((r"\.shift\(\s*-\s*\d", "negative shift (future values)"), (r"\b(?:bfill|backfill)\b|method=['\"]bfill", "backward fill"),
                          (r"train_test_split\([^)]*shuffle\s*=\s*True", "shuffled split")):
            for m in re.finditer(rx, code):
                line = code[: m.start()].count("\n") + 1
                code_hits.append({"path": path, "line": line, "pattern": label})
                if len(code_hits) > 12:
                    break
    if code_hits:
        add("look_ahead_code_review", "review", "Code patterns that can leak future data (often legitimate, e.g. building targets).",
            code_hits[:12])
    return flags


def _snippet(t: str, pattern: str, width: int = 160) -> str | None:
    m = re.search(pattern, t, re.I)
    if not m:
        return None
    s = max(0, m.start() - width // 2)
    return C._clean_md(t[s: m.end() + width // 2].replace("\n", " "))
