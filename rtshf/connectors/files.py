"""Walk a project folder and pick the files worth indexing, with privacy and noise rules.

Rules are generic (no project names): heavy/third-party/secret folders are pruned, documents are taken from
anywhere else, structured results only from result-like places, and code is sampled for technology and
research-practice signals. A folder can opt out with an empty `.rtsignore` file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..util import sha1

PRUNE = {".git", ".venv", "venv", "env", ".env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
         ".ruff_cache", "target", "dist", "build", "site-packages", ".ipynb_checkpoints", "catboost_info", ".cargo",
         ".idea", ".vscode", ".claude", "raw", "cache", "caches", ".cache", "tmp", "temp", "scratch", "wf_cache",
         "sellside", "sell-side", "filings", "tiles", "logs", ".trash", "private", "secrets", "credentials", "vendor",
         "third_party", "third-party", "external", "clone_check", ".preview", "htmlcov", ".tox", ".nox", "lib64",
         ".github", "site-build", "_site", "site_build"}
LOW_PRIORITY = {"data", "datasets", "dataset", "runs", "db", "database", "outputs_raw", "downloads"}
SECRET_NAME = re.compile(r"(^\.env)|(\.pem$)|(\.key$)|(id_rsa)|(secret)|(credential)|(password)|(token)|(\.kdbx$)|(cookies?\.)", re.I)
DOC_EXT = {".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".pdf", ".ipynb", ".docx", ".pptx"}
MODEL_EXT = {".xlsx", ".xlsm"}
RESULT_EXT = {".json", ".csv", ".yaml", ".yml", ".tsv"}
CODE_EXT = {".py", ".rs", ".ts", ".tsx", ".js", ".mjs", ".r", ".jl", ".sql", ".cpp", ".go"}
ARTIFACT_EXT = {".pt", ".pth", ".pkl", ".joblib", ".onnx", ".npz", ".h5", ".cbm", ".lgb"}
MANIFESTS = {"rts.yaml", "rts.yml", "pyproject.toml", "requirements.txt", "requirements-lock.txt", "cargo.toml",
             "package.json", "setup.py", "environment.yml"}
REPORT_DIRS = {"reports", "report", "out", "output", "outputs", "paper", "papers", "docs", "doc", "memo", "memos",
               "deck", "notes", "research", "analysis", "results", "writeup", "summary", "model"}
RESULT_DIRS = {"results", "result", "outputs", "output", "out", "reports", "runs", "experiments", "registry", "metrics",
               "eval", "evaluation", "backtests", "backtest", "summary", "summaries", "book", "books", "model", "data"}
RESULT_NAME = re.compile(r"(summary|metric|result|report|book|screen|forecast|signal|eval|backtest|perform|stats|score|"
                         r"position|weight|portfolio|holding|registry|leaderboard|compare|scenario|valuation|target|rank|"
                         r"hypothes|verdict|manifest|experiment|ic_|sharpe|alpha|trades?\b)", re.I)
THIRD_PARTY_NAME = re.compile(r"(transcript|10-k|10-q|8-k|20-f|6-k|424b|s-1|press[_ -]?release|prospectus|annual[_ -]report|"
                              r"stdout|stderr|\.log$|license|copying|changelog|code_of_conduct|contributing)", re.I)
LOCK_FILE = re.compile(r"^~\$")

MAX_SIZE = {"md": 3_000_000, "txt": 1_000_000, "html": 8_000_000, "pdf": 15_000_000, "ipynb": 6_000_000,
            "docx": 20_000_000, "pptx": 25_000_000, "xlsx": 25_000_000, "result": 2_000_000, "code": 400_000}


@dataclass
class FileEntry:
    rel: str            # posix-style relative path
    size: int
    mtime: float
    role: str           # doc | model | result | code | artifact | manifest
    fmt: str            # md | html | ... | json | csv | py ...


def _is_vendored(d: Path, depth: int) -> bool:
    if depth < 2:
        return False
    try:
        names = {n.lower() for n in os.listdir(d)}
    except OSError:
        return False
    has_license = any(n.startswith(("license", "copying")) for n in names)
    has_readme = any(n.startswith("readme") for n in names)
    return has_license and has_readme


def walk(root: Path, max_files: int = 4000, max_code: int = 400) -> tuple[list[FileEntry], dict]:
    """Return selected files and walk statistics. Data-like folders are visited last."""
    entries: list[FileEntry] = []
    stats = {"listed": 0, "pruned_dirs": 0, "examined": 0, "truncated": False, "ext": {}}
    stack: list[tuple[Path, int, bool]] = [(root, 0, False)]
    code_n = 0
    result_n = 0
    while stack:
        d, depth, in_data = stack.pop(0)
        try:
            items = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            continue
        if any(e.name == ".rtsignore" for e in items):
            stats["pruned_dirs"] += 1
            continue
        names_here = {e.name.lower() for e in items}
        subdirs_hi, subdirs_lo = [], []
        for e in items:
            name = e.name
            if e.is_dir(follow_symlinks=False):
                low = name.lower()
                if low in PRUNE or low.endswith(".egg-info") or name.startswith(".") or _is_vendored(Path(e.path), depth + 1):
                    stats["pruned_dirs"] += 1
                    continue
                (subdirs_lo if (low in LOW_PRIORITY or in_data) else subdirs_hi).append((Path(e.path), depth + 1, in_data or low in LOW_PRIORITY))
                continue
            stats["listed"] += 1
            ext = os.path.splitext(name)[1].lower()
            stats["ext"][ext] = stats["ext"].get(ext, 0) + 1
            if SECRET_NAME.search(name) or LOCK_FILE.match(name):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            rel = Path(e.path).relative_to(root).as_posix()
            parts = [p.lower() for p in rel.split("/")[:-1]]
            role = fmt = None
            low = name.lower()
            if low in MANIFESTS:
                role, fmt = "manifest", low
            elif ext in DOC_EXT:
                fmt = {".markdown": "md", ".rst": "txt", ".htm": "html"}.get(ext, ext[1:])
                if _doc_wanted(fmt, low, parts, names_here, in_data):
                    role = "doc"
            elif ext in MODEL_EXT:
                role, fmt = "model", "xlsx"
            elif ext in RESULT_EXT:
                if result_n < 400 and st.st_size <= MAX_SIZE["result"] and (
                        RESULT_NAME.search(low) or (parts and parts[-1] in RESULT_DIRS and depth <= 4)):
                    role, fmt = "result", ext[1:]
                    result_n += 1
            elif ext in CODE_EXT:
                if code_n < max_code and st.st_size <= MAX_SIZE["code"] and not in_data:
                    role, fmt = "code", ext[1:]
                    code_n += 1
            elif ext in ARTIFACT_EXT:
                role, fmt = "artifact", ext[1:]
            if role is None:
                continue
            if role in ("doc", "model") and st.st_size > MAX_SIZE.get(fmt, 5_000_000):
                continue
            stats["examined"] += 1
            entries.append(FileEntry(rel, st.st_size, st.st_mtime, role, fmt))
            if stats["examined"] >= max_files:
                stats["truncated"] = True
                stack = []
                break
        stack = subdirs_hi + stack + subdirs_lo if depth == 0 else stack + subdirs_hi + subdirs_lo
    return entries, stats


OUTPUT_DIRS = {"reports", "report", "out", "output", "outputs", "paper", "papers", "memo", "memos", "deck", "writeup", "summary"}
INPUT_DIRS = {"notes", "research", "work", "docs", "doc", "analysis", "sources", "inputs", "references", "literature", "engines",
              "pages", "corresp", "jobs", "downloads", "filings", "transcripts", "web", "html", "pdf", "text", "txt"}


def _doc_wanted(fmt: str, name: str, parts: list[str], siblings: set[str], in_data: bool) -> bool:
    """Markdown is what the owner writes, so it is taken anywhere. Text, HTML and PDF files are often downloaded
    third-party material (filings, transcripts, broker notes, fetched pages); they are taken only from the project root
    or from output folders, and never from inside research-input folders."""
    stem = name.rsplit(".", 1)[0]
    if THIRD_PARTY_NAME.search(name):
        return False
    if fmt == "md":
        return True
    if in_data:
        return False
    own_output = (not parts) or (any(p in OUTPUT_DIRS for p in parts) and not any(p in INPUT_DIRS for p in parts))
    if fmt == "txt":
        return own_output and not re.search(r"(log|stdout|stderr|requirements|sources|top_level|dependency|cache)", name)
    if fmt == "html":
        if re.search(r"(dashboard|template|index|page|fragment|_files)", name):
            return False
        return own_output and f"{stem}.md" not in siblings
    if fmt == "pdf":
        # PDFs are often downloaded third-party material; take only the project's own outputs
        doc_like = re.search(r"(memo|report|paper|thesis|deck|deep[_ -]?dive|study|pitch|onepager|one-pager)", name)
        if any(p in INPUT_DIRS for p in parts):
            return False
        return (own_output or bool(doc_like)) and f"{stem}.md" not in siblings and f"{stem}.html" not in siblings
    if fmt == "ipynb":
        return True
    if fmt in ("docx", "pptx"):
        return True
    return False


def fingerprint(entries: list[FileEntry]) -> str:
    return sha1(*sorted(f"{e.rel}|{e.size}|{int(e.mtime)}" for e in entries))


def read_git_remote(folder: Path) -> str | None:
    cfg = folder / ".git" / "config"
    if not cfg.exists():
        return None
    txt = cfg.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', txt, re.S) or re.search(r"url\s*=\s*(\S+)", txt)
    return m.group(1).strip() if m else "local-git"


def github_slug(remote: str | None) -> str | None:
    if not remote:
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.\s]+?)(?:\.git)?/?$", remote)
    return f"{m.group(1)}/{m.group(2)}" if m else None
