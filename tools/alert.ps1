<#
Show a Windows notification when the publisher fails, or when it has stopped
running. Driven by the "Gamma_X Alert" task (tools/GammaX-Alert.xml).

Why this is its own task: the snapshot tasks run as LogonType=Password, which
puts them in a non-interactive session - a toast raised from there goes
nowhere. So publish_snapshot.bat only writes the reason to <Root>\alert.txt
and starts this task, which runs as InteractiveToken on the desktop.

It does two jobs on every run:

  1. Pending failure. If alert.txt exists, show it with the tail of that run's
     log section, then delete it. The same reason is not repeated within an
     hour - a push that fails every 5 minutes through the close window is one
     problem, not 22.

  2. Watchdog. A failure the publisher never reaches cannot report itself:
     the PC asleep, the task skipped for no network, a changed Windows
     password (the stored one stops working and the task never starts), a run
     killed at the 3 minute limit. So during the session, if the last
     successful run (<Root>\last_ok.txt) is older than $StaleMinutes, say so
     once, with the best guess at why. When runs resume, say that too.

State lives in <Root>\alert_state.json. Nothing here leaves the machine.
#>
param(
    # Longest gap between scheduled runs is 15 minutes; allow two misses.
    [int] $StaleMinutes = 35
)

$ErrorActionPreference = "Stop"

# tools\ -> repo -> the parent that holds both clones, same as the publisher.
$repo  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$root  = Split-Path -Parent $repo
$log   = Join-Path $root "publish.log"
$alertFile = Join-Path $root "alert.txt"
$okFile    = Join-Path $root "last_ok.txt"
$startFile = Join-Path $root "last_start.txt"
$stateFile = Join-Path $root "alert_state.json"

function Show-Toast {
    param([string] $Title, [string] $Body)
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $esc = { param($s) [Security.SecurityElement]::Escape($s) }
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    # duration=long keeps it up ~25s; it stays in Action Center after that.
    $xml.LoadXml(@"
<toast duration="long">
  <visual><binding template="ToastGeneric">
    <text>$(& $esc $Title)</text>
    <text>$(& $esc $Body)</text>
  </binding></visual>
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
"@)
    # Windows PowerShell's own AppUserModelID: registered on every Windows 10+
    # install, so no shortcut or app registration is needed to post a toast.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
        [Windows.UI.Notifications.ToastNotification]::new($xml))
}

function Read-Stamp {
    param($path)
    if (-not (Test-Path $path)) { return $null }
    try { return [datetime]::ParseExact((Get-Content $path -Raw).Trim(), "yyyy-MM-dd HH:mm:ss", $null) }
    catch { return $null }
}

# The lines of the most recent run, minus noise, for the notification body.
function Get-RunTail {
    param([int] $Lines = 6)
    if (-not (Test-Path $log)) { return "" }
    $all = Get-Content $log -Tail 400
    $start = -1
    for ($i = $all.Count - 1; $i -ge 0; $i--) { if ($all[$i] -match '--- run start') { $start = $i; break } }
    $run = if ($start -ge 0) { $all[($start + 1)..($all.Count - 1)] } else { $all }
    $run = $run | Where-Object {
        $_.Trim() -and
        $_ -notmatch 'DeprecationWarning|utcfromtimestamp|^\s+(rows\.append|stamp =)' -and
        $_ -notmatch '^\s*(From https|\* branch|HEAD is now at|Already up to date)' -and
        $_ -notmatch '^\[\d{4}-\d\d-\d\d [\d:]+\] (ERROR|WARN):'
    }
    ($run | Select-Object -Last $Lines) -join "`n"
}

$state = @{ lastReason = ""; lastShown = ""; staleFor = "" }
if (Test-Path $stateFile) {
    try {
        $s = Get-Content $stateFile -Raw | ConvertFrom-Json
        foreach ($k in @($state.Keys)) { if ($s.$k) { $state[$k] = [string]$s.$k } }
    } catch {}
}
$now = Get-Date

# ------------------------------------------------------ 1. pending failure
if (Test-Path $alertFile) {
    $reason = (Get-Content $alertFile -Raw).Trim()
    Remove-Item $alertFile -Force
    $recent = $false
    if ($state.lastShown) {
        try { $recent = ($now - [datetime]$state.lastShown).TotalMinutes -lt 60 } catch {}
    }
    if (-not ($recent -and $reason -eq $state.lastReason)) {
        $detail = Get-RunTail
        Show-Toast "Gamma_X snapshot failed" ("$reason" + $(if ($detail) { "`n$detail" }))
        $state.lastReason = $reason
        $state.lastShown  = $now.ToString("o")
    }
}

# ------------------------------------------------------------- 2. watchdog
# Weekday runs are 09:15-16:45, never more than 15 minutes apart. Start
# checking once two runs could have happened, stop shortly after the last.
$inSession = $now.DayOfWeek -notin 'Saturday', 'Sunday' -and
             $now.TimeOfDay -ge [timespan]"09:50" -and $now.TimeOfDay -le [timespan]"17:05"
$lastOk    = Read-Stamp $okFile
$lastStart = Read-Stamp $startFile

if ($state.staleFor -and $lastOk -and $lastOk.ToString("s") -ne $state.staleFor) {
    Show-Toast "Gamma_X snapshots running again" "Last successful run $($lastOk.ToString('HH:mm'))."
    $state.staleFor = ""
}

$stale = $inSession -and (-not $lastOk -or ($now - $lastOk).TotalMinutes -gt $StaleMinutes)
$key = if ($lastOk) { $lastOk.ToString("s") } else { "never" }
if ($stale -and $state.staleFor -ne $key) {
    $since = if ($lastOk) { "since $($lastOk.ToString('ddd HH:mm'))" } else { "on record" }

    # Best guess at why, from what Task Scheduler recorded.
    $why = @()
    foreach ($t in Get-ScheduledTask -TaskName "Gamma_X Snapshot*" -ErrorAction SilentlyContinue) {
        $i = $t | Get-ScheduledTaskInfo
        $code = '0x{0:X8}' -f $i.LastTaskResult
        $hint = switch ($i.LastTaskResult) {
            0          { "" }
            1          { "the publisher exited with an error" }
            267011     { "has not run yet" }
            267014     { "was stopped - hit the 3 minute limit or was ended by hand" }
            2147943726 { "Windows rejected the stored password - it changed; re-run tools\install_tasks.ps1" }
            2147943785 { "the account lacks 'Log on as a batch job'" }
            default    { "" }
        }
        if ($t.State -eq 'Disabled') { $why += "$($t.TaskName) is DISABLED" }
        elseif ($i.LastTaskResult -ne 0) { $why += "$($t.TaskName) last result $code $hint".TrimEnd() }
    }
    if ($lastStart -and $lastOk -and $lastStart -gt $lastOk) {
        $why += "runs are starting but not finishing - last start $($lastStart.ToString('HH:mm'))"
        $tail = Get-RunTail 3
        if ($tail) { $why += $tail }
    } elseif (-not $lastStart -or ($now - $lastStart).TotalMinutes -gt $StaleMinutes) {
        $why += "the task is not firing at all (sleep, no network at trigger time, or the task is broken)"
    }
    Show-Toast "Gamma_X: no successful snapshot $since" ($why -join "`n")
    $state.staleFor = $key
}

$state | ConvertTo-Json | Set-Content $stateFile -Encoding utf8
