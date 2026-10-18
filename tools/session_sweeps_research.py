#!/usr/bin/env python3
"""Sweeps in the FIRST HOUR of each session, of the prior session's range.

    python tools/session_sweeps_research.py --out session_sweeps.md

  session   first hour (sweep must start)   levels swept                 flat by
  Asia      18:00-19:00                     prior NY cash session H/L    03:00
  London    03:00-04:00                     Asia H/L (18:00-03:00)       09:30
  New York  09:30-10:30                     London H/L (03:00-09:30)     16:00

Same rule as the live London sweep (tools/sweeps.py): sweep by a tick, close
back inside within three bars, enter on that close, stop a tick past the wick
(2-5 ES / 8-30 NQ, wider skipped), breakeven at +1R, target 10 ES / 40 NQ,
each level once, one trade at a time, two a session. A bar spanning stop and
target is the stop. Also reported: the same with every earlier level in play
(prior day, Asia, London), and the first TWO hours, for comparison.
Continuous front month, ~60 days of 5-minute bars. Halves are by date.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import gex_terminal as g          # noqa: E402
import sweeps as sw               # noqa: E402

URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
       "{sym}?range=60d&interval=5m&includePrePost=true")
SERIES = {"ES": "ES=F", "NQ": "NQ=F"}
MULT = {"ES": 50, "NQ": 20}
TICK = 0.25


def load(series):
    res = json.loads(g.http_get(URL.format(sym=series)))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    off = (res.get("meta") or {}).get("gmtoffset") or 0
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        hi, lo, cl = q["high"][i], q["low"][i], q["close"][i]
        if None in (hi, lo, cl):
            continue
        t = datetime.fromtimestamp(ts + off, timezone.utc).replace(tzinfo=None)
        out.append((t, float(hi), float(lo), float(cl)))
    return out


def at(d, hhmm):
    return datetime.combine(d, datetime.strptime(hhmm, "%H:%M").time())


def hl(bars, a, b):
    xs = [r for r in bars if a <= r[0] < b]
    return (max(r[1] for r in xs), min(r[2] for r in xs)) if xs else (None, None)


def last_trading_day(d):
    while not g.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def instances(bars, hours=1, all_levels=False):
    """(session, start datetime, levels {name: (price, is_high)}, trigger end, flat-by)."""
    days = sorted({r[0].date() for r in bars})
    out = []
    for d in days:
        # Asia: the evening of d (Sunday-Thursday), levels from the last cash session
        if d.weekday() in (6, 0, 1, 2, 3):
            pd_ = last_trading_day(d)
            h, l = hl(bars, at(pd_, "09:30"), at(pd_, "16:00"))
            if h is not None:
                lv = {"PDH": (h, True), "PDL": (l, False)}
                out.append(("Asia", at(d, "18:00"), lv,
                            at(d, "18:00") + timedelta(hours=hours),
                            at(d + timedelta(days=1), "03:00")))
        if not g.is_trading_day(d):
            continue
        prev = last_trading_day(d - timedelta(days=1))
        # Asia window: from the prior evening's 18:00 (Sunday's for a Monday)
        ah, al = hl(bars, at(d - timedelta(days=1), "18:00"), at(d, "03:00"))
        pdh, pdl = hl(bars, at(prev, "09:30"), at(prev, "16:00"))
        lh, ll = hl(bars, at(d, "03:00"), at(d, "09:30"))
        if ah is not None:
            lv = {"AH": (ah, True), "AL": (al, False)}
            if all_levels and pdh is not None:
                lv.update({"PDH": (pdh, True), "PDL": (pdl, False)})
            out.append(("London", at(d, "03:00"), lv,
                        at(d, "03:00") + timedelta(hours=hours), at(d, "09:30")))
        if lh is not None:
            lv = {"LH": (lh, True), "LL": (ll, False)}
            if all_levels:
                if ah is not None:
                    lv.update({"AH": (ah, True), "AL": (al, False)})
                if pdh is not None:
                    lv.update({"PDH": (pdh, True), "PDL": (pdl, False)})
            out.append(("New York", at(d, "09:30"), lv,
                        at(d, "09:30") + timedelta(hours=hours), at(d, "16:00")))
    return out


def run_session(bars, start, levels, trig_end, flat, sym):
    rows = [r for r in bars if start <= r[0] < flat]
    if len(rows) < 6:
        return []
    out, busy_until, done = [], -1, set()
    tgt, mn, mx = sw.TARGET[sym], sw.MIN_STOP[sym], sw.MAX_STOP[sym]
    order = sorted(levels.items(), key=lambda kv: list(levels).index(kv[0]))
    for k, (t, hi, lo, cl) in enumerate(rows):
        if t >= trig_end or len(out) >= sw.MAX_TRADES:
            break
        for name, (lv, up) in order:
            if name in done:
                continue
            if not ((hi >= lv + TICK) if up else (lo <= lv - TICK)):
                continue
            done.add(name)
            got = next((j for j in range(k, min(k + sw.RECLAIM_BARS, len(rows)))
                        if ((rows[j][3] < lv) if up else (rows[j][3] > lv))), None)
            if got is None or got + 1 >= len(rows) or got < busy_until:
                continue
            if len(out) >= sw.MAX_TRADES:
                continue
            d = -1 if up else 1
            seg = rows[k:got + 1]
            ext = max(r[1] for r in seg) if up else min(r[2] for r in seg)
            entry = rows[got][3]
            risk = max((ext + TICK - entry) if up else (entry - (ext - TICK)), mn)
            if risk > mx:
                continue
            pts, end, how = walk(rows, got + 1, entry, d, risk, tgt)
            out.append({"time": rows[got][0].isoformat(), "level": name, "pts": pts,
                        "usd": pts * MULT[sym], "how": how, "risk": risk})
            busy_until = end
    return out


def walk(rows, start, entry, d, risk, tgt):
    armed = False
    for i in range(start, len(rows)):
        _, hi, lo, cl = rows[i]
        a = (entry - lo) if d > 0 else (hi - entry)
        f = (hi - entry) if d > 0 else (entry - lo)
        if a >= (0.0 if armed else risk):
            return (0.0 if armed else -risk), i, ("breakeven" if armed else "lost")
        if f >= tgt:
            return tgt, i, "won"
        if f >= sw.BREAKEVEN_R * risk:
            armed = True
    return (rows[-1][3] - entry) * d, len(rows) - 1, "time"


def stats(ts):
    if not ts:
        return {"n": 0}
    eq = pk = dd = 0.0
    for t in ts:
        eq += t["usd"]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return {"n": len(ts), "won": sum(t["how"] == "won" for t in ts),
            "be": sum(t["how"] == "breakeven" for t in ts),
            "usd": eq, "per": eq / len(ts), "dd": dd}


def fmt(s):
    if not s["n"]:
        return "0 | — | — | — | —"
    return (f"{s['n']} | {s['won']}/{s['be']} | {s['usd']:+,.0f} | {s['per']:+,.0f} | {s['dd']:,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="session_sweeps.md")
    a = ap.parse_args()
    L = ["# First-hour sweeps of the prior session, every session", "",
         f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. Columns: trades | won/breakeven | "
         "total $ | per trade $ | max drawdown $, one lot, current rule (breakeven +1R).", ""]
    dump = {}
    for sym, series in SERIES.items():
        bars = load(series)
        days = sorted({r[0].date() for r in bars})
        cut = days[len(days) // 2]
        L += [f"## {sym}  ({days[0]} to {days[-1]}, halves split at {cut})", "",
              "| session | version | all | first half | second half |", "|---|---|---|---|---|"]
        dump[sym] = {}
        for label, hours, all_lv in (("prior session, first hour", 1, False),
                                     ("all earlier levels, first hour", 1, True),
                                     ("prior session, first 2 hours", 2, False)):
            per = {}
            for sess, start, lv, te, fl in instances(bars, hours, all_lv):
                for t in run_session(bars, start, lv, te, fl, sym):
                    t["session"] = sess
                    t["date"] = start.date().isoformat()
                    per.setdefault(sess, []).append(t)
            for sess in ("Asia", "London", "New York", "ALL"):
                ts = [t for v in per.values() for t in v] if sess == "ALL" else per.get(sess, [])
                ts = sorted(ts, key=lambda t: t["time"])
                h1 = [t for t in ts if t["date"] < cut.isoformat()]
                h2 = [t for t in ts if t["date"] >= cut.isoformat()]
                L.append(f"| {'**ALL**' if sess == 'ALL' else sess} | {label} | {fmt(stats(ts))} | "
                         f"{fmt(stats(h1))} | {fmt(stats(h2))} |")
                dump[sym][f"{sess}|{label}"] = ts
        L.append("")
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    Path(a.out).with_suffix(".json").write_text(json.dumps(dump), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    raise SystemExit(main())
