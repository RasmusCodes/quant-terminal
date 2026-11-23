# Scheduled sync entry point (Windows Task Scheduler). Appends UTF-8 output to data\logs\sync.log.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "sync.log"
Set-Location $repo
Add-Content -Path $log -Encoding UTF8 -Value "=== $(Get-Date -Format s) scheduled sync ==="
# cmd's redirection writes Python's UTF-8 bytes as they are (PowerShell 5.1 redirection would re-encode to UTF-16)
cmd /c "`"$py`" -u -X utf8 -W ignore -m rtshf sync --trigger scheduled >> `"$log`" 2>&1"
Add-Content -Path $log -Encoding UTF8 -Value "=== exit $LASTEXITCODE ==="
