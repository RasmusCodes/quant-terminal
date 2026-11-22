# Registers the scheduled sync: every N hours (default 2) and at logon, also on battery, catching up when missed.
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
param([int]$Hours = 2)
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run_sync.ps1"
$name = "rts-quant-terminal sync"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" -WorkingDirectory $repo
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours $Hours)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Priority 4 `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($repeat, $logon) -Settings $settings `
  -Description "RTS quant terminal: discover and index new or changed repositories, folders and website pages." -Force | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
