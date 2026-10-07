#!/usr/bin/env python3
"""Score the published gamma levels against what price actually did.

    python tools/score_levels.py                       # every symbol, near book
    python tools/score_levels.py --select nearest --top 2 --json calibration.json
    python tools/score_levels.py --era multi           # two-chain levels only

Why this exists
---------------
The dashboard asserts things it has never checked: that call walls cap
rallies, that put walls support, that positive gamma means a quieter session.
Trading those levels without measuring them is trading an assumption. This
reads the levels the dashboard ACTUALLY PUBLISHED - every data.json in the
snapshot branch's history - and replays each session's 5 minute futures bars
against them.

No look-ahead. For each session a symbol's levels are taken from the last
snapshot published at or before ENTRY_HHMM, and only bars from that snapshot's
time to the cash close are scored. Those are the levels you could have had on
screen, and the moves you could have traded.

Two trades per level, each simulated bar by bar
-----------------------------------------------
  fade      a resting order AT the wall, against the approach: short a call
            wall, long a put wall. Filled on the first touch.
  breakout  WITH the break, entered on the first 5 minute CLOSE beyond the
            wall - a wick through is not a break. Long through a call wall,
            short through a put wall.

Each is walked forward bar by bar against a fixed stop and a fixed target,
whichever comes first; a trade still open at the cash close is marked at the
close. A bar that spans both stop and target is scored as the STOP, because 5
minute bars cannot say which came first and the conservative reading is the
one that does not invent wins. The fill bar of a fade only counts against the
trade: price may have made its favourable extreme before it reached the wall.

The result is EXPECTANCY in R - the average outcome per trade in multiples of
the risk taken - at targets of 1, 1.5 and 2R. That is the number that says
whether a setup pays, and it needs no perfect exit to get there.

The stop is sized per symbol and side from the trades themselves: the p75 of
how far price went against the entry before reaching its best point, so a
stop at that distance stayed in three trades in four long enough to see it.

History
-------
An earlier version measured the fade's reward from the EXTREME price reached
past the wall rather than from the entry at the wall. A trade run 34 points
against and then retraced 30 was scored as 30 points of reward when it was 4
points down from where it was filled, so every fade R:R it reported was too
high. Its figures (ES calls 0.77-1.39, GC puts 2.24) should not be compared
with these.

Caveats this cannot fix
-----------------------
  - The stop is fitted to the same trades it is then scored on, which flatters
    every figure somewhat. A setup that only just clears zero has not cleared it.
  - The figures are unstable at this length. Refreshing the old version on
    2026-10-06 rolled the window by one session and moved ES calls from 0.77
    R:R to 1.39 and NQ calls from 0.85 to 1.53. Expect verdicts to move until
    n per side is in the dozens; the weekly refresh logs each one so the drift
    is visible rather than remembered.
  - The payload changed shape on 2026-09-29 and the ranking from gross to net
    on 10-01, and ES and NQ moved from one option chain to two on 10-06. Every
    session is tagged with its chain era; --era scores one era alone, and the
    report prints the split once both have data.
  - A session whose panel was held from an earlier run (feed gap) is skipped:
    those levels were not computed from that session's book.

--json writes the stops and expectancies the terminal reads back for its
trade plans, so the numbers on the page come from measured behaviour rather
than a round number someone liked.
"""
import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gex_terminal as g                                  # noqa: E402

ENTRY_HHMM = "09:50"      # first session run; levels are fixed by then
CLOSE_HHMM = "16:00"      # cash close - what these levels are traded against
BARS_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
            "{sym}?range=60d&interval=5m&includePrePost=true")
TARGETS_R = (1.0, 1.5, 2.0)
PLAN_R = 1.5              # the target multiple the dashboard's verdict reads
STOP_PCTILE = 75
TOP_DEFAULT = 3
APPROACHES = ("fade", "breakout")
CURRENT = {inst["future"] for inst in g.INSTRUMENTS}


# ----------------------------------------------------------------- history
def snapshot_commits(branch="origin/snapshot"):
    """(sha, committed datetime) for every snapshot, oldest first."""
    out = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H %cI",
                          branch], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"cannot read {branch}: {out.stderr.strip()}\n"
                         f"run: git fetch origin snapshot")
    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        sha, when = line.split()
        rows.append((sha, datetime.fromisoformat(when)))
    return list(reversed(rows))


def payload_at(sha):
    out = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:data.json"],
                         capture_output=True, text=True)
    if out.returncode:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None           # the first commits predate the sidecar


def generated_et(payload):
    """The ET timestamp the payload was generated at, or None."""
    raw = (payload.get("generated") or "").replace(" ET", "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def structure_of(sym, book):
    """Every level the terminal could have targeted: all ladder rows, both
    sides, plus the book's flip - the same set trade_plans() chooses from."""
    out = []
    ladder = sym.get("ladder")
    if ladder:
        out = [float(w["strike"]) for w in ladder if w.get("strike") is not None]
    else:                                   # pre-2026-09-29 shape
        walls = (((sym.get("regimes") or {}).get(book) or {}).get("walls")) or {}
        out = [float(w["strike"]) for side in ("call", "put")
               for w in walls.get(side) or []]
    flip = ((sym.get("regimes") or {}).get(book) or {}).get("flip")
    if flip:
        out.append(float(flip))
    return out


def levels_of(sym, book, top, select="net"):
    """Top `top` call and put levels for one symbol, newest payload shape or old.

    Returns {"call": [strike, ...], "put": [...], "flip": float|None}, strikes
    in FUTURES terms.

    select 'net' ranks by the book's own net gamma, strongest first - the
    dashboard's own ordering. 'nearest' takes the levels closest to spot
    instead. The two answer different questions: the biggest wall is where
    the most hedging sits, the nearest is the one price has to get through
    first, and only one of them can be the level a session actually trades
    against. Scoring both is the point.
    """
    out = {"call": [], "put": [], "flip": None}
    spot = float(sym["spot"])
    regimes = sym.get("regimes") or {}
    r = regimes.get(book) or {}
    out["flip"] = r.get("flip")

    ladder = sym.get("ladder")
    if ladder:
        for side in ("call", "put"):
            rows = []
            for w in ladder:
                if w.get("side") != side:
                    continue
                cell = (w.get("books") or {}).get(book) or {}
                net = cell.get("net")
                if net is None:
                    continue
                rows.append((abs(float(net)), float(w["strike"])))
            if select == "nearest":
                rows.sort(key=lambda r: abs(r[1] - spot))
            else:
                rows.sort(reverse=True)
            out[side] = [k for _, k in rows[:top]]
        return out

    walls = r.get("walls") or {}             # pre-2026-09-29 shape
    for side in ("call", "put"):
        ks = [float(w["strike"]) for w in (walls.get(side) or [])]
        if select == "nearest":
            ks.sort(key=lambda k: abs(k - spot))
        out[side] = ks[:top]
    return out


def sessions(book, top, since=None, until=None, select="net"):
    """One entry per (date, symbol): the levels on screen at ENTRY_HHMM.

    The LAST snapshot at or before ENTRY_HHMM wins, so a session whose 09:50
    run failed falls back to 08:00 rather than being dropped - those levels
    were still what the page showed.
    """
    chosen = {}
    for sha, _ in snapshot_commits():
        payload = payload_at(sha)
        if not payload:
            continue
        stamp = generated_et(payload)
        if not stamp:
            continue
        day = stamp.date()
        if (since and day < since) or (until and day > until):
            continue
        if stamp.strftime("%H:%M") > ENTRY_HHMM or not g.is_trading_day(day):
            continue
        for sym in payload.get("symbols") or []:
            entry = session_entry(sym, day, stamp, book, top, select)
            if entry:
                chosen[(day, sym["symbol"])] = entry
    return [chosen[k] for k in sorted(chosen)]


def session_entry(sym, day, stamp, book, top, select):
    """One symbol's levels from one snapshot, or None if not scoreable."""
    if not sym.get("ok") or sym.get("regimes_from"):
        return None               # failed, or a panel held from another session
    # Only what the dashboard still publishes. The history holds symbols since
    # dropped, and scoring them put figures for products nobody trades here
    # into the calibration and the weekly log.
    if sym.get("symbol") not in CURRENT:
        return None
    lv = levels_of(sym, book, top, select)
    if not lv["call"] and not lv["put"]:
        return None
    regime = ((sym.get("regimes") or {}).get(book) or {}).get("regime")
    return {
        "date": day, "symbol": sym["symbol"], "at": stamp,
        "spot": float(sym["spot"]), "regime": regime,
        "contract": (sym.get("contract") or {}).get("symbol"),
        "levels": lv,
        "structure": structure_of(sym, book),
        # Which option-chain era built these levels: ES and NQ moved from one
        # chain to two on 2026-10-06, and the two eras are not the same levels.
        "era": "multi" if len(sym.get("chains") or []) > 1 else "single",
        # Which gamma the walls were read from: the chain's own ("chain"), or
        # repriced to the live future after the close ("live", from
        # 2026-10-07). Snapshots before the tag read the close's, overnight.
        "basis": sym.get("gamma_basis") or (
            "chain" if g.chain_greeks_live(stamp) else "close"),
    }


# ---------------------------------------------------------------- evening
# The Globex evening into Asia, scored like the day. The map is the FIRST
# clean snapshot after the 18:00 roll - by then the book has dropped the day's
# expired options and the ratio stands on the newest cash close, so it is the
# evening's own book - and its walls are traded from that snapshot to London's
# open. "date" is the calendar evening: the evening of Monday trades into
# Tuesday's session. Friday and Saturday evenings have no session.
EVENING_MAP_FROM = "18:00"
EVENING_MAP_TO = "21:00"   # a map later than this has missed the evening
EVENING_END = "03:00"
WINDOWS = ("day", "evening")


def evening_sessions(book, top, since=None, until=None, select="net"):
    chosen = {}
    for sha, _ in snapshot_commits():
        payload = payload_at(sha)
        if not payload:
            continue
        stamp = generated_et(payload)
        if not stamp:
            continue
        day = stamp.date()
        if (since and day < since) or (until and day > until):
            continue
        hhmm = stamp.strftime("%H:%M")
        if not EVENING_MAP_FROM <= hhmm < EVENING_MAP_TO or day.weekday() in (4, 5):
            continue
        for sym in payload.get("symbols") or []:
            key = (day, sym.get("symbol"))
            # The FIRST snapshot: a later one has seen the evening's bars.
            if key in chosen and chosen[key]["at"] <= stamp:
                continue
            entry = session_entry(sym, day, stamp, book, top, select)
            if entry:
                chosen[key] = entry
    return [chosen[k] for k in sorted(chosen)]


def evening_bars(sym, day, start_hhmm):
    """Bars from the evening map to EVENING_END, across midnight."""
    b = bars(sym)
    return ([r for r in b.get(day) or [] if r[0] >= start_hhmm]
            + [r for r in b.get(day + timedelta(days=1)) or []
               if r[0] < EVENING_END])


# -------------------------------------------------------------------- bars
_BARS = {}


def bars(sym):
    """5 minute bars as {date: [(hhmm, high, low, close), ...]}, ET.

    Same timestamp convention as fetch_rows: epoch plus the feed's own
    gmtoffset, read as naive exchange-local time.
    """
    if sym in _BARS:
        return _BARS[sym]
    result = json.loads(g.http_get(BARS_URL.format(sym=sym)))["chart"]["result"][0]
    stamps = result["timestamp"]
    q = result["indicators"]["quote"][0]
    off = (result.get("meta") or {}).get("gmtoffset") or 0
    by_day = defaultdict(list)
    for i, ts in enumerate(stamps):
        hi, lo, cl = q["high"][i], q["low"][i], q["close"][i]
        if hi is None or lo is None or cl is None:
            continue
        t = datetime.fromtimestamp(ts + off, timezone.utc).replace(tzinfo=None)
        by_day[t.date()].append((t.strftime("%H:%M"), float(hi), float(lo),
                                 float(cl)))
    _BARS[sym] = dict(by_day)
    return _BARS[sym]


def session_bars(sym, day, start_hhmm):
    rows = bars(sym).get(day) or []
    # Strictly before the close: the bar stamped 16:00 covers 16:00-16:05,
    # after the cash close. Including it marked open trades at 16:05 and let
    # stops and targets be "hit" after the session; the 15:55 bar's close IS
    # the 16:00 price. Found cross-checking the setups page, which had it right.
    return [b for b in rows if start_hhmm <= b[0] < CLOSE_HHMM]


# ------------------------------------------------------------------ trades
def fade_event(rows, level, side):
    """The fade at one level: (start index, entry, direction) or None.

    Filled at the level itself on the first bar that reaches it. Direction is
    -1 (short) at a call wall, +1 (long) at a put wall.
    """
    up = side == "call"
    for i, (_, hi, lo, _c) in enumerate(rows):
        if (hi >= level) if up else (lo <= level):
            return i, level, (-1 if up else +1)
    return None


def breakout_event(rows, level, side):
    """The breakout: entered on the CLOSE of the first bar to close beyond.

    Starts on the bar after, since the entry bar is over once its close is
    known. A break on the session's last bar leaves nothing to score.
    """
    up = side == "call"
    for i, (_, _h, _l, cl) in enumerate(rows):
        if (cl > level) if up else (cl < level):
            if i + 1 >= len(rows):
                return None
            return i + 1, cl, (+1 if up else -1)
    return None


def excursions(rows, start, entry, direction, fill_bar_adverse_only):
    """(adverse before the best point, best point), both in points.

    'Adverse before the best point' is how far price went against the trade
    before it reached its most favourable price - the heat a stop has to
    survive for the trade to still be on at its best. Heat AFTER the best
    point is not risk a trade exiting there ever carried.
    """
    fav, adv = [], []
    for k, (_, hi, lo, _c) in enumerate(rows[start:]):
        f = (hi - entry) if direction > 0 else (entry - lo)
        a = (entry - lo) if direction > 0 else (hi - entry)
        if k == 0 and fill_bar_adverse_only:
            f = 0.0
        fav.append(max(0.0, f))
        adv.append(max(0.0, a))
    if not fav:
        return None
    peak = max(range(len(fav)), key=lambda i: fav[i])
    return max(adv[:peak + 1]), fav[peak]


def simulate(rows, start, entry, direction, stop_pts, target_pts,
             fill_bar_adverse_only):
    """Outcome in R: +target/stop, -1, or marked to the close if neither hit.

    A bar reaching both is the STOP - 5 minute bars cannot order them, and
    assuming the target would invent wins.
    """
    for k, (_, hi, lo, _c) in enumerate(rows[start:]):
        a = (entry - lo) if direction > 0 else (hi - entry)
        f = (hi - entry) if direction > 0 else (entry - lo)
        if a >= stop_pts:
            return -1.0, "stop"
        if k == 0 and fill_bar_adverse_only:
            continue
        if f >= target_pts:
            return target_pts / stop_pts, "target"
    last = rows[-1][3]
    return (last - entry) * direction / stop_pts, "close"


def pct(values, p):
    """Percentile by nearest rank; no numpy, and honest about tiny samples."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    i = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[i]


def fmt(v, nd=2):
    return "-" if v is None else f"{v:,.{nd}f}"


# ------------------------------------------------------------------ rules
# The trader's own rules: a fixed 1:1 stop and target in futures points, both
# setups. ES is not traded but is measured and shown exactly like the others,
# as a reference for NQ - REFERENCE_ONLY marks it so the pages say so. Measured
# alongside the calibrated figures, split by the gamma context known when each
# trade triggers, because that is the call the trader makes at the screen.
RULE_POINTS = {"NQ": 40.0, "GC": 10.0, "ES": 10.0}
REFERENCE_ONLY = {"ES"}


def trade_path(rows, start, entry, direction, fill_bar_adverse_only):
    """A trade's running worst and best, enough to replay ANY stop and target.

    adv, fav   [[bar, points], ...] each time the running max grows, bars
               counted from `start`. The first adv step at or past a stop is
               where it stops out; the first fav step at or past a target is
               where it pays; on the same bar the stop wins, as in simulate().
               A fade's fill bar counts against only, as there.
    marks      [[hh:mm, bar, points], ...] the open result at every half hour
               and at the last bar, so a trade can be closed by a time.

    This is derived from the bars without being them: no prices, only how far
    each trade went for and against, so it can sit on a public page.
    """
    adv, fav, marks = [], [], []
    worst = best = 0.0
    seg = rows[start:]
    for k, (t, hi, lo, cl) in enumerate(seg):
        a = (entry - lo) if direction > 0 else (hi - entry)
        f = (hi - entry) if direction > 0 else (entry - lo)
        if k == 0 and fill_bar_adverse_only:
            f = 0.0
        if a > worst:
            worst = a
            adv.append([k, round(a, 4)])
        if f > best:
            best = f
            fav.append([k, round(f, 4)])
        end = (datetime.strptime(t, "%H:%M") + timedelta(minutes=5)).strftime("%H:%M")
        if end[3:] in ("00", "30") or k == len(seg) - 1:
            marks.append([end, k, round((cl - entry) * direction, 4)])
    return adv, fav, marks


def replay(t, stop, target):
    """(R, how) for a stored trade at any stop and target, held to its end.
    The page's replay() is this, line for line."""
    ks = next((k for k, v in t["adv"] if v >= stop), None)
    kt = next((k for k, v in t["fav"] if v >= target), None)
    if ks is not None and (kt is None or ks <= kt):
        return -1.0, "stop"
    if kt is not None:
        return target / stop, "target"
    return t["marks"][-1][2] / stop, "close"


def rule_trades(since=None, until=None):
    """Every fade and breakout at every scored wall, day and evening, any
    symbol still published, each with its path (trade_path).

    Returns (trades, covered): covered is every (date, window, symbol) these
    bars could score, so a merge knows which stored sessions they replace.
    """
    now = g.now_et().replace(tzinfo=None)
    out, covered = [], set()
    for win, rows_of, bars_of, end in (
            ("day", sessions, session_bars, CLOSE_HHMM),
            ("evening", evening_sessions, evening_bars, EVENING_END)):
        for s in rows_of("near", 2, since, until, "nearest"):
            sym = s["symbol"]
            over = datetime.combine(
                s["date"] + timedelta(days=1 if win == "evening" else 0),
                datetime.strptime(end, "%H:%M").time())
            if over > now or not s["contract"]:
                continue              # still trading, or nothing to price it
            try:
                rb = bars_of(s["contract"], s["date"], s["at"].strftime("%H:%M"))
            except Exception:
                continue
            if len(rb) < 6:
                continue
            covered.add((s["date"].isoformat(), win, sym))
            flip = s["levels"].get("flip")
            open_px = rb[0][3]
            for side in ("call", "put"):
                for level in s["levels"][side]:
                    if (level <= open_px) if side == "call" else (level >= open_px):
                        continue      # passed before the map: never a trade
                    for name, finder, fill in (("fade", fade_event, True),
                                               ("breakout", breakout_event, False)):
                        ev = finder(rb, level, side)
                        if not ev:
                            continue
                        start, entry, d = ev
                        adv, fav, marks = trade_path(rb, start, entry, d, fill)
                        out.append({
                            "date": s["date"].isoformat(), "window": win,
                            "symbol": sym, "setup": name, "side": side,
                            "level": round(level, 2), "regime": s["regime"],
                            "flip_side": (("above" if entry > flip else "below")
                                          if flip else None),
                            "era": s["era"], "basis": s["basis"],
                            "map_at": s["at"].strftime("%H:%M"),
                            "time": rb[start if fill else start - 1][0],
                            "entry": round(entry, 2),
                            "adv": adv, "fav": fav, "marks": marks})
    return out, covered


def merge_trades(stored, fresh, covered):
    """Fresh trades replace stored ones for every session the bars covered;
    every other stored session is kept. Yahoo serves 60 days of 5-minute bars,
    so without this the record could never grow past two months."""
    keep = [t for t in stored
            if (t["date"], t.get("window", "day"), t["symbol"]) not in covered]
    return sorted(keep + fresh, key=lambda t: (t["date"], t.get("window", "day"),
                                               t["symbol"], t["time"]))


def score_rules(trades, window="day"):
    """Fades and breakouts under RULE_POINTS, split by gamma context.

    regime   the near book's net GEX sign in the snapshot the levels came from
    flip     the ENTRY price above or below that book's flip

    Same entries and bar rules as everything else here; only the stop and
    target are fixed. Nothing is fitted, so these carry none of the in-sample
    flattery of the calibrated stops.

    Measured 2026-10-07 over 50 trades: fades +0.60R in negative gamma (12 of
    15 won) against -0.09R in positive; breakouts lost in both (-0.29R /
    -0.24R). Against the textbook, which has positive gamma favouring fades.
    """
    acc = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for t in trades:
        sym = t["symbol"]
        if sym not in RULE_POINTS or t.get("window", "day") != window:
            continue
        pts = RULE_POINTS[sym]
        r, how = replay(t, pts, pts)
        rec = (r, how == "target")
        grp = acc[sym][t["setup"]]
        grp["all"].append(rec)
        if t["regime"] in ("positive", "negative"):
            grp[t["regime"]].append(rec)
        if t["flip_side"]:
            grp[t["flip_side"]].append(rec)
    out = {}
    for sym, setups in acc.items():
        for name, groups in setups.items():
            out.setdefault(sym, {"points": RULE_POINTS[sym]})[name] = {
                k: {"n": len(v), "won": sum(1 for _, w in v if w),
                    "exp": round(sum(r for r, _ in v) / len(v), 3)}
                for k, v in groups.items()}
    return out


MIN_STOP_PCT = 0.10       # floor on a calibrated stop, percent of spot. About
                          # one 5 minute ES bar: a stop inside a single bar's
                          # range is noise, and a breakout that ran at once
                          # otherwise calibrates a stop near zero and reports
                          # its close-out as dozens of R.


def open_location(series, day):
    """'in' or 'out': the 09:30 price against the prior session's 09:30-16:00
    range, or None if either is missing.

    Logged weekly as a split, not used by the plans. On 2026-10-07, over 33
    sessions, fades did +0.16R when the open was inside the prior range and
    -0.47R outside, breakouts the reverse (-0.13R / +0.18R) - the only split
    where both setups lined up with auction theory, at about 1.8 standard
    errors. Suggestive, not acted on: this records whether it holds.
    """
    days = bars(series)
    today = days.get(day) or []
    at_open = [b for b in today if b[0] == "09:25"] or \
              [b for b in today if b[0] == "09:30"]
    if not at_open:
        return None
    px = at_open[0][3]
    d = day
    for _ in range(6):
        d = d - timedelta(days=1)
        if not g.is_trading_day(d):
            continue
        prev = [b for b in (days.get(d) or []) if "09:30" <= b[0] < "16:00"]
        if prev:
            lo, hi = min(b[2] for b in prev), max(b[1] for b in prev)
            return "in" if lo <= px <= hi else "out"
        return None
    return None


def summarise(evs):
    """Fit a stop to these trades, then score every target multiple.

    Returns None with fewer than two trades: a percentile of one is that one.
    """
    if len(evs) < 2:
        return None
    heat = [100.0 * e["adv"] / e["entry"] for e in evs]
    # Rounded BEFORE it is used, to exactly the figure calibration.json
    # publishes and the setups page trades. Simulating with the unrounded
    # value while publishing the rounded one let a base rate and the page
    # disagree on a close trade: an ES call fade on 2026-09-24 came within
    # 0.08 points (under a tick) of its stop - stopped out at 0.21734%, held
    # at the published 0.217%.
    stop_pct = round(max(MIN_STOP_PCT, pct(heat, STOP_PCTILE)), 3)
    out = {"n": len(evs), "stop_pct": round(stop_pct, 3), "exp": {}, "win": {},
           "by_era": {}}
    for mult in TARGETS_R:
        rs, wins = [], 0
        for e in evs:
            stop_pts = e["entry"] * stop_pct / 100.0
            r, how = simulate(e["rows"], e["start"], e["entry"], e["dir"],
                              stop_pts, stop_pts * mult, e["fill"])
            e.setdefault("r", {})[mult] = r
            rs.append(r)
            wins += how == "target"
        out["exp"][f"{mult:g}"] = round(sum(rs) / len(rs), 3)
        out["win"][f"{mult:g}"] = round(wins / len(rs), 3)
    out["exp_plan"] = out["exp"][f"{PLAN_R:g}"]
    out["win_plan"] = out["win"][f"{PLAN_R:g}"]

    # The trade the terminal actually proposes: the same fitted stop, but the
    # target is the first wall or flip on THAT morning's published ladder in
    # the trade's direction at least a stop away - exactly the rule in
    # gex_terminal.trade_plans(). The fixed-R figures above measure an exit
    # nobody takes; this is what the page's R:R column was shown beside, so
    # it is what the verdict reads. A trade with nothing to target is
    # skipped, as the page would have shown it "no room".
    sr, swins, tgt_r = [], 0, []
    for e in evs:
        stop_pts = e["entry"] * stop_pct / 100.0
        ahead = sorted(abs(k - e["entry"]) for k in e.get("structure") or ()
                       if (k - e["entry"]) * e["dir"] >= stop_pts)
        if not ahead:
            continue
        r, how = simulate(e["rows"], e["start"], e["entry"], e["dir"],
                          stop_pts, ahead[0], e["fill"])
        e["r_wall"] = r
        sr.append(r)
        swins += how == "target"
        tgt_r.append(ahead[0] / stop_pts)
    out["wall"] = ({"n": len(sr), "exp": round(sum(sr) / len(sr), 3),
                    "win": round(swins / len(sr), 3),
                    "avg_target_r": round(sum(tgt_r) / len(tgt_r), 2)}
                   if sr else None)
    eras = defaultdict(list)
    for e in evs:
        eras[e["era"]].append(e["r"][PLAN_R])
    for era, rs in eras.items():
        out["by_era"][era] = {"n": len(rs), "exp_plan": round(sum(rs) / len(rs), 3)}
    locs = defaultdict(list)
    for e in evs:
        if e.get("loc"):
            locs[e["loc"]].append(e["r"][PLAN_R])
    out["by_open"] = {k: {"n": len(rs), "exp_plan": round(sum(rs) / len(rs), 3)}
                      for k, rs in locs.items()}
    return out


# --------------------------------------------------------- opening range
ORB_CUTOFF = "09:30"      # context only from snapshots published before this
ORB_LAST_ENTRY = "15:00"


def orb_context(since=None, until=None):
    """Pre-open gamma context per (date, symbol): regime, flip, nearby walls.

    From the last snapshot published BEFORE 09:30, so everything an ORB split
    uses was known before the range even formed.
    """
    chosen = {}
    for sha, _ in snapshot_commits():
        p = payload_at(sha)
        if not p:
            continue
        stamp = generated_et(p)
        if not stamp or stamp.strftime("%H:%M") >= ORB_CUTOFF:
            continue
        day = stamp.date()
        if (since and day < since) or (until and day > until):
            continue
        if not g.is_trading_day(day):
            continue
        for sym in p.get("symbols") or []:
            if (not sym.get("ok") or sym.get("regimes_from")
                    or sym.get("symbol") not in CURRENT):
                continue
            lv = levels_of(sym, "near", 3, "nearest")
            if not lv["call"] and not lv["put"]:
                continue        # a feed-gap snapshot with no walls
            near = (sym.get("regimes") or {}).get("near") or {}
            chosen[(day, sym["symbol"])] = {
                "date": day, "symbol": sym["symbol"],
                "contract": (sym.get("contract") or {}).get("symbol"),
                "regime": near.get("regime"), "flip": near.get("flip"),
                "walls": lv["call"] + lv["put"]}
    return [chosen[k] for k in sorted(chosen)]


def score_orb(since=None, until=None):
    """15-minute opening-range breakout, split by pre-open gamma context.

    Rules fixed on 2026-10-07 and NOT fitted: range = 09:30-09:45 high/low;
    entry on the first 5-minute close beyond it up to 15:00, one per session;
    stop at the far side of the range ("range") or its midpoint ("mid");
    1.5R target; a bar touching both is the stop; open trades marked at the
    close. Logged weekly, never shown on the page.

    Why it is logged: over 36 sessions to 2026-10-06 it paid +0.61R when the
    09:30 price was ABOVE the near-book gamma flip and -0.33R below it, for
    long and short breaks alike, and better with no published wall between
    entry and target (+0.39R clear, -0.06R blocked) - against the textbook
    view that negative gamma favours breakouts. About 2.3 standard errors on
    a few dozen comparisons; this records whether it survives.
    """
    now = g.now_et()
    live = now.date() if now.strftime("%H:%M") < CLOSE_HHMM else None
    trades = []
    for c in orb_context(since, until):
        if c["date"] == live or not c["contract"]:
            continue
        try:
            days = bars(c["contract"])
        except Exception:
            continue
        today = days.get(c["date"]) or []
        rth = [b for b in today if "09:30" <= b[0] < CLOSE_HHMM]
        first = [b for b in rth if b[0] in ("09:30", "09:35", "09:40")]
        if len(first) < 3 or len(rth) < 30:
            continue
        pre = [b for b in today if b[0] == "09:25"]
        open_px = pre[0][3] if pre else first[0][3]
        hi, lo = max(b[1] for b in first), min(b[2] for b in first)
        after = [b for b in rth if b[0] >= "09:45"]
        entry_i = next((i for i, b in enumerate(after)
                        if b[0] <= ORB_LAST_ENTRY and (b[3] > hi or b[3] < lo)),
                       None)
        if entry_i is None or entry_i + 1 >= len(after):
            continue
        entry = after[entry_i][3]
        direction = +1 if entry > hi else -1
        rec = {"symbol": c["symbol"],
               "flip": (("above" if open_px > c["flip"] else "below")
                        if c["flip"] else None),
               "regime": c["regime"], "r": {}}
        for stop_name, stop_px in (("range", lo if direction > 0 else hi),
                                   ("mid", (hi + lo) / 2)):
            risk = abs(entry - stop_px)
            if risk <= 0:
                continue
            r, _ = simulate(after, entry_i + 1, entry, direction, risk,
                            risk * PLAN_R, False)
            rec["r"][stop_name] = r
            if stop_name == "range":
                tgt = entry + direction * risk * PLAN_R
                a, b = sorted((entry, tgt))
                rec["path"] = ("blocked" if any(a < w < b for w in c["walls"])
                               else "clear")
        trades.append(rec)

    out = {}
    for sym in sorted({t["symbol"] for t in trades}) + ["ALL"]:
        sub = [t for t in trades if sym == "ALL" or t["symbol"] == sym]
        for stop_name in ("range", "mid"):
            cell = out.setdefault(sym, {}).setdefault(stop_name, {})
            for split, key in (("all", lambda t: "all"),
                               ("open_vs_flip", lambda t: t["flip"]),
                               ("regime", lambda t: t["regime"]),
                               ("wall_in_path", lambda t: t.get("path"))):
                groups = defaultdict(list)
                for t in sub:
                    if stop_name in t["r"] and key(t):
                        groups[key(t)].append(t["r"][stop_name])
                cell[split] = {v: {"n": len(rs),
                                   "exp_plan": round(sum(rs) / len(rs), 3)}
                               for v, rs in groups.items()}
    return out


def fmt_r(v):
    return "-" if v is None else f"{v:+.2f}"


def score(book, top, since=None, until=None, select="net", era="all",
          verbose=False, trades_path=None):
    rows = sessions(book, top, since, until, select)
    if era != "all":
        rows = [r for r in rows if r["era"] == era]
    if not rows:
        raise SystemExit("no sessions matched - try --since or --era, or "
                         "fetch the snapshot branch")

    levels = defaultdict(lambda: defaultdict(int))          # sym -> side -> n
    trades = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    ranges = defaultdict(lambda: defaultdict(list))
    counted = defaultdict(int)
    missing_bars = defaultdict(int)
    wrong_side = defaultdict(int)

    now = g.now_et()
    live_day = now.date() if now.strftime("%H:%M") < CLOSE_HHMM else None
    in_progress = defaultdict(int)
    scored_days = set()
    for s in rows:
        sym, day = s["symbol"], s["date"]
        # A session still trading is not scored: its open trades would be
        # marked at the latest bar rather than the close, and a figure that
        # changes every time this runs during the day is not a measurement.
        if day == live_day:
            in_progress[sym] += 1
            continue
        series = s["contract"] or None
        rb = []
        if series:
            try:
                rb = session_bars(series, day, s["at"].strftime("%H:%M"))
            except Exception as exc:
                print(f"  bars unavailable for {series}: {type(exc).__name__}",
                      file=sys.stderr)
        if len(rb) < 6:
            missing_bars[sym] += 1
            continue
        counted[sym] += 1
        scored_days.add(day)
        hi = max(b[1] for b in rb)
        lo = min(b[2] for b in rb)
        if s["regime"]:
            ranges[sym][s["regime"]].append(100.0 * (hi - lo) / s["spot"])

        open_px = rb[0][3]
        for side in ("call", "put"):
            for level in s["levels"][side]:
                # Already past at entry: not a level being tested.
                if (level <= open_px) if side == "call" else (level >= open_px):
                    wrong_side[sym] += 1
                    continue
                levels[sym][side] += 1
                if "loc" not in s:
                    s["loc"] = open_location(series, day)
                for name, finder, fill in (("fade", fade_event, True),
                                           ("breakout", breakout_event, False)):
                    ev = finder(rb, level, side)
                    if not ev:
                        continue
                    start, entry, direction = ev
                    ex = excursions(rb, start, entry, direction, fill)
                    if not ex:
                        continue
                    trades[sym][side][name].append({
                        "rows": rb, "start": start, "entry": entry,
                        "dir": direction, "fill": fill, "adv": ex[0],
                        "best": ex[1], "era": s["era"], "date": day,
                        # Only once the open is known: a trade entered before
                        # 09:30 could not have been filtered on it.
                        "loc": s["loc"] if rb[start][0] >= "09:30" else None,
                        "structure": s["structure"]})
                    if verbose:
                        print(f"  {day} {sym} {side} {name:<8} {level:,.2f} "
                              f"entry {entry:,.2f} heat {ex[0]:.2f} "
                              f"best {ex[1]:.2f}")

    picked = "largest net gamma" if select == "net" else "nearest to spot"
    eras = sorted({r["era"] for r in rows})
    print(f"Levels scored: book '{book}', top {top} a side by {picked}, "
          f"{len(rows)} session-symbols, entry {ENTRY_HHMM} -> {CLOSE_HHMM} ET, "
          f"era {era}")
    print(f"Each trade walked bar by bar: stop = p{STOP_PCTILE} of heat before "
          f"the best point (floor {MIN_STOP_PCT}%), bar touching both = stop.")
    print()
    hdr = (f"{'sym':<4} {'side':<5} {'setup':<9} {'n':>3} {'rate':>5} "
           f"{'stop':>6} {'exp@1R':>7} {'@1.5R':>7} {'@2R':>7} {'win@1.5':>8}"
           f"   {'to wall':>8} {'n':>3} {'win':>5} {'avg tgt':>8}")
    print(hdr)
    print("-" * len(hdr))

    calib = {}
    for sym in sorted(trades):
        for side in ("call", "put"):
            n_lv = levels[sym][side]
            if not n_lv:
                continue
            entry = calib.setdefault(sym, {}).setdefault(side, {
                "sessions": counted[sym], "n_levels": n_lv})
            for name in APPROACHES:
                evs = trades[sym][side][name]
                res = summarise(evs)
                rate = len(evs) / n_lv
                if res:
                    res["rate"] = round(rate, 3)
                    entry[name] = res
                    e = res["exp"]
                    w = res.get("wall") or {}
                    wall = (f"   {fmt_r(w['exp']):>8} {w['n']:>3} "
                            f"{w['win']:>5.0%} {w['avg_target_r']:>7.1f}R"
                            if w else f"   {'-':>8}")
                    print(f"{sym:<4} {side:<5} {name:<9} {len(evs):>3} "
                          f"{rate:>5.0%} {res['stop_pct']:>5.2f}% "
                          f"{fmt_r(e['1']):>7} {fmt_r(e['1.5']):>7} "
                          f"{fmt_r(e['2']):>7} {res['win_plan']:>8.0%}" + wall)
                else:
                    print(f"{sym:<4} {side:<5} {name:<9} {len(evs):>3} "
                          f"{rate:>5.0%}      -       -       -       -        -")

    print()
    print(f"Expectancy at {PLAN_R:g}R by option-chain era (same stops):")
    shown = 0
    for sym in sorted(calib):
        for side, v in calib[sym].items():
            if not isinstance(v, dict):
                continue
            for name in APPROACHES:
                split = (v.get(name) or {}).get("by_era") or {}
                if len(split) > 1:
                    parts = "  ".join(f"{k} {fmt_r(x['exp_plan'])} (n={x['n']})"
                                      for k, x in sorted(split.items()))
                    print(f"  {sym:<4} {side:<5} {name:<9} {parts}")
                    shown += 1
    if not shown:
        # Said rather than left blank: the split is the point of tagging eras,
        # and an empty table reads as "no difference" when it means "no data".
        print("  none yet - no session built from two chains has finished "
              "trading")

    print()
    print("Session range by reported regime (the claim that needs no levels):")
    for sym in sorted(ranges):
        parts = []
        for regime in ("positive", "negative"):
            v = ranges[sym].get(regime) or []
            if v:
                parts.append(f"{regime} {sum(v)/len(v):.2f}% (n={len(v)})")
        print(f"  {sym:<4} " + ("  ".join(parts) if parts else "-"))

    off = {k: v for k, v in wrong_side.items() if v}
    if off:
        print("\nLevels skipped as already passed at entry: "
              + ", ".join(f"{k} {v}" for k, v in sorted(off.items())))
    if in_progress:
        print("Sessions still trading, not scored: "
              + ", ".join(f"{k} {v}" for k, v in sorted(in_progress.items())))
    skipped = {k: v for k, v in missing_bars.items() if v}
    if skipped:
        print("Sessions skipped for want of bars: "
              + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
    print("Sessions with bars: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counted.items())))

    fresh, covered = rule_trades(since, until)
    stored = []
    if trades_path and Path(trades_path).exists():
        stored = json.loads(Path(trades_path).read_text(encoding="utf-8"))["trades"]
    all_trades = merge_trades(stored, fresh, covered)
    lo, hi = (since.isoformat() if since else ""), (until.isoformat() if until else "9")
    window_trades = [t for t in all_trades if lo <= t["date"] <= hi]
    rules = score_rules(window_trades, "day")
    rules_evening = score_rules(window_trades, "evening")
    for label, rs in (("day", rules), ("evening, 18:00 map to 03:00", rules_evening)):
        if not rs:
            continue
        print(f"\nYour rules (fixed 1:1 points), {label}, by gamma context:")
        for sym in sorted(rs):
            for name in APPROACHES:
                x = rs[sym].get(name) or {}
                parts = []
                for k in ("all", "positive", "negative", "above", "below"):
                    if k in x:
                        parts.append(f"{k} {x[k]['won']}/{x[k]['n']} "
                                     f"{fmt_r(x[k]['exp'])}")
                print(f"  {sym} {rs[sym]['points']:g}pt {name:<9} "
                      + "   ".join(parts))

    orb = score_orb(since, until)
    if orb.get("ALL"):
        print(f"\nOpening-range breakout (logged, not used), {PLAN_R:g}R target:")
        for stop_name in ("range", "mid"):
            sp = orb["ALL"][stop_name]
            parts = [f"all {fmt_r(sp['all']['all']['exp_plan'])} "
                     f"(n={sp['all']['all']['n']})"] if sp.get("all") else []
            for split in ("open_vs_flip", "wall_in_path"):
                for v, x in sorted((sp.get(split) or {}).items()):
                    parts.append(f"{v} {fmt_r(x['exp_plan'])} (n={x['n']})")
            print(f"  {stop_name:<5} stop: " + "   ".join(parts))

    # The span of sessions actually SCORED: one still trading, or without
    # bars, is in rows but measured nothing, and counting it would let the
    # page claim a day of evidence it does not have.
    days = sorted(scored_days)
    return {"book": book, "top": top, "select": select, "era": era,
            "plan_r": PLAN_R, "stop_pctile": STOP_PCTILE,
            "entry": ENTRY_HHMM, "close": CLOSE_HHMM,
            "sessions": len(rows), "days": len(days),
            "from": days[0].isoformat() if days else None,
            "to": days[-1].isoformat() if days else None,
            "scored_at": g.now_et().strftime("%Y-%m-%d"),
            "symbols": calib, "orb": orb, "rules": rules,
            "rules_evening": rules_evening, "_trades": all_trades}


LOG_HEADER = ["scored_at", "days", "from", "to", "symbol", "side", "setup",
              "n", "rate", "stop_pct", f"exp_{PLAN_R:g}R", f"win_{PLAN_R:g}R",
              "n_wall", "exp_wall", "win_wall", "avg_target_r"]


SPLIT_HEADER = ["scored_at", "days", "from", "to", "symbol", "side", "setup",
                "split", "value", "n", f"exp_{PLAN_R:g}R"]


def append_split_log(path, out):
    """The open-location split, one row per symbol, side, setup and value.

    Its own file so the main log keeps its format; same-day rows replaced.
    """
    path = Path(path)
    rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh)][1:]
    rows = [r for r in rows if r and r[0] != out["scored_at"]]
    for sym, sides in sorted(out["symbols"].items()):
        for side, v in sorted(sides.items()):
            if not isinstance(v, dict):
                continue
            for name in APPROACHES:
                for val, x in sorted(((v.get(name) or {}).get("by_open") or {}).items()):
                    rows.append([out["scored_at"], out["days"], out["from"],
                                 out["to"], sym, side, name, "open_vs_prior_range",
                                 val, x["n"], x["exp_plan"]])
    # Rule rows: fixed-point 1:1, split by gamma context.
    for sym, setups in sorted((out.get("rules") or {}).items()):
        for name in APPROACHES:
            for val, x in sorted((setups.get(name) or {}).items()):
                split = ("regime" if val in ("positive", "negative") else
                         "entry_vs_flip" if val in ("above", "below") else "all")
                rows.append([out["scored_at"], out["days"], out["from"],
                             out["to"], sym, "both", f"rule_{name}", split, val,
                             x["n"], x["exp"]])
    # ORB rows: side is "both", setup names its stop.
    for sym, stops in sorted((out.get("orb") or {}).items()):
        for stop_name, splits in stops.items():
            for split, values in splits.items():
                for val, x in sorted(values.items()):
                    rows.append([out["scored_at"], out["days"], out["from"],
                                 out["to"], sym, "both", f"orb_{stop_name}stop",
                                 split, val, x["n"], x["exp_plan"]])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SPLIT_HEADER)
        w.writerows(rows)


def append_log(path, out):
    """One row per symbol, side and setup, so verdict drift is on record.

    A second run on the same day REPLACES that day's rows rather than adding
    to them: a hand run and the scheduled one on one date put every row in
    twice, which reads as two measurements when it was one.
    """
    path = Path(path)
    rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh)][1:]
    rows = [r for r in rows if r and r[0] != out["scored_at"]]
    for sym, sides in sorted(out["symbols"].items()):
        for side, v in sorted(sides.items()):
            for name in APPROACHES:
                r = v.get(name)
                if r:
                    w = r.get("wall") or {}
                    rows.append([out["scored_at"], out["days"], out["from"],
                                 out["to"], sym, side, name, r["n"], r["rate"],
                                 r["stop_pct"], r["exp_plan"], r["win_plan"],
                                 w.get("n", ""), w.get("exp", ""),
                                 w.get("win", ""), w.get("avg_target_r", "")])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(LOG_HEADER)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--book", default="near", choices=("week", "near", "full"))
    ap.add_argument("--top", type=int, default=TOP_DEFAULT,
                    help="levels a side to score")
    ap.add_argument("--select", default="net", choices=("net", "nearest"),
                    help="pick levels by largest net gamma, or nearest to spot")
    ap.add_argument("--era", default="all", choices=("all", "single", "multi"),
                    help="score only levels built from one chain, or from two")
    ap.add_argument("--since", help="YYYY-MM-DD, skip sessions before this")
    ap.add_argument("--until", help="YYYY-MM-DD")
    ap.add_argument("--json", metavar="PATH",
                    help="write the calibration the dashboard reads")
    ap.add_argument("--log", metavar="PATH",
                    help="append this run's figures to a CSV history")
    ap.add_argument("--split-log", metavar="PATH",
                    help="write the open-location split to its own CSV history")
    ap.add_argument("--trades", metavar="PATH",
                    help="every rule trade with its path, merged into PATH; "
                         "the scorecard lab replays these")
    ap.add_argument("--verbose", action="store_true", help="print every trade")
    a = ap.parse_args()
    day = lambda v: datetime.strptime(v, "%Y-%m-%d").date() if v else None
    out = score(a.book, a.top, day(a.since), day(a.until), a.select, a.era,
                a.verbose, a.trades)
    trades = out.pop("_trades")
    if a.trades:
        Path(a.trades).write_text(json.dumps(
            {"scored_at": out["scored_at"], "points": RULE_POINTS,
             "reference": sorted(REFERENCE_ONLY),
             "multipliers": {i["future"]: i["multiplier"] for i in g.INSTRUMENTS},
             "evening": {"map_from": EVENING_MAP_FROM, "map_to": EVENING_MAP_TO,
                         "end": EVENING_END},
             "day": {"map_at": ENTRY_HHMM, "end": CLOSE_HHMM},
             "trades": trades}, separators=(",", ":")), encoding="utf-8")
        print(f"trades -> {a.trades} ({len(trades)})")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\ncalibration -> {a.json}")
    if a.log:
        append_log(a.log, out)
        print(f"history -> {a.log}")
    if a.split_log:
        append_split_log(a.split_log, out)
        print(f"splits -> {a.split_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
