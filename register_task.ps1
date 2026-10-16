# Registers a Windows Scheduled Task that runs run_check.py every 2 hours,
# indefinitely, catching up after sleep/reboot. Safe to re-run; it replaces
# the existing task definition if one already exists.

$TaskName = "VodklOphthalmologyWatcher"
$ScriptDir = $PSScriptRoot
$Python = (Get-Command python3).Source

$Action = New-ScheduledTaskAction -Execute $Python -Argument "run_check.py" -WorkingDirectory $ScriptDir

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force -ErrorAction Stop

Write-Host "Task '$TaskName' registered: runs '$Python run_check.py' every 2 hours from $ScriptDir"
Write-Host "To check it:    Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "To run it now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it:   Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
