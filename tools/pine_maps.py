#!/usr/bin/env python3
"""The gamma map history as strings for pine/gx_killzone_sweeps*.pine.

    python tools/pine_maps.py --out pine_maps.json --days 15

For each killzone, the last clean snapshot published at or before its start:
  A  ASIA  20:00 (keyed by that evening's date)   L  LNDN  02:00
  N  NYAM  09:30                                   P  NYPM  13:30
One entry per killzone, oldest first, separated by ';':
  MMDDK,R,flip,cw1/cw2,pw1/pw2        R = + or - (the near book's regime)
The setups page adds the current session's maps on top of this file.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import sweeps as sw               # noqa: E402

CUT = (("A", "20:00"), ("L", "02:00"), ("N", "09:30"), ("P", "13:30"))
ORDER = {"A": 0, "L": 1, "N": 2, "P": 3}


def entry(day, code, lv, regime):
    f = lambda xs: "/".join(f"{float(x):.2f}" for x in xs)
    flip = f"{float(lv['flip']):.2f}" if lv.get("flip") else ""
    reg = "+" if regime == "positive" else "-" if regime == "negative" else ""
    # empty fields as "x": Pine's str.split may drop empty fields
    parts = [f"{day:%m%d}{code}", reg, flip, f(lv.get('call') or []), f(lv.get('put') or [])]
    return ",".join(x or "x" for x in parts)


def build(days=15):
    maps = sw.map_sessions(sorted({c for _, c in CUT}))
    out = {}
    for sym in sw.SYMBOLS:
        rows = []
        for code, cut in CUT:
            for (day, s), e in maps[cut].items():
                if s == sym:
                    # an evening map belongs to the evening; others to the day
                    rows.append((day, ORDER[code], entry(day, code, e["levels"], e["regime"])))
        dates = sorted({d for d, _, _ in rows})[-days:]
        rows = sorted(r for r in rows if r[0] in dates)
        out[sym] = ";".join(r[2] for r in rows)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="pine_maps.json")
    ap.add_argument("--days", type=int, default=15)
    a = ap.parse_args()
    out = build(a.days)
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    for k, v in out.items():
        print(k, len(v), "chars,", v.count(";") + 1 if v else 0, "maps")


if __name__ == "__main__":
    raise SystemExit(main())
