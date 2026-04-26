$ErrorActionPreference = "Stop"

$taskName = "AIHotNewsEmailAgent"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python).Source
$script = Join-Path $projectDir "agent.py"

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Daily -At 08:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "Daily AI hot news digest email agent" -Force | Out-Null

Write-Host "Installed scheduled task '$taskName'. It will run every day at 08:00 local Windows time."
