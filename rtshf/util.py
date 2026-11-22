"""Small shared helpers: hashing, time, text, logging and retrying HTTP."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("rtshf")


def setup_logging(level: str = "INFO") -> None:
    if logging.getLogger().handlers:
        logging.getLogger().setLevel(level)
        return
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return dt.date.today().isoformat()


def sha1(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, bytes):
            h.update(p)
        else:
            h.update(str(p).encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def file_sha1(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "item"


_WS = re.compile(r"[ \t\r\f\v]+")


def clean_ws(text: str) -> str:
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as cp1252 somewhere upstream (e.g. 'â€“' for an en dash)."""
    if "â€" not in text and "Ã" not in text:
        return text
    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
        return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def read_text(path: Path, limit: int | None = None) -> str:
    raw = path.read_bytes() if limit is None else path.open("rb").read(limit)
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="ignore")


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def chunks_of(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def parse_date(text: str | None) -> str | None:
    """Best-effort ISO date from strings like '2026-11-19', '19 Nov 2026', 'November 19, 2026'."""
    if not text:
        return None
    text = str(text).strip()
    m = re.search(r"(20\d\d|19\d\d)-(\d\d)-(\d\d)", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    months = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(20\d\d)", text)
    if m and m.group(2)[:3].lower() in months:
        return f"{m.group(3)}-{months[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d\d)", text)
    if m and m.group(1)[:3].lower() in months:
        return f"{m.group(3)}-{months[m.group(1)[:3].lower()]:02d}-{int(m.group(2)):02d}"
    return None


class HttpClient:
    """requests wrapper with retries, backoff and a polite identifying User-Agent (no personal data)."""

    def __init__(self, user_agent: str = "rts-quant-terminal/0.1 (+https://github.com/RasmusCodes)", timeout: int = 30,
                 retries: int = 3, backoff: float = 1.5):
        import requests  # local import keeps CLI start fast

        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def get(self, url: str, **kw):
        last = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.get(url, timeout=kw.pop("timeout", self.timeout), **kw)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = float(r.headers.get("Retry-After", 0) or 0) or self.backoff * (2 ** attempt)
                    time.sleep(min(wait, 30))
                    continue
                return r
            except Exception as exc:  # network errors: retry with backoff
                last = exc
                if attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
        if last:
            raise last
        raise RuntimeError(f"GET failed: {url}")
