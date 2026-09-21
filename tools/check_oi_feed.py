#!/usr/bin/env python3
"""Check that the futures open-interest feed still works, and say so loudly.

The dashboard picks each instrument's front contract by open interest, which
comes from Yahoo's v7 quote endpoint. That endpoint is undocumented and needs a
cookie+crumb handshake it did not need historically, so it is the most likely
thing in the stack to break without warning. When it does, gex_terminal falls
back to volume and then to the nearest-expiry date rule - the numbers stay
plausible, which is exactly why a silent failure would go unnoticed.

Run it by hand, or through the "Feed diagnostics" workflow:

    python tools/check_oi_feed.py

Exit status is 0 when every instrument resolved on open interest, 1 when any
instrument fell back. Nothing is written and nothing is published.
"""
import sys
from datetime import date

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import gex_terminal as g  # noqa: E402


def main():
    today = date.today()
    print("crumb handshake:", end=" ")
    try:
        crumb = g.yahoo_crumb(refresh=True)
        print(f"OK ({len(crumb)} chars)")
    except Exception as exc:
        print(f"FAILED - {type(exc).__name__}: {str(exc)[:90]}")
        print("\n::error::OI feed handshake is broken; the dashboard is on a fallback.")
        return 1

    degraded = []
    for inst in g.INSTRUMENTS:
        fut = inst["future"]
        try:
            active = g.active_contract(inst, today)
        except Exception as exc:
            print(f"\n{fut}: lookup raised {type(exc).__name__}: {str(exc)[:80]}")
            degraded.append(fut)
            continue

        source = active["source"]
        chosen = (active["chosen"] or {}).get("label", "-")
        flag = "OK  " if source == "open interest" else "WARN"
        print(f"\n{flag} {fut}: {chosen} by {source}")
        for row in active["candidates"]:
            oi = f"{row['oi']:,}" if row["oi"] else "n/a"
            vol = f"{row['volume']:,}" if row["volume"] else "n/a"
            mark = " <- active" if row.get("active") else ""
            print(f"       {row['symbol']:<14} {row['label']:<7} "
                  f"OI {oi:>12}  vol {vol:>10}{mark}")

        date_rule = g.next_futures_expiry(today, inst["cycle"]).isoformat()
        if active["expiry"].isoformat() != date_rule:
            print(f"       note: date rule would have used {date_rule} - this is the "
                  f"disagreement the OI feed exists to catch")
        if source != "open interest":
            degraded.append(fut)

    print("")
    if degraded:
        print(f"::warning::Open interest unavailable for {', '.join(degraded)}. "
              f"The dashboard is ranking contracts on a fallback measure; "
              f"check whether Yahoo's v7 quote endpoint changed.")
        return 1
    print("All instruments resolved their front contract on open interest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
