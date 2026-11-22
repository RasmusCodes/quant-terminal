"""SQLite storage: one file per mode (private/public), WAL, FTS5 for text search.

SQLite instead of PostgreSQL: one user, one machine, no server to run or patch, a single file to back up, and FTS5
built in. The schema sticks to portable SQL apart from the FTS table, so a move to PostgreSQL stays possible.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .util import dumps, now_iso

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- where content comes from: a GitHub repository, a local folder, the website
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                 -- github | local | website
  key TEXT NOT NULL UNIQUE,           -- gh:owner/name | local:<root>/<folder> | web:<host>
  name TEXT, url TEXT, path TEXT,
  visibility TEXT NOT NULL DEFAULT 'private',   -- public | private
  description TEXT, languages TEXT, topics TEXT, license TEXT, homepage TEXT,
  default_branch TEXT, head_sha TEXT, pushed_at TEXT, created_at TEXT,
  remote_url TEXT,                    -- git remote of a local folder
  is_archived INTEGER DEFAULT 0, is_fork INTEGER DEFAULT 0, is_empty INTEGER DEFAULT 0,
  fingerprint TEXT, file_count INTEGER DEFAULT 0, size_bytes INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, last_synced TEXT, last_changed TEXT, last_error TEXT,
  status TEXT DEFAULT 'active',       -- active | missing | archived | error
  project_id INTEGER
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  summary TEXT,
  category TEXT,                      -- primary category key (taxonomy) or not_relevant
  secondary TEXT,                     -- json list of further categories
  scores TEXT,                        -- json {category: score}
  subtopics TEXT,                     -- json [{key,label,score}]
  themes TEXT,                        -- json [{key,label,score}]
  relevance TEXT,                     -- investment | game_theory | tooling | reference | not_relevant
  relevance_score REAL,
  evidence TEXT,                      -- json classification evidence
  kind TEXT,                          -- code_project | research_folder | document_library | publication | empty
  technologies TEXT, data_sources TEXT, capabilities TEXT,
  visibility TEXT,                    -- public when any source or page is public
  website_url TEXT, repo_url TEXT,
  status TEXT,                        -- active | dormant | new | in_progress | archived
  first_seen TEXT, last_activity TEXT, last_synced TEXT,
  override TEXT
);

CREATE TABLE IF NOT EXISTS file_index (
  source_id INTEGER NOT NULL, path TEXT NOT NULL,
  size INTEGER, mtime REAL, hash TEXT, role TEXT, status TEXT DEFAULT 'active', seen_at TEXT,
  PRIMARY KEY (source_id, path)
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL, project_id INTEGER,
  path TEXT NOT NULL, url TEXT,
  title TEXT, doc_type TEXT, fmt TEXT, date TEXT, modified TEXT, size INTEGER,
  content_hash TEXT, visibility TEXT, meta TEXT, text_len INTEGER,
  superseded INTEGER DEFAULT 0,       -- e.g. in an archive/ folder or an older version
  first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'active',
  UNIQUE (source_id, path)
);
CREATE INDEX IF NOT EXISTS ix_documents_project ON documents(project_id);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  heading TEXT, text TEXT NOT NULL, line_start INTEGER, line_end INTEGER,
  UNIQUE (document_id, seq)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title, heading, text, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                 -- company | theme | data_source
  key TEXT NOT NULL UNIQUE,           -- company:NFLX | theme:ai_infrastructure | data:sec_edgar
  name TEXT, symbol TEXT, meta TEXT
);

CREATE TABLE IF NOT EXISTS securities (
  id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL UNIQUE, name TEXT, isin TEXT, exchange TEXT, currency TEXT, country TEXT,
  asset_class TEXT DEFAULT 'equity', sector TEXT, industry TEXT, yahoo TEXT, entity_id INTEGER,
  themes TEXT, origin TEXT            -- seed | discovered | portfolio
);

CREATE TABLE IF NOT EXISTS mentions (
  entity_id INTEGER NOT NULL, chunk_id INTEGER NOT NULL, document_id INTEGER NOT NULL,
  count INTEGER, in_heading INTEGER DEFAULT 0,
  PRIMARY KEY (entity_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_mentions_doc ON mentions(document_id);

CREATE TABLE IF NOT EXISTS project_entities (
  project_id INTEGER NOT NULL, entity_id INTEGER NOT NULL,
  mentions INTEGER, documents INTEGER, salience REAL, role TEXT,
  PRIMARY KEY (project_id, entity_id)
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  project_id INTEGER, document_id INTEGER, chunk_id INTEGER,
  text TEXT NOT NULL,
  claim_type TEXT NOT NULL,           -- research_claim | model_estimate | observed_fact | verified_result
  evidence_kind TEXT,                 -- backtest | simulation | statistical_test | valuation | filing_data | market_data | forecast | qualitative
  section TEXT, polarity REAL, numbers TEXT, entities TEXT, themes TEXT,
  confidence REAL, rule TEXT, line INTEGER, importance REAL,
  first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS ix_findings_project ON findings(project_id);

CREATE TABLE IF NOT EXISTS theses (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  entity_id INTEGER, symbol TEXT, project_id INTEGER, document_id INTEGER,
  stance TEXT,                        -- long | short | neutral
  rating TEXT, target_price REAL, price_at_thesis REAL, currency TEXT, upside REAL,
  scenarios TEXT, horizon TEXT, thesis_date TEXT,
  summary TEXT, arguments TEXT, risks TEXT, catalysts TEXT, assumptions TEXT,
  status TEXT,                        -- active | superseded
  version INTEGER, first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS financial_models (
  id INTEGER PRIMARY KEY,
  document_id INTEGER UNIQUE, project_id INTEGER, entity_id INTEGER, symbol TEXT,
  name TEXT, path TEXT, model_type TEXT, sheets TEXT, outputs TEXT, assumptions TEXT,
  previous_outputs TEXT, content_hash TEXT, modified TEXT, first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  project_id INTEGER, document_id INTEGER,
  name TEXT, hypothesis TEXT,
  result_kind TEXT,                   -- historical_backtest | simulation | statistical_study | forecast_evaluation | live | paper | synthetic_demo
  datasets TEXT, periods TEXT, oos INTEGER, costs TEXT, significance TEXT, verdict TEXT,
  metrics TEXT, limitations TEXT, reproducibility TEXT, flags TEXT,
  source_kind TEXT,                   -- results_file | report_table | text | hypothesis | sdk
  location TEXT, line INTEGER, run_date TEXT,
  first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS ix_experiments_project ON experiments(project_id);

CREATE TABLE IF NOT EXISTS experiment_metrics (
  experiment_id INTEGER NOT NULL, name TEXT NOT NULL, value REAL, unit TEXT, raw TEXT,
  PRIMARY KEY (experiment_id, name)
);

CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE, project_id INTEGER, name TEXT, family TEXT, description TEXT,
  costs_modeled INTEGER, first_date TEXT, last_date TEXT, source TEXT, meta TEXT, first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE, project_id INTEGER, kind TEXT, name TEXT, path TEXT, framework TEXT,
  artifacts INTEGER, modified TEXT, meta TEXT, first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS signal_sources (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE, project_id INTEGER, path TEXT, kind TEXT, mapping TEXT,
  status TEXT,                        -- active | proposed | disabled
  confidence REAL, rows INTEGER, last_loaded TEXT, note TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  strategy_id TEXT NOT NULL, instrument TEXT NOT NULL, ts TEXT, available_at TEXT, horizon TEXT,
  direction INTEGER, expected_return REAL, confidence REAL, risk REAL, value REAL,
  model_version TEXT, provenance TEXT, validation_ref TEXT, validation_status TEXT,
  project_id INTEGER, source_kind TEXT, meta TEXT, created_at TEXT, active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_signals_instrument ON signals(instrument);

CREATE TABLE IF NOT EXISTS portfolios (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE, name TEXT, kind TEXT,     -- real | paper | game
  base_currency TEXT, benchmark TEXT, source TEXT, read_only INTEGER DEFAULT 0, meta TEXT, created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  portfolio_id INTEGER NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL,   -- buy | sell | dividend | deposit | withdrawal | fee | split
  symbol TEXT, qty REAL, price REAL, currency TEXT, amount REAL, fees REAL, source TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS ix_tx_portfolio ON transactions(portfolio_id, date);

CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL, currency TEXT, source TEXT,
  PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
  currency TEXT NOT NULL, date TEXT NOT NULL, per_eur REAL,  -- units of currency per 1 EUR
  PRIMARY KEY (currency, date)
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  portfolio_id INTEGER NOT NULL, created_at TEXT, symbol TEXT, side TEXT, qty REAL, order_type TEXT,
  limit_price REAL, mode TEXT,        -- paper | live
  status TEXT, note TEXT, signal_ref TEXT
);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL, ts TEXT, qty REAL, price REAL, fees REAL, transaction_id INTEGER
);

CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY,
  src_kind TEXT, src_id INTEGER, predicate TEXT, dst_kind TEXT, dst_id INTEGER,
  weight REAL, basis TEXT,            -- declared | extracted | inferred | manual
  evidence TEXT, created_at TEXT, updated_at TEXT,
  UNIQUE (src_kind, src_id, predicate, dst_kind, dst_id)
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  dedupe_key TEXT NOT NULL UNIQUE,
  kind TEXT,                          -- new_project | connection | impact | stale_thesis | model_change | strategy_decay | gap | integration
  basis TEXT,                         -- actual_update | speculative_connection | gap | system
  severity TEXT,                      -- info | notice | warning
  title TEXT, detail TEXT, evidence TEXT, subjects TEXT,
  status TEXT DEFAULT 'new',          -- new | seen | dismissed | resolved
  created_at TEXT, updated_at TEXT, run_id INTEGER
);

CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY,
  run_id INTEGER, source_key TEXT, project_id INTEGER, kind TEXT, path TEXT, detail TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_changes_run ON changes(run_id);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT, finished_at TEXT, mode TEXT, trigger TEXT, status TEXT, stats TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS sync_errors (
  id INTEGER PRIMARY KEY,
  run_id INTEGER, source_key TEXT, stage TEXT, message TEXT, detail TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT
);

CREATE INDEX IF NOT EXISTS ix_pe_entity ON project_entities(entity_id);
CREATE INDEX IF NOT EXISTS ix_mentions_entity ON mentions(entity_id);
CREATE INDEX IF NOT EXISTS ix_theses_symbol ON theses(symbol);
CREATE INDEX IF NOT EXISTS ix_theses_document ON theses(document_id);
CREATE INDEX IF NOT EXISTS ix_findings_document ON findings(document_id);
CREATE INDEX IF NOT EXISTS ix_experiments_document ON experiments(document_id);
CREATE INDEX IF NOT EXISTS ix_relations_src ON relations(src_kind, src_id);
CREATE INDEX IF NOT EXISTS ix_relations_dst ON relations(dst_kind, dst_id);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source_id, status);
"""


_READY: set = set()      # database files whose schema was checked in this process


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        key = str(self.path.resolve())
        if key not in _READY:                       # schema + migrations once per process, not on every API request
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.conn.commit()
            _READY.add(key)

    def _migrate(self) -> None:
        """Additive column migrations for databases created by earlier versions."""
        wanted = {"theses": {"method": "TEXT", "meta": "TEXT"}}
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # --- basic helpers -------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, rows)

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return default if row is None or row[0] is None else row[0]

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def insert(self, table: str, row: dict, replace: bool = False) -> int:
        cols = list(row)
        sql = f"INSERT {'OR REPLACE ' if replace else ''}INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        cur = self.conn.execute(sql, [_jsonish(row[c]) for c in cols])
        return cur.lastrowid

    def upsert(self, table: str, row: dict, conflict: list[str], update: list[str] | None = None) -> int:
        """INSERT ... ON CONFLICT(conflict) DO UPDATE; returns the row id."""
        cols = list(row)
        update = update if update is not None else [c for c in cols if c not in conflict]
        sets = ",".join(f"{c}=excluded.{c}" for c in update) or f"{conflict[0]}=excluded.{conflict[0]}"
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT({','.join(conflict)}) DO UPDATE SET {sets}")
        self.conn.execute(sql, [_jsonish(row[c]) for c in cols])
        where = " AND ".join(f"{c}=?" for c in conflict)
        rid = self.scalar(f"SELECT id FROM {table} WHERE {where}", [_jsonish(row[c]) for c in conflict])
        return int(rid) if rid is not None else 0

    def update(self, table: str, rid: int, fields: dict) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [_jsonish(v) for v in fields.values()] + [rid])

    # --- meta / audit --------------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else default

    def audit(self, actor: str, action: str, target: str, detail: Any = None) -> None:
        self.conn.execute("INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                          (now_iso(), actor, action, target, dumps(detail) if detail is not None else None))
        self.conn.commit()


def _jsonish(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple, set)):
        return dumps(list(v) if isinstance(v, set) else v)
    return v


def rows_to_dicts(rows: Iterable[sqlite3.Row], json_cols: Iterable[str] = ()) -> list[dict]:
    from .util import loads

    jc = set(json_cols)
    out = []
    for r in rows:
        d = dict(r)
        for c in jc:
            if c in d:
                d[c] = loads(d[c], d[c])
        out.append(d)
    return out
