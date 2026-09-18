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
rem
rem Adjust these four paths if you cloned somewhere else.

set "REPO=C:\GammaX\Gamma_X"
set "PUB=C:\GammaX\Gamma_X-snapshot"
set "PY=py -3"
set "LOG=C:\GammaX\publish.log"

for /f "tokens=* usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "NOW=%%t"
echo [%NOW%] --- run start >> "%LOG%"

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
