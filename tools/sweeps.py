#!/usr/bin/env python3
"""Prior-session liquidity sweeps, gated by the 09:50 gamma map.

    python tools/sweeps.py                              # backtest, print report
    python tools/sweeps.py --json sweeps_report.json --trades sweeps_trades.json

The setup, in one line: price runs a prior session's high or low, fails to
hold beyond it, and closes back inside - fade it toward the other side.

Liquidity levels (built from 5-minute futures bars, ET, no look-ahead)
---------------------------------------------------------------------
  PDH / PDL   prior trading day's cash session high / low   09:30-16:00
  ONH / ONL   the whole Globex overnight before today        18:00-09:30
  AH  / AL    Asia                                           18:00-03:00
  LH  / LL    London                                         03:00-09:30

A level already taken out between 09:30 and the 09:50 map is spent - its
liquidity went before the map existed - and is skipped, as the scorecard skips
a wall price had already passed.

The rule (fixed points, nothing fitted)
---------------------------------------
  window   triggers 09:50-11:30 ET only, the trader's window
  sweep    a bar trades at least one tick beyond the level
  reclaim  that bar, or one of the next two, CLOSES back inside the level.
           No reclaim in three bars and the level is broken: dead for the day
  entry    at that reclaim close, short a swept high, long a swept low
  stop     one tick past the sweep's extreme, never tighter than MIN_STOP;
           a sweep whose stop would be wider than MAX_STOP is skipped
  target   TARGET points (10 ES / 40 NQ, the trader's 10-point win)
  limits   one trade per level, one position at a time, at most two a day

Gamma context, tagged on every trade so the report can say which gate pays:
  regime   the near book's net-gamma sign in the 09:50 map
  flip     the entry above or below that book's flip
  gex      a wall (either side) or the flip within GEX_TOL of the swept level

5-minute bars cannot order a stop and a target inside one bar; such a bar is
the STOP, as everywhere else in this repo. Each trade also stores its running
adverse/favourable path (score_levels.trade_path) so any stop, target or
time exit can be replayed without the bars. Not advice.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# ------------------------------------------------------------------ rules
TICK = 0.25
SYMBOLS = ("ES", "NQ")
# NQ is scaled 4x ES, the same ratio score_levels.RULE_POINTS uses (40 / 10).
TARGET = {"ES": 10.0, "NQ": 40.0}
MIN_STOP = {"ES": 2.0, "NQ": 8.0}
MAX_STOP = {"ES": 5.0, "NQ": 20.0}
GEX_TOL = {"ES": 5.0, "NQ": 20.0}
RECLAIM_BARS = 3          # the sweep bar plus two more
FIRST = "09:50"
LAST = "11:30"            # no new trigger on a bar starting at or after this
RTH_OPEN, RTH_CLOSE = "09:30", "16:00"
MAX_TRADES = 2
TIME_EXIT = "12:00"       # second exit reported: flat at noon if still open
LEVELS = (("PDH", "high"), ("PDL", "low"), ("ONH", "high"), ("ONL", "low"),
          ("AH", "high"), ("AL", "low"), ("LH", "high"), ("LL", "low"))


# ------------------------------------------------------------------ levels
def _hi(rows):
    return max(r[1] for r in rows) if rows else None


def _lo(rows):
    return min(r[2] for r in rows) if rows else None


def prior_day(by_day, day):
    """The latest date before `day` with cash-session bars."""
    for d in sorted((d for d in by_day if d < day), reverse=True):
        if any(RTH_OPEN <= r[0] < RTH_CLOSE for r in by_day[d]):
            return d
    return None


def liquidity_levels(by_day, day):
    """{name: price} for `day`, from bars strictly before its 09:30 open.

    by_day is {date: [(hhmm, high, low, close), ...]} in ET, the shape
    score_levels.bars() returns. Overnight runs from 18:00 on the prior
    trading day through any non-trading calendar days (a Sunday evening) to
    09:30 today.
    """
    pd_ = prior_day(by_day, day)
    if pd_ is None:
        return {}
    rth = [r for r in by_day[pd_] if RTH_OPEN <= r[0] < RTH_CLOSE]
    on, asia, london = [], [], []
    d = pd_
    while d <= day:
        for r in by_day.get(d) or []:
            t = r[0]
            if d == pd_ and t < "18:00":
                continue
            if d == day and t >= RTH_OPEN:
                continue
            on.append(r)
            if d == day and t >= "03:00":
                london.append(r)
            elif t >= "18:00" or (t < "03:00"):
                asia.append(r)
        d += timedelta(days=1)
    out = {"PDH": _hi(rth), "PDL": _lo(rth), "ONH": _hi(on), "ONL": _lo(on),
           "AH": _hi(asia), "AL": _lo(asia), "LH": _hi(london), "LL": _lo(london)}
    return {k: round(v, 2) for k, v in out.items() if v is not None}


# ------------------------------------------------------------------ gamma
def gex_context(level, gmap, sym):
    """(tag, nearest gamma level, distance) for a swept level.

    gmap is {"call": [...], "put": [...], "flip": x|None, "regime": ...}."""
    cands = [("call wall", k) for k in gmap.get("call") or []]
    cands += [("put wall", k) for k in gmap.get("put") or []]
    if gmap.get("flip"):
        cands.append(("flip", float(gmap["flip"])))
    if not cands:
        return None, None, None
    name, k = min(cands, key=lambda c: abs(c[1] - level))
    dist = abs(k - level)
    return (name if dist <= GEX_TOL[sym] else None), round(k, 2), round(dist, 2)


# ------------------------------------------------------------------ signal
def find_sweeps(rows, levels, sym):
    """Every sweep-and-reclaim in today's cash bars, in time order, before any
    daily limit. rows are today's bars from 09:30. Returns dicts with index
    of the entry bar (`i`), entry, stop, direction, level name and price."""
    first = next((k for k, r in enumerate(rows) if r[0] >= FIRST), None)
    if first is None:
        return []
    pre = rows[:first]
    out = []
    for name, kind in LEVELS:
        lv = levels.get(name)
        if lv is None:
            continue
        up = kind == "high"
        # Spent before the map: traded through between 09:30 and 09:50.
        if pre and ((_hi(pre) > lv) if up else (_lo(pre) < lv)):
            continue
        k = first
        while k < len(rows) and rows[k][0] < LAST:
            t, hi, lo, cl = rows[k]
            swept = (hi >= lv + TICK) if up else (lo <= lv - TICK)
            if not swept:
                k += 1
                continue
            got = None
            for j in range(k, min(k + RECLAIM_BARS, len(rows))):
                if (rows[j][3] < lv) if up else (rows[j][3] > lv):
                    got = j
                    break
            if got is None:
                break                       # broken, not swept: dead today
            if got + 1 >= len(rows):
                break                       # nothing left to trade
            seg = rows[k:got + 1]
            entry = rows[got][3]
            d = -1 if up else +1
            ext = _hi(seg) if up else _lo(seg)
            risk = (ext + TICK - entry) if up else (entry - (ext - TICK))
            risk = max(risk, MIN_STOP[sym])
            if risk > MAX_STOP[sym]:
                break                       # too wide to take: skip the level
            out.append({"level_name": name, "level": lv, "kind": kind,
                        "sweep_time": t, "time": rows[got][0], "i": got + 1,
                        "entry": round(entry, 2), "direction": d,
                        "risk": round(risk, 2),
                        "stop": round(entry - d * risk, 2),
                        "target": round(entry + d * TARGET[sym], 2),
                        "extreme": round(ext, 2)})
            break                           # one trade per level
    out.sort(key=lambda s: (s["i"], s["level_name"]))
    # Two levels swept by the same bar are one trade: keep the wider-known
    # level (prior day first, then overnight, then sessions) - LEVELS order.
    seen, uniq = set(), []
    rank = {n: r for r, (n, _) in enumerate(LEVELS)}
    for s in sorted(out, key=lambda s: (s["i"], s["direction"], rank[s["level_name"]])):
        key = (s["i"], s["direction"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    uniq.sort(key=lambda s: s["i"])
    return uniq


def resolve(rows, s, end="16:00"):
    """Walk one trade from its start bar: (status, r, exit time, noon r)."""
    d, entry, risk = s["direction"], s["entry"], s["risk"]
    tgt = abs(s["target"] - entry)
    noon = None                 # r at the 12:00 mark, once it is passed
    mark = 0.0                  # r at the latest bar close before 12:00
    last = entry
    for t, hi, lo, cl in rows[s["i"]:]:
        if t >= end:
            break
        if noon is None and t >= TIME_EXIT:
            noon = mark
        a = (entry - lo) if d > 0 else (hi - entry)
        f = (hi - entry) if d > 0 else (entry - lo)
        if a >= risk:
            return "lost", -1.0, t, -1.0 if noon is None else noon
        if f >= tgt:
            r = tgt / risk
            return "won", r, t, r if noon is None else noon
        mark = (cl - entry) * d / risk
        last = cl
    r = (last - entry) * d / risk
    return "closed", r, end, r if noon is None else noon


def day_trades(rows, levels, gmap, sym):
    """Today's trades after the limits: one at a time, at most MAX_TRADES."""
    out, free_from = [], None
    for s in find_sweeps(rows, levels, sym):
        if len(out) >= MAX_TRADES:
            break
        if free_from is not None and s["time"] < free_from:
            continue                        # still in the previous trade
        status, r, at, noon_r = resolve(rows, s)
        tag, near, dist = gex_context(s["level"], gmap, sym)
        flip = gmap.get("flip")
        s.update({"status": status, "r": round(r, 2), "noon_r": round(noon_r, 2),
                  "resolved_at": at, "regime": gmap.get("regime"),
                  "flip_side": (("above" if s["entry"] > flip else "below")
                                if flip else None),
                  "gex": tag, "gex_level": near, "gex_dist": dist})
        out.append(s)
        free_from = at
    return out


# ---------------------------------------------------------------- backtest
def backtest(since=None, until=None):
    import gex_terminal as g          # noqa: E402
    import score_levels as sl         # noqa: E402
    now = g.now_et().replace(tzinfo=None)
    trades, sessions_seen = [], 0
    for s in sl.sessions("near", 2, since, until, "nearest"):
        sym = s["symbol"]
        if sym not in SYMBOLS or not s["contract"]:
            continue
        if datetime.combine(s["date"], datetime.strptime("16:00", "%H:%M").time()) > now:
            continue
        try:
            by_day = sl.bars(s["contract"])
        except Exception:
            continue
        rows = [r for r in by_day.get(s["date"]) or [] if RTH_OPEN <= r[0] < RTH_CLOSE]
        levels = liquidity_levels(by_day, s["date"])
        if len(rows) < 30 or not levels:
            continue
        sessions_seen += 1
        gmap = dict(s["levels"], regime=s["regime"])
        for t in day_trades(rows, levels, gmap, sym):
            t.update({"date": s["date"].isoformat(), "symbol": sym,
                      "era": s["era"], "map_at": s["at"].strftime("%H:%M")})
            adv, fav, marks = sl.trade_path(rows, t["i"], t["entry"],
                                            t["direction"], False)
            t.update({"adv": adv, "fav": fav, "marks": marks})
            trades.append(t)
    return trades, sessions_seen


def stats(ts, key="r", mult=None):
    if not ts:
        return {"n": 0}
    rs = [t[key] for t in ts]
    won = sum(1 for t in ts if t["status"] == "won")
    out = {"n": len(ts), "won": won, "lost": sum(1 for t in ts if t["status"] == "lost"),
           "win_rate": round(won / len(ts), 2), "exp_r": round(sum(rs) / len(rs), 2),
           "sum_r": round(sum(rs), 2)}
    if mult:
        usd = [t[key] * t["risk"] * mult[t["symbol"]] for t in ts]
        out["sum_usd"] = round(sum(usd))
        eq, peak, dd = 0.0, 0.0, 0.0
        for u in usd:
            eq += u
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        out["max_dd_usd"] = round(dd)
    return out


GATES = {
    "all": lambda t: True,
    "negative gamma": lambda t: t["regime"] == "negative",
    "positive gamma": lambda t: t["regime"] == "positive",
    "at a gamma level": lambda t: t["gex"] is not None,
    "not at a gamma level": lambda t: t["gex"] is None,
    "gamma level + negative": lambda t: t["gex"] is not None and t["regime"] == "negative",
    "gamma level + positive": lambda t: t["gex"] is not None and t["regime"] == "positive",
    "prior day (PDH/PDL)": lambda t: t["level_name"] in ("PDH", "PDL"),
    "overnight (ON/A/L)": lambda t: t["level_name"] not in ("PDH", "PDL"),
}


def report(trades, sessions_seen):
    mult = {"ES": 50, "NQ": 20}
    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "rules": {"target": TARGET, "min_stop": MIN_STOP, "max_stop": MAX_STOP,
                     "gex_tol": GEX_TOL, "window": [FIRST, LAST],
                     "max_trades": MAX_TRADES, "reclaim_bars": RECLAIM_BARS},
           "sessions": sessions_seen, "symbols": {}}
    for sym in SYMBOLS:
        ts = [t for t in trades if t["symbol"] == sym]
        days = sorted({t["date"] for t in ts})
        out["symbols"][sym] = {
            "trades": len(ts), "days_with_trade": len(days),
            "gates": {name: {"held": stats([t for t in ts if f(t)], "r", mult),
                             "noon": stats([t for t in ts if f(t)], "noon_r", mult)}
                      for name, f in GATES.items()}}
    return out


def print_report(rep):
    print(f"sweeps: {rep['sessions']} symbol-sessions scored")
    for sym, s in rep["symbols"].items():
        print(f"\n{sym}  {s['trades']} trades on {s['days_with_trade']} days"
              f"  (target {TARGET[sym]:g}, stop {MIN_STOP[sym]:g}-{MAX_STOP[sym]:g})")
        print(f"  {'gate':26}{'n':>4}{'won':>5}{'win%':>6}{'exp R':>7}{'$ total':>9}"
              f"{'max DD':>8}  | noon exit: exp R, $")
        for name, v in s["gates"].items():
            h, nn = v["held"], v["noon"]
            if not h["n"]:
                print(f"  {name:26}{0:>4}")
                continue
            print(f"  {name:26}{h['n']:>4}{h['won']:>5}{h['win_rate']*100:>5.0f}%"
                  f"{h['exp_r']:>7.2f}{h['sum_usd']:>9,}{h['max_dd_usd']:>8,}"
                  f"  | {nn['exp_r']:>5.2f} {nn['sum_usd']:>8,}")


# -------------------------------------------------------------------- live
def live(contract, sym, gmap, now, by_day=None):
    """Today's sweep board for the setups page: levels, their state, and any
    trades so far. Uses only completed bars."""
    import gex_terminal as g          # noqa: E402
    import score_levels as sl         # noqa: E402
    if sym not in SYMBOLS:
        return None
    if by_day is None:
        by_day = sl.bars(contract)
    day = now.date()
    rows = [r for r in by_day.get(day) or [] if RTH_OPEN <= r[0] < RTH_CLOSE
            and datetime.combine(day, datetime.strptime(r[0], "%H:%M").time())
            + timedelta(minutes=5) <= now]
    levels = liquidity_levels(by_day, day)
    last = rows[-1][3] if rows else None
    board = []
    for name, kind in LEVELS:
        lv = levels.get(name)
        if lv is None:
            continue
        tag, near, dist = gex_context(lv, gmap, sym)
        board.append({"name": name, "kind": kind, "level": lv, "gex": tag,
                      "gex_level": near, "gex_dist": dist,
                      "dist_pts": round(lv - last, 2) if last is not None else None})
    trades = day_trades(rows, levels, gmap, sym) if rows else []
    for t in trades:
        if t["status"] == "closed" and rows and rows[-1][0] < "15:55":
            t["status"] = "open"
        t.pop("i", None)
    return {"levels": board, "trades": trades, "last": last,
            "bars_to": rows[-1][0] if rows else None,
            "window": [FIRST, LAST], "target": TARGET[sym],
            "stop_range": [MIN_STOP[sym], MAX_STOP[sym]]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--json", metavar="PATH", help="write the report")
    ap.add_argument("--trades", metavar="PATH", help="write every trade with its path")
    a = ap.parse_args()
    day = lambda v: datetime.strptime(v, "%Y-%m-%d").date() if v else None
    trades, n = backtest(day(a.since), day(a.until))
    rep = report(trades, n)
    print_report(rep)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    if a.trades:
        for t in trades:
            t.pop("i", None)
        Path(a.trades).write_text(json.dumps(trades, separators=(",", ":")),
                                  encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
