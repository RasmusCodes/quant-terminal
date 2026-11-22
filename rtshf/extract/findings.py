"""Findings: short statements of results, facts and claims, each typed by epistemic status.

claim_type
  observed_fact    a reported or measured observation (filings, data counts, prices on a date)
  model_estimate   output of a model, backtest, forecast, valuation or statistical estimate
  verified_result  a result checked against an independent reference, replicated, or validated on real data
  research_claim   an argument or judgement (the default)
The rule that assigned the type is stored, so every label can be audited.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import common as C

VERIFIED = re.compile(
    r"(independent(?:ly)?\s+(?:verified|checked|reference|source)|verified\s+(?:against|on|with)|validated\s+(?:against|on|with)|"
    r"cross-?check\w*\s+(?:against|with)|replicat\w*\s+(?:passed|succeeded|confirmed|holds|the paper)|reproduc\w*\s+(?:exactly|to\s+(?:the|within)|every)|"
    r"match(?:es|ed)?\s+(?:[\w\s'-]{0,40})?(?:exactly|to\s+(?:the\s+)?(?:cent|0\.\d+|within))|"
    r"\b0\s+(?:false fills|inconsistencies|mismatches|errors)|agree[sd]?\s+with\s+[\w\s'-]{0,40}to\s+(?:within|0\.\d))", re.I)
OBSERVED = re.compile(
    r"(\breported\b|\breports that\b|\bthe company reports\b|\bfiled\b|\bdisclos\w*|\baccording to\b|\bper (?:the )?(?:10-k|10-q|8-k|filing|company)\b|"
    r"\b10-k\b|\b10-q\b|\b8-k\b|\b20-f\b|\bannual report\b|\bearnings release\b|\bpress release\b|\bguided\b|\bguidance (?:of|was|for)\b|"
    r"\bthe company said\b|\bmanagement said\b|\bannounced\b|\bawarded\b|\bcontract (?:worth|valued|for)\b|\bas of \d|"
    r"\bas of (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|\bclosed at\b|\btraded at\b|\bholds? \d|\bowns\b|"
    r"\bregistry (?:shows|lists)\b|\bthe data show\b|\bdata show\b|\brecords? show\b|\bin the (?:sample|data|database)\b|"
    r"\bwe (?:observe|count)\w*|\bcounted\b|\bthere (?:are|were) \d)", re.I)
MODEL = re.compile(
    r"(\bmodel\w*|\bdcf\b|valuation|\bestimat\w*|forecast\w*|project(?:ed|ion)s?|\bimpl(?:y|ies|ied)\b|price target|target price|"
    r"fair value|base case|bear case|bull case|scenario|expected|we expect|predict\w*|simulat\w*|backtest\w*|sharpe|\balpha\b|"
    r"t-stat|\bt \d|p-value|\bp 0?\.\d|annuali[sz]ed|regression|coefficient|\bbeta\b|\bic\b|hit rate|probabilit\w*|"
    r"confidence interval|\bci\b|bootstrap|walk-forward|out-of-sample|net of costs|per year|a year)", re.I)
EVIDENCE_KINDS = [
    ("simulation", re.compile(r"(simulat\w*|synthetic|monte carlo|toy world|agent-based)", re.I)),
    ("backtest", re.compile(r"(backtest\w*|sharpe|\balpha\b|drawdown|net of costs|after costs|annual return|per year|a year|cagr|long-short|portfolio return)", re.I)),
    ("statistical_test", re.compile(r"(t-stat|\bt \d|p-value|\bp 0?\.\d|confidence interval|\bci\b|significan\w*|bootstrap|regression|hypothes\w*|\bh\d+\b)", re.I)),
    ("valuation", re.compile(r"(\bdcf\b|price target|target price|fair value|valuation|multiple|\bwacc\b|ev/|upside|downside|per share)", re.I)),
    ("forecast", re.compile(r"(forecast\w*|predict\w*|guidance|consensus|\bbeat\w*|estimate)", re.I)),
    ("filing_data", re.compile(r"(10-k|10-q|8-k|20-f|\bsec\b|filing|xbrl|annual report|edgar)", re.I)),
    ("alt_data", re.compile(r"(contract\w*|award\w*|shipment\w*|job post\w*|hiring|google trends|reddit|imports?|permit\w*|procurement|scanner)", re.I)),
    ("market_data", re.compile(r"(implied vol\w*|volatility|spread|yield|option\w*|price\w* (?:fell|rose)|order book|fills?)", re.I)),
]
CLAIM_MARKERS = re.compile(r"(\bwe (?:find|found|show|estimate|conclude|believe|think|argue|expect)|\bthe (?:result|evidence|data|test)|"
                           r"\bverdict|\bsupport(?:s|ed)?\b|\breject\w*|\bsurviv\w*|\boutperform\w*|\bunderperform\w*|\bedge\b|"
                           r"\bthesis\b|\bkey\b|\bmain\b|\bonly\b|\bno\b \w+ (?:edge|alpha|effect)|\bdoes not\b|\bdid not\b|\bbeats?\b|\bmiss\w*)", re.I)
SECTION_WEIGHT = {"summary": 1.0, "verdict": 1.0, "findings": 0.95, "thesis": 0.9, "risks": 0.85, "valuation": 0.85,
                  "limitations": 0.7, "catalysts": 0.7, "assumptions": 0.65, "method": 0.35, "body": 0.5}
DOC_WEIGHT = {"published_research": 1.0, "memo": 1.0, "report": 0.95, "paper": 0.95, "project_page": 0.85, "essay": 0.8,
              "readme": 0.8, "deck": 0.8, "note": 0.65, "notebook": 0.6, "model": 0.6, "doc": 0.55, "design_doc": 0.3,
              "technical_doc": 0.3}


@dataclass
class Finding:
    text: str
    claim_type: str
    evidence_kind: str
    section: str
    polarity: float
    numbers: list = field(default_factory=list)
    confidence: float = 0.5
    rule: str = ""
    line: int | None = None
    importance: float = 0.5
    chunk_seq: int | None = None
    companies: list = field(default_factory=list)
    themes: list = field(default_factory=list)


def classify_statement(text: str) -> tuple[str, str, float]:
    if VERIFIED.search(text):
        return "verified_result", "verified:" + VERIFIED.search(text).group(0)[:40], 0.7
    has_model = MODEL.search(text)
    has_obs = OBSERVED.search(text)
    if has_obs and not has_model:
        return "observed_fact", "observed:" + has_obs.group(0)[:40], 0.65
    if has_model:
        return "model_estimate", "model:" + has_model.group(0)[:40], 0.6
    if has_obs:
        return "observed_fact", "observed:" + has_obs.group(0)[:40], 0.55
    return "research_claim", "default", 0.5


def evidence_kind(text: str) -> str:
    for kind, rx in EVIDENCE_KINDS:
        if rx.search(text):
            return kind
    return "qualitative"


def _wanted(text: str, role: str) -> bool:
    if not 35 <= len(text) <= 650:
        return False
    if text.count("|") >= 2 or re.match(r"^(see|figure|table|source|note|click|run|install|python|pip|cd |```)", text, re.I):
        return False
    if re.search(r"(https?://\S+$|\.py\b.*\(|def |import |\$ )", text):
        return False
    has_num = bool(re.search(r"\d", text))
    if role in ("summary", "verdict", "findings", "valuation"):
        return has_num or bool(CLAIM_MARKERS.search(text))
    if role in ("thesis", "risks", "limitations", "catalysts", "assumptions"):
        return True
    # body / method sections: only statements that report a result
    return has_num and bool(CLAIM_MARKERS.search(text) or C.text_metrics(text))


def extract(chunks: list, doc_type: str, facts: list[tuple[str, str]] | None = None, max_per_doc: int = 60) -> list[Finding]:
    """chunks: objects with .seq .heading .text .line_start."""
    out: list[Finding] = []
    seen = set()
    dw = DOC_WEIGHT.get(doc_type, 0.5)
    for label, value in facts or []:
        text = f"{label}: {value}".strip(": ") if label else value
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        ct, rule, conf = classify_statement(text)
        out.append(Finding(text=text, claim_type=ct, evidence_kind=evidence_kind(text), section="key facts",
                           polarity=C.polarity(text), numbers=C.numbers(text), confidence=conf, rule="facts:" + rule,
                           importance=round(1.0 * dw, 3)))
    for ch in chunks:
        role = C.section_role(ch.heading or "")
        if role == "body" and ch.seq == 0 and doc_type in ("readme", "project_page", "published_research"):
            role = "summary"            # the opening paragraph of a README or page states what it is and found
        for off, stmt in C.statements(ch.text):
            if not _wanted(stmt, role):
                continue
            key = re.sub(r"\W+", " ", stmt.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            ct, rule, conf = classify_statement(stmt)
            nums = C.numbers(stmt)
            imp = SECTION_WEIGHT.get(role, 0.5) * dw * (1.0 if nums else 0.75) * {"verified_result": 1.15, "observed_fact": 1.0,
                                                                                    "model_estimate": 1.0, "research_claim": 0.9}[ct]
            out.append(Finding(text=stmt, claim_type=ct, evidence_kind=evidence_kind(stmt), section=(ch.heading or "")[:160],
                               polarity=C.polarity(stmt, role), numbers=nums, confidence=conf, rule=f"{role}:{rule}",
                               line=(ch.line_start or 1) + off, importance=round(imp, 3), chunk_seq=ch.seq))
    out.sort(key=lambda f: -f.importance)
    return out[:max_per_doc]
