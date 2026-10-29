# Registers the daily coupon performance update in Windows Task Scheduler (this PC, current user).
#
#   powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1            # install / update
#   powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1 -Remove    # remove
#
# Runs `run.py --daily --apply --quiet` every day at 09:00 (this PC's time, IST): coupons whose campaign
# starts from COUPON_LOOKBACK_DAYS ago to COUPON_LOOKAHEAD_DAYS ahead.
# The backup run at 15:00 only matters when Amazon was slow or the PC was off: it carries on with the
# same report; once the day is written, it sees that and stops at once.
# Missed runs (PC off) start as soon as the PC is on again. A run still going when the next one is due
# is left alone (no second copy).
# Each run adds one line to logs\cron.log; details are in logs\coupon_updater.log.
param([switch]$Remove)

$TaskName = "Amazon Coupon Performance Updater"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\pythonw.exe"   # pythonw: no console window

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed '$TaskName'."
    return
}
if (-not (Test-Path $Python)) { throw "Python not found at $Python (create the .venv first)" }

$action = New-ScheduledTaskAction -Execute $Python -Argument "-B run.py --daily --apply --quiet" -WorkingDirectory $Project
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At 09:00
    New-ScheduledTaskTrigger -Daily -At 15:00
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Amazon Coupon Performance (SP-API) -> powerbidb.api_coupon_data: every day 09:00 (15:00: backup run while Amazon is slow)" -Force | Out-Null
Write-Output "Registered '$TaskName': daily 09:00 (backup 15:00), $Python -B run.py --daily --apply --quiet (in $Project)"
