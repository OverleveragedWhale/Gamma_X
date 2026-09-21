#!/usr/bin/env python3
"""Check that the ratio, the live spot and the near-term bucket agree on one contract.

m is built from inst["fut"] - a continuous front-month series such as ES=F -
while the near-term bucket names a specific contract picked on open interest.
Those are the same symbol but not always the same contract: a continuous series
rolls, so across a roll the prior close and the live print describe different
deliveries, and m carries the basis of a contract nobody is looking at.

Prints the recent closes of both series side by side, so a roll shows up as a
step in one and not the other, and reports the basis each implies against the
chain underlying.
"""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import gex_terminal as g  # noqa: E402


def closes(sym, n=6):
    try:
        return [(r["date"], r["c"]) for r in g.fetch_rows(sym, max_rows=n)]
    except Exception as exc:
        print(f"    {sym}: FAILED {type(exc).__name__}: {str(exc)[:70]}")
        return []


def main():
    today = g.now_et().date()
    bad = 0
    for inst in g.INSTRUMENTS:
        fut = inst["fut"]
        print(f"\n=== {inst['future']} ===")
        try:
            active = g.active_contract(inst, today)
            chosen = (active.get("chosen") or {}).get("symbol")
        except Exception as exc:
            print(f"  active_contract failed: {exc!r}")
            continue
        print(f"  bucket contract : {chosen}  (exp {active['expiry']})")

        cont = closes(fut)
        spec = closes(chosen) if chosen else []
        print(f"  {'date':<12}{fut:>14}{(chosen or '-'):>16}")
        dates = sorted({d for d, _ in cont} | {d for d, _ in spec})
        cd, sd = dict(cont), dict(spec)
        for d in dates:
            a = f"{cd[d]:,.2f}" if d in cd else "-"
            b = f"{sd[d]:,.2f}" if d in sd else "-"
            print(f"  {d:<12}{a:>14}{b:>16}")

        if not spec:
            print(f"  -> no daily history for {chosen}; the fix cannot use it")
            bad += 1
            continue

        # Basis each series implies against the chain underlying's prior close.
        try:
            chain_prior = g.prior_session(g.fetch_rows(inst["hist"], max_rows=6), today)
            idx_prior = g.prior_session(g.fetch_rows(inst["index"], max_rows=6), today)
        except Exception as exc:
            print(f"  chain/index history failed: {exc!r}")
            continue
        if not chain_prior or not idx_prior:
            print("  no completed chain session to compare against")
            continue
        k = idx_prior["c"] / chain_prior["c"]
        last = lambda rows: [c for d, c in rows if d < today.isoformat()][-1]
        for label, rows in ((fut, cont), (chosen, spec)):
            if not rows:
                continue
            carry = last(rows) / chain_prior["c"] / k - 1.0
            print(f"  basis via {label:<14} {carry:+.4f}"
                  f"   ({'plausible' if -0.01 <= carry <= 0.04 else 'OUT OF GATE'})")
        if cont and spec and abs(last(cont) - last(spec)) > 0.005 * last(spec):
            print(f"  -> MISMATCH: {fut} and {chosen} disagree by "
                  f"{100*(last(cont)/last(spec)-1):+.2f}% on the same session; "
                  f"{fut} is not tracking the bucket contract")
            bad += 1
    print(f"\n{bad} instrument(s) where the ratio source and the bucket disagree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
