# Scheduled sync entry point (Windows Task Scheduler). Appends to data\logs\sync.log.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "sync.log"
Set-Location $repo
"=== $(Get-Date -Format s) scheduled sync ===" | Out-File -Append -Encoding utf8 $log
& $py -u -X utf8 -W ignore -m rtshf sync --trigger scheduled *>> $log
"=== exit $LASTEXITCODE ===" | Out-File -Append -Encoding utf8 $log
