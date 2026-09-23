@echo off
setlocal enabledelayedexpansion

rem Set the AC idle-sleep timeout. Argument is minutes, or "auto" to pick from
rem the clock: 0 (never sleep) on a weekday between 09:00 and 17:15, else 10.
rem
rem Why this exists: Task Scheduler's WakeToRun wakes the machine for a
rem trigger's START BOUNDARY only. Repetition instances inside a trigger's
rem <Duration> do not wake it. With a 10 minute idle timeout the PC slept about
rem two minutes after each start-boundary run, so on 2026-09-21 and again on
rem 2026-09-22 the snapshot task fired 3 times on its schedule instead of 52 -
rem 09:15, 15:00 and 20:00 landed, and every repeat behind them was lost.
rem
rem This is called by publish_snapshot.bat on every run rather than by its own
rem scheduled task. The first attempt used two extra tasks at 09:00 and 17:15,
rem but they ran as InteractiveToken, and a task with that logon type reports
rem success without running its action when it fires on a wake-from-sleep: on
rem 2026-09-22 "Gamma_X Stay Awake" recorded LastTaskResult 0x0 at 09:00:00
rem having done nothing, and the machine slept again at 09:01:58.
rem
rem Driving it from the publisher avoids that entirely. The snapshot task runs
rem as LogonType=Password, which does wake and run correctly - it is the one
rem thing that demonstrably fired from sleep all day - so the 09:15 start
rem boundary wakes the PC, disables idle sleep, and every repetition behind it
rem then fires because the machine is simply awake.
rem
rem powercfg /change needs no elevation: it edits the calling user's own
rem active power scheme.

set "LOG=C:\GammaX\publish.log"
set "ARG=%~1"

for /f "tokens=* usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "NOW=%%t"

if "%ARG%"=="" (
  echo [%NOW%] ERROR: market_sleep.bat needs minutes, or "auto" >> "%LOG%"
  exit /b 1
)

if /i "%ARG%"=="auto" (
  for /f "tokens=* usebackq" %%w in (`powershell -NoProfile -Command ^
    "$n=Get-Date; $d=[int]$n.DayOfWeek; $t=$n.TimeOfDay;" ^
    "if ($d -ge 1 -and $d -le 5 -and $t -ge [timespan]'09:00:00' -and $t -lt [timespan]'17:15:00') { 0 } else { 10 }"`) do set "MINS=%%w"
) else (
  set "MINS=%ARG%"
)

rem Read the scheme before touching it, so an unchanged setting is a no-op
rem rather than a line in the log on every one of the day's 50 runs.
call :read_ac CURRENT
set /a WANT=!MINS!*60
if "!CURRENT!"=="!WANT!" exit /b 0

powercfg /change standby-timeout-ac !MINS!
if errorlevel 1 (
  echo [%NOW%] ERROR: powercfg failed setting standby-timeout-ac to !MINS! >> "%LOG%"
  exit /b 1
)

rem Read it back rather than trust the exit code: a silently ignored change
rem here is the whole failure mode this is meant to prevent, and it would
rem otherwise only show up as missing snapshots hours later.
call :read_ac ACTUAL
if "!ACTUAL!"=="!WANT!" (
  echo [%NOW%] idle sleep -^> !MINS! min >> "%LOG%"
  exit /b 0
)

echo [%NOW%] ERROR: asked for !WANT!s idle sleep, scheme reports !ACTUAL!s >> "%LOG%"
exit /b 1

:read_ac
for /f "tokens=* usebackq" %%v in (`powershell -NoProfile -Command ^
  "$g=(powercfg /getactivescheme) -replace '.*GUID: ([a-f0-9-]+).*','$1';" ^
  "$v=(powercfg /query $g SUB_SLEEP STANDBYIDLE | Select-String 'Current AC Power Setting Index');" ^
  "[Convert]::ToInt32(($v.ToString() -split ':')[1].Trim(),16)"`) do set "%~1=%%v"
goto :eof
