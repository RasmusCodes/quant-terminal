"""Security master and entity linking (companies, themes, data sources) with conservative ticker rules."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .db import DB
from .taxonomy import Taxonomy
from .util import dumps, loads

EXCHANGES = r"NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSEARCA|NYSE|AMEX|TSX|TSXV|LSE|OTC|OTCQX|ASX|HKEX|Euronext|XETRA|Nasdaq Helsinki"
EXCHANGE_TAG = re.compile(r"\((?:" + EXCHANGES + r")\s*:\s*([A-Z][A-Z0-9.\-]{0,6})\)")
NAME_BEFORE_TAG = re.compile(r"((?:[A-Z][A-Za-z0-9&.\-']+(?:,)?\s){1,6})\((?:" + EXCHANGES + r")\s*:\s*([A-Z][A-Z0-9.\-]{0,6})\)")
CASHTAG = re.compile(r"(?<![A-Za-z0-9$])\$([A-Z]{1,5})(?![A-Za-z0-9])")
# words that look like tickers but are not, in prose written about finance
STOP = {"A", "I", "AI", "ALL", "ANY", "API", "ARE", "BE", "CEO", "CFO", "CSV", "DCF", "EPS", "ETF", "EUR", "FAQ", "FOR", "GDP",
        "GPU", "HTML", "IT", "IPO", "JSON", "NEW", "NOT", "ON", "ONE", "OR", "PDF", "QA", "SEC", "SQL", "TAM", "THE", "TO", "US",
        "USA", "USD", "VAR", "WACC", "YES", "NO", "OK", "UK", "EU", "CPI", "FX", "PE", "EV", "IRR", "MW", "GW", "OOS", "IS", "AT",
        "BY", "OUR", "NOW", "OUT", "LONG", "SHORT", "BUY", "SELL", "HOLD", "CASH", "BEAR", "BULL", "BASE", "TOP", "KEY", "LOW", "HIGH"}


@dataclass
class Security:
    symbol: str
    name: str
    aliases: list[str] = field(default_factory=list)
    ambiguous: bool = False
    meta: dict = field(default_factory=dict)


class EntityLinker:
    def __init__(self, taxonomy: Taxonomy, securities: list[Security]):
        self.tax = taxonomy
        self.securities = {s.symbol: s for s in securities}
        ci, cs = {}, {}
        for s in securities:
            for a in [s.name] + list(s.aliases):
                if not a:
                    continue
                if a.startswith("CS:"):
                    cs[a[3:]] = s.symbol
                elif len(a) >= 4:
                    ci[a.lower()] = s.symbol
        self.ci_map, self.cs_map = ci, cs
        self.ci_re = self._alt(sorted(ci, key=len, reverse=True), re.I)
        self.cs_re = self._alt(sorted(cs, key=len, reverse=True), 0)
        bare = [s.symbol for s in securities if not s.ambiguous and len(s.symbol) >= 3 and s.symbol not in STOP and " " not in s.symbol]
        self.bare_re = re.compile(r"(?<![A-Za-z0-9$.\-/])(" + "|".join(sorted(map(re.escape, bare), key=len, reverse=True)) + r")(?![A-Za-z0-9\-/])") if bare else None

    @staticmethod
    def _alt(words: list[str], flags: int):
        if not words:
            return None
        body = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in words)
        return re.compile(r"(?<![A-Za-z0-9])(" + body + r")(?![A-Za-z0-9])", flags)

    # -------------------------------------------------------------------------------------
    def companies(self, text: str) -> Counter:
        found: Counter = Counter()
        if not text:
            return found
        if self.ci_re:
            for m in self.ci_re.finditer(text):
                sym = self.ci_map.get(re.sub(r"\s+", " ", m.group(1).lower()))
                if sym:
                    found[sym] += 1
        if self.cs_re:
            for m in self.cs_re.finditer(text):
                sym = self.cs_map.get(re.sub(r"\s+", " ", m.group(1)))
                if sym:
                    found[sym] += 1
        if self.bare_re:
            for m in self.bare_re.finditer(text):
                found[m.group(1)] += 1
        for m in EXCHANGE_TAG.finditer(text):
            sym = m.group(1).rstrip(".")
            if sym in self.securities:
                found[sym] += 1
        for m in CASHTAG.finditer(text):
            sym = m.group(1)
            if sym in self.securities:
                found[sym] += 1
        return found

    def discover(self, text: str) -> list[tuple[str, str]]:
        """New (symbol, name) pairs written as 'Name (NASDAQ: SYM)'."""
        out = []
        for m in NAME_BEFORE_TAG.finditer(text or ""):
            sym = m.group(2).rstrip(".")
            if sym in self.securities or sym in STOP or len(sym) > 6:
                continue
            name = m.group(1).strip().rstrip(",")
            name = re.sub(r"^(?:The|And|Of|In|At|For|On|By)\s+", "", name)
            if 2 <= len(name) <= 80:
                out.append((sym, name))
        return out

    def themes(self, text: str) -> dict[str, float]:
        return {k: round(v[0], 3) for k, v in self.tax.score_themes(text).items()}

    def data_sources(self, text: str) -> dict[str, int]:
        return self.tax.find_data_sources(text)


# ----------------------------------------------------------------------------------------- persistence
def load_master(db: DB, cfg_securities: dict) -> None:
    """Seed securities/entities from config (idempotent; discovered ones are kept)."""
    for s in (cfg_securities or {}).get("securities", []):
        sym = str(s["symbol"]).strip()
        eid = db.upsert("entities", {"kind": "company", "key": f"company:{sym}", "name": s.get("name", sym), "symbol": sym,
                                      "meta": dumps({k: s.get(k) for k in ("aliases", "sector", "industry", "country", "exchange", "ambiguous")})},
                        ["key"])
        db.upsert("securities", {"symbol": sym, "name": s.get("name", sym), "isin": s.get("isin"), "exchange": s.get("exchange"),
                                 "currency": s.get("currency", "USD"), "country": s.get("country"),
                                 "asset_class": s.get("asset_class", "equity"), "sector": s.get("sector"),
                                 "industry": s.get("industry"), "yahoo": s.get("yahoo", sym), "entity_id": eid,
                                 "themes": dumps(s.get("themes") or {}), "origin": "seed"}, ["symbol"])
    db.commit()


def ensure_theme_entities(db: DB, tax: Taxonomy) -> None:
    for key, t in tax.themes.items():
        db.upsert("entities", {"kind": "theme", "key": f"theme:{key}", "name": t.get("label", key), "symbol": None,
                               "meta": dumps({"terms": t.get("terms", [])[:12]})}, ["key"])
    for key, d in tax.data_sources.items():
        db.upsert("entities", {"kind": "data_source", "key": f"data:{key}", "name": d.get("label", key), "symbol": None,
                               "meta": dumps({"kind": d.get("kind")})}, ["key"])
    db.commit()


def add_discovered(db: DB, symbol: str, name: str, origin: str = "discovered", **extra) -> None:
    if db.one("SELECT 1 FROM securities WHERE symbol=?", (symbol,)):
        return
    eid = db.upsert("entities", {"kind": "company", "key": f"company:{symbol}", "name": name, "symbol": symbol,
                                  "meta": dumps({"aliases": [], "origin": origin, **{k: v for k, v in extra.items() if v}})}, ["key"])
    db.upsert("securities", {"symbol": symbol, "name": name, "exchange": extra.get("exchange"), "currency": extra.get("currency", "USD"),
                             "yahoo": extra.get("yahoo", symbol), "entity_id": eid, "origin": origin, "themes": "{}"}, ["symbol"])


def build_linker(db: DB, tax: Taxonomy) -> EntityLinker:
    secs = []
    for r in db.all("SELECT s.symbol, s.name, e.meta FROM securities s LEFT JOIN entities e ON e.id = s.entity_id"):
        meta = loads(r["meta"], {}) or {}
        secs.append(Security(r["symbol"], r["name"] or r["symbol"], list(meta.get("aliases") or []), bool(meta.get("ambiguous")), meta))
    return EntityLinker(tax, secs)


def entity_id(db: DB, key: str) -> int | None:
    v = db.scalar("SELECT id FROM entities WHERE key=?", (key,))
    return int(v) if v is not None else None
