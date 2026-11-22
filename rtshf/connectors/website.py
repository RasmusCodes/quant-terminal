"""Website connector: crawl the live site from its sitemap and (in private mode) read the site's local source.

The live crawl decides what is published. The local source adds exact front matter (the `source:` path of a research
note, project links, drafts), which links pages to the projects they describe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml

from ..textproc import parse_html, parse_markdown
from ..util import HttpClient, clean_ws, log, parse_date, read_text, sha1

SECTIONS = ("research", "projects", "essays", "portfolio", "pages")


@dataclass
class WebPage:
    url: str
    path: str                      # e.g. /research/netflix-between-earnings/
    section: str                   # research | projects | essays | portfolio | about | home
    slug: str
    title: str = ""
    subtitle: str = ""
    kind: str = ""                 # e.g. "Quant study", "Investment memo"
    date: str | None = None
    tags: list[str] = field(default_factory=list)
    ticker: str | None = None
    company: str | None = None
    facts: list[tuple[str, str]] = field(default_factory=list)
    status: str | None = None
    stack: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    text: str = ""
    content_hash: str = ""
    published: bool = True
    draft: bool = False
    source_hint: str | None = None  # front matter `source:` (relative path to the project file)
    local_file: str | None = None
    category: str | None = None
    tickers: list[str] = field(default_factory=list)
    call: dict = field(default_factory=dict)   # structured call: rating, price, target, weighted, scenarios, ...


CALL_KEYS = ("rating", "rating_label", "conviction", "horizon", "currency", "price", "price_date", "target", "target_label",
             "weighted", "weighted_label", "scenarios")


def _money(s: str) -> float | None:
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_sheet_call(soup) -> dict:
    """The summary sheet of a published research page: rating pill, price, base/weighted values, scenarios."""
    sheet = soup.select_one("section.sheet")
    if not sheet:
        return {}
    call: dict = {}
    pill = sheet.select_one(".sheet__head .pill")
    label = pill.get_text(" ", strip=True) if pill else ""
    mod = next((c.split("--", 1)[1] for c in sheet.get("class", []) if c.startswith("sheet--")), "")
    low = label.lower()
    call["rating_label"] = label or None
    call["rating"] = low if low in ("buy", "sell", "short", "long", "hold", "neutral", "watch", "watchlist") else \
        ("pair" if "+" in label else (mod if mod in ("short", "long") else ("watch" if "watch" in low else None)))
    for div in sheet.select(".sheet__facts > div"):
        dt_, dd = div.find("dt"), div.find("dd")
        if not dt_ or not dd:
            continue
        k, v = dt_.get_text(" ", strip=True), dd.get_text(" ", strip=True)
        kl = k.lower()
        combined = "/" in k or "/" in v          # e.g. "DCF base / weighted: $87 / $79" is a secondary method, not the call
        if kl.startswith("price") and "price" not in call:
            call["price"] = _money(v)
            call["price_date"] = k.split(",", 1)[1].strip() if "," in k else None
        elif "weighted" in kl and not combined and "weighted" not in call:
            call["weighted"], call["weighted_label"] = _money(v), k
        elif ("value" in kl or "target" in kl) and not combined and _money(v) and "target" not in call:
            call["target"], call["target_label"] = _money(v), k
        elif kl == "horizon":
            call["horizon"] = v
        elif kl == "conviction":
            call["conviction"] = v
    scen = []
    for lbl in sheet.select(".range__lbl"):
        b, span = lbl.find("b"), lbl.find("span")
        if b and span:
            m = re.search(r"\$\s?([\d,.]+)\s*·\s*(\d+)%", span.get_text(" ", strip=True))
            if m:
                scen.append({"label": b.get_text(strip=True), "value": float(m.group(1).replace(",", "")), "prob": int(m.group(2))})
    if scen:
        call["scenarios"] = scen
    return {k: v for k, v in call.items() if v not in (None, "")} if call.get("rating") else {}


def _section_of(path: str) -> tuple[str, str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "home", ""
    if parts[0] in ("research", "projects", "essays") and len(parts) >= 2:
        return parts[0], parts[1]
    if parts[0] == "portfolio":
        return "portfolio", parts[1] if len(parts) > 1 else ""
    if parts[0] == "about":
        return "about", "about"
    return parts[0], parts[-1]


def sitemap_urls(base: str, http: HttpClient) -> list[str]:
    r = http.get(base + "/sitemap.xml")
    if r.status_code != 200:
        raise RuntimeError(f"sitemap {r.status_code}")
    return re.findall(r"<loc>([^<]+)</loc>", r.text)


def parse_live_page(url: str, html: str) -> WebPage:
    from bs4 import BeautifulSoup

    path = urlparse(url).path or "/"
    section, slug = _section_of(path)
    soup = BeautifulSoup(html, "html.parser")
    page = WebPage(url=url, path=path, section=section, slug=slug)

    def txt(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else ""

    page.title = txt(".post__title") or (soup.title.get_text(strip=True) if soup.title else "")
    page.subtitle = txt(".post__sub")
    page.kind = txt(".kicker__kind")
    t = soup.select_one("time[datetime]")
    page.date = parse_date(t["datetime"]) if t else None
    page.tags = sorted({a.get_text(strip=True) for a in soup.select("a.tag") if a.get_text(strip=True)})
    tick = txt(".sheet__ticker")
    page.tickers = [t for t in re.split(r"[\s,·/]+", tick) if re.fullmatch(r"[A-Z][A-Z0-9.]{0,6}", t)] if tick else []
    page.ticker = page.tickers[0] if len(page.tickers) == 1 else None
    page.call = parse_sheet_call(soup)
    page.company = txt(".sheet__company") or None
    page.status = txt(".pstatus") or None
    page.stack = [s.get_text(strip=True) for s in soup.select(".stack li, .stack span") if s.get_text(strip=True)][:20]
    facts = soup.select_one(".sheet__facts")
    if facts:
        dts = facts.find_all("dt")
        if dts:
            for dt_ in dts:
                dd = dt_.find_next_sibling("dd")
                page.facts.append((dt_.get_text(" ", strip=True), dd.get_text(" ", strip=True) if dd else ""))
        else:
            for item in facts.find_all(["li", "div"], recursive=False):
                parts = [x.get_text(" ", strip=True) for x in item.find_all(recursive=False)] or [item.get_text(" ", strip=True)]
                if len(parts) >= 2:
                    page.facts.append((parts[0], " ".join(parts[1:])))
                elif parts and parts[0]:
                    page.facts.append(("", parts[0]))
    for a in soup.select(".sheet__files a, .post__body a, .prose a"):
        href = a.get("href") or ""
        if href.startswith("http") and "github.com" in href:
            page.links.append(href.split("#")[0])
    page.links = sorted(set(page.links))
    body = soup.select_one(".post__body") or soup.select_one("main") or soup.body
    parsed = parse_html(str(body)) if body else parse_html(html)
    head = "\n".join(x for x in [f"# {page.title}", page.subtitle,
                                 "\n".join(f"- {k}: {v}" for k, v in page.facts)] if x)
    page.text = clean_ws(head + "\n\n" + parsed.text)
    page.content_hash = sha1(page.text)
    return page


def crawl(base: str, http: HttpClient, previous_hashes: dict[str, str] | None = None) -> tuple[list[WebPage], list[str]]:
    """Fetch every sitemap page in the research/projects/essays/portfolio/about sections."""
    errors: list[str] = []
    pages: list[WebPage] = []
    for url in sitemap_urls(base, http):
        path = urlparse(url).path
        section, _ = _section_of(path)
        if section not in ("research", "projects", "essays", "portfolio", "about") or path.rstrip("/") in ("/research", "/projects", "/essays"):
            continue
        try:
            r = http.get(url)
            if r.status_code != 200:
                errors.append(f"{url}: HTTP {r.status_code}")
                continue
            r.encoding = "utf-8"
            pages.append(parse_live_page(url, r.text))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    return pages, errors


# ----------------------------------------------------------------------------------------- local source
def find_local_source(project_dirs: list[Path], site_url: str) -> Path | None:
    """The workspace folder whose site.yaml points at the website."""
    host = urlparse(site_url).netloc
    for d in project_dirs:
        cfg = d / "site.yaml"
        if cfg.exists():
            try:
                data = yaml.safe_load(read_text(cfg)) or {}
            except yaml.YAMLError:
                continue
            if host and host in str(data.get("url", "")) and (d / "content").is_dir():
                return d
    return None


def read_local_source(site_dir: Path, base: str, live_paths: set[str]) -> list[WebPage]:
    pages: list[WebPage] = []
    content = site_dir / "content"
    for section in ("research", "projects", "essays"):
        for f in sorted((content / section).glob("*.md")):
            parsed = parse_markdown(read_text(f))
            fm = parsed.meta
            slug = f.stem
            path = f"/{section}/{slug}/"
            draft = bool(fm.get("draft"))
            raw_t = fm.get("ticker")
            tickers = [str(t).strip() for t in raw_t] if isinstance(raw_t, list) else ([str(raw_t).strip()] if raw_t else [])
            page = WebPage(url=base + path, path=path, section=section, slug=slug, title=parsed.title,
                           subtitle=str(fm.get("subtitle") or ""), kind=str(fm.get("kind") or ""),
                           date=parse_date(str(fm.get("date") or "")) or parsed.date,
                           tags=[str(t) for t in (fm.get("tags") or [])], ticker=tickers[0] if len(tickers) == 1 else None,
                           tickers=tickers, call={k: fm[k] for k in CALL_KEYS if fm.get(k) not in (None, "")} if fm.get("rating") else {},
                           company=(str(fm["company"]) if fm.get("company") else None), status=fm.get("status"),
                           stack=[str(s) for s in (fm.get("stack") or [])], draft=draft,
                           published=(path in live_paths) and not draft, source_hint=fm.get("source"),
                           local_file=str(f), category=fm.get("category"))
            for key in ("facts", "highlights"):
                for item in fm.get(key) or []:
                    if isinstance(item, dict):
                        page.facts.append((str(item.get("label", "")), str(item.get("value", ""))))
            links = []
            for item in fm.get("links") or []:
                if isinstance(item, dict) and item.get("url"):
                    links.append(str(item["url"]))
                elif isinstance(item, str):
                    links.append(item)
            links += re.findall(r"https?://github\.com/[^\s)\"'>]+", parsed.text)
            page.links = sorted({l.rstrip(".,") for l in links})
            page.text = parsed.text             # line numbers stay aligned with the source file
            page.content_hash = sha1(page.text, page.facts, page.draft, page.published)
            pages.append(page)
    return pages
