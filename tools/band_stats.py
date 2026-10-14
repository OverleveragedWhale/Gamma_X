#!/usr/bin/env python3
"""Walk-forward hit rates for the liquidation bands, old and new.

For each session, using only data available at the prior close:
  * NEW bands: gex_terminal's tail model - EWMA vol of the cash index and
    empirical quantiles of its standardized returns (expanding window, capped
    at TAIL_HISTORY_ROWS) - drawn about the prior FUTURES close.
  * OLD margin band: the fixed point width the previous code printed off the
    current config.ini margins (avg margin x 12.5% / multiplier), scored over
    the last 252 sessions in points, and over the full sample as the same
    percentage of price.
  * OLD realized-vol band: 2.3263 x sigma20 / 2 (the halved "99%").

A long is "liquidated" when the session LOW reaches anchor - down, a short
when the HIGH reaches anchor + up; the close-based rate is printed too, since
the quantiles are close-to-close and intraday extremes run further.

Sessions in each quarterly roll window are dropped: the continuous future's
prior close and its session can be different contracts there.

    python tools/band_stats.py              # markdown to stdout
    python tools/band_stats.py --json out.json
"""
import argparse
import bisect
import configparser
import json
import math
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gex_terminal as g  # noqa: E402

SYMBOLS = [("ES", "ES=F", "^GSPC", 50), ("NQ", "NQ=F", "^NDX", 20)]
RECENT = 252


def fetch(sym):
    p2 = int(time.time()) + 86400
    p1 = p2 - int(g.TAIL_HISTORY_YEARS * 365.25 * 86400)
    rows = g.fetch_rows(sym, max_rows=10 ** 6,
                        url=g.YAHOO_LONG_URL.format(p1=p1, p2=p2))
    return {r["date"]: r for r in rows}


def third_friday(y, m):
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def in_roll(d):
    if d.month not in (3, 6, 9, 12):
        return False
    tf = third_friday(d.year, d.month)
    return tf - timedelta(days=10) <= d <= tf + timedelta(days=3)


def old_margin_points(fut, mult):
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).resolve().parents[1] / "config.ini")
    m = cfg["margins"]
    avg = (float(m[f"{fut}_UP"]) + float(m[f"{fut}_DOWN"])) / 2
    # old chain: avg x 0.01 / 4 x (100/mult) / 2  == avg/mult x 12.5%
    return avg * 0.01 / 4 * (100.0 / mult) / 2


def rate(hits, n):
    return (100.0 * hits / n) if n else float("nan")


def binom_z(hits, n, p):
    return (hits - n * p) / math.sqrt(n * p * (1 - p)) if n else float("nan")


def run_symbol(name, fut_sym, tail_sym, mult):
    fut = fetch(fut_sym)
    tail = fetch(tail_sym)
    tdates = sorted(tail)
    closes = [tail[d]["c"] for d in tdates]
    lam, warm = g.EWMA_LAMBDA, g.TAIL_WARMUP
    # rets[i] is the return INTO tdates[i+1]; var_before[i] is its forecast
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    var = sum(r * r for r in rets[:warm]) / warm
    var_before = [None] * len(rets)
    for i in range(warm, len(rets)):
        var_before[i] = var
        var = lam * var + (1 - lam) * rets[i] ** 2
    resid = [None] * len(rets)
    for i in range(warm, len(rets)):
        resid[i] = rets[i] / math.sqrt(var_before[i])

    old_pts = old_margin_points(name, mult)
    last_fut = fut[max(fut)]["c"]
    old_pct = old_pts / last_fut

    window, window_abs = [], []          # sorted residual sample
    fifo = []
    levels = g.BAND_LEVELS
    stats = {lab: {"n": 0, "lo_hit": 0, "hi_hit": 0, "lo_close": 0,
                   "hi_close": 0, "width": []} for lab, _ in levels}
    old = {"n": 0, "lo": 0, "hi": 0, "both_new": {lab: 0 for lab, _ in levels}}
    oldvol = {"n": 0, "lo": 0, "hi": 0}
    recent = {"n": 0, "lo": 0, "hi": 0, "both_new": {lab: 0 for lab, _ in levels}}
    fdates = sorted(fut)
    fidx = {d: i for i, d in enumerate(fdates)}
    eval_dates = []

    for i in range(warm, len(rets)):
        d = tdates[i + 1]                 # session being scored
        # sample = residuals strictly before this return
        if len(fifo) >= g.TAIL_MIN_ROWS and d in fidx and fidx[d] > 0 \
                and not in_roll(date.fromisoformat(d)):
            fi = fidx[d]
            prev = fut[fdates[fi - 1]]
            # prior futures bar must be the prior session, not after a gap
            gap = (date.fromisoformat(d) - date.fromisoformat(prev["date"])).days
            if gap <= 4 and not in_roll(date.fromisoformat(prev["date"])):
                eval_dates.append(d)
                bar, a = fut[d], prev["c"]
                sig = math.sqrt(var_before[i])
                n = len(window)
                srt = window
                for lab, p in levels:
                    floor = p >= g.TAIL_FLOOR_FROM
                    z1 = g.norm_z(p, 1) if floor else 0.0
                    q_dn = max(-g.quantile(srt, 1 - p), z1)
                    q_up = max(g.quantile(srt, p), z1)
                    lo = a * math.exp(-q_dn * sig)
                    hi = a * math.exp(q_up * sig)
                    s = stats[lab]
                    s["n"] += 1
                    s["lo_hit"] += bar["l"] <= lo
                    s["hi_hit"] += bar["h"] >= hi
                    s["lo_close"] += bar["c"] <= lo
                    s["hi_close"] += bar["c"] >= hi
                    s["width"].append((a - lo, hi - a))
                    s.setdefault("_last", (lo, hi))
                # old margin band, as % of price over the full sample
                olo, ohi = a * (1 - old_pct), a * (1 + old_pct)
                old["n"] += 1
                hit_lo, hit_hi = bar["l"] <= olo, bar["h"] >= ohi
                old["lo"] += hit_lo
                old["hi"] += hit_hi
                for lab, p in levels:
                    floor = p >= g.TAIL_FLOOR_FROM
                    z1 = g.norm_z(p, 1) if floor else 0.0
                    lo = a * math.exp(-max(-g.quantile(srt, 1 - p), z1) * sig)
                    hi = a * math.exp(max(g.quantile(srt, p), z1) * sig)
                    if (hit_lo and bar["l"] <= lo) or (hit_hi and bar["h"] >= hi):
                        old["both_new"][lab] += 1
                # old realized-vol band: 2.3263 x sigma20 / 2
                if fi >= 21:
                    cs = [fut[fdates[k]]["c"] for k in range(fi - 21, fi)]
                    rr = [math.log(y / x) for x, y in zip(cs, cs[1:])]
                    mu = sum(rr) / len(rr)
                    s20 = math.sqrt(sum((r - mu) ** 2 for r in rr) / (len(rr) - 1))
                    w = a * 2.3263 * s20 / 2
                    oldvol["n"] += 1
                    oldvol["lo"] += bar["l"] <= a - w
                    oldvol["hi"] += bar["h"] >= a + w
        # admit this residual for later sessions
        r = resid[i]
        bisect.insort(window, r)
        fifo.append(r)
        if len(fifo) > g.TAIL_HISTORY_ROWS:
            window.pop(bisect.bisect_left(window, fifo.pop(0)))

    # recent 252 scored sessions with the OLD band in fixed points
    for d in eval_dates[-RECENT:]:
        fi = fidx[d]
        bar, a = fut[d], fut[fdates[fi - 1]]["c"]
        recent["n"] += 1
        hit_lo, hit_hi = bar["l"] <= a - old_pts, bar["h"] >= a + old_pts
        recent["lo"] += hit_lo
        recent["hi"] += hit_hi

    # today's bands from the live model, for scale
    model = g.tail_model(closes)
    today = g.band_set(model, last_fut, 0.25)
    w_recent = {lab: (sum(x for x, _ in s["width"][-RECENT:]) / min(RECENT, len(s["width"])),
                      sum(y for _, y in s["width"][-RECENT:]) / min(RECENT, len(s["width"])))
                for lab, s in stats.items()}
    return {"symbol": name, "from": eval_dates[0], "to": eval_dates[-1],
            "sessions": len(eval_dates), "last_close": last_fut,
            "old_pts": old_pts, "old_pct": old_pct, "levels": levels,
            "stats": stats, "old": old, "oldvol": oldvol, "recent": recent,
            "today": today, "sigma_pct": 100 * model["sigma"],
            "w_recent": w_recent}


def report(res):
    out = []
    for r in res:
        n = r["stats"][r["levels"][0][0]]["n"]
        out.append(f"## {r['symbol']}  ({r['from']} to {r['to']}, {n} sessions scored, "
                   f"roll windows excluded)\n")
        out.append(f"Last close {r['last_close']:.2f}, EWMA sigma {r['sigma_pct']:.2f}%/day. "
                   f"Old margin band {r['old_pts']:.2f} pts = {100*r['old_pct']:.2f}% of price.\n")
        out.append("| Band | Nominal/side | Long hit (low) | Short hit (high) | Long hit (close) | Short hit (close) | z (low) | Avg width last 252 (dn/up pts) | Today (dn/up pts) |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        tmap = {x["label"]: x for x in r["today"]}
        for lab, p in r["levels"]:
            s = r["stats"][lab]
            wd, wu = r["w_recent"][lab]
            t = tmap[lab]
            out.append(f"| {lab} | {100*(1-p):.1f}% | {rate(s['lo_hit'], s['n']):.2f}% | "
                       f"{rate(s['hi_hit'], s['n']):.2f}% | {rate(s['lo_close'], s['n']):.2f}% | "
                       f"{rate(s['hi_close'], s['n']):.2f}% | {binom_z(s['lo_hit'], s['n'], 1-p):+.1f} | "
                       f"{wd:.1f} / {wu:.1f} | {t['down']:.1f} / {t['up']:.1f} |")
        o, ov, rc = r["old"], r["oldvol"], r["recent"]
        out.append("")
        out.append(f"Old margin band ({100*r['old_pct']:.2f}% of price), full sample: long hit "
                   f"{rate(o['lo'], o['n']):.1f}%, short hit {rate(o['hi'], o['n']):.1f}%, "
                   f"either {rate(o['lo'] + o['hi'], o['n']):.1f}%.")
        out.append(f"Old margin band ({r['old_pts']:.2f} pts fixed), last {rc['n']} sessions: "
                   f"long {rate(rc['lo'], rc['n']):.1f}%, short {rate(rc['hi'], rc['n']):.1f}%.")
        out.append(f"Old realized-vol '99%' band (2.3263 sigma20 / 2): long "
                   f"{rate(ov['lo'], ov['n']):.1f}%, short {rate(ov['hi'], ov['n']):.1f}% "
                   f"({ov['n']} sessions).")
        hits = o["lo"] + o["hi"]
        out.append("Of old-margin-band breaches, share also breaching the new band: "
                   + ", ".join(f"{lab} {rate(o['both_new'][lab], hits):.1f}%"
                               for lab, _ in r["levels"]) + "\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    res = [run_symbol(*s) for s in SYMBOLS]
    md = report(res)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    if args.json:
        for r in res:
            for s in r["stats"].values():
                s.pop("width", None)
                s.pop("_last", None)
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
