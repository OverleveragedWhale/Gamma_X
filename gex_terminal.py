#!/usr/bin/env python3
"""
gex_terminal.py - single-file live GEX + margin dashboard (no email).

Fetches Cboe delayed chains for SPY/QQQ; computes net GEX, the gamma
flip, scored call/put walls, and a futures-margin buffer (Stooq is used only
for the prior close used to scale chain-terms levels into futures terms);
serves a live web terminal that recomputes every 15 minutes. One process,
no scheduled tasks, no email.

Run:   python gex_terminal.py        then open the URL it prints.
       python gex_terminal.py --snapshot site/index.html
Env:   DASH_HOST (default 127.0.0.1; 0.0.0.0 to expose - pair with DASH_TOKEN),
       DASH_PORT (default 8787), DASH_TOKEN (require ?k=token then a cookie).
Pure standard library; on Windows also run: pip install tzdata.
Optional config.ini [margins] section (ES/NQ initial margin) adds the band.
"""

import json
import math
import re
import hashlib
import logging
import configparser
import http.cookiejar
import urllib.request
from pathlib import Path
from datetime import datetime, date, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
import os
import time
import threading
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
# ---------------- settings ----------------
MAX_DTE = 95                 # GEX: ignore contracts beyond this many days
SHARES_PER_CONTRACT = 100
WALL_COUNT = 3               # ranked walls reported per side
MIN_WALL_SEP = 0.004         # min gap between reported walls (0.4% of spot)
WALL_CONFLUENCE_TOL = 0.0015  # wall within 0.15% of prior H/L/C = confluence
PRIOR_SESSION_ROWS = 5        # Stooq rows needed for prior close + chain-scale ratio

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}&i=d"

# GEX source per tradeable. SPX index options are free-with-greeks, so ES uses
# the native cash-index chain. Cboe's free feed does NOT carry NDX greeks,
# so NQ uses the ETF chain (QQQ). Every level is mapped to FUTURES terms
# by a single multiplicative ratio from real PRIOR CLOSES:
#     m = prior_future_close / prior_chain_close      (level_future = strike * m)
# m folds ETF tracking and the futures basis (which is itself ~multiplicative)
# into one empirical number. The future close is auto-fetched from Stooq
# (sanity-checked against the index ratio), overridable via config [closes].
#   chain = Cboe options ticker    hist = Stooq chain-scale prior close (m)
#   index = Stooq cash index (margin notional + tracking sanity)
#   fut   = Stooq futures symbol for the prior settle
INSTRUMENTS = [
    {"future": "ES",  "chain": "_SPX", "hist": "^spx",   "index": "^spx",
     "fut": "es.f",  "multiplier": 50},
    {"future": "NQ",  "chain": "QQQ",  "hist": "qqq.us", "index": "^ndx",
     "fut": "nq.f",  "multiplier": 20},
]

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
LOG_PATH = BASE_DIR / "gex_span.log"

# On Windows, zoneinfo needs the 'tzdata' package (pip install tzdata).
# If it's missing, fall back to local machine time rather than crashing -
# timestamps and the trading-day guard then assume the PC clock is on ET.
ET = None
if ZoneInfo:
    try:
        ET = ZoneInfo("America/New_York")
    except Exception:
        pass

logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

MARKET_HOLIDAYS = {   # US equity holidays 2026 - verify yearly, no half-days
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}


def now_et():
    return datetime.now(ET) if ET else datetime.now()


def is_trading_day(d):
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS


def next_friday(d):
    """Date of the upcoming Friday (d itself if d is already a Friday)."""
    return d + timedelta(days=(4 - d.weekday()) % 7)


FUTURES_EXPIRY_MONTHS = (3, 6, 9, 12)   # CME quarterly financial futures cycle


def nth_weekday(year, month, weekday, n):
    """Date of the n-th `weekday` (0=Mon..6=Sun) of the given month/year."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def next_futures_expiry(d):
    """Next quarterly ES/NQ expiration (3rd Friday of Mar/Jun/Sep/Dec) on/after d."""
    candidates = [nth_weekday(year, m, 4, 3)
                  for year in (d.year, d.year + 1)
                  for m in FUTURES_EXPIRY_MONTHS]
    return min(c for c in candidates if c >= d)


_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))

# Some hosts (Stooq) gate plain requests behind a JS proof-of-work check:
# a page with a challenge string `c`, a required hex-zero-prefix length `d`,
# and a `/__verify` endpoint that expects the winning nonce `n` such that
# sha256(c + str(n)) starts with `d` zero hex digits. Solving it once and
# keeping the resulting cookie lets subsequent requests through normally.
_CHALLENGE_RE = re.compile(r'c="([^"]+)",d=(\d+)')


def _solve_pow_challenge(html, origin_url, timeout):
    m = _CHALLENGE_RE.search(html)
    if not m:
        return False
    c, d = m.group(1), int(m.group(2))
    target = "0" * d
    n = 0
    while not hashlib.sha256(f"{c}{n}".encode()).hexdigest().startswith(target):
        n += 1
    verify_url = urllib.parse.urljoin(origin_url, "/__verify")
    body = f"c={urllib.parse.quote(c)}&n={n}".encode()
    req = urllib.request.Request(
        verify_url, data=body, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        })
    with _OPENER.open(req, timeout=timeout) as resp:
        resp.read()
    return True


def http_get(url, timeout=30, _retry=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _OPENER.open(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    if _retry and "/__verify" in text and _CHALLENGE_RE.search(text):
        if _solve_pow_challenge(text, url, timeout):
            return http_get(url, timeout=timeout, _retry=False)
    return text


# ---------------- Cboe chain + GEX ----------------
def fetch_chain(sym):
    payload = json.loads(http_get(CBOE_URL.format(sym=sym)))
    data = payload["data"]
    spot = data.get("current_price") or data.get("close")
    return float(spot), data.get("options", [])


def parse_occ(occ):
    strike = int(occ[-8:]) / 1000.0
    cp = occ[-9]
    ymd = occ[-15:-9]
    return (date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])),
            cp, strike)


def load_contracts(options, today):
    out = []
    for o in options:
        try:
            exp, cp, strike = parse_occ(o["option"])
            oi = int(o.get("open_interest") or 0)
            iv = float(o.get("iv") or 0.0)
            gamma = float(o.get("gamma") or 0.0)
        except (KeyError, ValueError, TypeError):
            continue
        dte = (exp - today).days
        if oi <= 0 or dte < 0 or dte > MAX_DTE:
            continue
        out.append({"exp": exp, "cp": cp, "strike": strike,
                    "oi": oi, "iv": iv, "gamma": gamma})
    return out


def contract_gex(gamma, oi, spot):
    return gamma * oi * SHARES_PER_CONTRACT * spot * spot * 0.01


def gex_by_strike(contracts, spot):
    per = {}
    for c in contracts:
        g = contract_gex(c["gamma"], c["oi"], spot)
        d = per.setdefault(c["strike"], {"call": 0.0, "put": 0.0})
        d["call" if c["cp"] == "C" else "put"] += g
    return per


def top_walls(per, side, spot, n=None, min_sep=None):
    """Rank distinct walls for one side ('call' or 'put') by GEX size.

    Keeps up to n strikes, skipping any within min_sep (fraction of spot)
    of an already-kept strike so clustered adjacent strikes collapse into
    one reported wall.
    """
    n = n if n is not None else WALL_COUNT
    min_sep = min_sep if min_sep is not None else MIN_WALL_SEP
    ranked = sorted(per.items(), key=lambda kv: kv[1][side], reverse=True)
    kept = []
    for k, v in ranked:
        if v[side] <= 0:
            break
        if all(abs(k - kk) / spot >= min_sep for kk, _ in kept):
            kept.append((k, v[side]))
        if len(kept) >= n:
            break
    return kept


def max_pain_by_expiry(contracts):
    """Return the max-pain strike for each expiration in the option book."""
    by_expiry = {}
    for expiry in sorted({c["exp"] for c in contracts}):
        expiry_contracts = [c for c in contracts if c["exp"] == expiry]
        strikes = sorted({c["strike"] for c in expiry_contracts})
        losses = []
        for settlement in strikes:
            loss = 0.0
            for c in expiry_contracts:
                intrinsic = (max(settlement - c["strike"], 0.0)
                             if c["cp"] == "C" else
                             max(c["strike"] - settlement, 0.0))
                loss += intrinsic * c["oi"] * SHARES_PER_CONTRACT
            losses.append((loss, settlement))
        if losses:
            loss, strike = min(losses)
            by_expiry[expiry] = {"strike": strike, "loss": loss}
    return by_expiry


def bs_gamma(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return pdf / (S * sigma * math.sqrt(T))


def build_regime(contracts, spot, today, prior, disp):
    """GEX/flip/walls for one bucket of contracts (chain terms in, disp() maps
    strikes/flip to display terms). Isolated so it can run once per expiration
    bucket (near-term vs full book) instead of once per symbol."""
    if not contracts:
        return {"net_gex_str": "n/a", "regime": "unknown",
                "role": "no contracts in this expiration bucket",
                "flip": None, "flip_dist": None,
                "walls": {"call": [], "put": []}, "dispersed": []}

    per = gex_by_strike(contracts, spot)
    net = sum(d["call"] - d["put"] for d in per.values())
    flip = find_flip(spot, contracts, today)
    out = {
        "net_gex": net,
        "net_gex_str": fmt_dollars(net),
        "regime": "positive" if net >= 0 else "negative",
        "flip": disp(flip) if flip else None,
        "flip_dist": round(100 * (spot - flip) / flip, 2) if flip else None,
        "walls": {"call": [], "put": []},
        "dispersed": [],
    }

    for side in ("call", "put"):
        walls = top_walls(per, side, spot)
        if not walls:
            continue
        ratio = (walls[0][1] / walls[1][1]
                 if len(walls) > 1 and walls[1][1] > 0 else None)
        if ratio is not None and ratio < 1.5:
            out["dispersed"].append(side)
        for i, (kk, v) in enumerate(walls):
            off = kk - spot                   # relative %: ratio cancels
            conf = None
            if prior:
                for ref, name in ((prior["h"], "PDH"), (prior["l"], "PDL"),
                                  (prior["c"], "PDC")):
                    if ref > 0 and abs(kk - ref) / spot <= WALL_CONFLUENCE_TOL:
                        conf = name
                        break
            w = {"strike": disp(kk), "gex": v, "gex_str": fmt_dollars(v),
                 "dist": round(100 * off / spot, 2),
                 "lead": round(ratio, 1) if (i == 0 and ratio) else None,
                 "conf": conf}
            out["walls"][side].append(w)

    out["role"] = ("Fade: sell call wall, buy put wall" if net >= 0
                   else "Put wall = break trigger; call wall caps rallies")
    return out


def net_gex_at(S, prepped):
    """prepped: (T, K, iv, cp, oi, real_gamma, anchor_gamma) per contract, where
    anchor_gamma = bs_gamma(spot, K, T, iv) at the real current spot. Scaling
    real_gamma by bs_gamma(S,...)/anchor_gamma means this reproduces the exact
    real-gamma total (the same one `regime` is computed from) at S == spot,
    and extrapolates away from spot using BS gamma's shape - instead of
    substituting a from-scratch BS gamma that can disagree in sign with the
    real chain gamma even at the anchor point."""
    total = 0.0
    for T, K, iv, cp, oi, real_gamma, anchor_gamma in prepped:
        scale = bs_gamma(S, K, T, iv) / anchor_gamma
        g = contract_gex(real_gamma * scale, oi, S)
        total += g if cp == "C" else -g
    return total


def find_flip(spot, contracts, today, span=0.07, steps=71):
    prepped = []
    for c in contracts:
        if c["iv"] <= 0 or c["gamma"] <= 0:
            continue
        T = max((c["exp"] - today).days / 365.0, 0.5 / 365.0)
        anchor = bs_gamma(spot, c["strike"], T, c["iv"])
        if anchor <= 0:
            continue
        prepped.append((T, c["strike"], c["iv"], c["cp"], c["oi"], c["gamma"], anchor))
    if not prepped:
        return None

    lo, hi = spot * (1 - span), spot * (1 + span)
    prev_v = prev_s = None
    crossings = []
    for i in range(steps):
        s = lo + (hi - lo) * i / (steps - 1)
        v = net_gex_at(s, prepped)
        if prev_v is not None and (v >= 0) != (prev_v >= 0):
            frac = abs(prev_v) / (abs(prev_v) + abs(v) + 1e-12)
            crossings.append(prev_s + (s - prev_s) * frac)
        prev_v, prev_s = v, s
    return min(crossings, key=lambda x: abs(x - spot)) if crossings else None


# ---------------- history (prior close + chain-scale ratio only) ----------------
def fetch_rows(stooq_sym, max_rows=PRIOR_SESSION_ROWS):
    """Daily OHLC rows (chronological) from Stooq CSV: Date,O,H,L,C,Volume."""
    text = http_get(STOOQ_URL.format(sym=stooq_sym))
    rows = []
    for line in text.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) >= 5:
            try:
                rows.append({"date": p[0], "o": float(p[1]), "h": float(p[2]),
                             "l": float(p[3]), "c": float(p[4])})
            except ValueError:
                continue
    if len(rows) < 2:
        preview = text.strip().replace("\n", " ")[:200]
        raise ValueError(f"Stooq history too short for {stooq_sym} "
                         f"({len(rows)} rows) - response was: {preview!r}")
    return rows[-max_rows:]


def fetch_closes(stooq_sym, max_rows=PRIOR_SESSION_ROWS):
    return [r["c"] for r in fetch_rows(stooq_sym, max_rows)]


def prior_session(rows, today):
    """Most recent completed session (skips today's partial row if present)."""
    iso = today.isoformat()
    for row in reversed(rows):
        if row["date"] < iso:
            return row
    return None


# ---------------- margin buffer ----------------
def margin_buffer(inst, idx_close, margins_cfg):
    fut = inst["future"]
    margin = float(margins_cfg[fut])
    notional = idx_close * inst["multiplier"]
    pct = margin / notional
    return {"future": fut, "index_close": idx_close, "notional": notional,
            "margin": margin, "pct": pct}


# ---------------- assembly ----------------
def fmt_dollars(x):
    a = abs(x)
    if a >= 1e9:
        return f"{x/1e9:+.2f}B"
    if a >= 1e6:
        return f"{x/1e6:+.1f}M"
    return f"{x:+,.0f}"


def load_margins():
    """Optional [margins] section from config.ini; None if absent/unreadable."""
    try:
        parser = configparser.ConfigParser()
        if parser.read(CONFIG_PATH) and parser.has_section("margins"):
            return parser["margins"]
    except Exception:
        logging.exception("could not read [margins] from config.ini")
    return None


def load_closes():
    """Optional [closes] section: prior futures SETTLEMENT per future, used to
    build the chain->future ratio when the auto Stooq futures pull is missing
    or fails the sanity check. Enter the number you read off your platform:

        [closes]
        NQ = 29300
        ES = 6700
    """
    out = {}
    try:
        parser = configparser.ConfigParser()
        if parser.read(CONFIG_PATH) and parser.has_section("closes"):
            for k, v in parser["closes"].items():
                try:
                    out[k.upper()] = float(v)
                except ValueError:
                    pass
    except Exception:
        logging.exception("could not read [closes] from config.ini")
    return out


# Config via environment (set these in the systemd unit on the Pi):
#   DASH_HOST   bind address. 127.0.0.1 = local only (default, safe).
#               0.0.0.0 = reachable over LAN / tailnet - pair with DASH_TOKEN.
#   DASH_PORT   TCP port (default 8787).
#   DASH_TOKEN  if set, every request must carry this token (?k=... first
#               visit, then a cookie). Leave unset only for localhost use.
HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8787"))
TOKEN = os.environ.get("DASH_TOKEN", "").strip()
REFRESH_SECONDS = 15 * 60

_cache = {"generated": None, "market": "STARTING", "symbols": [],
          "epoch": 0.0, "refresh_seconds": REFRESH_SECONDS}
_lock = threading.Lock()
_wake = threading.Event()


# ---------------- market status ----------------
def market_status(now):
    if now.weekday() >= 5:
        return "WEEKEND PREP"
    if not is_trading_day(now.date()):
        return "HOLIDAY"
    t = now.time()
    if t < dtime(9, 30):
        return "PRE-MARKET"
    if t <= dtime(16, 0):
        return "OPEN"
    return "AFTER HOURS"


# ---------------- per-symbol structured compute ----------------
def compute_symbol(inst, margins_cfg, closes_cfg):
    """Compute one instrument, then map every level to FUTURES terms with a
    single ratio m = prior_future_close / prior_chain_close (real closes; folds
    ETF tracking + the multiplicative futures basis into one number). Future
    close: auto from Stooq (sanity-checked vs the index ratio to reject
    back-adjusted continuous series), else config [closes], else index terms."""
    future = inst["future"]
    today = now_et().date()
    spot, options = fetch_chain(inst["chain"])   # SPX~6400 or QQQ~570
    contracts = load_contracts(options, today)

    # chain-scale prior close/session: feeds the chain->future ratio and PDH/
    # PDL/PDC wall confluence. Isolated from GEX/walls/flip below, which come
    # entirely from the Cboe chain - a Stooq outage should degrade the ratio
    # scaling and confluence tags, not blank the panel.
    try:
        rows = fetch_rows(inst["hist"])
        prior = prior_session(rows, today)
        chain_close = rows[-1]["c"]
    except Exception as exc:
        logging.exception("history fetch failed for %s (%s)", future, inst["hist"])
        prior, chain_close = None, None

    # prior cash-index close: margin notional + the tracking-ratio sanity anchor
    idx_close = None
    try:
        idx_close = fetch_closes(inst["index"], max_rows=5)[-1]
    except Exception:
        logging.exception("index close fetch failed for %s (%s)",
                          future, inst["index"])
    k_track = (idx_close / chain_close) if (idx_close and chain_close) else 1.0

    # futures/chain ratio m: auto Stooq future close -> config -> index terms
    m, fut_close, ratio_src = k_track, None, f"index terms (set [closes] {future})"
    try:
        fc = fetch_closes(inst["fut"], max_rows=5)[-1]
        carry = fc / chain_close / k_track - 1.0     # implied future/index carry
        if -0.01 <= carry <= 0.04:                   # plausible front-month carry
            fut_close, ratio_src = fc, f"auto {inst['fut']}"
        else:
            logging.info("%s: rejected auto %s close %.2f (implied carry %.3f)",
                         future, inst["fut"], fc, carry)
    except Exception:
        logging.info("%s: auto futures close %s unavailable", future, inst["fut"])
    if fut_close is None and future in closes_cfg:
        fut_close, ratio_src = closes_cfg[future], f"config [closes] {future}"
    if fut_close is not None and chain_close:
        m = fut_close / chain_close

    def disp(x):                                     # chain scale -> futures
        return round(x * m, 2)

    scale_note = (f"{inst['chain'].lstrip('_')} x {m:.3f} -> {future} ({ratio_src})"
                  if fut_close is not None else
                  f"{inst['chain'].lstrip('_')} x {m:.3f} -> {future} "
                  f"INDEX TERMS ({ratio_src})")

    out = {"symbol": future, "future": future, "chain": inst["chain"],
           "ratio": round(m, 4),
           "fut_close": round(fut_close, 2) if fut_close else None,
           "scale_note": scale_note, "ok": True, "error": None,
           "spot": disp(spot), "regimes": {}}

    # GEX + walls, split into two expiration buckets (internal math in chain
    # terms; strikes/flip shifted to display terms via disp()):
    #   near = contracts through the next quarterly ES/NQ futures expiration
    #          (the front-month contract dealers are actually hedging with)
    #   full = everything through MAX_DTE, as before
    if contracts:
        near_cutoff = next_futures_expiry(today)
        near_contracts = [c for c in contracts if c["exp"] <= near_cutoff]
        out["regimes"]["near"] = build_regime(near_contracts, spot, today, prior, disp)
        out["regimes"]["near"]["label"] = f"Near-term (thru {near_cutoff.strftime('%m/%d')} fut exp)"
        out["regimes"]["full"] = build_regime(contracts, spot, today, prior, disp)
        out["regimes"]["full"]["label"] = f"Full book (thru {MAX_DTE}d)"
        out["max_pain"] = [
            {"expiry": expiry.isoformat(), "strike": disp(item["strike"]),
             "dist": round(100 * (item["strike"] - spot) / spot, 2),
             "loss_str": fmt_dollars(item["loss"])}
            for expiry, item in max_pain_by_expiry(contracts).items()
        ]
    else:
        out["error_note"] = "no usable option contracts returned (verify chain is free)"
        out["max_pain"] = []

    # margin band (pct is scale-free; band built in chain terms then disp'd)
    if margins_cfg is not None and idx_close:
        try:
            mb = margin_buffer(inst, idx_close, margins_cfg)
            out["margin"] = {
                "future": mb["future"], "pct": round(mb["pct"] * 100, 2),
                "band_lo": disp(spot * (1 - mb["pct"])),
                "band_hi": disp(spot * (1 + mb["pct"])),
                "notional": round(mb["notional"]),
            }
        except Exception as exc:
            out["margin"] = {"error": repr(exc)}
    else:
        out["margin"] = {"error": "config.ini [margins] not loaded"
                         if margins_cfg is None else "no index close for notional"}
    return out


def recompute():
    now = now_et()
    status = market_status(now)
    margins_cfg = load_margins()
    closes_cfg = load_closes()

    # Instruments are independent (different symbols/URLs) and each one is
    # network-bound (Cboe chain + 3 Stooq calls), so run them concurrently
    # instead of paying for 12 sequential round-trips per cycle.
    syms = [None] * len(INSTRUMENTS)

    def run_one(i, inst):
        try:
            syms[i] = compute_symbol(inst, margins_cfg, closes_cfg)
            logging.info("dashboard %s OK", inst["future"])
        except Exception as exc:
            logging.exception("dashboard %s failed", inst["future"])
            syms[i] = {"symbol": inst["future"], "ok": False, "error": repr(exc)}

    threads = [threading.Thread(target=run_one, args=(i, inst))
               for i, inst in enumerate(INSTRUMENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with _lock:
        _cache["generated"] = now.strftime("%Y-%m-%d %H:%M:%S ET")
        _cache["market"] = status
        _cache["symbols"] = syms
        _cache["epoch"] = time.time()


def worker():
    while True:
        try:
            recompute()
        except Exception:
            logging.exception("recompute loop error")
        _wake.wait(timeout=REFRESH_SECONDS)
        _wake.clear()


def snapshot_json():
    with _lock:
        data = dict(_cache)
    elapsed = time.time() - data["epoch"] if data["epoch"] else 0
    data["seconds_to_refresh"] = max(0, int(REFRESH_SECONDS - elapsed))
    data["data_age_seconds"] = int(elapsed)
    return json.dumps(data)


# ---------------- HTML (served at /) ----------------
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gamma Terminal</title>
<style>
:root{
  --bg:#0b0f17; --panel:#131b28; --raised:#1a2434; --line:rgba(255,255,255,.08);
  --ink:#e7ecf4; --muted:#7f8 da0; --muted:#7f8da0;
  --brass:#d9a441; --jade:#4bbf8a; --verm:#e0603f; --stress:#ff5d63;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);
  font-family:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  font-size:14px;line-height:1.4;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:20px 18px 60px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:14px 20px;
  padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:22px}
h1{font-family:system-ui,sans-serif;font-weight:800;font-size:19px;
  letter-spacing:.14em;text-transform:uppercase;margin:0}
h1 .g{color:var(--brass)}
.status{display:flex;align-items:center;gap:8px;font-size:12px;letter-spacing:.12em;
  text-transform:uppercase}
.dot{width:8px;height:8px;border-radius:50%;background:var(--jade)}
.dot.pulse{animation:pulse 2.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.status.closed .dot{background:var(--muted)}
.status.weekend .dot{background:var(--brass)}
.meta{margin-left:auto;display:flex;gap:18px;align-items:baseline;
  font-size:12px;color:var(--muted)}
.meta b{color:var(--ink);font-weight:600}
button{font:inherit;font-size:12px;color:var(--ink);background:var(--raised);
  border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer;
  letter-spacing:.08em;text-transform:uppercase}
button:hover{border-color:var(--brass);color:var(--brass)}
button:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:18px 20px;margin-bottom:18px}
.phead{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.sym{font-family:system-ui,sans-serif;font-weight:800;font-size:22px;letter-spacing:.06em}
.spot{font-size:20px;font-weight:600}
.regime{font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  padding:3px 9px;border-radius:5px;border:1px solid transparent}
.regime.positive{color:var(--jade);border-color:rgba(75,191,138,.4);background:rgba(75,191,138,.08)}
.regime.negative{color:var(--verm);border-color:rgba(224,96,63,.4);background:rgba(224,96,63,.08)}
.regime.unknown,.regime.STARTING{color:var(--muted);border-color:var(--line)}
.role{font-size:12px;color:var(--muted);margin-left:auto}
.scale{font-size:10.5px;color:var(--brass);letter-spacing:.05em;
  margin:-6px 0 12px;opacity:.85}
.regimes{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.regimes{grid-template-columns:1fr}}
.regime-block{background:var(--raised);border:1px solid var(--line);
  border-radius:10px;padding:14px 16px}
.rhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.rlabel{font-family:system-ui,sans-serif;font-weight:700;font-size:11.5px;
  letter-spacing:.05em;color:var(--ink)}
.grid{display:grid;grid-template-columns:150px 1fr;gap:18px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
.stat{margin-bottom:11px}
.stat .k{font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.stat .v{font-size:17px;font-weight:600}
.v.pos{color:var(--jade)}.v.neg{color:var(--verm)}
.flash{animation:flash 1s ease-out}
@keyframes flash{from{background:rgba(217,164,65,.25)}to{background:transparent}}
/* range rail */
.rail-wrap{padding:6px 2px}
.rail-scale{display:flex;justify-content:space-between;font-size:10px;
  color:var(--muted);margin-bottom:6px}
.rail{position:relative;height:64px;border-radius:8px;
  background:linear-gradient(180deg,var(--raised),#141d2b);
  border:1px solid var(--line);overflow:hidden}
.band{position:absolute;top:0;bottom:0}
.band.margin{border-left:1px dashed rgba(255,255,255,.28);
  border-right:1px dashed rgba(255,255,255,.28)}
.tick{position:absolute;top:0;bottom:0;width:2px;transform:translateX(-1px)}
.tick.spot{background:var(--ink);width:2px;z-index:6}
.tick.flip{background:var(--brass);z-index:5}
.tick.wall{z-index:4}
.tick.wall.call{background:rgba(75,191,138,.9)}
.tick.wall.put{background:rgba(224,96,63,.9)}
.lab{position:absolute;font-size:9px;letter-spacing:.04em;white-space:nowrap;
  transform:translateX(-50%);color:var(--muted)}
.lab.top{top:3px}.lab.bot{bottom:3px}
.lab.spot{color:var(--ink)}.lab.flip{color:var(--brass)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:10px;color:var(--muted);
  margin-top:9px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;
  margin-right:5px;vertical-align:middle}
/* walls */
.walls{margin-top:16px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:500;padding:4px 8px;border-bottom:1px solid var(--line)}
td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,.04)}
td.num{text-align:right}
.side-c{color:var(--jade)}.side-p{color:var(--verm)}
.chip{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;
  padding:1px 6px;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.chip.conf{color:var(--ink);border-color:rgba(255,255,255,.25)}
.max-pain{margin-top:16px;padding-top:12px;border-top:1px solid var(--line)}
.max-pain .k{font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);
  margin-bottom:5px}
.eff{margin-top:12px;font-size:12.5px}
.eff b{color:var(--brass);letter-spacing:.08em}
.thin{color:var(--stress)}
.mrow{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-top:13px;
  padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.err{color:var(--stress);font-size:12.5px}
.foot{margin-top:26px;font-size:11px;color:var(--muted);line-height:1.7}
@media(prefers-reduced-motion:reduce){.dot.pulse{animation:none}.flash{animation:none}}
</style></head>
<body><div class="wrap">
<header>
  <h1>Gamma<span class="g">/</span>Terminal</h1>
  <div id="status" class="status"><span class="dot pulse"></span><span id="mkt">--</span></div>
  <div class="meta">
    <span>updated <b id="upd">--</b></span>
    <span>next refresh <b id="cd">--</b></span>
    <button id="refresh">Refresh now</button>
  </div>
</header>
<div id="panels"></div>
<div class="foot" id="foot"></div>
</div>
<script>
const REFRESH_SECONDS = __REFRESH_SECONDS__;
const SNAPSHOT_DATA = __SNAPSHOT_DATA__;
let secs=REFRESH_SECONDS, last = {};

function fmtCd(s){const m=Math.floor(s/60),x=s%60;return m+":"+String(x).padStart(2,"0");}
function pct(v,lo,hi){return hi>lo?100*(v-lo)/(hi-lo):50;}
function clamp(x){return Math.max(0,Math.min(100,x));}

function rail(s,r){
  const m=s.margin||{};
  const strikes=[...(r.walls.call||[]),...(r.walls.put||[])].map(w=>+w.strike);
  let lo=Math.min(s.spot,...strikes), hi=Math.max(s.spot,...strikes);
  if(!(hi>lo)){ lo=s.spot*0.95; hi=s.spot*1.05; }
  if(m.band_lo!==undefined){lo=Math.min(lo,m.band_lo);hi=Math.max(hi,m.band_hi);}
  const pad=(hi-lo)*0.06; lo-=pad; hi+=pad;
  const L=(v)=>clamp(pct(v,lo,hi));
  let html=`<div class="rail-wrap"><div class="rail-scale"><span>${lo.toFixed(1)}</span><span>${hi.toFixed(1)}</span></div><div class="rail">`;
  if(m.band_lo!==undefined)
    html+=`<div class="band margin" style="left:${L(m.band_lo)}%;right:${100-L(m.band_hi)}%"></div>`;
  // walls (only in-domain)
  for(const side of ["call","put"]) (r.walls[side]||[]).forEach(w=>{
    if(w.strike<lo||w.strike>hi) return;
    html+=`<div class="tick wall ${side}" style="left:${L(w.strike)}%"></div>`;
    html+=`<div class="lab bot" style="left:${L(w.strike)}%">${(+w.strike).toFixed(0)}</div>`;
  });
  if(r.flip!==null&&r.flip!==undefined&&r.flip>=lo&&r.flip<=hi){
    html+=`<div class="tick flip" style="left:${L(r.flip)}%"></div>`;
    html+=`<div class="lab top flip" style="left:${L(r.flip)}%">flip</div>`;
  }
  html+=`<div class="tick spot" style="left:${L(s.spot)}%"></div>`;
  html+=`<div class="lab top spot" style="left:${L(s.spot)}%">spot ${s.spot.toFixed(2)}</div>`;
  html+=`</div>`;
  html+=`<div class="legend"><span><i style="background:var(--ink)"></i>spot</span>`
      +`<span><i style="background:var(--brass)"></i>flip</span>`;
  if(m.band_lo!==undefined) html+=`<span>┊ margin band</span>`;
  html+=`</div></div>`;
  return html;
}

function wallRows(list, side){
  if(!list||!list.length) return "";
  return list.map(w=>{
    let chips="";
    if(w.lead) chips+=`<span class="chip">${w.lead>=2?"dominant":"lead"} x${w.lead}</span>`;
    if(w.conf) chips+=` <span class="chip conf">~${w.conf}</span>`;
    return `<tr><td class="side-${side[0]}">${side.toUpperCase()}</td>`
      +`<td class="num">${(+w.strike).toFixed(0)}</td>`
      +`<td class="num">${w.gex_str}</td>`
      +`<td class="num">${w.dist>0?"+":""}${w.dist}%</td>`
      +`<td>${chips}</td></tr>`;
  }).join("");
}

function regimeBlock(s,key,r){
  const gexClass=r.regime==="positive"?"pos":r.regime==="negative"?"neg":"";
  let eff = r.dispersed&&r.dispersed.length
     ? `<div class="eff thin">Dispersed gamma (${r.dispersed.join("/")}) — no single dominant wall. Lean on volume profile / prior levels or stand down.</div>`
     : "";
  return `<div class="regime-block">
    <div class="rhead">
      <span class="rlabel">${r.label||key}</span>
      <span class="regime ${r.regime}">${r.regime} gamma</span>
      <span class="role">${r.role||""}</span>
    </div>
    <div class="grid">
      <div class="stats">
        <div class="stat"><div class="k">Net GEX</div><div class="v ${gexClass}" data-k="${s.symbol}-${key}-gex">${r.net_gex_str}</div></div>
        <div class="stat"><div class="k">Flip</div><div class="v">${r.flip!==null&&r.flip!==undefined?r.flip.toFixed(2):"—"}${r.flip_dist!==null&&r.flip_dist!==undefined?` <span style="font-size:12px;color:var(--muted)">(${r.flip_dist>0?"+":""}${r.flip_dist}%)</span>`:""}</div></div>
      </div>
      <div>
        ${rail(s,r)}
        <div class="walls"><table><thead><tr><th>Side</th><th class="num">Strike</th><th class="num">GEX</th><th class="num">Dist</th><th>Tags</th></tr></thead>
        <tbody>${wallRows(r.walls.call,"call")}${wallRows(r.walls.put,"put")}</tbody></table></div>
        ${eff}
      </div>
    </div>
  </div>`;
}

function panel(s){
  if(!s.ok) return `<div class="panel"><div class="phead"><span class="sym">${s.symbol}</span></div><div class="err">error: ${s.error}</div></div>`;
  const m=s.margin||{};
  let marg="";
  if(m.error){ marg=`<div class="mrow">margin: ${m.error}</div>`; }
  else if(m.pct!==undefined){
    marg=`<div class="mrow"><span>${m.future} margin covers <b style="color:var(--ink)">${m.pct}%</b> → band ${m.band_lo}–${m.band_hi}</span></div>`;
  }
  const regimes=s.regimes||{};
  const maxPain=(s.max_pain||[]).length
    ? `<div class="max-pain"><div class="k">Max pain by expiry</div><table><thead><tr><th>Expiry</th><th class="num">Level</th><th class="num">Dist</th><th class="num">Open-interest loss</th></tr></thead><tbody>`
      +(s.max_pain||[]).map(p=>`<tr><td>${p.expiry}</td><td class="num">${(+p.strike).toFixed(2)}</td><td class="num">${p.dist>0?"+":""}${p.dist}%</td><td class="num">${p.loss_str}</td></tr>`).join("")
      +`</tbody></table></div>`
    : "";
  const body = (regimes.near||regimes.full)
    ? `<div class="regimes">`
      +(regimes.near?regimeBlock(s,"near",regimes.near):"")
      +(regimes.full?regimeBlock(s,"full",regimes.full):"")
      +`</div>`
    : `<div class="err">${s.error_note||"no usable option contracts returned"}</div>`;
  return `<div class="panel">
    <div class="phead">
      <span class="sym">${s.symbol}</span>
      <span class="spot" data-k="${s.symbol}-spot">${s.spot.toFixed(2)}</span>
    </div>
    ${s.scale_note?`<div class="scale">levels: ${s.scale_note}</div>`:""}
    ${body}
    ${maxPain}
    ${marg}
  </div>`;
}

async function load(){
  try{
    const d = SNAPSHOT_DATA || await fetch("/api/data",{cache:"no-store"}).then(res=>res.json());
    secs=d.seconds_to_refresh;
    document.getElementById("mkt").textContent=d.market;
    const st=document.getElementById("status");
    st.className="status"+(d.market==="OPEN"?"":d.market.startsWith("WEEKEND")?" weekend":" closed");
    document.getElementById("upd").textContent=d.generated||"—";
    if(SNAPSHOT_DATA) document.getElementById("refresh").style.display="none";
    document.getElementById("panels").innerHTML=(d.symbols||[]).map(panel).join("");
    // change flash
    (d.symbols||[]).forEach(s=>{
      if(!s.ok)return;
      for(const key of ["near","full"]){
        const r=(s.regimes||{})[key]; if(!r) continue;
        const gk=`${s.symbol}-${key}-gex`;
        if(last[gk]!==undefined&&last[gk]!==r.net_gex_str){
          const el=document.querySelector(`[data-k="${gk}"]`); if(el)el.classList.add("flash");
        }
        last[gk]=r.net_gex_str;
      }
    });
    document.getElementById("foot").innerHTML=
      `Data ~15&nbsp;min delayed · open interest is T-1 · index close proxies the futures close.`
      +` Server recomputes every ${Math.round(REFRESH_SECONDS/60)}&nbsp;min; margin numbers come from your config.ini and need manual upkeep.`;
  }catch(e){ document.getElementById("mkt").textContent="server unreachable"; }
}
function tick(){ secs=Math.max(0,secs-1); document.getElementById("cd").textContent=fmtCd(secs);
  if(secs<=0){ secs=REFRESH_SECONDS; } }
document.getElementById("refresh").addEventListener("click",async()=>{
  await fetch("/refresh",{method:"POST"}); setTimeout(load,600); });
load(); setInterval(load,30000); setInterval(tick,1000);
</script>
</body></html>"""
PAGE = PAGE.replace("__REFRESH_SECONDS__", str(REFRESH_SECONDS))
PAGE = PAGE.replace("__SNAPSHOT_DATA__", "null")
# stray-char guard from hand-authored CSS var line
PAGE = PAGE.replace("--muted:#7f8 da0;", "")


def render_snapshot(path):
    recompute()
    data = snapshot_json()
    page = PAGE.replace("const SNAPSHOT_DATA = null;",
                        f"const SNAPSHOT_DATA = {data};")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(page, encoding="utf-8")


# ---------------- HTTP handler ----------------
UNAUTH = ("<!doctype html><meta charset=utf-8><title>Gamma Terminal</title>"
          "<body style='font-family:system-ui;background:#0b0f17;color:#e7ecf4;"
          "padding:40px'><h2>Unauthorized</h2><p>Open this dashboard with your "
          "access token appended, e.g. <code>?k=YOUR_TOKEN</code>. It is then "
          "remembered on this device.</p></body>")


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("k", [None])[0] == TOKEN:
            return True
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            part = part.strip()
            if part.startswith("dash=") and part[5:] == TOKEN:
                return True
        return False

    def _send(self, code, body, ctype, set_cookie=False):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie and TOKEN:
            self.send_header("Set-Cookie",
                             f"dash={TOKEN}; HttpOnly; SameSite=Lax; Max-Age=2592000")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._send(401, UNAUTH, "text/html; charset=utf-8")
            return
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8", set_cookie=True)
        elif path == "/api/data":
            self._send(200, snapshot_json(), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._authed():
            self._send(401, '{"error":"unauthorized"}', "application/json")
            return
        if self.path.split("?")[0] == "/refresh":
            _wake.set()
            self._send(200, '{"ok":true}', "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):
        logging.info("http %s", fmt % args)


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    # ThreadingHTTPServer inherits allow_reuse_address=True, which on Windows
    # (unlike Linux) lets a second process bind the SAME port concurrently
    # instead of failing - two silent instances then each run their own
    # worker/cache on their own clock, and requests get routed unpredictably
    # between them (symptom: the refresh countdown jumps around erratically).
    # Disabling it makes a duplicate launch fail loudly at startup instead.
    allow_reuse_address = False


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", metavar="PATH",
                        help="write a static GitHub Pages-compatible HTML snapshot")
    args = parser.parse_args()
    if args.snapshot:
        render_snapshot(args.snapshot)
        print(f"Snapshot written to {args.snapshot}")
        return
    try:
        server = SingleInstanceHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"Could not bind {HOST}:{PORT} - is the dashboard already "
              f"running in another window? ({exc})")
        return
    threading.Thread(target=worker, daemon=True).start()
    shown = HOST if HOST not in ("0.0.0.0",) else "<pi-tailscale-ip>"
    url = f"http://{shown}:{PORT}"
    hint = f"{url}/?k={TOKEN}" if TOKEN else url
    print(f"Gamma Terminal on {HOST}:{PORT}  ->  open {hint}   (Ctrl+C to stop)")
    if HOST == "0.0.0.0" and not TOKEN:
        print("WARNING: bound to all interfaces with no DASH_TOKEN set.")
    logging.info("dashboard server started on %s:%s (token=%s)",
                   HOST, PORT, "yes" if TOKEN else "no")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
