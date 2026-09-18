#!/usr/bin/env python3
"""Measure how often the Cboe delayed-quote file actually changes.

"15 minutes delayed" describes latency - the quotes describe the market as of
15 minutes ago - and says nothing about how often the file is republished. A
delayed feed can still be rewritten every few seconds. Those are independent,
and only one of them decides whether polling faster than 15 minutes returns
anything new. This measures the second.

Run it during market hours, on a machine that can reach cdn.cboe.com:

    python tools/probe_feed.py                # SPY, every 30s for 20 minutes
    python tools/probe_feed.py QQQ 15 60      # symbol, interval secs, minutes

It reports each time the payload changes, and what the HTTP caching headers
claim, so you can tell a genuinely new file from a CDN re-serving an old one.
"""
import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"


def poll(sym):
    req = urllib.request.Request(URL.format(sym=sym),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
    data = json.loads(raw)["data"]
    opts = data.get("options", [])
    # Hash the quote fields only; ignore key order and any envelope noise.
    body = hashlib.sha256(json.dumps(
        [(o.get("option"), o.get("gamma"), o.get("iv"), o.get("open_interest"))
         for o in opts], sort_keys=True).encode()).hexdigest()
    return {
        "spot": data.get("current_price") or data.get("close"),
        "n": len(opts),
        "digest": body,
        "last_modified": headers.get("last-modified", "-"),
        "age": headers.get("age", "-"),
        "cache_control": headers.get("cache-control", "-"),
    }


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    every = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    minutes = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0

    print(f"polling {sym} every {every:g}s for {minutes:g} min\n")
    deadline = time.time() + minutes * 60
    prev = None
    changes = []
    polls = 0

    while time.time() < deadline:
        now = datetime.now()
        try:
            cur = poll(sym)
        except Exception as exc:
            print(f"{now:%H:%M:%S}  fetch failed: {exc!r}")
            time.sleep(every)
            continue
        polls += 1
        if prev is None:
            print(f"{now:%H:%M:%S}  baseline  spot={cur['spot']}  "
                  f"contracts={cur['n']}  last-modified={cur['last_modified']}"
                  f"  age={cur['age']}")
        elif cur["digest"] != prev["digest"]:
            gap = now.timestamp() - changes[-1] if changes else None
            changes.append(now.timestamp())
            moved = "spot moved" if cur["spot"] != prev["spot"] else "quotes only"
            print(f"{now:%H:%M:%S}  CHANGED   spot={cur['spot']}  {moved}"
                  + (f"  ({gap/60:.1f} min since last change)" if gap else "")
                  + f"  age={cur['age']}")
        prev = cur
        time.sleep(every)

    print(f"\n{polls} polls, {len(changes)} changes")
    if len(changes) > 1:
        gaps = [(b - a) / 60 for a, b in zip(changes, changes[1:])]
        gaps.sort()
        print(f"gap between changes: min {min(gaps):.1f} / median "
              f"{gaps[len(gaps)//2]:.1f} / max {max(gaps):.1f} min")
        print("\nIf the median is near your poll interval the file updates at "
              "least that fast, and publishing every minute is justified.\n"
              "If it clusters near 15 minutes, it does not.")
    elif polls:
        print("No change observed - either the market is closed or the file is "
              "republished more slowly than this run lasted.")


if __name__ == "__main__":
    main()
