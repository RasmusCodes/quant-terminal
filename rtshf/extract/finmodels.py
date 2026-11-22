"""Financial models (Excel): type, key outputs and assumptions, read from cached cell values (never recalculated)."""
from __future__ import annotations

import re

OUTPUT_LABELS = [
    ("price_target", re.compile(r"(price target|target price|12[- ]?m(?:onth)? target|probability[- ]weighted (?:value|target|price)|weighted (?:value|target))", re.I)),
    ("value_per_share", re.compile(r"(value per share|implied (?:share )?price|intrinsic value|fair value|dcf value|equity value per share|implied value)", re.I)),
    ("upside", re.compile(r"(upside|downside|implied return)", re.I)),
    ("wacc", re.compile(r"^\s*wacc\b|weighted average cost of capital", re.I)),
    ("cost_of_equity", re.compile(r"cost of equity", re.I)),
    ("terminal_growth", re.compile(r"terminal (?:growth|g)\b|perpetuity growth", re.I)),
    ("enterprise_value", re.compile(r"^\s*enterprise value|^\s*\bEV\b", re.I)),
    ("equity_value", re.compile(r"^\s*equity value", re.I)),
    ("irr", re.compile(r"\birr\b", re.I)),
    ("current_price", re.compile(r"(current (?:share )?price|share price|last price|price today)", re.I)),
    ("bear_value", re.compile(r"^\s*bear\b", re.I)),
    ("base_value", re.compile(r"^\s*base\b", re.I)),
    ("bull_value", re.compile(r"^\s*bull\b", re.I)),
]
TYPE_HINTS = [
    ("dcf", re.compile(r"(dcf|discounted cash|free cash flow|terminal value|wacc)", re.I)),
    ("wacc", re.compile(r"(\bwacc\b|cost of equity|levered beta|unlevered beta)", re.I)),
    ("comps", re.compile(r"(comps|comparable|peer multiples|trading multiples)", re.I)),
    ("operating", re.compile(r"(operating model|revenue build|segment|income statement|quarterly|p&l|unit economics|households|subscribers)", re.I)),
    ("scenario", re.compile(r"(bear|bull|scenario|sensitivity)", re.I)),
    ("data", re.compile(r"(data|raw|inputs?|series|tracker)", re.I)),
]
ASSUMPTION_SHEETS = re.compile(r"(assumption|input|driver|dial|parameter|setting)", re.I)


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


PRICE_KEYS = {"price_target", "value_per_share", "current_price", "bear_value", "base_value", "bull_value"}
RATE_KEYS = {"wacc", "cost_of_equity", "terminal_growth", "irr"}


def valid(key: str, v: float | None) -> bool:
    """Plausibility per output type, so a probability or a share count next to a label is not read as the value."""
    if v is None or v == 0:
        return False
    if key in PRICE_KEYS:
        return 0.5 <= v <= 1e5
    if key in ("wacc", "cost_of_equity"):
        return 0.02 <= v <= 0.4 or 2 <= v <= 40
    if key == "terminal_growth":
        return -0.05 <= v <= 0.08 or -5 <= v <= 8
    if key == "irr":
        return -1 <= v <= 5
    if key == "upside":
        return -1 <= v <= 10
    if key in ("enterprise_value", "equity_value"):
        return v > 0
    return True


def analyse(structure: dict, name: str) -> dict:
    sheets = structure.get("sheets", [])
    names = [s["name"] for s in sheets]
    blob = " ".join(names) + " " + " ".join(
        str(c) for s in sheets[:12] for r in s["rows"][:60] for c in r if isinstance(c, str))[:40000]
    types = [t for t, rx in TYPE_HINTS if rx.search(blob) or rx.search(name)]
    model_type = types[0] if types else "spreadsheet"
    outputs, assumptions = [], []
    for s in sheets:
        for r_i, row in enumerate(s["rows"]):
            for c_i, cell in enumerate(row):
                if not isinstance(cell, str) or len(cell) > 80:
                    continue
                for key, rx in OUTPUT_LABELS:
                    if rx.search(cell):
                        val = next((_num(v) for v in row[c_i + 1: c_i + 8] if valid(key, _num(v))), None)
                        if val is None and r_i + 1 < len(s["rows"]) and c_i < len(s["rows"][r_i + 1]):
                            below = _num(s["rows"][r_i + 1][c_i])
                            val = below if valid(key, below) else None
                        if val is not None and len(outputs) < 40:
                            outputs.append({"key": key, "label": cell.strip()[:80], "value": round(val, 6), "sheet": s["name"],
                                            "cell": f"R{r_i + 1}C{c_i + 1}"})
                        break
            if ASSUMPTION_SHEETS.search(s["name"]) and len(assumptions) < 60:
                label = next((c for c in row if isinstance(c, str) and 2 < len(c) < 90), None)
                val = next((_num(c) for c in row if _num(c) is not None), None)
                if label and val is not None:
                    assumptions.append({"label": label.strip(), "value": round(val, 6), "sheet": s["name"]})
    # one output per key (first found), keeping the sheet that looks most like a summary
    summary_first = sorted(outputs, key=lambda o: (0 if re.search(r"(summary|output|valuation|dcf|cover)", o["sheet"], re.I) else 1))
    seen, primary = set(), []
    for o in summary_first:
        if o["key"] not in seen:
            seen.add(o["key"])
            primary.append(o)
    return {"model_type": model_type, "types": types, "sheets": names, "outputs": primary, "all_outputs": outputs[:40],
            "assumptions": assumptions}
