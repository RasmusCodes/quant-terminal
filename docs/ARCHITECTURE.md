# Architecture

## Shape

One Python package (`rtshf`), one SQLite file per mode, one static web UI. No servers to keep alive apart from the
optional local API, no message queues, no containers.

```
sources ──► connectors ──► ingest (diff, parse, chunk, link) ──► extractors ──► profile/graph/index ──► assistant
 GitHub      github.py       ingest.py                           findings        classify.py              assistant.py
 workspace   files.py        textproc.py                         theses          graph.py
 website     website.py      entities.py                         experiments     search.py
                                                                 finmodels       signals.py
                                                                 signals         portfolio/store.py
                                     ▼
                        SQLite (data/private | data/public) ──► views.py ──► api.py (FastAPI, local)
                                                                         └──► export.py (static JSON, Pages)
```

| Decision | Why |
|---|---|
| SQLite + FTS5 instead of PostgreSQL | One user and one machine: no server to run, patch or back up; FTS5 gives BM25 search for free. The schema is plain SQL apart from the FTS table. |
| Dense vectors fitted on the corpus (TF-IDF → truncated SVD) | Semantic recall without model downloads or API keys; `Searcher.encode` is the one place to swap in an embedding model. |
| Rule-based extraction, optional LLM synthesis | Deterministic, auditable (the rule is stored with each item), free to run every two hours. Claude is used only to write answers from cited evidence, and only when credentials exist. |
| Plain JavaScript modules, no build step | Node is not part of this toolchain; no npm dependencies to maintain. The same UI runs on the API and on the static export. |
| Local scheduled task + GitHub Actions | Most projects live only on the laptop, so the full sync must run there. Actions rebuilds the public view from public sources even when the laptop is off. |

## Information model

| Table | Holds | Key links |
|---|---|---|
| `sources` | GitHub repositories, local folders, the website | `project_id` |
| `projects` | One per piece of work (a repository and its local working copy are one project) | area, relevance, sub-topics, themes, evidence, capabilities |
| `documents`, `chunks`, `chunk_fts` | Parsed text with headings and line numbers | source, project, public URL, visibility |
| `entities`, `securities` | Companies (security master + discovered tickers), themes, data sources | sector, industry, currency, theme roles |
| `mentions`, `project_entities` | Which chunks and projects mention which entities, with salience | |
| `findings` | Statements with claim type and evidence kind | document, chunk, line, rule, polarity, companies, themes |
| `theses` | Calls: rating, stance, target, price, scenarios, arguments, risks, assumptions; versioned | document, company, project |
| `financial_models` | Excel models: type, key outputs, assumptions, previous outputs | document, company |
| `experiments`, `experiment_metrics` | Results with metrics, result kind, out-of-sample scope, costs, periods, verdict, flags | document/result file, line |
| `strategies`, `models` | Strategy registries, model artefacts | project |
| `signal_sources`, `signals` | Adapters (auto-detected or SDK) and standard signals | project, validation reference |
| `portfolios`, `transactions`, `prices`, `fx_rates`, `orders`, `fills` | Portfolio layer | |
| `relations` | Graph edges with basis: declared, extracted, inferred, manual | |
| `alerts` | Research-assistant output with basis and status | subjects, evidence |
| `changes`, `sync_runs`, `sync_errors`, `audit_log` | What changed per run, run health, consequential user actions | |

### Claim types

| Type | Assigned when | Example |
|---|---|---|
| `verified_result` | checked against an independent reference, replicated or validated on real data | "the VIX recomputed from SPX quotes matches Cboe's close to within 0.01" |
| `observed_fact` | reported or measured (filings, counts, prices on a date) | "Skydio holds 92% of attributable federal drone obligations" |
| `model_estimate` | output of a model, backtest, forecast or valuation | "alpha +4.3% a year (t 2.4)" |
| `research_claim` | an argument or judgement (default) | "management is underappreciated" |

### Result kinds and out-of-sample scope

`historical_backtest`, `simulation`, `statistical_study`, `forecast_evaluation`, `synthetic_demo`, `live`, `paper`.
Out-of-sample is *explicit* only when the result's own row, heading or sentence says so (or an SDK file states it);
*document* means the surrounding document mentions out-of-sample testing somewhere.

## Discovery rules (generic, no project names)

- Pruned everywhere: VCS/virtualenv/build folders, `raw`, `cache`, `scratch`, `logs`, `private`, `secrets`, vendored
  sub-projects (a nested folder with its own LICENSE and README), and any folder containing `.rtsignore`.
- Markdown is read anywhere. Text, HTML and PDF files are read only from the project root or output folders
  (`reports`, `out`, `paper`, `memo`, `deck`, ...) and never from research-input folders (`notes`, `research`, `work`,
  `docs`, `sources`, ...), because those hold downloaded third-party material.
- Result files (JSON/CSV/YAML) are read from result-like names or folders; code is sampled for imports and patterns.
- A folder with only PDFs and no README is a document library: listed, not indexed.

## Thesis extraction

1. Structured: front matter (`rating`, `price`, `target`, `weighted`, `scenarios`, ...) or the summary sheet of a
   published research page.
2. Text: an explicit call ("Call: BUY", "Recommendation: WATCHLIST", "We are short ...") in the first part of the owner's
   own memos, reports, decks and project READMEs, with negation handling and price plausibility checks.
3. Versions: an edited call becomes a new version; the old one is kept as superseded. Per company and project the newest
   call is active. The terminal never changes a thesis.

## Research assistant

Runs at the end of every sync over the run's `changes`:

| Alert | Trigger | Basis |
|---|---|---|
| new_project | a repository or folder appeared | actual update (+ speculative related projects) |
| impact | content changed in a project that covers a company with an active thesis anywhere | actual update; the affected-assumption mapping is labelled speculative |
| connection | changed material and another project cover the same company | speculative connection |
| stale_thesis | a model's price output moved away from the published target, or the price moved >15% since the call | actual update |
| thesis_changed | a document's call changed | actual update |
| strategy_decay | a result's metrics fell between syncs, or new research states decay / regime change | actual update (first run: baseline) |
| gap | a holding with no research, a call with a target but no model | gap |
| integration | repeated source errors, proposed signal tables | system |

Re-extracting unchanged files under new rules never raises update alerts (file hashes decide what changed).

## Public build

GitHub Actions (`.github/workflows/public-terminal.yml`) runs the same code in `public` mode: public repositories via the
REST API and the live website only, then `export` writes JSON views plus a client-side search index, scans them and
deploys to Pages. The workflow cache keeps the previous public database for change detection between runs.
