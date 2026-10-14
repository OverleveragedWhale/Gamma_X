#!/usr/bin/env python3
"""Inspect and verify the raw Cboe chain archive written by gex_terminal.

    python tools/chain_archive.py list
    python tools/chain_archive.py verify                 # newest slot
    python tools/chain_archive.py verify 2026-10-15 open

`verify` checks every chain file in a slot for completeness (rows, expiries,
how many rows carry open interest / gamma / IV) and then recomputes call and
put walls from the archived rows ALONE - load_contracts + gex_by_strike, the
live pipeline's own functions - next to the ladder that was published at the
time, so you can see the archive is enough to rebuild the levels.

The recomputation uses the primary chain only and no binning, so it lands on
the same walls as the published ladder but not identical GEX totals (the live
page merges a second chain and bins strikes).
"""
import csv
import gzip
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gex_terminal as g  # noqa: E402


def load_chain(path):
    """Archived CSV back into Cboe-style row dicts, numbers restored."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in r.items():
            if k in ("option", "expiry", "cp", "last_trade_time") or v == "":
                continue
            try:
                f = float(v)
                r[k] = int(f) if f.is_integer() and k in ("open_interest", "volume") else f
            except ValueError:
                pass
    return rows


def slots(root):
    return sorted(p for p in root.glob("*/*") if (p / "meta.json").exists())


def cmd_list(root):
    found = slots(root)
    if not found:
        print(f"no archive slots under {root}")
        return
    total = 0
    for p in found:
        meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        size = sum(f.stat().st_size for f in p.glob("*.csv.gz"))
        total += size
        chains = ", ".join(f"{k}:{v['rows']}" for k, v in sorted(meta["chains"].items()))
        print(f"{p.parent.name} {p.name:5}  {size/1e6:6.2f} MB  {chains}")
    print(f"{len(found)} slots, {total/1e6:.1f} MB in {root}")


def cmd_verify(root, day=None, slot=None):
    found = slots(root)
    if day:
        found = [p for p in found if p.parent.name == day and (not slot or p.name == slot)]
    if not found:
        print("no matching slot")
        return 1
    folder = found[-1]
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    d = date.fromisoformat(meta["date"])
    print(f"slot {meta['date']} {meta['slot']}  (snapshot {meta.get('generated')})\n")
    ok = True
    chains = {}
    for path in sorted(folder.glob("*.csv.gz")):
        sym = path.name[:-len(".csv.gz")]
        rows = load_chain(path)
        chains[sym] = rows
        n = len(rows)
        exps = {r["expiry"] for r in rows if r["expiry"]}
        has = lambda k: sum(1 for r in rows if r.get(k) not in ("", None, 0, 0.0))
        oi, gm, iv = has("open_interest"), has("gamma"), has("iv")
        print(f"{sym:5} {n:6} rows  {len(exps):3} expiries  "
              f"OI {100*oi/n:5.1f}%  gamma {100*gm/n:5.1f}%  IV {100*iv/n:5.1f}%  "
              f"fields: {len(rows[0]) if rows else 0}")
        if n == 0 or oi == 0:
            ok = False
    print()
    for name, s in sorted(meta.get("symbols", {}).items()):
        chain = s.get("chain")
        if chain not in chains:
            print(f"{name}: primary chain {chain} not in this slot")
            continue
        m = s.get("ratio") or 1.0
        spot_chain = meta["chains"][chain]["spot"]
        contracts = g.load_contracts(chains[chain], d, m=m, src=chain)
        if not any(c["gamma"] for c in contracts):
            print(f"{name}: {chain} rows carry no gamma - Cboe sends none for "
                  f"this chain, so GEX cannot be rebuilt from it")
            continue
        per = g.gex_by_strike(contracts, spot_chain * m)
        top_c = sorted(per.items(), key=lambda kv: -kv[1]["call"])[:3]
        top_p = sorted(per.items(), key=lambda kv: -kv[1]["put"])[:3]
        pub = s.get("ladder") or []
        pub_c = [r["strike"] for r in pub if r.get("side") == "call"][:3]
        pub_p = [r["strike"] for r in pub if r.get("side") == "put"][:3]
        print(f"{name} ({chain} x {m:.5f}, {len(contracts)} contracts in range)")
        print(f"  rebuilt call walls {[k for k, _ in top_c]}   published {pub_c}")
        print(f"  rebuilt put walls  {[k for k, _ in top_p]}   published {pub_p}")
    return 0 if ok else 1


def main():
    root = g.archive_dir()
    if root is None:
        print("archive disabled ([archive] dir = off)")
        return 1
    args = sys.argv[1:] or ["list"]
    if args[0] == "list":
        cmd_list(root)
        return 0
    if args[0] == "verify":
        return cmd_verify(root, *args[1:3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
