#!/usr/bin/env python3
"""ICT killzone pivots swept and reclaimed, with gamma confluence.

    python tools/killzone_research.py --out killzones.md

Killzones (ET, TFO "ICT Killzones & Pivots" defaults):
  ASIA 20:00-00:00   LNDN 02:00-05:00   NYAM 09:30-11:00   NYPM 13:30-16:00
Each killzone trades a sweep of the PREVIOUS killzone's high or low inside its
own window, flat by the next killzone's start (NYPM by 16:00):
  ASIA  <- prior NYPM H/L      flat 02:00
  LNDN  <- ASIA H/L            flat 09:30
  NYAM  <- LNDN H/L            flat 13:30
  NYPM  <- NYAM H/L            flat 16:00
"all earlier" also lets each killzone trade every killzone pivot since the
prior NYPM. Rule otherwise as the live sweep: tick beyond, reclaim close in
three bars, stop a tick past the wick (2-5 ES / 8-30 NQ), breakeven +1R,
target 10 ES / 40 NQ, two a killzone, one open at a time.

Gamma: for killzones with a map published at or before their start, the trade
is tagged "at gamma" when a call wall, put wall or the flip sits within 5 ES /
20 NQ of the swept pivot.
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import gex_terminal as g                  # noqa: E402
import sweeps as sw                       # noqa: E402
import session_sweeps_research as ss      # noqa: E402

TICK = 0.25
TOL = {"ES": 5.0, "NQ": 20.0}
# name, start, end, flat-by, days offset of flat (0 same day, 1 next)
KZ = [("ASIA", "20:00", "00:00", "02:00"), ("LNDN", "02:00", "05:00", "09:30"),
      ("NYAM", "09:30", "11:00", "13:30"), ("NYPM", "13:30", "16:00", "16:00")]
CUT = {"ASIA": "20:00", "LNDN": "02:00", "NYAM": "09:30", "NYPM": "13:30"}


def at(d, hhmm):
    return datetime.combine(d, datetime.strptime(hhmm, "%H:%M").time())


def kz_windows(day):
    """[(name, start, end, flat)] for trading day `day`; its ASIA killzone is
    the evening before (Sunday's for a Monday)."""
    ev = day - timedelta(days=1)
    return [("ASIA", at(ev, "20:00"), at(day, "00:00"), at(day, "02:00")),
            ("LNDN", at(day, "02:00"), at(day, "05:00"), at(day, "09:30")),
            ("NYAM", at(day, "09:30"), at(day, "11:00"), at(day, "13:30")),
            ("NYPM", at(day, "13:30"), at(day, "16:00"), at(day, "16:00"))]


PREV = {"ASIA": ["pNYPM"], "LNDN": ["ASIA"], "NYAM": ["LNDN"], "NYPM": ["NYAM"]}
EARLIER = {"ASIA": ["pNYPM"], "LNDN": ["pNYPM", "ASIA"], "NYAM": ["pNYPM", "ASIA", "LNDN"],
           "NYPM": ["pNYPM", "ASIA", "LNDN", "NYAM"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="killzones.md")
    a = ap.parse_args()
    try:
        maps = sw.map_sessions(sorted(set(CUT.values())))
    except Exception:
        maps = {c: {} for c in CUT.values()}
    L = ["# Killzone pivot sweeps", "",
         "Columns: trades | won/breakeven | total $ | per trade $ | max DD $, one lot, breakeven +1R.", ""]
    for sym, series in ss.SERIES.items():
        bars = ss.load(series)
        days = sorted(d for d in {r[0].date() for r in bars} if g.is_trading_day(d))
        cut = days[len(days) // 2].isoformat() if days else ""
        L += [f"## {sym}  ({days[0]} to {days[-1]}; halves at {cut})", ""]
        for label, all_lv in (("previous killzone", False), ("all earlier killzones", True)):
            per, prev_pm = {}, None
            for d in days:
                wins = kz_windows(d)
                piv = {}
                if prev_pm:
                    piv["pNYPM"] = prev_pm
                for name, st, en, fl in wins:
                    lv = {}
                    for p in (EARLIER if all_lv else PREV)[name]:
                        if p in piv:
                            h, l = piv[p]
                            lv[p + "H"] = (h, True)
                            lv[p + "L"] = (l, False)
                    if lv:
                        mk = d - timedelta(days=1) if name == "ASIA" else d
                        m = maps[CUT[name]].get((mk, sym))
                        gl = []
                        if m:
                            gl = list(m["levels"]["call"]) + list(m["levels"]["put"])
                            if m["levels"].get("flip"):
                                gl.append(float(m["levels"]["flip"]))
                        for t in ss.run_session(bars, st, lv, en, fl, sym):
                            lvl = lv[t["level"]][0]
                            t["kz"], t["date"] = name, d.isoformat()
                            t["map"] = bool(m)
                            t["gamma"] = bool(gl) and min(abs(x - lvl) for x in gl) <= TOL[sym]
                            per.setdefault(name, []).append(t)
                    h, l = ss.hl(bars, st, en)
                    if h is not None:
                        piv[name] = (h, l)
                prev_pm = piv.get("NYPM")
            L += [f"### {label}", "", "| killzone | all | first half | second half | with a map: at gamma | with a map: not at gamma |",
                  "|---|---|---|---|---|---|"]
            for name in ("ASIA", "LNDN", "NYAM", "NYPM", "ALL"):
                ts = [t for v in per.values() for t in v] if name == "ALL" else per.get(name, [])
                ts = sorted(ts, key=lambda t: t["time"])
                f = lambda xs: ss.fmt(ss.stats(xs))
                L.append(f"| {'**ALL**' if name == 'ALL' else name} | {f(ts)} | "
                         f"{f([t for t in ts if t['date'] < cut])} | {f([t for t in ts if t['date'] >= cut])} | "
                         f"{f([t for t in ts if t['map'] and t['gamma']])} | {f([t for t in ts if t['map'] and not t['gamma']])} |")
            L.append("")
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    raise SystemExit(main())
