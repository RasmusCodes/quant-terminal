"""Term matching for categories, subtopics, themes and data sources (config/taxonomy.yaml)."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass


@dataclass
class Term:
    text: str
    weight: float
    regex: re.Pattern


def _compile(term: str) -> Term:
    weight = 1.0
    if "^" in term:
        term, w = term.rsplit("^", 1)
        try:
            weight = float(w)
        except ValueError:
            pass
    cs = term.startswith("CS:")
    if cs:
        term = term[3:]
    prefix = term.endswith("*")
    core = re.escape(term.rstrip("*"))
    core = core.replace(r"\ ", r"[\s_-]+")         # "order book" also matches order_book / order-book
    pat = r"(?<![A-Za-z0-9])" + core + (r"[A-Za-z0-9-]*" if prefix else r"(?![A-Za-z0-9])")
    return Term(term, weight, re.compile(pat, 0 if cs else re.I))


class Taxonomy:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.categories: dict[str, dict] = {}
        self.subtopic_terms: dict[tuple[str, str], list[Term]] = {}
        for ckey, c in (self.cfg.get("categories") or {}).items():
            self.categories[ckey] = c
            for skey, s in (c.get("subtopics") or {}).items():
                self.subtopic_terms[(ckey, skey)] = [_compile(t) for t in s.get("terms", [])]
        self.themes: dict[str, dict] = self.cfg.get("themes") or {}
        self.theme_terms = {k: [_compile(t) for t in v.get("terms", [])] for k, v in self.themes.items()}
        self.data_sources: dict[str, dict] = self.cfg.get("data_sources") or {}
        self.ds_terms = {k: [_compile(a) for a in v.get("aliases", [])] for k, v in self.data_sources.items()}
        self.imports: dict[str, list] = self.cfg.get("imports") or {}

    # -------------------------------------------------------------------------------------
    @staticmethod
    def _count(terms: list[Term], text: str) -> tuple[float, Counter]:
        hits: Counter = Counter()
        score = 0.0
        for t in terms:
            n = len(t.regex.findall(text))
            if n:
                hits[t.text] = n
                score += t.weight * math.log1p(n)
        return score, hits

    def score_categories(self, weighted_texts: list[tuple[str, float]]) -> dict:
        """weighted_texts: [(text, multiplier)]. Returns {category: {score, subtopics: {key: score}, terms: Counter}}."""
        out: dict = {}
        for (ckey, skey), terms in self.subtopic_terms.items():
            for text, mult in weighted_texts:
                if not text:
                    continue
                s, hits = self._count(terms, text)
                if s <= 0:
                    continue
                cat = out.setdefault(ckey, {"score": 0.0, "subtopics": defaultdict(float), "terms": Counter()})
                cat["score"] += s * mult
                cat["subtopics"][skey] += s * mult
                for k, v in hits.items():
                    cat["terms"][k] += v
        return out

    def score_themes(self, text: str) -> dict[str, tuple[float, Counter]]:
        res = {}
        for key, terms in self.theme_terms.items():
            s, hits = self._count(terms, text)
            if s > 0:
                res[key] = (s, hits)
        return res

    def find_data_sources(self, text: str) -> dict[str, int]:
        found = {}
        for key, terms in self.ds_terms.items():
            n = sum(len(t.regex.findall(text)) for t in terms)
            if n:
                found[key] = n
        return found

    def category_label(self, key: str) -> str:
        if key in self.categories:
            return self.categories[key].get("label", key)
        return {"not_relevant": "Not investment-related", "reference": "Reference library"}.get(key, key)

    def subtopic_label(self, ckey: str, skey: str) -> str:
        return ((self.categories.get(ckey) or {}).get("subtopics") or {}).get(skey, {}).get("label", skey)

    def theme_label(self, key: str) -> str:
        return (self.themes.get(key) or {}).get("label", key)

    def is_investment(self, ckey: str) -> bool:
        return bool((self.categories.get(ckey) or {}).get("investment"))
