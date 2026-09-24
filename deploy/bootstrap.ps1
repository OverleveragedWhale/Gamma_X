<#
Set Gamma_X up on a fresh Windows machine, end to end.

    powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1

This is the only file you need to carry to the new PC - everything else is
cloned from GitHub. Re-run it any time; every step checks before it acts, so a
second run repairs whatever is missing and leaves the rest alone.

What it does, in order:

    1. checks for Python 3 and Git, and that the clock is on Eastern time
    2. installs the tzdata package (Windows ships no IANA timezone database)
    3. clones the repo, and the snapshot branch into a second working copy
    4. runs the publisher once, which is what makes Git Credential Manager
       open a browser and store a credential for the scheduled task to use
    5. registers the scheduled task (asks for your Windows password)
    6. prints what it ended up with

Two things it deliberately does NOT do:

  - it never writes a token or password to disk. Step 4 hands the sign-in to
    Git Credential Manager, which stores it in Windows Credential Manager.
  - it does not set margins. Those live in config.ini and are per-account;
    set them from the repo's "Set margins" workflow on the Actions tab.
#>
[CmdletBinding()]
param(
    # Parent folder holding both clones. Any path works - the scripts derive
    # their own locations - but everything below assumes this layout:
    #     <Root>\Gamma_X            the repo
    #     <Root>\Gamma_X-snapshot   the published branch
    #     <Root>\publish.log
    [string] $Root = "C:\GammaX",

    [string] $RepoUrl = "https://github.com/OverleveragedWhale/Gamma_X.git",

    # Skip step 5 when you only want the checkout (for example on a machine
    # that will publish by hand, or a second machine kept as a cold spare).
    [switch] $SkipTask,

    # Skip step 4. Only useful if a credential is already stored.
    [switch] $SkipFirstRun
)

$ErrorActionPreference = "Stop"

function Step  { param($n, $m) Write-Host "`n[$n] $m" -ForegroundColor Cyan }
function Ok    { param($m) Write-Host "      $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "      $m" -ForegroundColor Yellow }
function Fail  { param($m) Write-Host "      $m" -ForegroundColor Red }

$repoDir = Join-Path $Root "Gamma_X"
$pubDir  = Join-Path $Root "Gamma_X-snapshot"

# ---------------------------------------------------------------- 1. checks
Step 1 "Checking prerequisites"

# The launcher takes "-3" to select Python 3; python.exe does not and would
# choke on it, so the version selector travels with the executable rather than
# being hardcoded at each call site.
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    $pyExe = $py.Source
    $pyArgs = @("-3")
} else {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) {
        $pyExe = $py.Source
        $pyArgs = @()
    }
}
if (-not $py) {
    Fail "Python 3 not found. Install from https://www.python.org/downloads/"
    Fail "and tick 'Add python.exe to PATH', then run this again."
    exit 1
}
$pyVer = & $pyExe @pyArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or -not $pyVer) { Fail "Could not run $pyExe"; exit 1 }
if ([version]$pyVer -lt [version]"3.9") {
    # zoneinfo landed in 3.9; the dashboard has no fallback for older.
    Fail "Python $pyVer is too old - 3.9 or newer is required."
    exit 1
}
Ok "Python $pyVer at $pyExe"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Fail "Git not found. Install Git for Windows from https://git-scm.com/download/win"
    Fail "and keep the default 'Git Credential Manager' option, then run this again."
    exit 1
}
Ok "$((git --version)) at $($git.Source)"

# The schedule is written in wall-clock time, so the machine has to agree with
# the exchange about what 09:15 means. This is the single most likely thing to
# be wrong on a new PC and the symptom - snapshots at the wrong moments - is
# easy to misread as a broken schedule.
$tz = (Get-TimeZone).Id
if ($tz -ne "Eastern Standard Time") {
    Warn "Clock is on '$tz', not Eastern Standard Time."
    Warn "The schedule is in market wall-clock time, so set it with:"
    Warn "    Set-TimeZone -Id 'Eastern Standard Time'      (needs an elevated prompt)"
    Warn "Continuing - but the snapshots will fire at the wrong times until you do."
} else {
    Ok "Time zone is Eastern"
}

# --------------------------------------------------------------- 2. tzdata
Step 2 "Installing the tzdata package"
& $pyExe @pyArgs -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('tzdata') else 1)" 2>$null
if ($LASTEXITCODE -eq 0) {
    Ok "tzdata already present"
} else {
    # Required, not optional: without it gex_terminal falls back to the PC
    # clock and assumes it is already Eastern, which is silent and wrong.
    & $pyExe @pyArgs -m pip install --quiet --disable-pip-version-check tzdata
    if ($LASTEXITCODE -ne 0) { Fail "pip install tzdata failed"; exit 1 }
    Ok "tzdata installed"
}

# --------------------------------------------------------------- 3. clones
Step 3 "Cloning into $Root"
if (-not (Test-Path $Root)) { New-Item -ItemType Directory -Path $Root -Force | Out-Null }

if (Test-Path (Join-Path $repoDir ".git")) {
    Ok "repo already at $repoDir - fetching"
    git -C $repoDir fetch origin main --quiet
    git -C $repoDir merge --ff-only origin/main --quiet
} else {
    git clone --quiet $RepoUrl $repoDir
    if ($LASTEXITCODE -ne 0) { Fail "clone failed"; exit 1 }
    Ok "cloned repo"
}

# A SECOND working copy, not a branch switch in the first one. The publisher
# overwrites this clone from the remote on every run, so it must hold nothing
# but the generated page.
if (Test-Path (Join-Path $pubDir ".git")) {
    Ok "snapshot clone already at $pubDir"
} else {
    git clone --quiet -b snapshot $RepoUrl $pubDir
    if ($LASTEXITCODE -ne 0) { Fail "snapshot clone failed"; exit 1 }
    Ok "cloned snapshot branch"
}

# ------------------------------------------------------------ 4. first run
if ($SkipFirstRun) {
    Step 4 "Skipping the first run (-SkipFirstRun)"
} else {
    Step 4 "First run - a browser may open for GitHub sign-in"
    Warn "Sign in if prompted. The credential goes to Windows Credential Manager;"
    Warn "nothing is written to disk here, and the scheduled task reuses it."
    & (Join-Path $repoDir "tools\publish_snapshot.bat")
    if ($LASTEXITCODE -eq 0) {
        Ok "published successfully - the pipeline works on this machine"
    } else {
        Fail "publisher exited $LASTEXITCODE. Check $Root\publish.log before going on."
        Fail "A push rejection here usually just means another machine published first."
    }
}

# ---------------------------------------------------------------- 5. task
if ($SkipTask) {
    Step 5 "Skipping task registration (-SkipTask)"
} else {
    Step 5 "Registering the scheduled task"
    Warn "You will be asked for your Windows password. It is needed because the"
    Warn "task must run with LogonType=Password: an S4U task has no network, so"
    Warn "it could neither fetch quotes nor push to GitHub."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoDir "tools\install_tasks.ps1")
}

# -------------------------------------------------------------- 6. summary
Step 6 "Result"
Ok "repo         $repoDir"
Ok "snapshot     $pubDir"
Ok "log          $(Join-Path $Root 'publish.log')"
$t = Get-ScheduledTask -TaskName "Gamma_X Snapshot" -ErrorAction SilentlyContinue
if ($t) {
    $i = Get-ScheduledTaskInfo -TaskName "Gamma_X Snapshot"
    Ok "task         state=$($t.State) triggers=$($t.Triggers.Count) next=$($i.NextRunTime)"
    if ($t.Triggers.Count -lt 50) {
        Warn "Expected 50 triggers. Fewer means an older XML is registered -"
        Warn "re-run tools\install_tasks.ps1 from $repoDir."
    }
} else {
    Warn "task         not registered"
}

Write-Host ""
Write-Host "Still to do by hand:" -ForegroundColor Cyan
Write-Host "  - set margins from the repo's Actions tab -> 'Set margins'"
Write-Host "  - if this machine is a SECOND publisher, read deploy/SETUP.md first:"
Write-Host "    two machines pushing the same branch will fight over it."
