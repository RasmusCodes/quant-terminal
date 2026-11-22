"""Investment theses: stance, price target, price at the time, scenarios, arguments, risks and assumptions.

Two routes:
  1. structured - a call stated as data (front matter `rating/price/target/weighted/scenarios`, or the summary sheet of a
     published research page). This is exact and preferred.
  2. text - an explicit call sentence ("Call: BUY ...", "Recommendation: WATCHLIST", "We are short FTAI") near the top of
     the owner's own output documents (memos, reports, decks, project READMEs). Downloaded third-party material (broker
     notes, filings, transcripts, letters) and research-input folders are never read for calls.
A thesis needs an identifiable company. Theses are versioned by content; nothing here changes a thesis on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import common as C

RATING = (r"(strong buy|buy|outperform|overweight|accumulate|long|hold|neutral|market[- ]perform|equal[- ]weight|watchlist|watch|"
          r"underweight|underperform|reduce|sell|short|pair)")
STANCE_MAP = {"strong buy": "long", "buy": "long", "outperform": "long", "overweight": "long", "accumulate": "long", "long": "long",
              "hold": "neutral", "neutral": "neutral", "market perform": "neutral", "equal weight": "neutral", "watchlist": "neutral",
              "watch": "neutral", "underweight": "short", "underperform": "short", "reduce": "short", "sell": "short",
              "short": "short", "pair": "relative_value"}
STANCE_PATTERNS = [
    # a labelled call at the start of a line, after bold markers or in a table cell
    re.compile(r"(?:^|\n|\*\*|\|)\s*(?:the\s+)?(?:call|rating|recommendation|verdict|stance|position|our view)\s*\**\s*[:\-–—|]\s*\**\s*" + RATING + r"\b", re.I),
    # a first-person call
    re.compile(r"\b(?:we are|i am|i'm|we're|we recommend|i recommend|we rate|i rate|initiat\w+ (?:at|with)|reiterat\w+)\s+(?:a\s+|an\s+)?\**" + RATING + r"\b", re.I),
]
NEGATION = re.compile(r"(\bnot\b|\bno\b|\bnothing\b|\bnever\b|\bneither\b|\bnor\b|isn't|aren't|without)\W+(?:\w+\W+){0,4}$", re.I)
PRICE = r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)"
TARGET = re.compile(r"(?:price target|target price|12[- ]month (?:price )?target|fair value|dcf value|intrinsic value|value per share|"
                    r"base[- ]case value)\s*(?:of|is|at|:|=)?\s*(?:\w+\s){0,2}?" + PRICE, re.I)
WEIGHTED = re.compile(r"(?:probability[- ]weighted|weighted)\s*(?:value|target|dcf)?\s*(?:of|is|at|:|=)?\s*" + PRICE
                      + r"|" + PRICE + r"\s*(?:\(|\s)?(?:probability[- ])?weighted"
                      + r"|weighting[^$\n]{0,80}?(?:gives|is|=)\s*" + PRICE, re.I)
SCEN = re.compile(r"\b(stress|bear|base|bull)(?:\s+case)?\s*(?:value|target)?\s*(?:of|is worth|is|worth|at|gives|:|=)?\s*(?:about\s+)?" + PRICE, re.I)
SPOT = re.compile(r"(?:vs\.?|versus|against|current(?:ly)? (?:share )?price(?: of)?|share price(?: of)?|closed? at|close of|"
                  r"trading at|priced at|(?:short|long|buy|sell|watchlist) at|no position at|price(?:,| of| on)?)\s*" + PRICE, re.I)
HORIZON = re.compile(r"(\d{1,2})[- ]month|(\d)[- ]year", re.I)
ARG_ROLES = {"thesis", "summary", "verdict", "findings"}
OUTPUT_DIRS = {"reports", "report", "out", "output", "outputs", "memo", "memos", "deck", "paper", "summary", "writeup"}
INPUT_DIRS = {"notes", "research", "work", "data", "engines", "sources", "raw", "cache", "transcripts", "corresp", "pages",
              "jobs", "sellside", "downloads", "filings", "inputs", "references", "docs", "scratch"}
THIRD_PARTY = re.compile(r"(lseg|refinitiv|morgan|jpmorgan|j\.p\.|goldman|bnp|deutsche|ubs|jefferies|evercore|guggenheim|seaport|"
                         r"plunkett|new constructs|barclays|citi|bofa|wells fargo|transcript|corresp|upload|10-k|10-q|8-k|20-f|"
                         r"marketbeat|seeking ?alpha|fool\.com|roic|press[_ -]?release|prospectus|form_answer)", re.I)


@dataclass
class Thesis:
    rating: str
    stance: str
    symbol: str | None
    target: float | None = None
    spot: float | None = None
    scenarios: dict = field(default_factory=dict)
    horizon: str | None = None
    summary: str = ""
    arguments: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    catalysts: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    evidence: str = ""
    line: int | None = None
    method: str = "text"
    extra: dict = field(default_factory=dict)

    @property
    def upside(self) -> float | None:
        if self.target and self.spot:
            return round(self.target / self.spot - 1, 4)
        return None


def _price(s) -> float | None:
    v = C.to_float(str(s).replace(",", "").replace("$", "")) if s is not None else None
    return v if v and v > 0 else None


def plausible(target: float | None, spot: float | None) -> tuple[float | None, float | None]:
    """Drop prices that cannot belong together (a $2 'spot' next to a $316 target is a different number)."""
    if target and spot and not (0.15 <= target / spot <= 6.0):
        return target, None
    return target, spot


def eligible(rel: str, doc_type: str, title: str = "") -> bool:
    """Only the owner's own output documents are read for calls."""
    low = rel.lower()
    parts = low.split("/")
    name = parts[-1]
    if THIRD_PARTY.search(name) or THIRD_PARTY.search(title or ""):
        return False
    dirs = parts[:-1]
    if any(p in INPUT_DIRS for p in dirs) and not re.search(r"(memo|thesis|pitch|deck)", name):
        return False
    if doc_type == "readme":
        return len(parts) == 1
    if doc_type in ("memo", "deck"):
        return True
    if doc_type in ("report", "paper"):
        return len(parts) == 1 or any(p in OUTPUT_DIRS for p in dirs) or bool(re.search(r"(memo|pitch|deep|stock|summary|thesis|ic_)", name))
    return False


def find_stance(text: str, title: str = "") -> tuple[str, str, int] | None:
    head = text[:3500]
    best = None
    for rx in STANCE_PATTERNS:
        for m in rx.finditer(head):
            before = head[max(0, m.start() - 50): m.start()]
            after = head[m.end(): m.end() + 2]
            if NEGATION.search(before) or after.startswith("/") or before.endswith("/"):
                continue
            rating = m.group(1).lower().replace("-", " ")
            if best is None or m.start() < best[2]:
                best = (rating, STANCE_MAP.get(rating, "neutral"), m.start())
            break
    if best is None:                         # file or deck titles such as "FTAI_Point72_LONG_Deck"
        m = re.search(r"(?:^|[_\s-])(LONG|SHORT)(?:[_\s-]|$)", title)
        if m:
            best = (m.group(1).lower(), STANCE_MAP[m.group(1).lower()], 0)
    return best


def _sections(th: Thesis, chunks: list) -> None:
    for ch in chunks:
        role = C.section_role(ch.heading or "")
        bullets = [s for _, s in C.statements(ch.text) if 30 <= len(s) <= 500]
        if role in ARG_ROLES and len(th.arguments) < 10:
            th.arguments += [b for b in bullets if b not in th.arguments][: 10 - len(th.arguments)]
        elif role == "risks" and len(th.risks) < 10:
            th.risks += bullets[: 10 - len(th.risks)]
        elif role == "catalysts" and len(th.catalysts) < 8:
            th.catalysts += bullets[: 8 - len(th.catalysts)]
        elif role in ("assumptions", "valuation") and len(th.assumptions) < 12:
            th.assumptions += [b for b in bullets if re.search(r"\d", b)][: 12 - len(th.assumptions)]


def from_structured(call: dict, symbol: str, chunks: list, title: str = "") -> Thesis | None:
    """call: {rating, rating_label, price, target, target_label, weighted, weighted_label, scenarios, horizon, conviction}."""
    rating = str(call.get("rating") or "").strip().lower().replace("-", " ")
    if not rating or not symbol:
        return None
    stance = STANCE_MAP.get(rating, "relative_value" if "pair" in rating or "+" in str(call.get("rating_label") or "") else "neutral")
    weighted, base = _price(call.get("weighted")), _price(call.get("target"))
    th = Thesis(rating=(call.get("rating_label") or rating).upper(), stance=stance, symbol=symbol, method="structured")
    th.target = weighted or base
    th.spot = _price(call.get("price"))
    for s in call.get("scenarios") or []:
        if isinstance(s, dict) and s.get("label") and _price(s.get("value")):
            th.scenarios[str(s["label"]).lower()] = _price(s["value"])
            if s.get("prob") is not None:
                th.extra.setdefault("probabilities", {})[str(s["label"]).lower()] = s["prob"]
    if base and weighted and "base" not in th.scenarios:
        th.scenarios["base"] = base
    th.horizon = str(call.get("horizon") or ("12m" if th.target else "")) or None
    th.extra.update({k: call.get(k) for k in ("conviction", "target_label", "weighted_label", "price_date", "currency") if call.get(k)})
    bits = [th.rating]
    if th.spot:
        bits.append(f"at ${th.spot:g}")
    if weighted:
        bits.append(f"probability-weighted ${weighted:g}")
    if base:
        bits.append(f"{call.get('target_label') or 'base'} ${base:g}")
    th.evidence = " · ".join(bits)
    th.summary = th.evidence
    _sections(th, chunks)
    return th


def extract(text: str, chunks: list, title: str, symbol_hint: str | None, dominant_symbol: str | None) -> Thesis | None:
    st = find_stance(text, title)
    if not st:
        return None
    rating, stance, pos = st
    symbol = symbol_hint or dominant_symbol
    if not symbol:
        return None
    strip = lambda s: re.sub(r"(\*\*|__|`|(?<!\w)\*(?!\s))", "", s)      # markdown emphasis around numbers
    window = strip(text[max(0, pos - 300): pos + 1500])
    th = Thesis(rating=rating.upper(), stance=stance, symbol=symbol)
    th.evidence = C._clean_md(text[max(0, pos - 60): pos + 360].strip().replace("\n", " "))
    th.line = text[:pos].count("\n") + 1
    m = WEIGHTED.search(window) or TARGET.search(window)
    if not m:                                   # the target is often stated a little further down the summary
        wide = strip(text[:8000])
        m2 = WEIGHTED.search(wide) or TARGET.search(wide)
        if m2:
            window, m = wide, m2
    if m:
        th.target = _price(next(g for g in m.groups() if g))
        sp = SPOT.search(window[m.end(): m.end() + 160])
        if sp:
            th.spot = _price(sp.group(1))
    for sm in SCEN.finditer(strip(text[:20000])):
        name, val = sm.group(1).lower(), _price(sm.group(2))
        if val and name not in th.scenarios:
            th.scenarios[name] = val
    if th.target is None and "base" in th.scenarios:
        th.target = th.scenarios["base"]
    ref = th.target or th.spot
    if ref and th.scenarios:                    # "$1.1bn" next to "bear" is not a per-share value
        th.scenarios = {k: v for k, v in th.scenarios.items() if 0.15 <= v / ref <= 6}
    if th.spot is None:
        sp = SPOT.search(window)
        if sp and (th.target is None or _price(sp.group(1)) != th.target):
            th.spot = _price(sp.group(1))
    th.target, th.spot = plausible(th.target, th.spot)
    h = HORIZON.search(window)
    th.horizon = (f"{h.group(1)}m" if h.group(1) else f"{int(h.group(2)) * 12}m") if h else ("12m" if th.target else None)
    _sections(th, chunks)
    th.summary = th.evidence[:400]
    return th
