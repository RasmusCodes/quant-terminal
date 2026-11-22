"""GitHub discovery: list every repository of the owner, detect changes, keep shallow read-only clones.

Private mode uses the GitHub CLI's existing login (no token is read, printed or stored). Public mode (GitHub Actions)
uses the REST API with the workflow token or anonymously and keeps public repositories only.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..util import HttpClient, log

GH_FIELDS = ("name,nameWithOwner,description,isPrivate,isFork,isArchived,isEmpty,primaryLanguage,languages,"
             "repositoryTopics,createdAt,updatedAt,pushedAt,diskUsage,defaultBranchRef,url,homepageUrl,licenseInfo,visibility")


@dataclass
class RepoInfo:
    owner: str
    name: str
    url: str
    visibility: str
    description: str = ""
    is_fork: bool = False
    is_archived: bool = False
    is_empty: bool = False
    default_branch: str | None = None
    pushed_at: str | None = None
    created_at: str | None = None
    languages: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    license: str | None = None
    homepage: str | None = None
    disk_kb: int = 0

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def _run(args: list[str], timeout: int = 180, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd)


_GH_OK: dict = {}


def gh_available() -> bool:
    """True when the GitHub CLI is installed and logged in (checked once per process)."""
    if "ok" in _GH_OK:
        return _GH_OK["ok"]
    ok = False
    if shutil.which("gh"):
        try:
            ok = _run(["gh", "auth", "status"], timeout=30).returncode == 0
        except Exception:
            ok = False
    _GH_OK["ok"] = ok
    return ok


def list_repos(owner: str, public_only: bool, http: HttpClient | None = None) -> list[RepoInfo]:
    if not public_only and gh_available():
        return _list_gh(owner)
    return _list_rest(owner, http or HttpClient(), public_only=True)


def _list_gh(owner: str) -> list[RepoInfo]:
    p = _run(["gh", "repo", "list", owner, "--limit", "1000", "--json", GH_FIELDS], timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"gh repo list failed: {p.stderr.strip()[:300]}")
    out = []
    for r in json.loads(p.stdout or "[]"):
        langs = [l["node"]["name"] for l in (r.get("languages") or []) if isinstance(l, dict) and l.get("node")]
        out.append(RepoInfo(
            owner=(r.get("nameWithOwner") or f"{owner}/{r['name']}").split("/")[0], name=r["name"], url=r["url"],
            visibility=(r.get("visibility") or ("PRIVATE" if r.get("isPrivate") else "PUBLIC")).lower(),
            description=r.get("description") or "", is_fork=bool(r.get("isFork")), is_archived=bool(r.get("isArchived")),
            is_empty=bool(r.get("isEmpty")), default_branch=(r.get("defaultBranchRef") or {}).get("name") or None,
            pushed_at=r.get("pushedAt"), created_at=r.get("createdAt"), languages=langs,
            topics=[t["name"] for t in (r.get("repositoryTopics") or []) if isinstance(t, dict) and t.get("name")],
            license=(r.get("licenseInfo") or {}).get("key"), homepage=r.get("homepageUrl") or None,
            disk_kb=int(r.get("diskUsage") or 0)))
    return out


def _list_rest(owner: str, http: HttpClient, public_only: bool = True) -> list[RepoInfo]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    out, page = [], 1
    while page < 30:
        r = http.get(f"https://api.github.com/users/{owner}/repos", params={"per_page": 100, "page": page, "type": "owner"},
                     headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"GitHub API {r.status_code}: {r.text[:200]}")
        batch = r.json()
        if not batch:
            break
        for x in batch:
            if public_only and x.get("private"):
                continue
            out.append(RepoInfo(owner=x["owner"]["login"], name=x["name"], url=x["html_url"],
                                visibility="private" if x.get("private") else "public", description=x.get("description") or "",
                                is_fork=bool(x.get("fork")), is_archived=bool(x.get("archived")),
                                is_empty=(x.get("size", 0) == 0 and not x.get("pushed_at")),
                                default_branch=x.get("default_branch"), pushed_at=x.get("pushed_at"),
                                created_at=x.get("created_at"), languages=[x["language"]] if x.get("language") else [],
                                topics=x.get("topics") or [], license=(x.get("license") or {}).get("key"),
                                homepage=x.get("homepage") or None, disk_kb=int(x.get("size") or 0)))
        page += 1
    return out


def tracked_paths(repo: RepoInfo, http: HttpClient | None = None) -> set[str]:
    """Paths tracked on the default branch (used to give local files their public GitHub URL)."""
    if repo.is_empty or not repo.default_branch:
        return set()
    try:
        if gh_available():
            p = _run(["gh", "api", f"repos/{repo.full_name}/git/trees/{repo.default_branch}?recursive=1"], timeout=60)
            if p.returncode == 0:
                return {e["path"] for e in json.loads(p.stdout).get("tree", []) if e.get("type") == "blob"}
        http = http or HttpClient()
        headers = {"Accept": "application/vnd.github+json"}
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        r = http.get(f"https://api.github.com/repos/{repo.full_name}/git/trees/{repo.default_branch}",
                     params={"recursive": 1}, headers=headers)
        if r.status_code == 200:
            return {e["path"] for e in r.json().get("tree", []) if e.get("type") == "blob"}
    except Exception as exc:
        log.warning("tree for %s: %s", repo.full_name, exc)
    return set()


def _rmtree(path: Path) -> None:
    """Delete a cache clone (git marks pack files read-only on Windows, so clear the flag and retry)."""
    def handler(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    if path.exists():
        import sys
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=handler)
        else:
            shutil.rmtree(path, onerror=handler)


def ensure_clone(repo: RepoInfo, cache_dir: Path, max_blob_mb: int = 8, use_gh: bool = True) -> tuple[Path, str | None]:
    """Fresh shallow partial clone into cache_dir/repos/<owner>__<name>. Returns (path, head sha)."""
    base = (cache_dir / "repos").resolve()
    base.mkdir(parents=True, exist_ok=True)
    dest = (base / f"{repo.owner}__{repo.name}").resolve()
    if base not in dest.parents:                       # never touch anything outside the cache
        raise RuntimeError("refusing to clone outside the cache directory")
    tmp = dest.with_name(dest.name + ".tmp")
    _rmtree(tmp)
    git_opts = ["--depth", "1", f"--filter=blob:limit={max_blob_mb}m", "--no-tags", "-q"]
    if use_gh and gh_available():
        p = _run(["gh", "repo", "clone", repo.full_name, str(tmp), "--"] + git_opts, timeout=600)
    else:
        p = _run(["git", "clone"] + git_opts + [f"https://github.com/{repo.full_name}.git", str(tmp)], timeout=600)
    if p.returncode != 0:
        _rmtree(tmp)
        raise RuntimeError(f"clone failed: {(p.stderr or p.stdout).strip()[:300]}")
    _rmtree(dest)
    tmp.rename(dest)
    sha = _run(["git", "-C", str(dest), "rev-parse", "HEAD"], timeout=30)
    return dest, (sha.stdout.strip() if sha.returncode == 0 else None)


def cached_clone(repo: RepoInfo, cache_dir: Path) -> Path | None:
    dest = cache_dir / "repos" / f"{repo.owner}__{repo.name}"
    return dest if (dest / ".git").exists() else None
