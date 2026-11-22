# Operations, security and manual steps

## Deployments

| | Private terminal | Public terminal |
|---|---|---|
| Where | the owner's laptop, `http://127.0.0.1:8811` | `https://rasmuscodes.github.io/quant-terminal/` |
| Sources | all repositories (GitHub CLI login), all workspace folders, the website (local source + live pages), the portfolio | public repositories (REST API) and the live website only |
| Schedule | Windows Task Scheduler, every 2 hours and at logon (`scripts/install_task.ps1`) | GitHub Actions, every 6 hours, on pushes to `main`, or by hand (`workflow_dispatch`) |
| Database | `data/private/rtshf.db` (never committed, never uploaded) | `data/public/rtshf.db` in the Actions cache |
| UI | FastAPI + the web UI (actions enabled) | static JSON + the same UI (read-only, search runs in the browser) |

Install once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

The task runs with `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable`, priority 4, one hour
limit and no overlapping instances. Output goes to `data/logs/sync.log`.

## Monitoring and failure handling

- Every run is recorded in `sync_runs` (status ok / partial / failed, per-change counts, duration).
- Each source and stage is isolated: an exception is written to `sync_errors` with a traceback and the run continues.
- HTTP calls retry with exponential backoff on 429/5xx and network errors.
- A lock file stops a scheduled and a manual sync from overlapping (stale locks from dead processes are ignored).
- Settings → health shows the last success, stale sources (no successful sync within 26 hours), missing sources
  (deleted repositories or folders), recent errors, the scheduled task's last result and next run, and the latest
  GitHub Actions runs. Repeated errors on one source raise a system alert.

## Security and permissions

| Topic | Rule |
|---|---|
| Credentials | Never read into code or stored. GitHub: the CLI's keyring login locally (scopes it already has), the workflow `GITHUB_TOKEN` in Actions (read-only on this repository; it cannot read private repositories). Claude: `ANTHROPIC_API_KEY` from the environment, optional. |
| Private material | Private repositories and local folders exist only in the private database. The public export reads only the public database and fails on local paths, e-mail addresses, credential patterns, broker account markers or private repository names. |
| Excluded from indexing | `private/`, `secrets/`, `credentials/`, `.env*`, key files, cookies, vendored code, raw/cache folders, folders with `.rtsignore`. |
| Portfolio | The website ledger is read through the website's own code, read-only. Amounts never leave the private database. |
| Trading | No broker connector exists. Orders are paper-only on paper portfolios; any other order is refused with an explicit error and written to the audit log. Adding real trading would need a separate, explicit authorisation mechanism outside this code. |
| Audit | Paper orders, portfolio creation, CSV imports, alert status changes and signal-adapter changes are written to `audit_log`. |

## Manual steps

| Step | When | Needed? |
|---|---|---|
| Install the scheduled task | once | required for unattended local syncs |
| Approve a proposed signal table | when a table's direction is ambiguous | optional (kept inactive until approved) |
| Set `ANTHROPIC_API_KEY` | once | optional (adds written answers; search and cited extractive answers work without it) |
| Read research-assistant alerts | after syncs | review, not maintenance |
| Correct a classification | rarely | optional: `config/overrides.yaml` (public-safe) or `overrides:` in `config/local.yaml` |

Nothing is needed per new repository, folder, report, experiment or published page.

## Common tasks

```powershell
.venv\Scripts\python -m rtshf sync --full          # re-extract everything (after changing rules)
.venv\Scripts\python -m rtshf health                # health report as JSON
.venv\Scripts\python -m rtshf --mode public sync    # build the public database locally
.venv\Scripts\python -m rtshf --mode public export --out site   # write and privacy-scan the static build
```

To keep a folder out of the index, put an empty `.rtsignore` file in it. To stop a signal adapter, set it to
`disabled` in Settings; the choice survives later syncs.
