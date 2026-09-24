<#
Register every Gamma_X scheduled task from the XML in this folder.

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_tasks.ps1

Run it again any time to pick up edits - each task is replaced in place.

Two things about this that are not obvious:

1. The XML files are stored UTF-8 so they diff sensibly in git, but the task
   API is handed a UTF-16 string, and it rejects a document whose declaration
   disagrees with the bytes it actually received:

       The task XML is malformed. (1,40)::ERROR: unable to switch the encoding

   Hence the declaration swap below. Converting the files themselves to UTF-16
   would work too, at the cost of every future diff.

2. The snapshot task needs a stored password. It pushes to GitHub, and an S4U
   task has no network; LogonType=Password is what gets it a token that does.
   Password is also the only logon type observed to actually run its action on
   a wake-from-sleep on this machine, which is why the sleep handling was moved
   into the publisher instead of living in its own task.
#>

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Two tasks, because Task Scheduler rejects a document holding more than 48
# triggers and the day is 50 runs. Measured on Windows 11 26200: 48 registers,
# 49 does not, and the error never names the limit - it says "The task XML
# contains too many nodes of the same type". Both run the same publisher.
#
# Idle sleep is handled inside publish_snapshot.bat rather than by separate
# 09:00/17:15 tasks: those ran as InteractiveToken, and a task with that logon
# type reports success without running its action when it fires on a
# wake-from-sleep, which is precisely when it was needed.
$tasks = @(
    @{ File = "GammaX-Snapshot.xml";       Name = "Gamma_X Snapshot";       NeedsPassword = $true },
    @{ File = "GammaX-Snapshot-Close.xml"; Name = "Gamma_X Snapshot Close"; NeedsPassword = $true }
)

# Asked once and reused. Windows re-asks on every update of a Password task,
# and there is no reason to make that two prompts for the same account.
$cred = $null
if ($tasks | Where-Object { $_.NeedsPassword }) {
    $cred = Get-Credential -UserName "$env:USERNAME" `
        -Message "Windows password for the Gamma_X tasks (needed to push to GitHub)"
}

$failed = @()
foreach ($t in $tasks) {
    $path = Join-Path $here $t.File
    if (-not (Test-Path $path)) { Write-Warning "missing $($t.File), skipped"; continue }

    $xml = (Get-Content $path -Raw) -replace 'encoding="UTF-8"', 'encoding="UTF-16"'

    # Point the action at THIS checkout. The XML ships an absolute path so it
    # can be imported by hand through taskschd.msc, but a second machine may
    # well put the tree somewhere else, and a task pointing at a path that
    # does not exist fails with 0x2 long after anyone is watching.
    $bat = Join-Path $here "publish_snapshot.bat"
    if (-not (Test-Path $bat)) { throw "publish_snapshot.bat not found next to this script ($bat)" }
    $xml = $xml -replace '<Command>[^<]*</Command>', "<Command>$bat</Command>"

    try {
        if ($t.NeedsPassword) {
            # The credential mints a network-capable token. Updating a
            # LogonType=Password task always re-asks for it; there is no way to
            # edit one of these in place without it.
            # The XML ships a placeholder so the repo is not machine-specific;
            # point it at whoever is actually installing.
            $xml = $xml -replace '<UserId>REPLACE\WITH_YOUR_USER</UserId>',
                                 "<UserId>$($cred.UserName)</UserId>"
            Register-ScheduledTask -TaskName $t.Name -Xml $xml -Force `
                -User $cred.UserName `
                -Password $cred.GetNetworkCredential().Password | Out-Null
        } else {
            Register-ScheduledTask -TaskName $t.Name -Xml $xml -Force | Out-Null
        }
        $info = Get-ScheduledTaskInfo -TaskName $t.Name
        # Count the triggers back off the registered task, not the file. The
        # schedule is 50 separate triggers now rather than 4 with repetitions
        # inside them, and a registration that silently kept only some of them
        # would look exactly like the sleep problem it was meant to fix:
        # snapshots quietly missing for part of the day.
        $want = ([regex]::Matches((Get-Content $path -Raw), '<CalendarTrigger>')).Count
        $got  = (Get-ScheduledTask -TaskName $t.Name).Triggers.Count
        "{0,-20} registered, {1}/{2} triggers, next run {3}" -f $t.Name, $got, $want, $info.NextRunTime
        if ($got -ne $want) {
            $failed += "$($t.Name): registered $got of $want triggers"
            Write-Warning "$($t.Name): registered $got of $want triggers - the schedule is incomplete"
        }
    } catch {
        # Loud, and reflected in the exit code. A failure here used to scroll
        # past among the success lines while the OLD task stayed registered and
        # kept running its old schedule - which looks identical to working.
        $failed += "$($t.Name): $($_.Exception.Message)"
        Write-Host ("{0,-24} FAILED: {1}" -f $t.Name, $_.Exception.Message) -ForegroundColor Red
    }
}

""
"Installed:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "Gamma_X*" } | ForEach-Object {
    $i = Get-ScheduledTaskInfo -TaskName $_.TaskName
    $n = $_.Triggers.Count
    "  {0,-24} state={1,-8} triggers={2,-3} next={3}" -f $_.TaskName, $_.State, $n, $i.NextRunTime
}

$total = (Get-ScheduledTask | Where-Object { $_.TaskName -like "Gamma_X*" } |
          ForEach-Object { $_.Triggers.Count } | Measure-Object -Sum).Sum
""
if ($failed.Count) {
    Write-Host "NOT INSTALLED CLEANLY:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}
Write-Host "All tasks registered. $total triggers across all Gamma_X tasks." -ForegroundColor Green
if ($total -lt 50) {
    Write-Warning "Expected 50. Fewer means a task is missing or partly registered."
}
