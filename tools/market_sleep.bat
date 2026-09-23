@echo off
setlocal enabledelayedexpansion

rem Keep the machine awake across the session. Argument is minutes of idle
rem sleep, or "auto" to pick from the clock: 0 (never sleep) on a weekday
rem between 09:00 and 17:15, else 10.
rem
rem Why this exists: Task Scheduler's WakeToRun wakes the machine for a
rem trigger's START BOUNDARY only. Repetition instances inside a trigger's
rem <Duration> do not wake it, so once the PC sleeps, every repeat behind the
rem start boundary is lost. Measured on the snapshot branch, 2026-09-21 to
rem 09-23: the three start boundaries (09:15, 15:00, 20:00) fired unattended
rem every day, and of the other 47 scheduled occurrences, none ever did.
rem
rem TWO timeouts decide this, and the first version set the wrong one.
rem
rem   standby-timeout-ac  the ATTENDED idle timeout - the one in Settings, and
rem                       the only one this script used to touch.
rem   UNATTENDSLEEP       the UNATTENDED sleep timeout. After a wake that no
rem                       human initiated - a wake timer firing for a scheduled
rem                       task - Windows returns to sleep on THIS timer instead,
rem                       and its default is 120 seconds. It is hidden from the
rem                       Settings UI, so it is easy to miss.
rem
rem The tell was in this file's own note: with a 10 minute idle timeout the PC
rem slept "about two minutes" after each start-boundary run. Two minutes is not
rem ten; it is the unattended default. Setting standby-timeout-ac to 0 could
rem never have held the machine up, because the wake was unattended and that
rem timer was not the one counting. Both are set now, on AC and on battery.
rem
rem An earlier attempt put this in two tasks of its own at 09:00 and 17:15, but
rem they ran as InteractiveToken, and a task with that logon type reports
rem success without running its action when it fires on a wake-from-sleep: on
rem 2026-09-22 "Gamma_X Stay Awake" recorded LastTaskResult 0x0 at 09:00:00
rem having done nothing. Driving it from the publisher avoids that - the
rem snapshot task runs as LogonType=Password, which does wake and run.
rem
rem powercfg needs no elevation: it edits the calling user's own active scheme.

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

rem Idle sleep in seconds, and the unattended timer alongside it. 0 means never
rem for both. Off-session the unattended timer goes back to the Windows default
rem of 120s rather than to the idle value: it governs a different situation and
rem there is no reason to hold a woken machine up for ten minutes at 03:00.
set /a WANT=!MINS!*60
if "!MINS!"=="0" (set "WANT_UNATT=0") else (set "WANT_UNATT=120")

rem Read both before touching anything, so an unchanged scheme is a no-op
rem rather than a line in the log on every one of the day's 50 runs.
call :read_val STANDBYIDLE AC CUR_IDLE
call :read_val UNATTENDSLEEP AC CUR_UNATT
if "!CUR_IDLE!"=="!WANT!" if "!CUR_UNATT!"=="!WANT_UNATT!" exit /b 0

rem Attended idle, both power sources. A portable machine that drops to
rem battery would otherwise keep the old timeout and sleep through the session.
powercfg /change standby-timeout-ac !MINS!
if errorlevel 1 (
  echo [%NOW%] ERROR: powercfg failed setting standby-timeout-ac to !MINS! >> "%LOG%"
  exit /b 1
)
powercfg /change standby-timeout-dc !MINS!

rem Unattended sleep. No /change alias exists for it, so it goes in by GUID
rem alias and needs /setactive to take effect - without that last line the
rem scheme is edited but the running configuration is not.
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP UNATTENDSLEEP !WANT_UNATT!
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP UNATTENDSLEEP !WANT_UNATT!
powercfg /setactive SCHEME_CURRENT
if errorlevel 1 (
  echo [%NOW%] ERROR: powercfg failed setting UNATTENDSLEEP to !WANT_UNATT! >> "%LOG%"
  exit /b 1
)

rem Read back rather than trust the exit codes. A silently ignored change here
rem is the whole failure mode this is meant to prevent, and it would otherwise
rem only show up as missing snapshots hours later. Some OEM images lock the
rem unattended timer; if that is happening, this line is where it says so.
call :read_val STANDBYIDLE AC GOT_IDLE
call :read_val UNATTENDSLEEP AC GOT_UNATT
if "!GOT_IDLE!"=="!WANT!" if "!GOT_UNATT!"=="!WANT_UNATT!" (
  echo [%NOW%] idle sleep -^> !MINS! min, unattended -^> !WANT_UNATT!s >> "%LOG%"
  exit /b 0
)

echo [%NOW%] ERROR: asked idle=!WANT!s unattended=!WANT_UNATT!s, scheme reports idle=!GOT_IDLE!s unattended=!GOT_UNATT!s >> "%LOG%"
exit /b 1

rem %1 setting alias under SUB_SLEEP, %2 AC or DC, %3 variable to set.
:read_val
for /f "tokens=* usebackq" %%v in (`powershell -NoProfile -Command ^
  "$g=(powercfg /getactivescheme) -replace '.*GUID: ([a-f0-9-]+).*','$1';" ^
  "$v=(powercfg /query $g SUB_SLEEP %~1 ^| Select-String 'Current %~2 Power Setting Index');" ^
  "if ($v) { [Convert]::ToInt32(($v.ToString() -split ':')[1].Trim(),16) } else { 'unreadable' }"`) do set "%~3=%%v"
goto :eof
