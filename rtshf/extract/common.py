"""Shared extraction vocabulary: metric parsing, statement splitting, sentiment, section roles."""
from __future__ import annotations

import re

# ----------------------------------------------------------------------------------------- metrics
METRIC_KEYS = {
    "sharpe": r"sharpe(?:_?ratio)?", "sortino": r"sortino", "calmar": r"calmar", "alpha": r"alpha(?:_ann(?:ual)?)?",
    "t_stat": r"t_?stat(?:istic)?|tstat|t_value|nw_t|t_nw", "p_value": r"p_?val(?:ue)?|pval", "ic": r"ic|rank_?ic|mean_?ic",
    "ir": r"ir|info(?:rmation)?_?ratio", "hit_rate": r"hit_?rate|win_?rate|accuracy_?dir", "cagr": r"cagr",
    "ann_return": r"ann(?:ual(?:ized|ised)?)?_?ret(?:urn)?|mean_?ret(?:urn)?_?ann|return_?ann", "total_return": r"total_?ret(?:urn)?|cum(?:ulative)?_?ret(?:urn)?",
    "max_drawdown": r"max_?dd|max_?drawdown|mdd|drawdown", "volatility": r"vol(?:atility)?(?:_ann)?|ann_?vol|sd|std",
    "turnover": r"turnover", "beta": r"beta", "r2": r"r2|r_?squared", "mae": r"mae", "rmse": r"rmse", "mape": r"mape",
    "brier": r"brier(?:_?score)?", "log_loss": r"log_?loss", "auc": r"auc|roc_?auc", "accuracy": r"accuracy|acc",
    "pnl": r"pnl|p&l|net_?pnl", "corr": r"corr(?:elation)?", "n_obs": r"n_?obs|n|nobs|count|trades|n_?trades",
}
METRIC_KEY_RE = {k: re.compile(rf"^(?:{v})$", re.I) for k, v in METRIC_KEYS.items()}
RETURN_METRICS = {"sharpe", "sortino", "calmar", "alpha", "cagr", "ann_return", "total_return", "pnl", "ir"}


def metric_name(key: str, table: bool = False) -> str | None:
    """Map a column/key name to a metric. Bare one-letter names are ambiguous: in a results file 't' or 'T' is usually a
    sample length or a time index, so they count as statistics only as lower-case table headers ('t', 'p')."""
    raw = str(key).strip()
    if raw == "T" or raw == "N":
        return "n_obs"
    if raw in ("t", "p"):
        return ("t_stat" if raw == "t" else "p_value") if table else None
    k = re.sub(r"[\s\-./()%]+", "_", raw.lower()).strip("_")
    k = re.sub(r"_(net|gross|oos|is|test|train|mean|median|est|estimate|annual|ann|pct|bps)$", "", k)
    for name, rx in METRIC_KEY_RE.items():
        if rx.match(k):
            return name
    return None


NUM = r"[-+−–]?\d+(?:[.,]\d+)?"
TEXT_METRICS = [
    ("sharpe", re.compile(r"\bsharpe(?:\s+ratio)?(?:\s+of)?\s*(?:[=:≈~]|is|was|of)?\s*(" + NUM + r")", re.I)),
    ("t_stat", re.compile(r"(?:\bt-stat(?:istic)?s?\s*(?:of|=|:)?\s*|\(t\s*[=:]?\s*|\bt\s*=\s*|[,;]\s*t\s+)(" + NUM + r")(?![\d%])", re.I)),
    ("p_value", re.compile(r"(?:\bp-value\s*(?:of|=|:|<)?\s*|\bp\s*[=<]\s*|\(p\s*[=<]?\s*|\bH\d+[a-z]?\s+p\s+|[,;]\s*p\s+)(0?\.\d+|1\.0+)(?![\d%])", re.I)),
    ("alpha", re.compile(r"\balpha\s*(?:of|is|was|=|:)?\s*(" + NUM + r")\s*%", re.I)),
    ("ic", re.compile(r"\b(?:rank\s+)?IC\s*(?:of|=|:)?\s*(" + NUM + r")", 0)),
    ("hit_rate", re.compile(r"\b(?:hit|win)\s+rate\s*(?:of|=|:)?\s*(" + NUM + r")\s*%", re.I)),
    ("cagr", re.compile(r"\bCAGR\s*(?:of|=|:)?\s*(" + NUM + r")\s*%", re.I)),
    ("max_drawdown", re.compile(r"\b(?:max(?:imum)?\s+)?drawdown\s*(?:of|=|:)?\s*(" + NUM + r")\s*%", re.I)),
    ("brier", re.compile(r"\bbrier(?:\s+score)?\s*(?:of|=|:)?\s*(" + NUM + r")", re.I)),
    ("r2", re.compile(r"\bR(?:²|\^2|2)\s*(?:of|=|:)?\s*(" + NUM + r")", 0)),
    ("ann_return", re.compile(r"(" + NUM + r")\s*%\s*(?:a|per|each)\s+year", re.I)),
]


def to_float(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace("−", "-").replace("–", "-").rstrip("%").strip()
    s = s.replace(",", ".") if re.fullmatch(r"[-+]?\d+,\d{1,2}", s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def text_metrics(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, rx in TEXT_METRICS:
        m = rx.search(text)
        if not m:
            continue
        v = to_float(m.group(1))
        if v is None:
            continue
        if name == "t_stat" and not -40 <= v <= 40:
            continue
        if name == "sharpe" and not -10 < v < 20:
            continue
        out[name] = v
    return out


# ----------------------------------------------------------------------------------------- statements
BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+(.*)$")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“*])")


def statements(text: str) -> list[tuple[int, str]]:
    """(line offset, statement) pairs: bullet items whole, paragraphs split into sentences."""
    out: list[tuple[int, str]] = []
    para: list[str] = []
    para_start = 0
    lines = text.splitlines()

    def flush_para():
        nonlocal para
        if para:
            joined = " ".join(p.strip() for p in para)
            for s in SENT_SPLIT.split(joined):
                out.append((para_start, s.strip()))
            para = []

    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith(("|", "```", "#", "<", "![", ">")) or s.count("|") >= 3:
            flush_para()
            continue
        m = BULLET.match(line)
        if m:
            flush_para()
            out.append((i, m.group(1).strip()))
            continue
        if not para:
            para_start = i
        para.append(s)
    flush_para()
    return [(i, _clean_md(s)) for i, s in out if s]


def _clean_md(s: str) -> str:
    s = s.replace("\\*", "").replace("\\_", "_")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__|`)", "", s)
    s = re.sub(r"(?<![*\w])\*(?!\s)([^*]+?)\*(?!\w)", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------------------------------------------- sections
SECTION_ROLES = [
    ("summary", re.compile(r"(executive summary|summary|tl;?dr|in brief|bottom line|abstract|overview of results|at a glance|key takeaways?|headline|the answer|answer)", re.I)),
    ("verdict", re.compile(r"(verdict|conclusions?|recommendation|the call|call\b|decision)", re.I)),
    ("findings", re.compile(r"(findings?|results?|what (?:we|i) found|evidence|outcome|key results?|what the data (?:say|show)s?|highlights)", re.I)),
    ("thesis", re.compile(r"(investment thesis|thesis|variant view|why now|what the market (?:is )?miss|key points|the case|bull case|arguments?)", re.I)),
    ("risks", re.compile(r"(risks?|bear case|what would change|downside|what could go wrong|pre-?mortem|counter-?arguments?|key debates?)", re.I)),
    ("limitations", re.compile(r"(limitations?|caveats?|what this (?:does not|doesn't) show|threats to validity|known issues|open questions)", re.I)),
    ("valuation", re.compile(r"(valuation|price target|dcf|scenarios?|model output)", re.I)),
    ("catalysts", re.compile(r"(catalysts?|events? to watch|what to watch)", re.I)),
    ("assumptions", re.compile(r"(assumptions?|key drivers|dials|inputs|model drivers)", re.I)),
    ("method", re.compile(r"(method|methodology|data|design|how it works|approach|setup)", re.I)),
]


def section_role(heading: str) -> str:
    last = heading.split(" > ")[-1] if heading else ""
    for role, rx in SECTION_ROLES:
        if rx.search(last):
            return role
    for role, rx in SECTION_ROLES:      # fall back to the parent heading
        if heading and rx.search(heading):
            return role
    return "body"


# ----------------------------------------------------------------------------------------- sentiment
POS = re.compile(r"\b(beat|beats|outperform\w*|grow\w*|growth|upside|strong\w*|gain\w*|profit\w*|supported|surviv\w*|edge|improv\w*|"
                 r"accelerat\w*|exceed\w*|higher|record|undervalued|cheap|tailwind\w*|expand\w*|robust|significant|wins?|won|positive|"
                 r"confirm\w*|resilien\w*|underappreciated|re-?rat\w*|catalyst)\b", re.I)
NEG = re.compile(r"\b(miss\w*|underperform\w*|declin\w*|downside|weak\w*|loss\w*|lose|risk\w*|reject\w*|fail\w*|null|worse|slow\w*|"
                 r"cut\w*|impair\w*|default\w*|dilut\w*|overvalued|expensive|headwind\w*|contract\w*|negative|no edge|not significant|"
                 r"insignificant|lower|fragile|concern\w*|bubble|overbuil\w*|competition|churn|short\b|bear\w*|deteriorat\w*|disappoint\w*|"
                 r"fell|fall\w*|drop\w*|plung\w*|slump\w*|shrank|shrink\w*|tumbl\w*|squeez\w*|erod\w*|delay\w*|withdr\w*)\b", re.I)
NEG_PHRASE = re.compile(r"\b(risk to|threat to|pressure on|headwind for|at risk|downside to|worse than)\b", re.I)


def polarity(text: str, role: str = "body") -> float:
    p, n = len(POS.findall(text)), len(NEG.findall(text)) + len(NEG_PHRASE.findall(text))
    base = 0.0 if p + n == 0 else (p - n) / (p + n)
    if role == "risks":
        base -= 0.5
    elif role == "thesis":
        base += 0.2
    return max(-1.0, min(1.0, round(base, 3)))


NUMBER = re.compile(r"(?P<cur>[$€£])?\s?(?P<num>[-+−–]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|[-+−–]?\d+(?:\.\d+)?)\s?(?P<unit>%|bps|x\b|bn|billion|m\b|million|k\b|MW|GW|pp)?", re.I)


def numbers(text: str, limit: int = 8) -> list[dict]:
    out = []
    for m in NUMBER.finditer(text):
        raw = m.group(0).strip()
        if re.fullmatch(r"(19|20)\d\d", raw):          # years are context, not values
            continue
        v = to_float(m.group("num").replace(" ", ""))
        if v is None:
            continue
        out.append({"value": v, "unit": (m.group("cur") or "") + (m.group("unit") or ""), "raw": raw})
        if len(out) >= limit:
            break
    return out


YEAR_RANGE = re.compile(r"\b((?:19|20)\d\d)\s*(?:[-–—]|to|through|until)\s*((?:19|20)?\d\d)\b")


def periods(text: str, limit: int = 6) -> list[str]:
    out = []
    for m in YEAR_RANGE.finditer(text):
        a, b = m.group(1), m.group(2)
        if len(b) == 2:
            b = a[:2] + b
        if 1900 < int(a) <= int(b) <= 2100:
            p = f"{a}-{b}"
            if p not in out:
                out.append(p)
        if len(out) >= limit:
            break
    return out
