"""Command line: python -m rtshf <command>."""
from __future__ import annotations

import argparse
import json
import sys

from . import config
from .util import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rtshf", description="RTS HF quant terminal")
    ap.add_argument("--mode", choices=["private", "public"], default=None, help="private (local, everything) or public (public sources only)")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="discover and ingest everything that changed")
    p.add_argument("--full", action="store_true", help="re-extract every file")
    p.add_argument("--only", default=None, help="comma list of github,local,website")
    p.add_argument("--trigger", default="manual")
    sub.add_parser("status", help="registry and health summary")
    p = sub.add_parser("search", help="hybrid search")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", type=int, default=10)
    p = sub.add_parser("ask", help="question answering with sources")
    p.add_argument("question", nargs="+")
    p = sub.add_parser("serve", help="run the terminal (API + UI)")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p = sub.add_parser("export", help="static public build (public mode data only)")
    p.add_argument("--out", default="site")
    p = sub.add_parser("import-portfolio", help="import a CSV of trades or holdings into a portfolio")
    p.add_argument("csv")
    p.add_argument("--portfolio", required=True, help="portfolio key, created if missing")
    p.add_argument("--name", default=None)
    p.add_argument("--kind", default="paper", choices=["paper", "real", "game"])
    sub.add_parser("health", help="sync health as JSON")
    p = sub.add_parser("inventory", help="write the project inventory as markdown (private: names private repositories)")
    p.add_argument("--out", default=None, help="default: data/<mode>/reports/INVENTORY.md")
    args = ap.parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    settings = config.load(args.mode, args.data_dir)

    if args.cmd == "sync":
        from . import sync
        only = set(args.only.split(",")) if args.only else None
        res = sync.run(settings, trigger=args.trigger, full=args.full, only=only)
        print(json.dumps(res, indent=1, default=str))
        return 0 if res["status"] in ("ok", "partial") else 1
    if args.cmd == "status":
        from .report import status_text
        print(status_text(settings))
        return 0
    if args.cmd == "search":
        from .db import DB
        from .search import Searcher
        db = DB(settings.db_path)
        for h in Searcher(db, settings).search(" ".join(args.query), limit=args.n):
            loc = h["url"] or h["path"]
            print(f"[{h['score']:.1f}] {h['project']} | {h['title']} > {h['heading'][:60]}  ({loc}:{h['line_start']})")
            print("    " + h["text"][:220].replace("\n", " "))
        return 0
    if args.cmd == "ask":
        from .db import DB
        from .qa import answer
        db = DB(settings.db_path)
        res = answer(db, settings, " ".join(args.question))
        print(res["answer"])
        print("\nSources:")
        for i, e in enumerate(res["evidence"][:15], 1):
            print(f"  [{i}] {e.get('project')} | {e.get('title')} | {e.get('url') or e.get('path')}:{e.get('line') or ''} ({e.get('claim_type') or e.get('kind')})")
        return 0
    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host or settings.get("server.host", "127.0.0.1"),
                    port=args.port or int(settings.get("server.port", 8811)), log_level="warning")
        return 0
    if args.cmd == "export":
        from .export import export_static
        res = export_static(settings, args.out)
        print(json.dumps(res, indent=1))
        return 0 if res.get("ok") else 1
    if args.cmd == "import-portfolio":
        from .db import DB
        from .portfolio import importers
        db = DB(settings.db_path)
        res = importers.import_csv_file(db, args.csv, args.portfolio, name=args.name, kind=args.kind)
        print(json.dumps(res, indent=1))
        return 0
    if args.cmd == "inventory":
        from pathlib import Path
        from .report import inventory_markdown
        out = Path(args.out) if args.out else settings.data_dir / "reports" / "INVENTORY.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(inventory_markdown(settings), encoding="utf-8")
        print(out)
        return 0
    if args.cmd == "health":
        from .db import DB
        from .health import health
        print(json.dumps(health(DB(settings.db_path), settings), indent=1, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
