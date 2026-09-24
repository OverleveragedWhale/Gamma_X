@echo off
setlocal enabledelayedexpansion

rem Generate the dashboard and publish it to the `snapshot` branch, which
rem GitHub Pages serves directly. Driven by Task Scheduler - see
rem tools/GammaX-Snapshot.xml.
rem
rem One-time setup on the machine running this:
rem   1. Install Python 3 (python.org) and Git for Windows.
rem   2. pip install tzdata
rem      Required. Windows ships no IANA timezone database, so without it
rem      gex_terminal.py silently falls back to the PC clock and assumes it
rem      is already on Eastern (see the comment at gex_terminal.py:73).
rem   3. git clone <repo-url> C:\GammaX\Gamma_X
rem   4. git clone -b snapshot <repo-url> C:\GammaX\Gamma_X-snapshot
rem   5. Run this file once by hand. Git Credential Manager opens a browser
rem      to sign in and stores the credential in Windows Credential Manager,
rem      so no token is ever written to disk.
rem   6. Set GitHub Pages to: Deploy from a branch -> snapshot / (root).
rem   7. powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_tasks.ps1
rem      Registers the snapshot task. There is only one task now - the sleep
rem      handling that used to live in Gamma_X Stay Awake / Allow Sleep runs
rem      inside this script instead, because those fired as InteractiveToken
rem      and did nothing on a wake-from-sleep. See market_sleep.bat.
rem
rem Adjust these four paths if you cloned somewhere else.

rem Every path is derived from this script's own location, so the tree can
rem live anywhere: tools\ -> repo root -> the parent that holds both clones.
rem Nothing here is machine-specific, which is what lets a second PC run the
rem same checkout without edits.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
for %%I in ("%REPO%\..") do set "ROOT=%%~fI"
set "PUB=%ROOT%\Gamma_X-snapshot"
set "LOG=%ROOT%\publish.log"
set "PY=py -3"

for /f "tokens=* usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "NOW=%%t"
echo [%NOW%] --- run start >> "%LOG%"

rem Hold the machine awake across the session. This has to ride the publisher
rem rather than its own scheduled task: WakeToRun wakes the PC for a trigger's
rem start boundary but not for a repetition inside it, so without this the
rem 09:15 run happens and every 15-minute repeat behind it is lost to sleep.
rem A no-op when the setting already matches, so it costs nothing on the other
rem 49 runs of the day.
call "%~dp0market_sleep.bat" auto

rem Discard the previous run's generated page and match the remote exactly.
rem Safe because this clone holds nothing but the generated index.html - it is
rem never a working copy. Keeps this machine from diverging if the GitHub
rem fallback workflow pushed in the meantime.
cd /d "%PUB%"
if errorlevel 1 (
  echo [%NOW%] ERROR: cannot enter %PUB% >> "%LOG%"
  exit /b 1
)
git fetch origin snapshot >> "%LOG%" 2>&1
git reset --hard origin/snapshot >> "%LOG%" 2>&1

cd /d "%REPO%"
if errorlevel 1 (
  echo [%NOW%] ERROR: cannot enter %REPO% >> "%LOG%"
  exit /b 1
)
rem Fast-forward code and config pushed while this machine was away - margin
rem values set from the "Set margins" workflow arrive this way. ff-only so a
rem local commit is never silently discarded, and a failure here is a warning
rem rather than a skipped run: publishing slightly stale code beats publishing
rem nothing.
git fetch origin main >> "%LOG%" 2>&1
git merge --ff-only origin/main >> "%LOG%" 2>&1
if errorlevel 1 echo [%NOW%] WARN: could not fast-forward main >> "%LOG%"

rem --skip-unchanged leaves index.html untouched when the figures match what it
rem already holds, so the git check below publishes only when the numbers
rem actually move. Open interest is T-1, so max pain cannot shift intraday at
rem all; how often the quotes themselves move is what tools/probe_feed.py
rem measures. Keeping this on bounds the GitHub Pages rebuild rate either way.
%PY% gex_terminal.py --snapshot "%PUB%\index.html" --skip-unchanged >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%NOW%] ERROR: snapshot generation failed >> "%LOG%"
  exit /b 1
)

cd /d "%PUB%"
git add index.html data.json >> "%LOG%" 2>&1
git diff --cached --quiet
if not errorlevel 1 (
  echo [%NOW%] no change, nothing to publish >> "%LOG%"
  exit /b 0
)

git commit -m "snapshot %NOW%" >> "%LOG%" 2>&1
if errorlevel 1 (
  rem Without this the failed commit falls through to a push that says
  rem "Everything up-to-date", exits 0, and the run logs "published"
  rem while the page never changed.
  echo [%NOW%] ERROR: commit failed, nothing published >> "%LOG%"
  exit /b 1
)
git push origin snapshot >> "%LOG%" 2>&1
if errorlevel 1 (
  rem Most likely the fallback workflow pushed first. The next run resets to
  rem the remote and republishes, so leave it rather than forcing.
  echo [%NOW%] WARN: push rejected, will retry next run >> "%LOG%"
  exit /b 1
)

echo [%NOW%] published >> "%LOG%"
exit /b 0
