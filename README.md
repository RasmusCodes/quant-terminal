# RTS Quant Terminal

One research and portfolio terminal over everything I build: every GitHub repository, every local project folder and
every page of [rasmuscodes.github.io](https://rasmuscodes.github.io), found and re-indexed automatically.

**Public build:** [rasmuscodes.github.io/quant-terminal](https://rasmuscodes.github.io/quant-terminal/) (public sources only,
rebuilt every six hours by GitHub Actions). The private terminal runs locally and also covers private repositories, local
projects and the portfolio.

## What it does

| Area | What happens automatically |
|---|---|
| Discovery | Lists every repository of the GitHub account (private ones only locally, via the GitHub CLI login), every sub-folder of the workspace, and the website's sitemap. New repositories, folders and pages appear on the next sync with no registration. |
| Change detection | Repositories by push time and commit, folders by a file fingerprint, files by content hash, pages by text hash. Only what changed is re-read. |
| Classification | Each project gets an area (quant trading, derivatives/rates/credit, alternative data, equity research, portfolio, game theory, tooling), sub-topics, themes, technologies and data sources, with the evidence terms stored. Projects with nothing written yet are "pending" and re-classified when content arrives. |
| Extraction | Findings typed as **research claim / model estimate / observed fact / verified result**; investment theses (rating, price, base and probability-weighted value, scenarios, arguments, risks); experiments and backtests from report tables, result files and hypothesis verdicts; Excel model outputs; strategy registries; signal tables. Every item links to its file and line. |
| Research checks | Out-of-sample stated for the result or only for the document, costs stated or not, synthetic data, survivorship, look-ahead patterns in code, holdout reuse and multiple-testing language. Flags are prompts, not verdicts. |
| Search and questions | Hybrid search (SQLite FTS5 BM25 + latent-semantic vectors). Questions are routed to structured answers (company dossier, theme view, thesis arguments, contradicting evidence, out-of-sample results, exposures, cross-project companies, predictive results) with numbered sources. Missing evidence is reported as missing. Optional Claude synthesis when an API key is set. |
| Signals | One schema for quant outputs, alt-data screens and fundamental theses (instrument, timestamp, availability time, horizon, direction, expected return, confidence, risk, model version, provenance, validation). Tables whose direction is ambiguous stay proposals until approved. Game-theory projects never produce investment signals. |
| Portfolio and risk | The personal portfolio is read, never written, from the website's private ledger; books found in projects are mirrored read-only; paper portfolios and CSV import are built in. Performance vs benchmark, exposures (sector, currency, theme), volatility, historical and parametric VaR/ES, drawdown, risk contributions, correlations, Fama-French factor betas, historical and hypothetical scenarios, position sizing and optimisation suggestions. No real trade is possible: there is no broker connection, and live orders are refused. |
| Research assistant | After each sync: new projects and their likely connections, changes that touch a thesis (and which assumptions share terms with the change), model outputs that moved away from a published number, decaying strategies, holdings with no research, calls with no model. Each alert says whether it is an actual update, a speculative connection, a gap or system health. It never edits a thesis. |

## Run it

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m rtshf sync          # discover + index (first run takes a few minutes)
.venv\Scripts\python -m rtshf serve         # http://127.0.0.1:8811  (or double-click Terminal.cmd)
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1   # scheduled sync every 2 hours + at logon
```

Other commands: `rtshf status`, `rtshf search <words>`, `rtshf ask "<question>"`, `rtshf health`,
`rtshf import-portfolio <csv> --portfolio <name>`, `rtshf --mode public export --out site`.

## Privacy model

- Two databases: `data/private` (everything, never leaves the machine) and `data/public` (public repositories and the
  live website only). The public build is made only from the public database.
- The export is scanned before anything is written: local paths, e-mail addresses, credential patterns, broker account
  markers and the names of private repositories fail the build.
- Credentials are never read into the code or stored: GitHub access uses the GitHub CLI's own login locally and the
  workflow token in Actions. Folders named `private`, `secrets`, `.env*` files and key files are never indexed.
- Downloaded third-party material (broker notes, filings, transcripts, letters) is not read for calls or findings.

## Optional integration SDK

Discovery never needs it. To have a project's outputs read exactly as intended, copy `sdk/rts_sdk.py` into it:
`write_manifest(...)`, `publish_signals(...)`, `log_experiment(...)`. See [docs/SDK.md](docs/SDK.md).

## Docs

[Architecture](docs/ARCHITECTURE.md) · [Operations, security and manual steps](docs/OPERATIONS.md) · [SDK](docs/SDK.md)
