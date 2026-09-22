@echo off
setlocal

rem Set the AC idle-sleep timeout, in minutes. 0 means never sleep.
rem
rem Why this exists: Task Scheduler's WakeToRun wakes the machine for a
rem trigger's START BOUNDARY only. Repetition instances inside a trigger's
rem <Duration> do not wake it. With a 10 minute idle timeout the PC slept
rem about two minutes after each start-boundary run, so on 2026-09-21 the
rem snapshot task fired 11 times instead of 53 - the 09:15 and 15:00 runs
rem happened and every repeat behind them was lost.
rem
rem Rather than keep the machine awake around the clock, two tasks call this:
rem   GammaX-StayAwake   09:00 Mon-Fri  ->  market_sleep.bat 0
rem   GammaX-AllowSleep  17:15 Mon-Fri  ->  market_sleep.bat 10
rem so the PC stays up across the session and sleeps normally otherwise.
rem
rem powercfg /change needs no elevation - it edits the calling user's active
rem power scheme - which is why these two tasks can run as InteractiveToken
rem and need no stored password.

set "LOG=C:\GammaX\publish.log"
set "MINS=%~1"

for /f "tokens=* usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "NOW=%%t"

if "%MINS%"=="" (
  echo [%NOW%] ERROR: market_sleep.bat needs a timeout in minutes >> "%LOG%"
  exit /b 1
)

powercfg /change standby-timeout-ac %MINS%
if errorlevel 1 (
  echo [%NOW%] ERROR: powercfg failed setting standby-timeout-ac to %MINS% >> "%LOG%"
  exit /b 1
)

rem Read it back rather than trust the exit code: a silently ignored change
rem here is the whole failure mode this is meant to prevent, and it would
rem otherwise only show up as missing snapshots hours later.
for /f "tokens=* usebackq" %%v in (`powershell -NoProfile -Command ^
  "$g=(powercfg /getactivescheme) -replace '.*GUID: ([a-f0-9-]+).*','$1';" ^
  "$v=(powercfg /query $g SUB_SLEEP STANDBYIDLE | Select-String 'Current AC Power Setting Index');" ^
  "[Convert]::ToInt32(($v.ToString() -split ':')[1].Trim(),16)"`) do set "ACTUAL=%%v"

set /a WANT=%MINS%*60
if "%ACTUAL%"=="%WANT%" (
  echo [%NOW%] sleep timeout set to %MINS% min >> "%LOG%"
  exit /b 0
)

echo [%NOW%] ERROR: asked for %WANT%s idle sleep, scheme reports %ACTUAL%s >> "%LOG%"
exit /b 1
