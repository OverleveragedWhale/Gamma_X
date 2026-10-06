#!/usr/bin/env python3
"""Score the published gamma levels against what price actually did.

    python tools/score_levels.py                       # every symbol, near book
    python tools/score_levels.py --book week --top 1
    python tools/score_levels.py --json calibration.json

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

What it measures, per level
---------------------------
  touch rate    share of sessions where price reached the level at all. A
                level price never visits is neither right nor wrong, and
                averaging it in flatters everything else.
  beyond        how far past the level price pushed after first touching it,
                in points and in percent. This is the stop-distance question:
                the p75 is the stop that survived three touches in four.
  back          how far price retraced from its extreme back toward the
                session's starting spot. This is the target question.
  held          share of touches that never went more than --stop-pct past
                the level. The headline number, and the one most sensitive to
                that threshold, which is why the distribution is printed too.

It also splits session range by the regime the dashboard reported, which is
the one claim that needs no levels at all: positive gamma is supposed to mean
a tighter range.

Caveats this cannot fix
-----------------------
  - R:R flatters the trade. 'back' is the furthest price came off its extreme,
    which only a perfect exit captures, while the stop is sized off the p75 of
    'beyond'. Read a figure under about 1.2 as "no edge here" rather than as
    a measurement of what a real fade would have returned.
  - The figures are unstable at this length. Refreshing on 2026-10-06 rolled
    the window by one session and moved ES calls from 0.77 R:R to 1.39 and NQ
    calls from 0.85 to 1.53 - both across the 1.2 the dashboard treats as the
    line between no edge and a setup. Re-run this as history accumulates and
    expect the verdicts to move until n per side is in the dozens.
  - The history is short. Thirteen sessions is a baseline to extend, not a
    verdict; n is printed beside every figure for that reason.
  - The payload changed shape on 2026-09-29 (per-book wall lists became one
    ladder) and the ranking changed from gross side gamma to net on 10-01.
    Both shapes are read, but a level ranked first under one rule is not
    always first under the other. --since skips the older period.
  - A session whose panel was held from an earlier run (feed gap) is skipped:
    those levels were not computed from that session's book.

--json writes the stop and target distances the terminal reads back for its
trade plans, so the numbers on the page come from measured behaviour rather
than a round number someone liked.
"""
import argparse
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
DEFAULT_STOP_PCT = 0.35   # percent of spot, the stop the held rate assumes.
                          # Measured 2026-09/10, price routinely ran 0.3-0.5%
                          # past a touched wall, so a tighter figure reported
                          # a near-zero held rate for every symbol and said
                          # more about the threshold than about the levels.
TOP_DEFAULT = 3


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
            if not sym.get("ok") or sym.get("regimes_from"):
                continue          # failed, or a panel held from another session
            lv = levels_of(sym, book, top, select)
            if not lv["call"] and not lv["put"]:
                continue
            regime = ((sym.get("regimes") or {}).get(book) or {}).get("regime")
            chosen[(day, sym["symbol"])] = {
                "date": day, "symbol": sym["symbol"], "at": stamp,
                "spot": float(sym["spot"]), "regime": regime,
                "contract": (sym.get("contract") or {}).get("symbol"),
                "levels": lv,
            }
    return [chosen[k] for k in sorted(chosen)]


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
    return [b for b in rows if start_hhmm <= b[0] <= CLOSE_HHMM]


# ------------------------------------------------------------------ scoring
def score_level(rows, level, side, spot):
    """One level against one session's bars.

    side 'call' is a level above spot that is supposed to cap, 'put' one below
    that is supposed to support, so 'beyond' and 'back' mirror.

    beyond: furthest past the level after first touching it - the stop
            question. back: how far price came off that extreme afterwards,
            which is the most a fade could have made - the target question.
            Measured from the extreme rather than from the level so a stop
            that survives and a trade that pays are scored separately.

    Returns None when price never reached it.
    """
    up = side == "call"
    first = None
    for i, (_, hi, lo, _c) in enumerate(rows):
        if (hi >= level) if up else (lo <= level):
            first = i
            break
    if first is None:
        return None

    after = rows[first:]
    if up:
        peak_i = max(range(len(after)), key=lambda i: after[i][1])
        extreme = after[peak_i][1]
        beyond = extreme - level
        back = extreme - min(b[2] for b in after[peak_i:])
    else:
        peak_i = min(range(len(after)), key=lambda i: after[i][2])
        extreme = after[peak_i][2]
        beyond = level - extreme
        back = max(b[1] for b in after[peak_i:]) - extreme
    return {"beyond": max(0.0, beyond), "back": max(0.0, back),
            "touch_hhmm": rows[first][0], "spot": spot}


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


def score(book, top, stop_pct, since=None, until=None, verbose=False,
          select="net"):
    rows = sessions(book, top, since, until, select)
    if not rows:
        raise SystemExit("no sessions matched - try --since, or fetch the "
                         "snapshot branch")

    per_symbol = defaultdict(lambda: defaultdict(list))   # sym -> side -> events
    ranges = defaultdict(lambda: defaultdict(list))       # sym -> regime -> range%
    counted = defaultdict(int)
    missing_bars = defaultdict(int)
    wrong_side = defaultdict(int)

    for s in rows:
        sym, day = s["symbol"], s["date"]
        series = s["contract"] or None
        if not series:
            missing_bars[sym] += 1
            continue
        try:
            rb = session_bars(series, day, s["at"].strftime("%H:%M"))
        except Exception as exc:
            print(f"  bars unavailable for {series}: {type(exc).__name__}",
                  file=sys.stderr)
            rb = []
        if len(rb) < 6:
            missing_bars[sym] += 1
            continue
        counted[sym] += 1

        hi = max(b[1] for b in rb)
        lo = min(b[2] for b in rb)
        if s["regime"]:
            ranges[sym][s["regime"]].append(100.0 * (hi - lo) / s["spot"])

        open_px = rb[0][3]
        for side in ("call", "put"):
            for rank, level in enumerate(s["levels"][side]):
                # A call level that is already below price at entry is not a
                # cap being tested, and price is "past" it before the session
                # starts. Ranking splits on the snapshot's spot, which has
                # moved by the time the bars begin, so a few land this way.
                if (level <= open_px) if side == "call" else (level >= open_px):
                    wrong_side[sym] += 1
                    continue
                ev = score_level(rb, level, side, s["spot"])
                per_symbol[sym][side].append({
                    "rank": rank, "level": level, "touched": ev is not None,
                    "dist_pct": 100.0 * abs(level - open_px) / open_px,
                    **(ev or {}), "spot": s["spot"], "date": day,
                })
                if verbose and ev:
                    print(f"  {day} {sym} {side}#{rank+1} {level:,.2f} "
                          f"touched {ev['touch_hhmm']} beyond {ev['beyond']:.2f} "
                          f"back {ev['back']:.2f}")

    picked = ("largest net gamma" if select == "net" else "nearest to spot")
    print(f"Levels scored: book '{book}', top {top} a side by {picked}, "
          f"{len(rows)} session-symbols, entry {ENTRY_HHMM} -> {CLOSE_HHMM} ET")
    print(f"'held' assumes a stop {stop_pct}% of spot beyond the level.")
    print()
    # stop = beyond p75, the stop that survived three touches in four.
    # target = back p50, the median retrace off the extreme. R:R is their
    # ratio, and it is the number that decides whether a level is worth
    # trading at all - a 90% touch rate pays nothing at 0.5 R:R.
    hdr = (f"{'sym':<4} {'side':<5} {'n':>3} {'away':>6} {'touched':>8} "
           f"{'beyond p50':>11} {'p75=stop':>9} {'p90':>7} "
           f"{'back p50':>9} {'R:R':>5} {'held':>6}")
    print(hdr)
    print("-" * len(hdr))

    calib = {}
    for sym in sorted(per_symbol):
        for side in ("call", "put"):
            evs = per_symbol[sym][side]
            if not evs:
                continue
            touched = [e for e in evs if e["touched"]]
            spot = evs[0]["spot"]
            stop_pts = spot * stop_pct / 100.0
            beyond = [e["beyond"] for e in touched]
            back = [e["back"] for e in touched]
            held = (sum(1 for b in beyond if b <= stop_pts) / len(beyond)
                    if beyond else None)
            away = sum(e["dist_pct"] for e in evs) / len(evs)
            stop = pct(beyond, 75)
            target = pct(back, 50)
            rr = (target / stop) if (stop and target) else None
            print(f"{sym:<4} {side:<5} {len(evs):>3} {away:>5.2f}% "
                  f"{len(touched)/len(evs):>7.0%} "
                  f"{fmt(pct(beyond, 50)):>11} {fmt(stop):>9} "
                  f"{fmt(pct(beyond, 90)):>7} {fmt(target):>9} "
                  f"{(f'{rr:.2f}' if rr else '-'):>5} "
                  f"{(f'{held:.0%}' if held is not None else '-'):>6}")
            if touched:
                calib.setdefault(sym, {})[side] = {
                    "n_levels": len(evs), "n_touched": len(touched),
                    "avg_dist_pct": round(away, 3),
                    "touch_rate": round(len(touched) / len(evs), 3),
                    "beyond_p50": round(pct(beyond, 50), 2),
                    "beyond_p75": round(pct(beyond, 75), 2),
                    "beyond_p90": round(pct(beyond, 90), 2),
                    "beyond_p75_pct": round(100.0 * pct(beyond, 75) / spot, 3),
                    "back_p50": round(pct(back, 50), 2),
                    "back_p50_pct": round(100.0 * pct(back, 50) / spot, 3),
                    "held_rate_at_stop": round(held, 3),
                    # Carried per side so the dashboard's caveat can state the
                    # sample instead of asserting it is small.
                    "sessions": counted[sym],
                    # What the dashboard's trade plans read: a stop that held
                    # 3 touches in 4, the median retrace as the target, and
                    # their ratio. Points are per-session absolutes, so the
                    # percentages are what travel to a different price level.
                    "stop_pts": round(stop, 2),
                    "stop_pct": round(100.0 * stop / spot, 3),
                    "target_pts": round(target, 2) if target else None,
                    "target_pct": (round(100.0 * target / spot, 3)
                                   if target else None),
                    "rr": round(rr, 2) if rr else None,
                }

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
        print()
        print("Levels skipped as already passed at entry: "
              + ", ".join(f"{k} {v}" for k, v in sorted(off.items())))

    skipped = {k: v for k, v in missing_bars.items() if v}
    if skipped:
        print()
        print("Sessions skipped for want of bars: "
              + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
    print()
    print("Sessions with bars: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counted.items())))
    days = sorted({r["date"] for r in rows})
    return {"book": book, "top": top, "stop_pct": stop_pct, "select": select,
            "entry": ENTRY_HHMM, "close": CLOSE_HHMM,
            "sessions": len(rows), "days": len(days),
            "from": days[0].isoformat() if days else None,
            "to": days[-1].isoformat() if days else None,
            "scored_at": g.now_et().strftime("%Y-%m-%d"),
            "symbols": calib}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--book", default="near", choices=("week", "near", "full"))
    ap.add_argument("--top", type=int, default=TOP_DEFAULT,
                    help="levels a side to score, strongest first")
    ap.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT,
                    help="stop distance beyond the level, percent of spot")
    ap.add_argument("--since", help="YYYY-MM-DD, skip sessions before this")
    ap.add_argument("--until", help="YYYY-MM-DD")
    ap.add_argument("--json", metavar="PATH",
                    help="write the calibration the dashboard reads")
    ap.add_argument("--select", default="net", choices=("net", "nearest"),
                    help="pick levels by largest net gamma, or nearest to spot")
    ap.add_argument("--verbose", action="store_true", help="print every touch")
    a = ap.parse_args()
    day = lambda v: datetime.strptime(v, "%Y-%m-%d").date() if v else None
    out = score(a.book, a.top, a.stop_pct, day(a.since), day(a.until),
                a.verbose, a.select)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\ncalibration -> {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
