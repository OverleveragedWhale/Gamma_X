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

2. Only the snapshot task needs a stored password. It pushes to GitHub, and an
   S4U task has no network; LogonType=Password is what gets it a token that
   does. The two power tasks call powercfg against the calling user's own
   scheme, which needs neither network nor elevation, so they run as
   InteractiveToken and prompt for nothing.
#>

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$tasks = @(
    @{ File = "GammaX-Snapshot.xml";  Name = "Gamma_X Snapshot";   NeedsPassword = $true  },
    @{ File = "GammaX-StayAwake.xml"; Name = "Gamma_X Stay Awake"; NeedsPassword = $false },
    @{ File = "GammaX-AllowSleep.xml";Name = "Gamma_X Allow Sleep";NeedsPassword = $false }
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
        "{0,-20} registered, next run {1}" -f $t.Name, $info.NextRunTime
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
