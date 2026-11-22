"""Text extraction for the document formats found in research projects, plus heading-aware chunking."""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .util import clean_ws, fix_mojibake, log, parse_date, read_text

TEXT_LIMIT = 3_000_000        # characters kept per document


@dataclass
class Parsed:
    title: str = ""
    text: str = ""            # markdown-ish text; '#' headings preserved
    meta: dict = field(default_factory=dict)
    date: str | None = None
    pages: int | None = None


# ----------------------------------------------------------------------------------------- markdown
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def parse_markdown(raw: str) -> Parsed:
    raw = fix_mojibake(raw)
    meta: dict = {}
    m = _FM.match(raw)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
            if not isinstance(meta, dict):
                meta = {}
        except yaml.YAMLError:
            meta = {}
        # keep line numbers aligned with the file: blank out the front matter instead of removing it
        raw = "\n" * m.group(0).count("\n") + raw[m.end():]
    title = str(meta.get("title") or "").strip()
    if not title:
        h = re.search(r"^#\s+(.+)$", raw, re.M)
        title = h.group(1).strip() if h else ""
    date = parse_date(str(meta.get("date"))) if meta.get("date") else None
    if not date:
        head = raw[:1500]
        date = parse_date(head) if re.search(r"(prepared|as of|date|updated|written|built)", head, re.I) else None
    return Parsed(title=title, text=raw[:TEXT_LIMIT], meta=meta, date=date)


# ----------------------------------------------------------------------------------------- html
def parse_html(raw: str) -> Parsed:
    from bs4 import BeautifulSoup

    raw = re.sub(r'src="data:[^"]{200,}"', 'src=""', raw)            # embedded images
    raw = re.sub(r"url\(data:[^)]{200,}\)", "url()", raw)
    soup = BeautifulSoup(raw, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button"]):
        t.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    meta = {}
    for m in soup.find_all("meta"):
        k = m.get("name") or m.get("property")
        if k and m.get("content"):
            meta[k] = m.get("content")
    for level in range(1, 5):
        for h in soup.find_all(f"h{level}"):
            h.insert_before("\n" + "#" * level + " ")
            h.insert_after("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for br in soup.find_all(["br", "p", "div", "tr", "table", "section", "article"]):
        br.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" | ")
    text = soup.get_text()
    text = "\n".join(line.strip() for line in text.splitlines())
    text = clean_ws(text)
    date = parse_date(meta.get("article:published_time") or meta.get("date") or "") or None
    return Parsed(title=title, text=text[:TEXT_LIMIT], meta=meta, date=date)


# ----------------------------------------------------------------------------------------- pdf
def parse_pdf(path: Path, max_pages: int = 80) -> Parsed:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    n = len(reader.pages)
    parts = []
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        parts.append(f"\n[page {i + 1}]\n{t}")
    text = clean_ws("\n".join(parts))
    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:
        pass
    if not title or len(title) < 4:
        for line in text.splitlines():
            if len(line.strip()) > 8 and not line.startswith("[page"):
                title = line.strip()[:140]
                break
    return Parsed(title=title, text=text[:TEXT_LIMIT], meta={"pages": n}, date=parse_date(text[:2000]), pages=n)


# ----------------------------------------------------------------------------------------- notebooks
def parse_ipynb(raw: str) -> Parsed:
    try:
        nb = json.loads(raw)
    except ValueError:
        return Parsed()
    parts, title = [], ""
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", [])) if isinstance(cell.get("source"), list) else str(cell.get("source", ""))
        if cell.get("cell_type") == "markdown":
            parts.append(src)
            if not title:
                m = re.search(r"^#\s+(.+)$", src, re.M)
                if m:
                    title = m.group(1).strip()
        elif cell.get("cell_type") == "code":
            for out in cell.get("outputs", [])[:3]:
                txt = out.get("text") or (out.get("data") or {}).get("text/plain")
                if txt:
                    txt = "".join(txt) if isinstance(txt, list) else str(txt)
                    if len(txt) < 4000:
                        parts.append("```\n" + txt.strip() + "\n```")
    return Parsed(title=title, text=clean_ws("\n\n".join(parts))[:TEXT_LIMIT])


# ----------------------------------------------------------------------------------------- office xml
def _zip_xml_text(path: Path, members_re: str, para_tag: str) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.match(members_re, n))
        names.sort(key=lambda n: int(re.findall(r"\d+", n)[-1]) if re.findall(r"\d+", n) else 0)
        for n in names:
            xml = z.read(n).decode("utf-8", "ignore")
            for para in re.split(para_tag, xml):
                t = "".join(re.findall(r"<(?:a|w):t[^>]*>([^<]*)</(?:a|w):t>", para))
                if t.strip():
                    out.append(t)
            out.append("")
    return "\n".join(out)


def parse_docx(path: Path) -> Parsed:
    text = _zip_xml_text(path, r"word/document\.xml$", r"</w:p>")
    title = next((l.strip() for l in text.splitlines() if len(l.strip()) > 5), "")
    return Parsed(title=title[:140], text=clean_ws(text)[:TEXT_LIMIT])


def parse_pptx(path: Path) -> Parsed:
    text = _zip_xml_text(path, r"ppt/slides/slide\d+\.xml$", r"</a:p>")
    title = next((l.strip() for l in text.splitlines() if len(l.strip()) > 5), "")
    return Parsed(title=title[:140], text=clean_ws(text)[:TEXT_LIMIT])


# ----------------------------------------------------------------------------------------- spreadsheets
def parse_xlsx(path: Path, max_rows: int = 120, max_cols: int = 30) -> tuple[Parsed, dict]:
    """Searchable text (sheet names + label/value rows) and a structure dict for the model extractor."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    lines, structure = [], {"sheets": []}
    for ws in wb.worksheets[:40]:
        sheet = {"name": ws.title, "rows": []}
        lines.append(f"## Sheet: {ws.title}")
        try:
            for r_i, row in enumerate(ws.iter_rows(min_row=1, max_row=max_rows, max_col=max_cols, values_only=True)):
                vals = list(row)
                if not any(v not in (None, "") for v in vals):
                    continue
                sheet["rows"].append(vals)
                cells = [str(v) for v in vals if v not in (None, "")]
                if any(isinstance(v, str) for v in vals):
                    lines.append(" | ".join(c[:60] for c in cells[:12]))
        except Exception as exc:  # broken sheets should not stop the rest
            log.debug("xlsx sheet %s: %s", ws.title, exc)
        structure["sheets"].append(sheet)
    wb.close()
    return Parsed(title=path.stem, text=clean_ws("\n".join(lines))[:TEXT_LIMIT]), structure


# ----------------------------------------------------------------------------------------- dispatcher
FORMATS = {".md": "md", ".markdown": "md", ".txt": "txt", ".rst": "txt", ".html": "html", ".htm": "html",
           ".pdf": "pdf", ".ipynb": "ipynb", ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".xlsm": "xlsx"}


def parse_file(path: Path, fmt: str) -> tuple[Parsed, dict | None]:
    if fmt == "md":
        return parse_markdown(read_text(path)), None
    if fmt == "txt":
        t = fix_mojibake(read_text(path))[:TEXT_LIMIT]
        first = next((l.strip("# ").strip() for l in t.splitlines() if l.strip()), path.stem)
        return Parsed(title=first[:140], text=t, date=parse_date(t[:1500])), None
    if fmt == "html":
        return parse_html(read_text(path)), None
    if fmt == "pdf":
        return parse_pdf(path), None
    if fmt == "ipynb":
        return parse_ipynb(read_text(path)), None
    if fmt == "docx":
        return parse_docx(path), None
    if fmt == "pptx":
        return parse_pptx(path), None
    if fmt == "xlsx":
        return parse_xlsx(path)
    raise ValueError(f"unsupported format {fmt}")


def parse_bytes_html(raw: bytes) -> Parsed:
    for enc in ("utf-8", "cp1252"):
        try:
            return parse_html(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_html(raw.decode("utf-8", "ignore"))


# ----------------------------------------------------------------------------------------- chunking
@dataclass
class Chunk:
    seq: int
    heading: str
    text: str
    line_start: int
    line_end: int
    id: int | None = None


_HEAD = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")


def chunk_text(text: str, max_chars: int = 1400, min_chars: int = 60) -> list[Chunk]:
    """Split at markdown headings, then at paragraph boundaries; keep the heading path and line numbers."""
    lines = text.splitlines()
    sections: list[tuple[str, int, list[tuple[int, str]]]] = []
    path: list[str] = []
    cur: list[tuple[int, str]] = []
    cur_head, cur_start = "", 1
    in_code = False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEAD.match(line)
        if m:
            if cur:
                sections.append((cur_head, cur_start, cur))
            level = len(m.group(1))
            path = path[: level - 1] + [m.group(2).strip()]
            cur_head, cur_start, cur = " > ".join(path), i, []
        else:
            cur.append((i, line))
    if cur:
        sections.append((cur_head, cur_start, cur))

    chunks: list[Chunk] = []
    for head, start, body in sections:
        buf: list[tuple[int, str]] = []
        size = 0

        def flush():
            nonlocal buf, size
            txt = "\n".join(l for _, l in buf).strip()
            if len(txt) >= min_chars or (txt and head):
                chunks.append(Chunk(len(chunks), head, txt, buf[0][0] if buf else start, buf[-1][0] if buf else start))
            buf, size = [], 0

        for ln, line in body:
            if size + len(line) > max_chars and buf and (not line.strip() or size > max_chars * 1.3):
                flush()
            buf.append((ln, line))
            size += len(line) + 1
        if buf:
            flush()
    # drop empty chunks, renumber
    out = [c for c in chunks if c.text.strip()]
    for i, c in enumerate(out):
        c.seq = i
    return out
