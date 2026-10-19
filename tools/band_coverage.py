#!/usr/bin/env python3
"""Walk-forward coverage of the realized-vol close bands (gex_terminal BAND_LEVELS).

    python tools/band_coverage.py --out band_coverage.md

For every session, using only what was known at the prior close: the EWMA
vol forecast and the empirical |standardized return| quantiles of the
history BEFORE that session (capped at TAIL_HISTORY_ROWS, as live), drawn
about the prior close. A session "stays inside" when its close is within
the band - the target is ~95% / ~99% of sessions.

Scored twice:
  index    the long cash series the model is fitted on (^GSPC, ^NDX, GLD),
           every session since the model has TAIL_MIN_ROWS of history
  futures  the front future (ES=F, NQ=F, GC=F) with the index model's band
           width in percent, quarterly roll windows dropped (the continuous
           series' prior close and session can be different contracts)
"""
import argparse
import bisect
import math
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gex_terminal as g  # noqa: E402

SETS = [("ES", "^GSPC", "ES=F"), ("NQ", "^NDX", "NQ=F"), ("GC", "GLD", "GC=F")]


def fetch(sym):
    p2 = int(time.time()) + 86400
    p1 = p2 - int(g.TAIL_HISTORY_YEARS * 365.25 * 86400)
    return g.fetch_rows(sym, max_rows=10 ** 6, url=g.YAHOO_LONG_URL.format(p1=p1, p2=p2))


def third_friday(y, m):
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def in_roll(d):
    if d.month not in (3, 6, 12, 9):
        return False
    tf = third_friday(d.year, d.month)
    return tf - timedelta(days=10) <= d <= tf + timedelta(days=3)


def walk(rows):
    """{date: (anchor, {label: band fraction x})} - x is q * sigma in log
    space, forecast at the prior close for the session on `date`."""
    closes = [r["c"] for r in rows]
    dates = [r["date"] for r in rows]
    lam, warm = g.EWMA_LAMBDA, g.TAIL_WARMUP
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    var = sum(r * r for r in rets[:warm]) / warm
    pool, fifo, out = [], [], {}
    for i in range(warm, len(rets)):
        sd = math.sqrt(var)
        # forecast for return i (session dates[i+1]) uses var before it
        if len(pool) >= g.TAIL_MIN_ROWS and sd > 0:
            out[dates[i + 1]] = (closes[i], {lab: g.quantile(pool, p) * sd
                                              for lab, p in g.BAND_LEVELS}, rets[i])
        if sd > 0:
            z = abs(rets[i] / sd)
            bisect.insort(pool, z)
            fifo.append(z)
            if len(fifo) > g.TAIL_HISTORY_ROWS:
                old = fifo.pop(0)
                pool.pop(bisect.bisect_left(pool, old))
        var = lam * var + (1 - lam) * rets[i] * rets[i]
    return out


def score(pairs):
    """pairs: [(anchor, x, outcome close)] -> {label: (inside, n)}."""
    res = {lab: [0, 0] for lab, _ in g.BAND_LEVELS}
    for anchor, xs, close in pairs:
        for lab, x in xs.items():
            lo, hi = anchor * math.exp(-x), anchor * math.exp(x)
            res[lab][1] += 1
            res[lab][0] += lo <= close <= hi
    return res


def fmt(res, lab):
    k, n = res[lab]
    return f"{100 * k / n:.1f}% of {n}" if n else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="band_coverage.md")
    a = ap.parse_args()
    L = ["# Realized-vol close bands: walk-forward coverage", "",
         "Share of sessions whose CLOSE landed inside the band drawn at the prior close. "
         f"Target: {', '.join(l for l, _ in g.BAND_LEVELS)}. EWMA lambda {g.EWMA_LAMBDA}, "
         f"quantiles from up to {g.TAIL_HISTORY_ROWS} prior sessions.", ""]
    for name, idx_sym, fut_sym in SETS:
        idx = fetch(idx_sym)
        w = walk(idx)
        ds = sorted(w)
        by = {r["date"]: r["c"] for r in idx}
        pairs = [(w[d][0], w[d][1], by[d]) for d in ds]
        L += [f"## {name}  (model on {idx_sym}, {ds[0]} to {ds[-1]})", "",
              "| sample | " + " | ".join(f"{l} band" for l, _ in g.BAND_LEVELS) + " |",
              "|---|" + "---|" * len(g.BAND_LEVELS)]
        for label, sub in (("index, all sessions", pairs), ("index, last 1000", pairs[-1000:]),
                           ("index, last 252", pairs[-252:])):
            r = score(sub)
            L.append(f"| {label} | " + " | ".join(fmt(r, l) for l, _ in g.BAND_LEVELS) + " |")
        try:
            fut = {r["date"]: r["c"] for r in fetch(fut_sym)}
            fds = sorted(fut)
            prev = {d: fds[i - 1] for i, d in enumerate(fds) if i}
            fp = [(fut[prev[d]], w[d][1], fut[d]) for d in ds
                  if d in fut and d in prev and not in_roll(date.fromisoformat(d))]
            for label, sub in (("futures, all (rolls dropped)", fp), ("futures, last 252", fp[-252:])):
                r = score(sub)
                L.append(f"| {label} | " + " | ".join(fmt(r, l) for l, _ in g.BAND_LEVELS) + " |")
        except Exception as exc:
            L.append(f"| futures | unavailable ({exc}) |")
        # by year, index
        L += ["", "By year (index):", ""]
        years = sorted({d[:4] for d in ds})
        for y in years:
            sub = [p for d, p in zip(ds, pairs) if d.startswith(y)]
            r = score(sub)
            L.append(f"- {y}: " + " · ".join(f"{l} {fmt(r, l)}" for l, _ in g.BAND_LEVELS))
        last = w[ds[-1]]
        L += ["", f"Latest band width: " + " · ".join(f"{l} ±{100 * (math.exp(x) - 1):.2f}%"
                                                       for l, x in last[1].items()), ""]
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    raise SystemExit(main())
