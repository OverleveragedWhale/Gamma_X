#!/usr/bin/env python3
"""Do RI multiples and band edges sit on high-volume prices?

    MASSIVE_API_KEY=... python tools/volume_levels.py --out backtests/volume_levels.md

Volume profile per session from Massive 5-minute bars, each bar's volume
spread evenly over the ticks of its range. For a level L the score is

    log( volume density within +/-w of L  /  density in the ring 2w..5w either side )

so a level sitting on a high-volume node scores > 0, one in a gap < 0, and a
level placed with no knowledge of volume ~0. Only levels the session traded
fully through (ring inside the session's range) count.

Placebo: the same ladder scaled by s from 0.50 to 1.50. Profiles have shape
near the anchor (the session's own value area), so the score is compared with
the ladder at OTHER scales, not with zero. If the published RI means
something, s = 1.00 should stand out against its neighbours.

Test 1, the published RI: ES 58.75 / NQ 211.25 for the H26 contract period
(official Rocket Scooter values, via github.com/tqfoster1035/TradingView_Risk_Interval),
on ESH6 / NQH6 while each was front (2025-12-15 to 2026-03-12). Multiples
1/8 1/4 1/2 3/4 1 1.5 2 3 4, off two anchors: the 16:00 ET close and the
session's last bar (16:55-17:00).

Test 2, the realized-vol bands (gex_terminal BAND_LEVELS, plus 90%), forecast
each prior close from the cash index as tools/band_coverage.py does, on the
continuous front month over the Massive history.
"""
import argparse
import math
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import bars as mb                  # noqa: E402

TICK = 0.25
OFFICIAL = {"ES": ("ESH6", 58.75), "NQ": ("NQH6", 211.25)}
H26 = (date(2025, 12, 15), date(2026, 3, 12))
MULTS = (0.125, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)
SCALES = [round(0.50 + 0.01 * i, 2) for i in range(101)]
HALF_W = {"ES": 1.25, "NQ": 4.5}          # +/- points counted as "at" the level


# ------------------------------------------------------------- sessions
def contract_rows(tk, start, end):
    out = []
    for c in mb.aggs(tk, start - timedelta(days=2), end + timedelta(days=1)):
        t = mb._et(c["window_start"])
        out.append((t, float(c["high"]), float(c["low"]), float(c["close"]),
                    float(c.get("volume") or 0)))
    return sorted(out)


def sessions(rows):
    """[(session date, rows)] in order; session = 18:00 to 17:00 ET."""
    by = {}
    for r in rows:
        by.setdefault(mb.session_of(r[0]), []).append(r)
    return sorted(by.items())


class Profile:
    """Volume per tick over one session (or its RTH part)."""

    def __init__(self, rows):
        lo = min(r[2] for r in rows)
        hi = max(r[1] for r in rows)
        self.base = round(lo / TICK)
        n = round(hi / TICK) - self.base + 1
        self.v = np.zeros(n)
        for _, h, l, _c, vol in rows:
            a, b = round(l / TICK) - self.base, round(h / TICK) - self.base
            if vol > 0:
                self.v[a:b + 1] += vol / (b - a + 1)
        self.cum = np.concatenate([[0.0], np.cumsum(self.v)])
        self.lo, self.hi = lo, hi

    def dens(self, a, b):
        """Mean volume per tick over prices [a, b]."""
        i, j = round(a / TICK) - self.base, round(b / TICK) - self.base
        return (self.cum[j + 1] - self.cum[i]) / (j - i + 1)

    def score(self, level, w):
        if level - 5 * w < self.lo or level + 5 * w > self.hi:
            return None
        at = self.dens(level - w, level + w)
        ring = (self.dens(level - 5 * w, level - 2 * w) + self.dens(level + 2 * w, level + 5 * w)) / 2
        if at <= 0 or ring <= 0:
            return None
        return math.log(at / ring)


def build(rows, rth=False):
    """[(session date, anchor16, anchorLast, Profile)] - anchors from the
    PRIOR session."""
    ss = sessions(rows)
    out = []
    for (pd_, prow), (sd, srow) in zip(ss, ss[1:]):
        a16 = next((r[3] for r in reversed(prow) if r[0].time() <= datetime.strptime("15:55", "%H:%M").time()
                    and r[0].date() == pd_), None)
        alast = prow[-1][3]
        use = [r for r in srow if (not rth) or ("09:30" <= r[0].strftime("%H:%M") < "16:00")]
        if len(use) < 12 or a16 is None:
            continue
        out.append((sd, a16, alast, Profile(use)))
    return out


# ------------------------------------------------------------- scoring
def ladder_scores(sess, dist_fn, w, scales=SCALES, which=1):
    """{scale: [per-session mean score]}; dist_fn(session index, s) ->
    [distances]; which=1 anchor16, 2 last bar."""
    out = {s: [] for s in scales}
    for i, row in enumerate(sess):
        anchor, prof = row[which], row[3]
        for s in scales:
            xs = []
            for d in dist_fn(i, s):
                for lv in (anchor + d, anchor - d):
                    sc = prof.score(lv, w)
                    if sc is not None:
                        xs.append(sc)
            if xs:
                out[s].append((i, sum(xs) / len(xs), len(xs)))
    return out


def summarize(by_scale):
    """mean at 1.00, its percentile among scales |s-1| >= 0.10, best scale,
    bootstrap 90% CI of (score at 1.00 - mean of the placebo scales)."""
    mean = {s: (sum(x[1] for x in v) / len(v) if v else float("nan")) for s, v in by_scale.items()}
    far = [mean[s] for s in mean if abs(s - 1) >= 0.10 and not math.isnan(mean[s])]
    m1 = mean.get(1.0, float("nan"))
    pct = 100 * sum(f < m1 for f in far) / len(far) if far else float("nan")
    best = max((s for s in mean if not math.isnan(mean[s])), key=lambda s: mean[s])
    # paired bootstrap over sessions: score(1.0) - mean placebo score
    per = {}
    for s, v in by_scale.items():
        for i, sc, _n in v:
            per.setdefault(i, {})[s] = sc
    diffs = []
    for i, d in per.items():
        if 1.0 in d:
            pl = [d[s] for s in d if abs(s - 1) >= 0.10]
            if pl:
                diffs.append(d[1.0] - sum(pl) / len(pl))
    rng = random.Random(7)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(2000)) if diffs else []
    ci = (boots[100], boots[1899]) if boots else (float("nan"),) * 2
    n_lv = sum(x[2] for x in by_scale.get(1.0, []))
    return {"m1": m1, "pct": pct, "best": best, "mbest": mean[best], "edge": (sum(diffs) / len(diffs)) if diffs else float("nan"),
            "ci": ci, "sessions": len(by_scale.get(1.0, [])), "levels": n_lv, "mean": mean}


def curve(mean):
    return " ".join(f"{s:.2f}:{mean[s]:+.3f}" for s in SCALES[::10])


def fmt(r):
    return (f"{r['sessions']} | {r['levels']} | {r['m1']:+.3f} | {r['edge']:+.3f} "
            f"[{r['ci'][0]:+.3f}, {r['ci'][1]:+.3f}] | {r['pct']:.0f} | {r['best']:.2f} ({r['mbest']:+.3f})")


HEAD = ("| test | sessions | level hits at 1.00 | score at 1.00 | edge vs placebo [90% CI] | "
        "percentile among placebo scales | best scale |\n|---|---|---|---|---|---|---|")


# ------------------------------------------------------------- tests
YAHOO = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         "{sym}?range=60d&interval=5m&includePrePost=true")


def yahoo_rows(series):
    """~60 days of 5-minute bars with volume, ET (continuous front month)."""
    import json as _json
    from datetime import timezone
    import gex_terminal as g
    res = _json.loads(g.http_get(YAHOO.format(sym=series)))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        hi, lo, cl, v = q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (hi, lo, cl):
            continue
        t = datetime.fromtimestamp(ts, timezone.utc).astimezone(mb.ET).replace(tzinfo=None)
        out.append((t, float(hi), float(lo), float(cl), float(v or 0)))
    return sorted(out)


def test_ri_now(L):
    """Our RI (config.ini margin / multiplier / 8, the terminal's 'RI') on
    Yahoo's last ~60 days. Margins were near-constant over the window
    (config history: ES 24.8-24.9k, NQ 42.3-42.7k average)."""
    import gex_terminal as g
    cfg = g.load_margins()
    inst = {i["future"]: i for i in g.INSTRUMENTS}
    L += ["## Test 1b - this terminal's RI (margin / point value / 8), last ~60 days", "",
          "Yahoo continuous front month with volume. Same score and placebo.", "", HEAD]
    for sym in ("ES", "NQ"):
        ri = g.margin_buffer(inst[sym], 1, cfg)["old"]["ri"]
        rows = yahoo_rows(f"{sym}=F")
        w = HALF_W[sym]
        for rth in (False, True):
            sess = build(rows, rth)
            for which, an in ((1, "16:00 close"), (2, "17:00 last bar")):
                r = summarize(ladder_scores(sess, lambda i, s: [k * s * ri for k in MULTS], w, which=which))
                L.append(f"| {sym} RI {ri:.2f} · {'RTH' if rth else 'full session'} profile · {an} | {fmt(r)} |")
                if not rth and which == 1:
                    L.append(f"| {sym} curve (full, 16:00) | {curve(r['mean'])} | | | | | |")
    L.append("")


def test_ri(L):
    L += ["## Test 1 - published RI multiples, H26 contract period", "",
          "Edge = score at s=1.00 minus the mean score of the same ladder at scales 0.50-0.90 and "
          "1.10-1.50, paired by session. Positive with a CI clear of 0 = the official RI ladder "
          "sits on more volume than a ladder of the wrong size.", "", HEAD]
    for sym, (tk, ri) in OFFICIAL.items():
        rows = contract_rows(tk, *H26)
        rows = [r for r in rows if H26[0] - timedelta(days=1) <= r[0].date() <= H26[1]]
        w = HALF_W[sym]
        for rth in (False, True):
            sess = build(rows, rth)
            for which, an in ((1, "16:00 close"), (2, "17:00 last bar")):
                r = summarize(ladder_scores(sess, lambda i, s: [k * s * ri for k in MULTS], w, which=which))
                L.append(f"| {sym} RI {ri} · {'RTH' if rth else 'full session'} profile · {an} | {fmt(r)} |")
                if not rth and which == 1:
                    curves = r["mean"]
                    # per multiple, at 1.00 vs placebo
                    for k in MULTS:
                        rk = summarize(ladder_scores(sess, lambda i, s, k=k: [k * s * ri], w, which=1))
                        L.append(f"| {sym} · {k:g}x only · full · 16:00 | {fmt(rk)} |")
        L.append(f"| {sym} curve (full, 16:00) | {curve(curves)} | | | | | |")
    L.append("")


def test_bands(L, years, source="massive"):
    import band_coverage as bc
    import gex_terminal as g
    labels = (("90%", 0.90), ("95%", 0.95), ("99%", 0.99))
    L += [f"## Test 2{'b' if source == 'yahoo' else ''} - realized-vol band edges, continuous front month"
          + (" (Yahoo, last ~60 days)" if source == "yahoo" else ""), "",
          "Band forecast at each prior close from the cash index (EWMA x fat-tail quantile), "
          "drawn about the prior session's last bar. Same score and placebo as test 1.", "", HEAD]
    for sym, idx in (("ES", "^GSPC"), ("NQ", "^NDX")):
        rows = (yahoo_rows(f"{sym}=F") if source == "yahoo" else
                mb.continuous(sym, date.today() - timedelta(days=int(years * 365)), with_volume=True))
        sess = build(rows, rth=False)
        # walk-forward band fractions by session date
        old = g.BAND_LEVELS
        g.BAND_LEVELS = labels
        try:
            w_ = bc.walk(bc.fetch(idx))
        finally:
            g.BAND_LEVELS = old
        xs = [w_.get(sd.isoformat()) for sd, *_ in sess]
        keep = [i for i, x in enumerate(xs) if x]
        sess = [sess[i] for i in keep]
        xs = [xs[i] for i in keep]
        for lab, _p in labels:
            def dist(i, s, lab=lab):
                x = xs[i][1][lab] * s
                return [sess[i][2] * (math.exp(x) - 1)]
            r = summarize(ladder_scores(sess, dist, HALF_W[sym], which=2))
            L.append(f"| {sym} {lab} band edge | {fmt(r)} |")
    L.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="volume_levels.md")
    ap.add_argument("--years", type=float, default=float(os.environ.get("BACKTEST_YEARS", "2") or 2))
    a = ap.parse_args()
    L = ["# Volume at RI multiples and band edges", "",
         f"Run {datetime.now():%Y-%m-%d %H:%M}. Score = log(volume density within ±w of the level / "
         f"density 2w-5w either side); w = {HALF_W['ES']} pts ES, {HALF_W['NQ']} pts NQ. "
         "Curve columns: mean score by scale.", ""]
    tests = [test_ri_now, lambda L: test_bands(L, a.years, "yahoo")]
    if mb.enabled():
        tests += [test_ri, lambda L: test_bands(L, a.years)]
    else:
        L += ["*MASSIVE_API_KEY not set: tests 1 (published H26 RI) and 2 (bands over 2 years) skipped.*", ""]
    for fn in tests:
        try:
            fn(L)
        except Exception as exc:          # one test failing should not lose the other
            import traceback
            L += [f"**failed:** `{exc!r}`", "", "```", traceback.format_exc()[-1500:], "```", ""]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
