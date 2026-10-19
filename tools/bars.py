#!/usr/bin/env python3
"""Futures bars for the backtests: Massive (ex-Polygon) when a key is set,
Yahoo's ~60 days otherwise.

    MASSIVE_API_KEY=...  python tools/bars.py ES --years 2      # smoke test

Massive's Futures API (https://massive.com/docs/rest/futures/aggregates):
  GET https://api.massive.com/futures/v1/aggs/{ticker}?resolution=5min
      &window_start.gte=YYYY-MM-DD&window_start.lt=YYYY-MM-DD&limit=50000
      &apiKey=KEY        -> results[{open,high,low,close,volume,window_start(ns),
                                     session_end_date}], next_url
Tickers are single contracts, root + month + LAST DIGIT of the year (ESZ6).
There is no continuous series (2026-10), so continuous() builds one: each
session goes to the contract with the most volume that session (Massive's own
advice; ES rolls ~3 days and NQ ~4 before expiry), and older contracts are
shifted by the price gap at each roll (difference back-adjustment). That keeps
every intraday distance - stops, targets, sweeps - exactly as traded.

Free plan: 2 years of history, 8-hour delay - fine for weekend backtests.
Developer ($79/mo) gives 5 years, Advanced all history since 2017-04.

The bars are only ever used in memory or in the CI cache; they are never
written into the repository, which is public.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover
    ET = None

BASE = "https://api.massive.com/futures/v1/aggs/{ticker}"
MONTHS = "HMUZ"                          # equity index quarterlies
MONTH_NUM = {"H": 3, "M": 6, "U": 9, "Z": 12}
CACHE = Path(os.environ.get("BARS_CACHE", Path(__file__).resolve().parent.parent / ".bars_cache"))


def key():
    return os.environ.get("MASSIVE_API_KEY", "").strip()


def enabled():
    return bool(key())


# ------------------------------------------------------------------ http
def _get_json(url):
    """GET with retries: 429 and 5xx back off, anything else raises."""
    wait = 12.0
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gamma-x-backtest/1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 7:
                time.sleep(wait)
                wait = min(wait * 1.6, 90)
                continue
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Massive {e.code}: {body}") from None
    raise RuntimeError("Massive: out of retries")


def _with_key(url):
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    q["apiKey"] = key()
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def aggs(ticker, start, end, resolution="5min"):
    """All candles for one contract with window_start in [start, end)."""
    cache = CACHE / f"{ticker}_{resolution}_{start}_{end}.json"
    if cache.exists() and end < date.today():
        return json.loads(cache.read_text())
    q = {"resolution": resolution, "window_start.gte": start.isoformat(),
         "window_start.lt": end.isoformat(), "limit": 50000, "sort": "window_start.asc"}
    url = BASE.format(ticker=ticker) + "?" + urllib.parse.urlencode(q)
    out = []
    while url:
        d = _get_json(_with_key(url))
        out += d.get("results") or []
        url = d.get("next_url")
        gap = float(os.environ.get("MASSIVE_MIN_INTERVAL", "0") or 0)
        if gap:
            time.sleep(gap)
    if end < date.today():
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    return out


def _et(ns):
    t = datetime.fromtimestamp(int(ns) / 1e9, timezone.utc)
    return (t.astimezone(ET) if ET else t - timedelta(hours=4)).replace(tzinfo=None)


# -------------------------------------------------------------- contracts
def massive_ticker(sym):
    """'ESZ26.CME' / 'ESZ26' / 'ESZ6' -> 'ESZ6'."""
    s = sym.split(".")[0].upper()
    if s[-2:].isdigit():                    # ESZ26
        root, mon, yr = s[:-3], s[-3], s[-2:]
    else:                                   # ESZ6
        root, mon, yr = s[:-2], s[-2], s[-1]
    return f"{root}{mon}{yr[-1]}"


def contracts_between(root, start, end):
    """Quarterly contracts that can be front between start and end:
    [(ticker, expiry month first day)]."""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end + timedelta(days=100):
        if m in MONTH_NUM.values():
            code = MONTHS[[3, 6, 9, 12].index(m)]
            out.append((f"{root}{code}{str(y)[-1]}", date(y, m, 1)))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def contract_bars(sym, days=120):
    """{date: [(hh:mm, high, low, close), ...]} ET for one contract - the
    shape tools/score_levels.bars() returns."""
    tk = massive_ticker(sym)
    end = date.today() + timedelta(days=1)
    by = {}
    for c in aggs(tk, end - timedelta(days=days), end):
        t = _et(c["window_start"])
        by.setdefault(t.date(), []).append((t.strftime("%H:%M"), float(c["high"]),
                                            float(c["low"]), float(c["close"])))
    return {d: sorted(v) for d, v in by.items()}


def session_of(t):
    """The trading session a bar belongs to: 18:00 ET starts the next day's."""
    return (t + timedelta(hours=6)).date()


def continuous(root, start, end=None):
    """[(datetime ET, high, low, close)] 5-minute, front month by volume,
    difference back-adjusted to the newest contract."""
    end = end or date.today() + timedelta(days=1)
    cons = contracts_between(root, start, end)
    vol = {}                                 # session date -> {ticker: volume}
    for tk, exp in cons:
        s = max(start - timedelta(days=1), exp - timedelta(days=200))
        e = min(end, exp + timedelta(days=31))
        if s >= e:
            continue
        for c in aggs(tk, s, e, "1session"):
            sd = c.get("session_end_date")
            if sd:
                vol.setdefault(sd, {})[tk] = int(c.get("volume") or 0)
    front = {sd: max(v, key=v.get) for sd, v in vol.items() if v}
    if not front:
        return []
    days = sorted(front)
    # the bars, per contract, over the sessions it is front plus the one before
    spans = {}
    for i, sd in enumerate(days):
        tk = front[sd]
        prev = days[i - 1] if i else sd
        a, b = spans.get(tk, (prev, sd))
        spans[tk] = (min(a, prev), max(b, sd))
    raw = {}
    for tk, (a, b) in spans.items():
        s = date.fromisoformat(a) - timedelta(days=1)
        e = date.fromisoformat(b) + timedelta(days=1)
        rows = []
        for c in aggs(tk, s, e):
            t = _et(c["window_start"])
            rows.append((t, float(c["high"]), float(c["low"]), float(c["close"])))
        raw[tk] = sorted(rows)
    # roll offsets: at a switch old -> new on session d, the gap between the
    # two contracts' closes at the last common bar of the session before
    offset, adj = 0.0, {}
    for i in range(len(days) - 1, -1, -1):
        sd = days[i]
        adj[sd] = offset
        if i and front[days[i - 1]] != front[sd]:
            old, new = front[days[i - 1]], front[sd]
            prev = days[i - 1]
            o = {t: c for t, _, _, c in raw.get(old, []) if session_of(t).isoformat() == prev}
            n = {t: c for t, _, _, c in raw.get(new, []) if session_of(t).isoformat() == prev}
            common = sorted(set(o) & set(n))
            if common:
                offset += n[common[-1]] - o[common[-1]]
    out = []
    for tk, rows in raw.items():
        for t, h, l, c in rows:
            sd = session_of(t).isoformat()
            if front.get(sd) == tk and sd >= start.isoformat():
                k = adj[sd]
                out.append((t, h + k, l + k, c + k))
    out.sort()
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="ES")
    ap.add_argument("--years", type=float, default=2)
    a = ap.parse_args()
    if not enabled():
        print("MASSIVE_API_KEY is not set")
        return 1
    start = date.today() - timedelta(days=int(a.years * 365))
    rows = continuous(a.root, start)
    print(f"{a.root}: {len(rows)} bars, {rows[0][0] if rows else '-'} to {rows[-1][0] if rows else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
