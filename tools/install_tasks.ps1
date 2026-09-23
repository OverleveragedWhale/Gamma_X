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

# One task. Idle sleep is handled inside publish_snapshot.bat rather than by
# separate 09:00/17:15 tasks: those ran as InteractiveToken, and a task with
# that logon type reports success without running its action when it fires on
# a wake-from-sleep, which is precisely when it was needed.
$tasks = @(
    @{ File = "GammaX-Snapshot.xml";  Name = "Gamma_X Snapshot";   NeedsPassword = $true  }
)

foreach ($t in $tasks) {
    $path = Join-Path $here $t.File
    if (-not (Test-Path $path)) { Write-Warning "missing $($t.File), skipped"; continue }

    $xml = (Get-Content $path -Raw) -replace 'encoding="UTF-8"', 'encoding="UTF-16"'

    try {
        if ($t.NeedsPassword) {
            # Needs the Windows account password to mint a network-capable token.
            # Updating a LogonType=Password task always re-asks for it; there is
            # no way to edit one of these in place without the credential.
            $cred = Get-Credential -UserName "$env:USERNAME" `
                -Message "Windows password for $($t.Name) (needed to push to GitHub)"
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
            Write-Warning "$($t.Name): registered $got of $want triggers - the schedule is incomplete"
        }
    } catch {
        "{0,-20} FAILED: {1}" -f $t.Name, $_.Exception.Message
    }
}

""
"Installed:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "Gamma_X*" } | ForEach-Object {
    $i = Get-ScheduledTaskInfo -TaskName $_.TaskName
    "  {0,-20} state={1,-8} next={2}" -f $_.TaskName, $_.State, $i.NextRunTime
}
