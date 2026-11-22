"""Find signal-like tables (ticker + score/forecast/weight) in project outputs and map them to the standard schema.

The detected mapping is saved as an adapter (signal_sources) so it is visible, reusable and can be disabled.
"""
from __future__ import annotations

import csv
import io
import re
import statistics

TICKER_COL = re.compile(r"^(ticker|symbol|act_symbol|instrument|code|asset|name_ticker)$", re.I)
TICKER_VAL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")
VALUE_COL = re.compile(r"(pred|forecast|score|signal|rank|expected|exp_ret|alpha|weight|target|zscore|z_score|prob|edge|beat_pct|model_|"
                       r"conviction|rating)", re.I)
RETURN_COL = re.compile(r"(exp_ret|expected_return|pred_ret|forecast_ret|ret_pred|expected_ret)", re.I)
DATE_COL = re.compile(r"^(date|as_of|asof|origin_utc|origin|period_end|ts|timestamp|filed|snapshot|run_date)$", re.I)
ACTION_COL = re.compile(r"^(action|side|direction|position)$", re.I)
SKIP_VALUE = re.compile(r"(_l\d|_lag|hist|prev|actual|^y_|true|realized|realised|^rev$|^gm$|ttm|^n$|count|_id$)", re.I)


def sniff_csv(rel: str, raw: str) -> dict | None:
    try:
        rows = list(csv.reader(io.StringIO(raw)))
    except csv.Error:
        return None
    if len(rows) < 3 or len(rows) > 20000:
        return None
    header = [h.strip() for h in rows[0]]
    body = [r for r in rows[1:] if len(r) == len(header)]
    if len(body) < 2:
        return None
    tcol = next((i for i, h in enumerate(header) if TICKER_COL.match(h)), None)
    if tcol is None:
        return None
    tickers = [r[tcol].strip() for r in body]
    if sum(1 for t in tickers if TICKER_VAL.match(t)) < 0.8 * len(tickers):
        return None
    acol = next((i for i, h in enumerate(header) if ACTION_COL.match(h)), None)
    vcols = []
    for i, h in enumerate(header):
        if i == tcol or not VALUE_COL.search(h) or SKIP_VALUE.search(h):
            continue
        vals = [_f(r[i]) for r in body]
        nums = [v for v in vals if v is not None]
        if len(nums) >= max(2, 0.6 * len(body)) and len(set(nums)) > 1:
            vcols.append(i)
    if not vcols and acol is None:
        return None
    dcol = next((i for i, h in enumerate(header) if DATE_COL.match(h)), None)
    vcol = sorted(vcols, key=lambda i: (0 if RETURN_COL.search(header[i]) else 1 if re.search(r"pred|forecast|score|signal|weight", header[i], re.I) else 2))[0] if vcols else None
    confidence = 0.4 + (0.2 if vcol is not None and re.search(r"pred|forecast|signal|score|exp", header[vcol], re.I) else 0) \
        + (0.15 if dcol is not None else 0) + (0.15 if acol is not None else 0)
    return {"path": rel, "ticker_col": header[tcol], "value_col": header[vcol] if vcol is not None else None,
            "action_col": header[acol] if acol is not None else None, "date_col": header[dcol] if dcol is not None else None,
            "is_return": bool(vcol is not None and RETURN_COL.search(header[vcol])),
            "is_weight": bool(vcol is not None and re.search(r"weight", header[vcol], re.I)),
            "rows": len(body), "confidence": round(min(confidence, 0.95), 2)}


BOOK_FILE = re.compile(r"(book|position|portfolio|signal|weights|orders|trades|rts_signals)", re.I)


def auto_activate(mapping: dict, rel: str) -> bool:
    """Activate only when the meaning of 'direction' is unambiguous: an expected-return column, a signal/weight/position
    column in a book-like file, or BUY/SELL actions in a book-like file. Everything else stays a proposal."""
    name = rel.rsplit("/", 1)[-1]
    vcol = (mapping.get("value_col") or "").lower()
    if mapping.get("is_return"):
        return True
    if BOOK_FILE.search(name) and (vcol in ("signal", "weight", "position", "target_weight") or mapping.get("action_col")):
        return not re.search(r"(scenario|compare|stats|summary)", name, re.I)
    return False


def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_signals(raw: str, mapping: dict) -> list[dict]:
    """Rows of the latest as-of date -> standard signal dicts (direction, value, z-score, confidence)."""
    rows = list(csv.DictReader(io.StringIO(raw)))
    tcol, vcol, acol, dcol = mapping["ticker_col"], mapping.get("value_col"), mapping.get("action_col"), mapping.get("date_col")
    if dcol:
        dates = [r.get(dcol, "") for r in rows if r.get(dcol)]
        latest = max(dates) if dates else None
        if latest:
            rows = [r for r in rows if r.get(dcol) == latest]
    vals = [_f(r.get(vcol)) for r in rows] if vcol else []
    nums = [v for v in vals if v is not None]
    mu = statistics.fmean(nums) if len(nums) >= 2 else 0.0
    sd = statistics.pstdev(nums) if len(nums) >= 2 else 0.0
    out = []
    seen = set()
    for r in rows:
        sym = (r.get(tcol) or "").strip()
        if not TICKER_VAL.match(sym) or sym in seen:
            continue
        seen.add(sym)
        v = _f(r.get(vcol)) if vcol else None
        direction, z = 0, None
        if acol and r.get(acol):
            a = r[acol].upper()
            direction = -1 if ("SELL" in a or "SHORT" in a) else 1 if ("BUY" in a or "LONG" in a) else 0
        if v is not None:
            if mapping.get("is_weight") or mapping.get("is_return"):
                direction = direction or (1 if v > 0 else -1 if v < 0 else 0)
            if sd > 0:
                z = (v - mu) / sd
                if not direction and not mapping.get("is_weight"):
                    direction = 1 if z > 0.5 else -1 if z < -0.5 else 0
        conf = round(min(0.9, 0.3 + (abs(z) / 4 if z is not None else 0.2)), 3)
        out.append({"instrument": sym, "value": v, "z": None if z is None else round(z, 3), "direction": direction,
                    "expected_return": v if mapping.get("is_return") else None, "confidence": conf,
                    "as_of": r.get(dcol) if dcol else None})
    return out
