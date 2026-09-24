# Running Gamma_X on another Windows machine

Carry **one file** to the new PC: `bootstrap.ps1` from this folder. Everything
else is cloned from GitHub.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1
```

Re-run it whenever you like. Every step checks before it acts, so a second run
repairs what is missing and leaves the rest alone.

---

## Read this first: two publishers will fight

The dashboard is published by pushing a generated `index.html` to the
`snapshot` branch. **Two machines running the schedule at once both push to
that same branch.** They do not corrupt anything — the publisher resets to the
remote at the start of every run, and a rejected push is logged and retried
next time:

```
[...] WARN: push rejected, will retry next run
```

but you get double the Pages builds, and roughly half of each machine's runs
are wasted. Pick one:

| You want | Do this |
|---|---|
| **Move** to the new PC | Run bootstrap there, then on the old PC: `Disable-ScheduledTask -TaskName "Gamma_X Snapshot"` |
| **Cold spare** | `bootstrap.ps1 -SkipTask`. The checkout is ready; register the task only if the primary dies. |
| **Both, deliberately** | Run bootstrap on both and accept the churn. Nothing breaks. |

There is a third option that needs no second PC at all: the repo's
**"Publish snapshot (manual backup)"** workflow generates and publishes from a
GitHub runner. It has no schedule on purpose — GitHub delivered only ~13% of
this repo's scheduled firings at the requested minute, which would make it an
unreliable second writer rather than a useful backup.

---

## Prerequisites

`bootstrap.ps1` checks all three and stops with instructions if any is missing.

1. **Python 3** — python.org, tick *Add python.exe to PATH*.
2. **Git for Windows** — git-scm.com, keep the default *Git Credential
   Manager*. That is what stores the GitHub credential so the scheduled task
   can push without a token on disk.
3. **Clock set to Eastern** — `Set-TimeZone -Id 'Eastern Standard Time'`
   (elevated). The schedule is written in market wall-clock time. If the
   machine is on Pacific, every snapshot fires three hours out and the symptom
   looks like a broken schedule rather than a wrong clock.

`tzdata` is installed for you. It is **required**, not optional: Windows ships
no IANA timezone database, and without it `gex_terminal.py` silently falls back
to the PC clock and assumes it is already Eastern.

---

## Layout

Default root is `C:\GammaX`; override with `-Root D:\wherever`. The scripts
derive their own paths from where they sit, so nothing needs editing:

```
<Root>\
  Gamma_X\                  the repo
    gex_terminal.py         the whole dashboard
    config.ini              margins
    tools\
      publish_snapshot.bat  what the scheduled task runs
      market_sleep.bat      holds the machine awake during the session
      install_tasks.ps1     registers the task
      make_schedule.py      regenerates GammaX-Snapshot.xml
      GammaX-Snapshot.xml   50 triggers, one per run
  Gamma_X-snapshot\         second clone, the published branch
  publish.log
```

`Gamma_X-snapshot` is a **separate clone**, not a branch switch. The publisher
hard-resets it to the remote on every run, so it must hold nothing but the
generated page.

---

## The scheduled tasks

50 runs a weekday, across **two** tasks:

| Task | Window | Cadence | Triggers |
|---|---|---|---|
| `Gamma_X Snapshot` | 09:15 – 14:45 weekdays | every 15 min | 23 |
| | 18:20 weekdays | once | 1 |
| | 00:00, 04:00, 08:00, 20:00 daily | — | 4 |
| `Gamma_X Snapshot Close` | 15:00 – 16:45 weekdays | every 5 min | 22 |

**Each occurrence is its own trigger, not a `<Repetition>`, and that is the
whole point.** `WakeToRun` wakes a sleeping PC for a trigger's *start
boundary* and never for a repetition inside a trigger's duration. Measured on
this repo 09-21 to 09-23: every start boundary fired unattended, and **zero**
repetitions did — the task landed 3 or 4 runs a day instead of 50.

### Why two tasks and not one

Task Scheduler accepts at most **48 triggers** in one task and rejects the
whole document past that, with an error that never names the limit:

```
The task XML contains too many nodes of the same type.
(680,7):CalendarTrigger:
```

Measured on Windows 11 26200 by registering the real file truncated to each
length: 46, 47 and 48 register, 49 and 50 are rejected. The full day is 50, so
it is split. Both tasks run the same publisher and their windows do not
overlap — the narrowest gap is 15 minutes (14:45 → 15:00) against runs that
take about ten seconds.

**A rejected registration leaves the previous task in place**, still running
its old schedule. That looks identical to working, which is how a 50-trigger
file that had never once registered went unnoticed for two days.
`install_tasks.ps1` now counts the triggers back off the registered task,
prints failures in red, and exits non-zero. Regenerate with:

```powershell
py -3 tools\make_schedule.py     # refuses to write a file over 48 triggers
```

`publish_snapshot.bat` also calls `market_sleep.bat auto` on every run, which
holds the machine awake 09:00–17:15 on weekdays. That needs **two** timers:

- `standby-timeout-ac/dc` — the ordinary idle timer
- `SUB_SLEEP UNATTENDSLEEP` — the *unattended* timer, which is the one that
  governs a machine woken by a scheduled task. Its default is **120 seconds**,
  and it was sending the PC back to sleep two minutes after each wake no
  matter what the idle timer said.

`UNATTENDSLEEP` is hidden (`Attributes=1`), so `powercfg /query` will not print
it. Read it from the registry instead:

```powershell
$g=(powercfg /getactivescheme) -replace '.*GUID: ([a-f0-9-]+).*','$1'
Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\$g\238C9FA8-0AAD-41ED-83F4-97BE242C8F20\7bc4a2f9-d8fc-4469-b07b-33eb785aaca0" |
  Select-Object ACSettingIndex, DCSettingIndex
```

Either mechanism alone is enough. Both are in place because this failed twice.

### The task needs your Windows password

`LogonType=Password`, not S4U — an S4U task runs **without network access**,
which would break both the quote fetch and the git push. It is also the only
logon type observed to actually run its action on a wake-from-sleep on this
hardware: a task registered `InteractiveToken` recorded `LastTaskResult 0x0`
while doing nothing at all.

Windows re-asks for the password on every update of such a task. There is no
way around it and no way to store it in the repo.

### Failure notifications

A third task, `Gamma_X Alert`, raises a Windows notification with the reason:

- **When a run fails.** The publisher cannot show a toast itself — a
  `LogonType=Password` task runs in a non-interactive session — so it writes
  the reason to `<Root>\alert.txt` and starts `Gamma_X Alert`, which runs as
  `InteractiveToken` on the desktop and shows it with the tail of that run's
  log. The same reason is not repeated within an hour.
- **When runs stop.** Every 15 minutes through the weekday session it checks
  `<Root>\last_ok.txt`; if no run has succeeded for 35 minutes it says so,
  with Task Scheduler's last result for each task (a changed Windows password
  shows up here as `0x8007052E`). This covers what the publisher cannot report
  itself: the PC asleep or off, the task skipped for no network, a run killed
  at the 3 minute limit. It notifies once per outage, and again when runs
  resume.

The `InteractiveToken` caveat above does not bite here: this task is meant to
run only while someone is at the desktop, and never wakes the PC. Nothing
leaves the machine; state is in `<Root>\alert_state.json`.

---

## Checking it works

```powershell
# every scheduled run across both tasks - should be 50 times, 15 min apart
# through the session and 5 min apart from 15:00
Get-ScheduledTask | Where-Object { $_.TaskName -like "Gamma_X Snapshot*" } |
  ForEach-Object { $_.Triggers.StartBoundary } |
  ForEach-Object { ([datetime]$_).ToString("HH:mm") } | Sort-Object

# what actually ran today
Select-String "run start" C:\GammaX\publish.log | Select-Object -Last 20

# the feed most likely to break silently
py -3 C:\GammaX\Gamma_X\tools\check_oi_feed.py
```

The run count per day is the real test:

```powershell
(Select-String "run start" C:\GammaX\publish.log |
  Where-Object { $_.Line -match (Get-Date -Format 'yyyy-MM-dd') }).Count
```

On a full weekday that should approach 50. If you see 3 or 4, the registered
task still has the old `<Repetition>` triggers — re-run
`tools\install_tasks.ps1` **and read its output**, since a rejected
registration leaves the old task running.

---

## Things that will bite

- **The margins are per-account and are not set by bootstrap.** Until you set
  them from the Actions tab → *Set margins*, those panels show no liquidation
  bands. Do not guess values: a plausible-but-wrong margin distorts position
  sizing and is worse than a visibly missing one.
- **Editing `GammaX-Snapshot.xml` by hand** means editing 50 triggers. Change
  `tools/make_schedule.py` and regenerate instead.
- **The XML is stored UTF-8 so it diffs sensibly, but the task API is handed a
  UTF-16 string.** `install_tasks.ps1` swaps the declaration. Registering the
  file directly without that swap fails with
  `(1,40)::ERROR: unable to switch the encoding`.
- **`Microsoft-Windows-TaskScheduler/Operational` is disabled by default**, so
  there is no event log to consult when a run goes missing. `publish.log` and
  the Kernel-Power events are what you have. Enable it (needs elevation) if you
  want per-trigger detail.
