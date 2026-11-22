"""Ken French daily factors (developed markets 5 factors + momentum), cached for a week."""
from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pandas as pd

from ..util import HttpClient, log

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FILES = {"ff5": "Developed_5_Factors_Daily_CSV.zip", "mom": "Developed_Mom_Factor_Daily_CSV.zip"}
MODEL_NAME = "Fama-French developed 5 factors + momentum (daily)"
_cache: dict = {}


def _parse(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip()[:8].isdigit()) - 1
    rows = []
    header = [h.strip() for h in lines[start].split(",")]
    for l in lines[start + 1:]:
        parts = [p.strip() for p in l.split(",")]
        if not parts or not parts[0].isdigit() or len(parts[0]) != 8:
            if rows:
                break
            continue
        rows.append(parts)
    df = pd.DataFrame(rows, columns=["date"] + header[1:len(rows[0])])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.set_index("date").astype(float) / 100.0
    df.columns = [c.replace("WML", "MOM").replace("Mom", "MOM").strip() for c in df.columns]
    return df


def load(settings=None) -> pd.DataFrame | None:
    if "F" in _cache and time.time() - _cache["t"] < 3600:
        return _cache["F"]
    from ..config import REPO
    cache_dir = Path(settings.cache_dir if settings else REPO / "data" / "cache") / "factors"
    cache_dir.mkdir(parents=True, exist_ok=True)
    http = None
    frames = []
    for key, fname in FILES.items():
        path = cache_dir / fname
        if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
            try:
                http = http or HttpClient(timeout=60)
                r = http.get(BASE + fname)
                if r.status_code == 200 and r.content[:2] == b"PK":
                    path.write_bytes(r.content)
            except Exception as exc:
                log.warning("factor download %s: %s", fname, exc)
        if path.exists():
            frames.append(_parse(path.read_bytes()))
    if not frames:
        return None
    F = frames[0]
    for f in frames[1:]:
        F = F.join(f[[c for c in f.columns if c not in F.columns]], how="left")
    _cache.update(F=F, t=time.time())
    return F
