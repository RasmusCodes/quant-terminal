"""Configuration: committed defaults (config/terminal.yaml) + machine-local overrides (config/local.yaml) + env."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass
class Settings:
    mode: str                     # private | public
    raw: dict
    data_dir: Path
    db_path: Path
    cache_dir: Path
    taxonomy: dict = field(default_factory=dict)
    securities: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)

    # convenient accessors -------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def is_public(self) -> bool:
        return self.mode == "public"

    @property
    def owner(self) -> str:
        return os.environ.get("RTS_GITHUB_OWNER") or self.get("github.owner", "")

    @property
    def website_url(self) -> str:
        return (os.environ.get("RTS_WEBSITE_URL") or self.get("website.url", "")).rstrip("/")

    def workspace_roots(self) -> list[Path]:
        """Resolve workspace roots relative to the folder holding this repository; drop duplicates."""
        base = REPO.parent
        roots = []
        env = os.environ.get("RTS_WORKSPACE_ROOTS")
        items = env.split(os.pathsep) if env else self.get("workspace.roots", [".", ".."])
        for item in items:
            p = Path(item)
            p = (base / p).resolve() if not p.is_absolute() else p.resolve()
            if p.exists() and p.is_dir() and p not in roots:
                roots.append(p)
        return roots


def load(mode: str | None = None, data_dir: str | Path | None = None) -> Settings:
    mode = (mode or os.environ.get("RTS_MODE") or "private").lower()
    if mode not in ("private", "public"):
        raise ValueError("mode must be 'private' or 'public'")
    raw = _merge(_load_yaml(CONFIG_DIR / "terminal.yaml"), _load_yaml(CONFIG_DIR / "local.yaml") if mode == "private" else {})
    base = Path(data_dir or os.environ.get("RTS_DATA_DIR") or (REPO / "data"))
    ddir = base / mode
    ddir.mkdir(parents=True, exist_ok=True)
    cache = base / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    overrides = _merge(_load_yaml(CONFIG_DIR / "overrides.yaml"),
                       _load_yaml(CONFIG_DIR / "local.yaml").get("overrides", {}) if mode == "private" else {})
    return Settings(mode=mode, raw=raw, data_dir=ddir, db_path=ddir / "rtshf.db", cache_dir=cache,
                    taxonomy=_load_yaml(CONFIG_DIR / "taxonomy.yaml"),
                    securities=_load_yaml(CONFIG_DIR / "securities.yaml"),
                    overrides=overrides)
