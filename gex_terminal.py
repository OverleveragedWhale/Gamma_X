#!/usr/bin/env python3
"""
gex_terminal.py - single-file live GEX + margin dashboard (no email).

Fetches Cboe delayed chains for SPY/QQQ; computes net GEX, the gamma
flip, scored call/put walls, and a futures-margin buffer (Yahoo is used only
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
import calendar
import configparser
import http.cookiejar
import urllib.error
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
from urllib.parse import urlparse, parse_qs, quote
# ---------------- settings ----------------
MAX_DTE = 95                 # GEX: ignore contracts beyond this many days
NEAR_MAX_DTE = 32            # near-term bucket: never look further than this
MAX_PAIN_DAILY_DAYS = 5      # max pain: every expiry inside this many days,
                             # and nothing but monthly opex beyond it
SHARES_PER_CONTRACT = 100
WALL_COUNT = 6               # ranked walls reported per side
MIN_WALL_SEP = 0.004         # min gap between reported walls (0.4% of spot)
WALL_CONFLUENCE_TOL = 0.0015  # wall within 0.15% of prior H/L/C = confluence
PRIOR_SESSION_ROWS = 5        # daily bars kept for prior close + chain-scale ratio
VOL_LOOKBACK = 20             # daily bars behind the realized-vol estimate
Z_99, Z_999 = 2.3263, 3.0902  # two-sided normal quantiles for 99% / 99.9%
BAND_DIVISOR = 2.0            # bands reported halved: margin is bi-directional
# Confidence levels for the liquidation bands. The multiplier used to be the
# tail probability itself - 0.01 at 99%, 0.001 at 99.9% - which inverted the
# relationship: a deeper tail came out a TENTH the width when it has to be
# wider, because reaching further into the tail takes a bigger move. The 99%
# leg is the anchor (LIQ_BASE_SHARE, hand-checked against the margin chain) and
# other levels scale by the ratio of normal quantiles, the same Z values the
# realized-vol bands under "risk" use, so the two band families stay comparable.
LIQ_BASE_SHARE = 0.01                            # share of margin at 99%
LIQ_LEVELS = (("99%", Z_99), ("99.9%", Z_999))   # label -> normal quantile
if any(a[1] >= b[1] for a, b in zip(LIQ_LEVELS, LIQ_LEVELS[1:])):
    raise ValueError("LIQ_LEVELS must ascend in quantile: a wider confidence "
                     "level has to produce a wider band, not a narrower one")
TICKS_PER_POINT = 4           # ES and NQ both quote in quarter points

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{sym}?range=1mo&interval=1d")
# The v8 chart feed carries no open interest. v7 quote does, for futures, and
# takes a whole basket in one call - but only with a cookie+crumb pair, which
# is a free unauthenticated handshake, not a login.
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_QUOTE_URL = ("https://query1.finance.yahoo.com/v7/finance/quote"
                   "?symbols={syms}&crumb={crumb}")

# GEX source per tradeable. SPX index options are free-with-greeks, so ES uses
# the native cash-index chain. Cboe's free feed does NOT carry NDX greeks,
# so NQ uses the ETF chain (QQQ). Every level is mapped to FUTURES terms
# by a single multiplicative ratio from real PRIOR CLOSES:
#     m = prior_future_close / prior_chain_close      (level_future = strike * m)
# m folds ETF tracking and the futures basis (which is itself ~multiplicative)
# into one empirical number. The future close is auto-fetched from Yahoo
# (sanity-checked against the index ratio), overridable via config [closes].
#   chain = Cboe options ticker    hist = Yahoo chain-scale prior close (m)
#   index = Yahoo cash index (margin notional + tracking sanity)
#   fut   = Yahoo futures symbol for the prior settle
#   cycle = which futures expiration calendar the near-term bucket follows
# GC and CL have no free cash-index feed (XAUUSD=X is gone), so their `index`
# is the futures symbol itself. That makes margin notional the futures notional
# - correct, there being no cash index to convert from - but it also makes the
# carry sanity gate in compute_symbol vacuous (carry is 0 by construction), so
# a back-adjusted continuous series would not be caught for those two.
INSTRUMENTS = [
    {"future": "ES",  "chain": "_SPX", "hist": "^GSPC", "index": "^GSPC",
     "fut": "ES=F",  "multiplier": 50,   "cycle": "quarterly", "exch": ".CME"},
    {"future": "NQ",  "chain": "QQQ",  "hist": "QQQ",   "index": "^NDX",
     "fut": "NQ=F",  "multiplier": 20,   "cycle": "quarterly", "exch": ".CME"},
    # Commodity books ride an ETF chain the same way NQ rides QQQ, but the
    # proxy is looser: see the ratio-noise note in compute_symbol.
    {"future": "GC",  "chain": "GLD",  "hist": "GLD",   "index": "GC=F",
     "fut": "GC=F",  "multiplier": 100,  "cycle": "gc",        "exch": ".CMX"},
    {"future": "CL",  "chain": "USO",  "hist": "USO",   "index": "CL=F",
     "fut": "CL=F",  "multiplier": 1000, "cycle": "cl",        "exch": ".NYM"},
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

# INFO stays in gex_span.log, but WARNING and above also go to stderr, which the
# desktop publisher redirects into publish.log. Without this a feed regression -
# the OI lookup quietly dropping to a fallback, say - would only ever be written
# to a file nobody opens, and the dashboard would keep printing plausible
# numbers derived the wrong way.
_stderr_handler = logging.StreamHandler()
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
logging.getLogger().addHandler(_stderr_handler)

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
FUTURES_ROLL_DAYS = 3     # roll to the deferred contract this many days out


def nth_weekday(year, month, weekday, n):
    """Date of the n-th `weekday` (0=Mon..6=Sun) of the given month/year."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def shift_business_days(d, n):
    """Move n trading days from d (negative goes back), skipping holidays."""
    step = 1 if n > 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_trading_day(d):
            remaining -= 1
    return d


def last_business_day(year, month):
    d = date(year, month, calendar.monthrange(year, month)[1])
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def quarterly_expiry(year, month):
    """ES/NQ: 3rd Friday of the delivery month."""
    return nth_weekday(year, month, 4, 3)


def gc_expiry(year, month):
    """GC: third last business day of the delivery month."""
    return shift_business_days(last_business_day(year, month), -2)


def cl_expiry(year, month):
    """CL: 3 business days before the 25th of the month preceding delivery.

    When the 25th is not a business day the rule counts back from the business
    day preceding it instead, which is what the step-back loop below gives.
    """
    year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    anchor = date(year, month, 25)
    while not is_trading_day(anchor):
        anchor -= timedelta(days=1)
    return shift_business_days(anchor, -3)


# Delivery months and the termination rule for each cycle. GC lists only the
# even months, which is where essentially all of its open interest sits.
EXPIRY_CYCLES = {
    "quarterly": (FUTURES_EXPIRY_MONTHS, quarterly_expiry),
    "gc":        ((2, 4, 6, 8, 10, 12), gc_expiry),
    "cl":        (tuple(range(1, 13)), cl_expiry),
}


def next_futures_expiry(d, cycle="quarterly"):
    """Front futures expiration for `cycle` as of d.

    Rolls to the next contract once the near one is within FUTURES_ROLL_DAYS,
    so the near-term bucket tracks the contract dealers are actually hedging
    with rather than one whose open interest is being closed out.
    """
    months, rule = EXPIRY_CYCLES[cycle]
    # CL terminates in the month *before* delivery, so start a year back to
    # catch a December termination belonging to a January delivery.
    candidates = [rule(year, m)
                  for year in (d.year - 1, d.year, d.year + 1)
                  for m in months]
    return min(c for c in candidates if (c - d).days > FUTURES_ROLL_DAYS)


MONTH_CODES = "FGHJKMNQUVXZ"      # Jan..Dec, the standard futures month letters
CONTRACT_CANDIDATES = 3           # how many contracts deep to compare on volume


def contract_symbol(inst, year, month):
    """Yahoo symbol for one delivery month, e.g. GCZ26.CMX."""
    return f"{inst['future']}{MONTH_CODES[month - 1]}{year % 100:02d}{inst['exch']}"


def contract_candidates(inst, d, n=CONTRACT_CANDIDATES):
    """The next n contracts on this instrument's cycle, nearest first.

    Each entry is (last trade date, delivery year, delivery month, symbol).
    Contracts already inside FUTURES_ROLL_DAYS are dropped, matching the date
    rule this list is compared against.
    """
    months, rule = EXPIRY_CYCLES[inst["cycle"]]
    out = sorted({(rule(y, m), y, m)
                  for y in (d.year, d.year + 1)
                  for m in months
                  if (rule(y, m) - d).days > FUTURES_ROLL_DAYS})[:n]
    return [(exp, y, m, contract_symbol(inst, y, m)) for exp, y, m in out]


_CRUMB = None


def yahoo_crumb(refresh=False):
    """Cookie + crumb pair the v7 quote endpoint wants. Cached per process."""
    global _CRUMB
    if refresh:
        _CRUMB = None
    if _CRUMB is None:
        try:
            http_get(YAHOO_COOKIE_URL)
        except Exception:
            pass          # this URL 404s; the Set-Cookie on the way past is the point
        _CRUMB = http_get(YAHOO_CRUMB_URL).strip()
    return _CRUMB


def contract_stats(symbols):
    """Open interest and session volume for several futures contracts at once.

    One request covers every candidate across every instrument, so adding a
    contract to compare costs nothing. A stale crumb comes back as a 401, which
    is worth exactly one silent retry with a fresh one.
    """
    if not symbols:
        return {}
    for attempt in (0, 1):
        url = YAHOO_QUOTE_URL.format(syms=quote(",".join(symbols)),
                                     crumb=quote(yahoo_crumb(refresh=bool(attempt))))
        try:
            rows = json.loads(http_get(url))["quoteResponse"]["result"]
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or attempt:
                raise
    return {r["symbol"]: {"oi": r.get("openInterest"),
                          "volume": r.get("regularMarketVolume"),
                          "price": r.get("regularMarketPrice")}
            for r in rows if r.get("symbol")}


def active_contract(inst, d, stats=None):
    """Pick the front contract by open interest, not by proximity to expiry.

    The nearest contract is not always the one being held. Gold is the clear
    case: Oct carries 43k open against Dec's 314k, because metals liquidity
    concentrates in a few delivery months rather than rolling evenly, so a date
    rule would anchor the near-term bucket to a contract almost nobody holds.

    Open interest is preferred over volume because it measures positions rather
    than turnover, and it does not light up in both legs at once during a roll
    week. Volume is the fallback when a contract reports no OI, and the date
    rule the fallback when the whole lookup fails.
    """
    candidates = contract_candidates(inst, d)
    if stats is None:
        try:
            stats = contract_stats([c[3] for c in candidates])
        except Exception:
            logging.exception("%s: contract stats unavailable", inst["future"])
            stats = {}

    rows = []
    for exp, year, month, sym in candidates:
        st = stats.get(sym) or {}
        rows.append({"symbol": sym, "expiry": exp.isoformat(),
                     "oi": st.get("oi"), "volume": st.get("volume"),
                     "label": f"{calendar.month_abbr[month]} {year % 100:02d}"})

    for key, source in (("oi", "open interest"), ("volume", "volume")):
        if any(r[key] for r in rows):
            if key != "oi":
                logging.warning("%s: no open interest, ranked on %s instead - "
                                "the Yahoo v7 quote feed may have changed",
                                inst["future"], source)
            # Ties go to the nearer contract, which is the order rows are in.
            idx = max(range(len(rows)), key=lambda i: rows[i][key] or -1)
            for i, r in enumerate(rows):
                r["active"] = (i == idx)
            return {"expiry": date.fromisoformat(rows[idx]["expiry"]),
                    "source": source, "chosen": rows[idx], "candidates": rows}

    logging.warning("%s: no OI and no volume for any candidate contract - "
                    "falling back to the date rule", inst["future"])
    return {"expiry": next_futures_expiry(d, inst["cycle"]),
            "source": "date rule (no OI or volume)",
            "chosen": None, "candidates": rows}


_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _OPENER.open(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
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


def is_monthly_opex(d):
    """True for a standard monthly expiration: the third Friday of the month.

    The third Friday is the only one that can land between the 15th and the
    21st, so the day-of-month range identifies it without counting weeks.
    """
    return d.weekday() == 4 and 15 <= d.day <= 21


def max_pain_by_expiry(contracts, today):
    """Max-pain strike per expiration, for the expirations worth showing.

    A full chain carries 30+ expirations, most of them thinly traded dailies
    months out that pin nothing. Kept here: every expiry within
    MAX_PAIN_DAILY_DAYS days, plus monthly opex - the two that actually have
    the open interest behind them. Filtering before the loop rather than after
    also means no max pain is computed for a row that gets thrown away.

    Open-interest loss at settlement S is sum((S-K)*oi) over calls struck at or
    below S, plus sum((K-S)*oi) over puts struck at or above S. Dropping the
    positive part turns each side into S*sum(oi) - sum(K*oi), so one ascending
    pass and one descending pass price every candidate strike - rather than
    rescanning the expiration's contracts once per strike.
    """
    by_expiry = {}
    for c in contracts:
        exp = c["exp"]
        if (exp - today).days <= MAX_PAIN_DAILY_DAYS or is_monthly_opex(exp):
            by_expiry.setdefault(exp, []).append(c)

    out = {}
    for expiry in sorted(by_expiry):
        call_oi, put_oi = {}, {}
        for c in by_expiry[expiry]:
            side = call_oi if c["cp"] == "C" else put_oi
            side[c["strike"]] = side.get(c["strike"], 0) + c["oi"]
        strikes = sorted(set(call_oi) | set(put_oi))
        if not strikes:
            continue

        call_loss, oi_sum, koi_sum = [], 0.0, 0.0
        for k in strikes:
            oi = call_oi.get(k, 0)
            oi_sum += oi
            koi_sum += k * oi
            call_loss.append(k * oi_sum - koi_sum)

        put_loss, oi_sum, koi_sum = [0.0] * len(strikes), 0.0, 0.0
        for i in range(len(strikes) - 1, -1, -1):
            k = strikes[i]
            oi = put_oi.get(k, 0)
            oi_sum += oi
            koi_sum += k * oi
            put_loss[i] = koi_sum - k * oi_sum

        # min over (loss, strike) keeps the original tie-break: lowest strike.
        loss, strike = min(((call_loss[i] + put_loss[i]) * SHARES_PER_CONTRACT, k)
                           for i, k in enumerate(strikes))
        out[expiry] = {"strike": strike, "loss": loss,
                       "monthly": is_monthly_opex(expiry)}
    return out


def bs_gamma(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return pdf / (S * sigma * math.sqrt(T))


def build_regime(contracts, spot, today, prior, disp, ref_spot=None):
    """GEX/flip/walls for one bucket of contracts (chain terms in, disp() maps
    strikes/flip to display terms). Isolated so it can run once per expiration
    bucket (near-term vs full book) instead of once per symbol.

    ref_spot (chain terms, defaults to spot) is the price the reported
    distances are measured from. It is separate from spot because the chain
    underlying stops printing outside its cash session while the future keeps
    trading: the levels stay where the settled book put them, and "how far is
    that from here" is answered from wherever the future is now.
    """
    if ref_spot is None:
        ref_spot = spot
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
        "flip_dist": round(100 * (ref_spot - flip) / flip, 2) if flip else None,
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
            off = kk - ref_spot               # relative %: ratio cancels
            conf = None
            if prior:
                for ref, name in ((prior["h"], "PDH"), (prior["l"], "PDL"),
                                  (prior["c"], "PDC")):
                    if ref > 0 and abs(kk - ref) / spot <= WALL_CONFLUENCE_TOL:
                        conf = name
                        break
            w = {"strike": disp(kk), "gex": v, "gex_str": fmt_dollars(v),
                 "dist": round(100 * off / ref_spot, 2),
                 "lead": round(ratio, 1) if (i == 0 and ratio) else None,
                 "conf": conf}
            out["walls"][side].append(w)

    out["role"] = ("Fade: sell call wall, buy put wall" if net >= 0
                   else "Put wall = break trigger; call wall caps rallies")
    return out


_EXP_MAX = 700.0     # math.exp raises OverflowError past ~709


def find_flip(spot, contracts, today, span=0.07, steps=71):
    """Price nearest spot where net GEX changes sign, or None.

    Each contract's real chain gamma is scaled by bs_gamma(S)/bs_gamma(spot),
    so the curve reproduces the exact real-gamma total at S == spot (the same
    one `regime` reports) and extrapolates away from it with BS gamma's shape,
    rather than substituting a from-scratch BS gamma that can disagree in sign
    with the chain even at the anchor point.

    Because the scale is a ratio of two gammas, almost everything cancels.
    With vt = iv*sqrt(T), a = ln(K) - vt*vt/2, d1(S) = (ln(S) - a)/vt, and
    A = d1(spot):

        bs_gamma(S)/bs_gamma(spot) = exp((A*A - d1*d1)/2) * spot/S
        net(S) = S * sum(base * exp((A*A - d1*d1)/2))
        base   = +/- gamma * oi * SHARES_PER_CONTRACT * 0.01 * spot

    base, a and A are fixed per contract and ln(S) is shared across a step, so
    a step costs one exp per contract rather than a log, two sqrts and two
    function calls - interpreter overhead being most of the bill on a small
    ARM core. Keeping the two gammas together as one exponent also avoids
    forming bs_gamma(spot) alone, which underflows for far-OTM short-dated
    contracts and turned the ratio into inf or nan.
    """
    lo, hi = spot * (1 - span), spot * (1 + span)
    if lo <= 0:
        return None
    ln_spot, ln_lo, ln_hi = math.log(spot), math.log(lo), math.log(hi)

    prepped = []
    for c in contracts:
        iv, gamma = c["iv"], c["gamma"]
        if iv <= 0 or gamma <= 0:
            continue
        T = max((c["exp"] - today).days / 365.0, 0.5 / 365.0)
        K = c["strike"]
        if bs_gamma(spot, K, T, iv) <= 0:
            continue
        vt = iv * math.sqrt(T)
        inv_vt = 1.0 / vt
        a = math.log(K) - 0.5 * vt * vt
        a2 = ((ln_spot - a) * inv_vt) ** 2
        # The exponent peaks where ln(S) sits closest to a, so its largest
        # value over the scan is known here. Contracts that would overflow
        # exp() are ones whose anchor gamma is denormal; the ratio they stand
        # for is not representable either way.
        near = min(max(a, ln_lo), ln_hi)
        if 0.5 * (a2 - ((near - a) * inv_vt) ** 2) > _EXP_MAX:
            continue
        base = gamma * c["oi"] * SHARES_PER_CONTRACT * 0.01 * spot
        prepped.append((a, inv_vt, a2, base if c["cp"] == "C" else -base))

    if not prepped:
        return None

    exp_, log_ = math.exp, math.log
    prev_v = prev_s = None
    crossings = []
    for i in range(steps):
        s = lo + (hi - lo) * i / (steps - 1)
        ln_s = log_(s)
        acc = 0.0
        for a, inv_vt, a2, base in prepped:
            d1 = (ln_s - a) * inv_vt
            acc += base * exp_(0.5 * (a2 - d1 * d1))
        v = s * acc
        if prev_v is not None and (v >= 0) != (prev_v >= 0):
            frac = abs(prev_v) / (abs(prev_v) + abs(v) + 1e-12)
            crossings.append(prev_s + (s - prev_s) * frac)
        prev_v, prev_s = v, s
    return min(crossings, key=lambda x: abs(x - spot)) if crossings else None


# ---------------- history (prior close + chain-scale ratio only) ----------------
def fetch_rows(sym, max_rows=PRIOR_SESSION_ROWS):
    """Daily OHLC rows (chronological) from the Yahoo chart API.

    Bar timestamps are epoch seconds at the exchange's session start, so the
    meta gmtoffset converts them to the exchange's own calendar date; taking
    the UTC date instead rolls a futures session onto the wrong day and makes
    prior_session pick the wrong bar. A month is requested so a run of
    holidays still leaves PRIOR_SESSION_ROWS usable bars.
    """
    text = http_get(YAHOO_URL.format(sym=quote(sym)))
    try:
        result = json.loads(text)["chart"]["result"][0]
        stamps = result["timestamp"]
        q = result["indicators"]["quote"][0]
        off = result.get("meta", {}).get("gmtoffset") or 0
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        preview = text.strip()[:200]
        raise ValueError(f"Yahoo history unusable for {sym} ({exc!r}) - "
                         f"response was: {preview!r}") from exc

    rows = []
    for n, ts in enumerate(stamps):
        bar = (q["open"][n], q["high"][n], q["low"][n], q["close"][n])
        if any(v is None for v in bar):           # holidays come back as nulls
            continue
        rows.append({"date": datetime.utcfromtimestamp(ts + off).date().isoformat(),
                     "o": float(bar[0]), "h": float(bar[1]),
                     "l": float(bar[2]), "c": float(bar[3])})
    if len(rows) < 2:
        raise ValueError(f"Yahoo history too short for {sym} ({len(rows)} rows)")
    return rows[-max_rows:]


def fetch_closes(sym, max_rows=PRIOR_SESSION_ROWS):
    return [r["c"] for r in fetch_rows(sym, max_rows)]


def realized_sigma(rows):
    """Stdev of daily log returns from a close series, or None if too short."""
    closes = [r["c"] for r in rows if r["c"] > 0]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 3:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def prior_session(rows, today):
    """Most recent completed session (skips today's partial row if present)."""
    iso = today.isoformat()
    for row in reversed(rows):
        if row["date"] < iso:
            return row
    return None


# ---------------- margin buffer ----------------
def _margin_leg(margins_cfg, fut, side):
    """Margin for one side, falling back to a single symmetric value."""
    for key in (f"{fut}_{side}", fut):
        if key in margins_cfg:
            return float(margins_cfg[key])
    raise KeyError(f"no margin for {fut} ({side.lower()} leg)")


def liq_factor(multiplier):
    """Per-instrument scaling that puts every symbol on the same share of margin.

    The chain the panel prints is

        avg x share / TICKS_PER_POINT x liq_factor / BAND_DIVISOR

    which equals (margin in points) x share x liq_factor x multiplier / 8.
    Setting liq_factor = 100 / multiplier collapses that to a flat 12.5% of
    margin at the 99% level for any contract size, which is the whole point of
    the number - it is NOT a tick value. It lands on 5.0 for NQ, matching its
    $5.00 tick purely by coincidence, and on 2.0 for ES, 1.0 for GC and 0.1 for
    CL against ticks of $12.50, $10.00 and $10.00. Do not "correct" it.
    """
    return 100.0 / multiplier


def margin_buffer(inst, idx_close, margins_cfg):
    """Margin per contract in index points, up and down legs kept apart.

    A futures contract's P&L is points * multiplier, so margin / multiplier is
    exactly how many points the market can move against one contract before the
    posted margin is gone. Sizing the band off index notional instead measures
    the move against the cash index and then draws it on a futures axis,
    stretching the band by the basis.

    `compare` is the requested cross-check, reported beside the band rather
    than drawn. The two legs are averaged, taken at 1% for the 99%-liquidated
    level, then converted from ticks to points at TICKS_PER_POINT. The inputs
    are kept in the payload so the figure can be audited without re-deriving
    it. Realized sigma is not part of this - it sizes the confidence bands
    reported separately under "risk", though both families now step between
    confidence levels on the same normal quantiles.
    """
    fut = inst["future"]
    up = _margin_leg(margins_cfg, fut, "UP")
    down = _margin_leg(margins_cfg, fut, "DOWN")
    mult = inst["multiplier"]
    avg = (up + down) / 2.0
    tick = liq_factor(mult)
    # avg -> share at this level -> ticks to points -> liq_factor -> halved,
    # since the posted margin covers moves in both directions. The share is the
    # 99% anchor stretched by this level's quantile, so 99% is unchanged and
    # every wider level comes out wider.
    liq = []
    for label, z in LIQ_LEVELS:
        share = LIQ_BASE_SHARE * z / Z_99
        liq.append({"label": label, "share": round(share, 6), "z": z,
                    "points": avg * share / TICKS_PER_POINT * tick / BAND_DIVISOR})
    return {"future": fut, "index_close": idx_close,
            "notional": idx_close * mult, "liq_factor": tick,
            "margin_up": up, "margin_down": down, "margin_avg": avg,
            "points_up": up / mult, "points_down": down / mult,
            "liq": liq, "compare": avg * 0.01 / TICKS_PER_POINT}


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
    build the chain->future ratio when the auto Yahoo futures pull is missing
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
REFRESH_SECONDS = 30 * 60

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
    close: auto from Yahoo (sanity-checked vs the index ratio to reject
    back-adjusted continuous series), else config [closes], else index terms."""
    future = inst["future"]
    today = now_et().date()
    spot, options = fetch_chain(inst["chain"])   # SPX~6400 or QQQ~570
    contracts = load_contracts(options, today)

    # Resolve the front contract BEFORE the ratio, because the ratio has to be
    # built from that same contract. inst["fut"] is a continuous front-month
    # series: it rolls, and across a roll its prior close and its live print
    # are different deliveries, so m ends up carrying the basis of a contract
    # nobody is looking at. Measured on the 2026-09-21 roll, that put the
    # expiring September basis into ES and NQ and October's into CL, wrong by
    # -0.72%, -1.01% and +4.39% respectively.
    stats = {}
    try:
        stats = contract_stats([c[3] for c in contract_candidates(inst, today)]
                               + [inst["fut"]])
    except Exception:
        logging.exception("%s: quote lookup failed", future)
    try:
        active = active_contract(inst, today, stats=stats or None)
    except Exception:
        logging.exception("active contract lookup failed for %s", future)
        active = {"expiry": next_futures_expiry(today, inst["cycle"]),
                  "source": "date rule (lookup failed)",
                  "chosen": None, "candidates": []}
    ratio_sym = (active["chosen"] or {}).get("symbol") or inst["fut"]

    # Every leg of the ratio is the most recent COMPLETED session, never the bar
    # in progress. Yahoo returns today's partial bar as the newest row, and
    # taking it made m drift through the day - worst over a weekend, when
    # futures reopen Sunday evening against an ETF that last traded Friday, so
    # the numerator moved while the denominator could not. Freezing both on the
    # same settled session means every level converts at one fixed number for
    # the whole day: spot still moves, the axis it is drawn on does not.
    try:
        rows = fetch_rows(inst["hist"])
        prior = prior_session(rows, today)
        chain_close = prior["c"] if prior else None
    except Exception as exc:
        logging.exception("history fetch failed for %s (%s)", future, inst["hist"])
        prior, chain_close = None, None

    # prior cash-index close: margin notional + the tracking-ratio sanity anchor
    # GC and CL have no free cash index, so their "index" IS the futures
    # symbol. Point those at the same contract the ratio uses, or k_track is
    # built from the continuous series while fc is not, and the carry gate
    # measures the roll gap instead of the basis - which is how it rejected
    # CL's correct close at -4.21% and fell back to index terms. Where a real
    # cash index exists this is unchanged.
    idx_sym = ratio_sym if inst["index"] == inst["fut"] else inst["index"]
    idx_close = None
    try:
        idx_prior = prior_session(fetch_rows(idx_sym, max_rows=5), today)
        idx_close = idx_prior["c"] if idx_prior else None
    except Exception:
        logging.exception("index close fetch failed for %s (%s)", future, idx_sym)
    k_track = (idx_close / chain_close) if (idx_close and chain_close) else 1.0

    # futures/chain ratio m: the front contract's own prior close -> config
    # -> index terms
    m, fut_close, ratio_src = k_track, None, f"index terms (set [closes] {future})"
    fut_rows, fut_completed = [], []
    for sym in ([ratio_sym, inst["fut"]] if ratio_sym != inst["fut"]
                else [inst["fut"]]):
        try:
            # Same bars feed the ratio, the realized-vol estimate and the
            # liquidation anchor below - all three want the contract actually
            # being held, and a specific contract has no roll step to inflate
            # sigma either.
            fut_rows = fetch_rows(sym, max_rows=VOL_LOOKBACK + 2)
            fut_completed = [r for r in fut_rows if r["date"] < today.isoformat()]
            fc = fut_completed[-1]["c"]
        except Exception:
            logging.info("%s: futures history %s unavailable", future, sym)
            continue
        carry = fc / chain_close / k_track - 1.0     # implied future/index carry
        if -0.01 <= carry <= 0.04:                   # plausible front-month carry
            fut_close, ratio_src = fc, f"auto {sym}"
        else:
            logging.info("%s: rejected auto %s close %.2f (implied carry %.3f)",
                         future, sym, fc, carry)
        break
    if fut_close is None and future in closes_cfg:
        fut_close, ratio_src = closes_cfg[future], f"config [closes] {future}"
    if fut_close is not None and chain_close:
        m = fut_close / chain_close
    # The session both legs are pinned to, so the panel can show it.
    ratio_date = prior["date"] if prior else None

    def disp(x):                                     # chain scale -> futures
        return round(x * m, 2)

    asof = f", {ratio_date} closes" if ratio_date else ""
    scale_note = (f"{inst['chain'].lstrip('_')} x {m:.3f} -> {future} "
                  f"({ratio_src}{asof})"
                  if fut_close is not None else
                  f"{inst['chain'].lstrip('_')} x {m:.3f} -> {future} "
                  f"INDEX TERMS ({ratio_src}{asof})")

    # One quote call covers both the live futures print and the contract
    # ranking further down. inst["fut"] rides along because it is the series m
    # was built from, so the live price and fut_close can never end up coming
    # from different contracts.
    # The chain underlying stops printing when its cash session closes while
    # the future keeps trading, which left spot pinned to the prior settle for
    # most of the day. Take spot from the live future when there is one, and
    # measure distances from it; the levels themselves still come from the
    # settled book, and m still converts them at the prior close.
    live = (stats.get(ratio_sym) or {}).get("price")
    live = float(live) if isinstance(live, (int, float)) and live > 0 else None
    shown_spot = round(live, 2) if live else disp(spot)
    ref_spot = live / m if live else spot          # back to chain terms
    spot_src = f"live {ratio_sym}" if live else f"{inst['chain'].lstrip('_')} x {m:.3f}"

    out = {"symbol": future, "future": future, "chain": inst["chain"],
           "ratio": round(m, 4),
           "fut_close": round(fut_close, 2) if fut_close else None,
           "scale_note": scale_note, "ratio_date": ratio_date,
           "ok": True, "error": None,
           "spot": shown_spot, "spot_src": spot_src,
           "chain_spot": disp(spot), "regimes": {}}

    # GEX + walls, split into two expiration buckets (internal math in chain
    # terms; strikes/flip shifted to display terms via disp()):
    #   near = contracts through the next expiration on this instrument's own
    #          futures cycle (the front-month contract dealers are actually
    #          hedging with), but never more than NEAR_MAX_DTE out. Quarterly
    #          spacing is ~91 days against a 95 day book, so without the cap
    #          the bucket would hold almost the whole chain for most of the
    #          quarter and the two panels would report identical numbers. CL
    #          is monthly, so for it the cycle date binds and the cap rarely
    #          does; GC alternates between the two.
    #   full = everything through MAX_DTE, as before
    # Which contract the near-term bucket belongs to, chosen on traded volume
    # rather than nearness to expiry - see active_contract.
    out["contract"] = {
        "label": (active["chosen"] or {}).get("label"),
        "symbol": (active["chosen"] or {}).get("symbol"),
        "expiry": active["expiry"].isoformat(),
        "source": active["source"],
        "degraded": active["source"] != "open interest",
        "date_rule": next_futures_expiry(today, inst["cycle"]).isoformat(),
        "candidates": active["candidates"],
    }

    if contracts:
        fut_exp = active["expiry"]
        near_cutoff = min(fut_exp, today + timedelta(days=NEAR_MAX_DTE))
        near_contracts = [c for c in contracts if c["exp"] <= near_cutoff]
        out["regimes"]["near"] = build_regime(near_contracts, spot, today, prior,
                                             disp, ref_spot)
        label_contract = (active["chosen"] or {}).get("label") or "fut"
        out["regimes"]["near"]["label"] = (
            f"Near-term (thru {near_cutoff.strftime('%m/%d')} {label_contract})"
            if near_cutoff == fut_exp else
            f"Near-term (thru {near_cutoff.strftime('%m/%d')}, {NEAR_MAX_DTE}d cap)")
        out["regimes"]["full"] = build_regime(contracts, spot, today, prior,
                                             disp, ref_spot)
        out["regimes"]["full"]["label"] = f"Full book (thru {MAX_DTE}d)"
        out["max_pain"] = [
            {"expiry": expiry.isoformat(), "strike": disp(item["strike"]),
             "dist": round(100 * (item["strike"] - ref_spot) / ref_spot, 2),
             "loss_str": fmt_dollars(item["loss"]),
             "monthly": item["monthly"]}
            for expiry, item in max_pain_by_expiry(contracts, today).items()
        ]
    else:
        out["error_note"] = "no usable option contracts returned (verify chain is free)"
        out["max_pain"] = []

    # Confidence bands from the future's own realized vol, drawn about the
    # previous session's close so the reference is fixed for the whole day
    # rather than sliding with spot.
    # Futures reopen Sunday evening, so the newest bar is routinely an
    # in-progress session. Including it puts a partial move - and the weekend
    # gap - into the vol estimate, which inflated sigma by ~6%. fut_completed
    # is the same settled-bars list the chain->future ratio is pinned to.
    completed = fut_completed
    sigma = realized_sigma(completed)
    prior_fut = completed[-1] if completed else None
    if sigma and prior_fut:
        anchor = prior_fut["c"]
        out["risk"] = {
            "anchor": round(anchor, 2), "anchor_date": prior_fut["date"],
            "sigma_pct": round(100 * sigma, 3), "bars": len(completed),
            "bands": [
                {"label": label, "z": z,
                 "points": round(anchor * z * sigma / BAND_DIVISOR, 2),
                 "lo": round(anchor * (1 - z * sigma / BAND_DIVISOR), 2),
                 "hi": round(anchor * (1 + z * sigma / BAND_DIVISOR), 2)}
                for label, z in (("99%", Z_99), ("99.9%", Z_999))
            ],
        }
    else:
        out["risk"] = {"error": "not enough futures history for a vol estimate"}

    # margin band, in points either side of the future - the axis the walls,
    # flip and spot are already drawn on, so the band is directly comparable
    if margins_cfg is not None and idx_close:
        try:
            mb = margin_buffer(inst, idx_close, margins_cfg)
            fut_spot = shown_spot
            out["margin"] = {
                "future": mb["future"],
                "points_up": round(mb["points_up"], 2),
                "points_down": round(mb["points_down"], 2),
                "band_lo": round(fut_spot - mb["points_down"], 2),
                "band_hi": round(fut_spot + mb["points_up"], 2),
                "margin_up": round(mb["margin_up"]),
                "margin_down": round(mb["margin_down"]),
                "compare": round(mb["compare"], 2),
                "compare_avg": round(mb["margin_avg"]),
                "liq_factor": mb["liq_factor"],
                "liq_anchor": (round(prior_fut["c"], 2) if prior_fut else None),
                "liq": [
                    {"label": b["label"], "points": round(b["points"], 2),
                     "lo": (round(prior_fut["c"] - b["points"], 2)
                            if prior_fut else None),
                     "hi": (round(prior_fut["c"] + b["points"], 2)
                            if prior_fut else None)}
                    for b in mb["liq"]
                ],
                "notional": round(mb["notional"]),
                "set_at": margins_cfg.get("set_at") or "date not recorded",
            }
        except KeyError as exc:
            out["margin"] = {"error": f"{exc.args[0]} - set it in config.ini"}
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
    # network-bound (Cboe chain + 3 Yahoo calls), so run them concurrently
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
<meta name="gx-digest" content="__GX_DIGEST__">
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
/* walls */
.walls{margin-top:16px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:500;padding:4px 8px;border-bottom:1px solid var(--line)}
td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,.04)}
td.num{text-align:right}
.side-c{color:var(--jade)}.side-p{color:var(--verm)}
tr.spotrow td{color:var(--ink);font-weight:700;letter-spacing:.06em;
  background:rgba(255,255,255,.05);
  border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.chip{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;
  padding:1px 6px;border-radius:4px;border:1px solid var(--line);color:var(--muted)}
.chip.conf{color:var(--ink);border-color:rgba(255,255,255,.25)}
.max-pain{margin-top:16px;padding-top:12px;border-top:1px solid var(--line)}
.active-con{font-size:10px;letter-spacing:.1em;padding:2px 7px;margin-left:10px;
  border:1px solid var(--jade);border-radius:3px;color:var(--jade);vertical-align:middle}
.con-on{color:var(--jade)}
.con-off{opacity:.5}
.con-warn{color:var(--brass);opacity:.85}
.max-pain .sub{letter-spacing:.04em;text-transform:none;opacity:.6;font-weight:400}
.max-pain tr.opex td{color:var(--ink)}
.max-pain .tag{font-size:9px;letter-spacing:.1em;padding:1px 5px;margin-left:6px;
  border:1px solid var(--line);border-radius:3px;color:var(--muted);vertical-align:middle}
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
    <span><span id="cdlab">next refresh</span> <b id="cd">--</b></span>
    <button id="refresh">Refresh now</button>
  </div>
</header>
<div id="panels"></div>
<div class="foot" id="foot"></div>
</div>
<script>
const REFRESH_SECONDS = __REFRESH_SECONDS__;
const SNAPSHOT_DATA = __SNAPSHOT_DATA__;
const MAX_PAIN_DAILY_DAYS = __MAX_PAIN_DAILY_DAYS__;
// A published snapshot has its figures baked in so it renders standalone, and
// also polls data.json, which the publisher writes beside index.html. That is
// what lets a static page follow later snapshots without the viewer reloading.
const IS_SNAPSHOT = SNAPSHOT_DATA !== null;
const POLL_MS = 60000;
let secs=REFRESH_SECONDS, last = {}, dataEpoch = null;

function fmtCd(s){const m=Math.floor(s/60),x=s%60;return m+":"+String(x).padStart(2,"0");}

function wallRow(w){
  let chips="";
  if(w.lead) chips+=`<span class="chip">${w.lead>=2?"dominant":"lead"} x${w.lead}</span>`;
  if(w.conf) chips+=` <span class="chip conf">~${w.conf}</span>`;
  return `<tr><td class="side-${w.side[0]}">${w.side.toUpperCase()}</td>`
    +`<td class="num">${(+w.strike).toFixed(0)}</td>`
    +`<td class="num">${w.gex_str}</td>`
    +`<td class="num">${w.dist>0?"+":""}${w.dist}%</td>`
    +`<td>${chips}</td></tr>`;
}

// A price ladder: every wall sits where its strike actually falls, so calls and
// puts interleave and the SPOT row lands in its own place rather than acting as
// a divider between two books. Strictly descending by strike, which means the
// distance from spot grows as you read away from the spot row in either
// direction. Walls are still *chosen* by GEX size in the payload; this only
// decides the order they appear in, and it sorts a copy because the payload
// array is re-rendered on every poll.
function wallLadder(walls, spot){
  const rows=[];
  for(const side of ["call","put"])
    ((walls&&walls[side])||[]).forEach(w=>rows.push(Object.assign({},w,{side})));
  if(!rows.length) return "";
  // Equal strikes read call-then-put, so a level carrying both is consistent.
  rows.sort((a,b)=>(+b.strike)-(+a.strike) || (a.side==="call"?-1:1));
  if(spot==null) return rows.map(wallRow).join("");
  const above=rows.filter(w=>+w.strike>+spot);
  const below=rows.filter(w=>+w.strike<=+spot);
  return above.map(wallRow).join("")+spotRow(spot)+below.map(wallRow).join("");
}

// Divider between the call and put blocks, carrying spot itself so the two
// sides are read against the level they are measured from.
function spotRow(spot){
  if(spot==null) return "";
  return `<tr class="spotrow"><td>SPOT</td>`
    +`<td class="num">${(+spot).toFixed(2)}</td>`
    +`<td class="num">—</td><td class="num">0.00%</td><td></td></tr>`;
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
        <div class="walls"><table><thead><tr><th>Side</th><th class="num">Strike</th><th class="num">GEX</th><th class="num">Dist</th><th>Tags</th></tr></thead>
        <tbody>${wallLadder(r.walls,s.spot)}</tbody></table></div>
        ${eff}
      </div>
    </div>
  </div>`;
}

function panel(s){
  if(!s.ok) return `<div class="panel"><div class="phead"><span class="sym">${s.symbol}</span></div><div class="err">error: ${s.error}</div></div>`;
  const m=s.margin||{};
  let marg="";
  const rk=s.risk||{};
  if(rk.bands){
    marg+=`<div class="mrow"><span>realized σ <b style="color:var(--ink)">${rk.sigma_pct}%</b>/day`
        +` over ${rk.bars} bars, about the ${rk.anchor_date} close ${rk.anchor} → `
        +rk.bands.map(b=>`${b.label} ±${b.points} (${b.lo}–${b.hi})`).join(" · ")
        +`</span></div>`;
  } else if(rk.error){ marg+=`<div class="mrow">risk bands: ${rk.error}</div>`; }
  if(m.error){ marg+=`<div class="mrow">margin: ${m.error}</div>`; }
  else if(m.points_up!==undefined){
    marg+=`<div class="mrow"><span>${m.future} margin `
        +`<b style="color:var(--ink)">+${m.points_up}/−${m.points_down} pts</b>`
        +` → band ${m.band_lo}–${m.band_hi}`
        +` · set ${m.set_at}</span></div>`;
    if((m.liq||[]).length&&m.liq[0].points!==undefined){
      marg+=`<div class="mrow"><span>liquidation, off the ${m.liq_anchor} close: `
          +m.liq.map(b=>`<b style="color:var(--jade)">${b.label} ±${b.points}</b>`
                       +` (${b.lo}–${b.hi})`).join(" · ")
          +` <span style="opacity:.7">avg ${m.compare_avg} × `
          +m.liq.map(b=>b.share).join(" / ")
          +` ÷ 4 × ${m.liq_factor} ÷ 2</span>`
          +`</span></div>`;
    }
  }
  const regimes=s.regimes||{};
  const maxPain=(s.max_pain||[]).length
    ? `<div class="max-pain"><div class="k">Max pain by expiry `
      +`<span class="sub">next ${MAX_PAIN_DAILY_DAYS}d + monthly opex</span></div>`
      +`<table><thead><tr><th>Expiry</th><th class="num">Level</th><th class="num">Dist</th><th class="num">Open-interest loss</th></tr></thead><tbody>`
      +(s.max_pain||[]).map(p=>`<tr${p.monthly?' class="opex"':''}><td>${p.expiry}${p.monthly?' <span class="tag">OPEX</span>':''}</td><td class="num">${(+p.strike).toFixed(2)}</td><td class="num">${p.dist>0?"+":""}${p.dist}%</td><td class="num">${p.loss_str}</td></tr>`).join("")
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
      ${contractTag(s.contract)}
    </div>
    ${contractRow(s.contract)}
    ${s.scale_note?`<div class="scale">levels: ${s.scale_note}</div>`:""}
    ${body}
    ${maxPain}
    ${marg}
  </div>`;
}

function contractTag(c){
  if(!c||!c.label) return "";
  return `<span class="active-con" title="front contract by ${c.source}">${c.label}</span>`;
}

// The runners-up are shown too: on a normal day one contract carries nearly all
// the volume, and seeing the gap is what tells you the pick is unambiguous.
function contractRow(c){
  if(!c||!(c.candidates||[]).length) return "";
  const rolled = c.date_rule && c.date_rule!==c.expiry;
  // Rank on whichever measure actually drove the pick, so the highlighted
  // contract is always the largest number shown.
  const key = c.source==="volume" ? "volume" : "oi";
  const list = c.candidates.map(r=>{
    const n = r[key]==null ? "n/a" : (+r[key]).toLocaleString();
    return r.active
      ? `<b class="con-on">${r.label} ${n}</b>`
      : `<span class="con-off">${r.label} ${n}</span>`;
  }).join(" · ");
  const by = c.degraded
    ? `<b class="con-warn"> · by ${c.source} — OI feed down</b>`
    : `<span style="opacity:.7"> · by ${c.source}</span>`;
  return `<div class="scale">contract: ${list}${by}`
       + (rolled?` <span class="con-warn">date rule would use ${c.date_rule}</span>`:"")
       + `</div>`;
}

async function fetchData(){
  if(!IS_SNAPSHOT) return await fetch("/api/data",{cache:"no-store"}).then(res=>res.json());
  // Unique query param gets past the Pages CDN. Falling back to the baked-in
  // copy keeps the page working on first paint and when opened off disk.
  try{
    const res = await fetch("data.json?t="+Date.now(),{cache:"no-store"});
    if(res.ok) return await res.json();
  }catch(e){}
  return SNAPSHOT_DATA;
}

async function load(){
  try{
    const d = await fetchData();
    secs=d.seconds_to_refresh;
    dataEpoch=d.epoch||null;
    document.getElementById("mkt").textContent=d.market;
    const st=document.getElementById("status");
    st.className="status"+(d.market==="OPEN"?"":d.market.startsWith("WEEKEND")?" weekend":" closed");
    document.getElementById("upd").textContent=d.generated||"—";
    document.getElementById("cdlab").textContent=IS_SNAPSHOT?"data age":"next refresh";
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
      +(IS_SNAPSHOT
        ? ` Page re-checks for a new snapshot every ${Math.round(POLL_MS/1000)}s.`
        : ` Server recomputes every ${Math.round(REFRESH_SECONDS/60)}&nbsp;min.`)
      +` Margin numbers come from your config.ini and need manual upkeep.`;
  }catch(e){ document.getElementById("mkt").textContent="server unreachable"; }
}
function tick(){
  const cd=document.getElementById("cd");
  if(IS_SNAPSHOT){
    // Count up from the recompute behind this snapshot, so a stalled publisher
    // shows as growing age instead of hiding behind a fake countdown.
    if(dataEpoch) cd.textContent=fmtCd(Math.max(0,Math.floor(Date.now()/1000-dataEpoch)));
    return;
  }
  secs=Math.max(0,secs-1); cd.textContent=fmtCd(secs);
  if(secs<=0){ secs=REFRESH_SECONDS; } }
document.getElementById("refresh").addEventListener("click",async()=>{
  if(IS_SNAPSHOT){ await load(); return; }
  await fetch("/refresh",{method:"POST"}); setTimeout(load,600); });
load(); setInterval(load,IS_SNAPSHOT?POLL_MS:30000); setInterval(tick,1000);
</script>
</body></html>"""
PAGE = PAGE.replace("__REFRESH_SECONDS__", str(REFRESH_SECONDS))
PAGE = PAGE.replace("__MAX_PAIN_DAILY_DAYS__", str(MAX_PAIN_DAILY_DAYS))
PAGE = PAGE.replace("__SNAPSHOT_DATA__", "null")
# stray-char guard from hand-authored CSS var line
PAGE = PAGE.replace("--muted:#7f8 da0;", "")


_VOLATILE_KEYS = ("generated", "epoch", "seconds_to_refresh", "data_age_seconds")
_DIGEST_RE = re.compile(r'name="gx-digest" content="([0-9a-f]*)"')


def payload_digest(data):
    """Fingerprint of the numbers on the page, ignoring when it was rendered.

    Lets a caller poll faster than the upstream feed refreshes without
    republishing: the Cboe endpoint is delayed ~15 min and open interest is
    T-1, so most runs recompute to byte-identical figures under a new
    timestamp, which would otherwise look like a change to git.
    """
    stable = {k: v for k, v in data.items() if k not in _VOLATILE_KEYS}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def render_snapshot(path, skip_unchanged=False):
    """Write the static snapshot and the data sidecar beside it.

    index.html carries a baked-in copy so it renders on its own; data.json is
    what the published page polls to pick up later snapshots without a reload.
    Returns True if the files were written.
    """
    recompute()
    data = snapshot_json()
    digest = payload_digest(json.loads(data))
    target = Path(path)
    sidecar = target.with_name("data.json")

    # Require the sidecar too, or the first run after it was introduced would
    # match on digest and never create it.
    if skip_unchanged and target.exists() and sidecar.exists():
        try:
            found = _DIGEST_RE.search(target.read_text(encoding="utf-8"))
        except OSError:
            found = None
        if found and found.group(1) == digest:
            return False

    page = PAGE.replace("const SNAPSHOT_DATA = null;",
                        f"const SNAPSHOT_DATA = {data};")
    page = page.replace("__GX_DIGEST__", digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    sidecar.write_text(data, encoding="utf-8")
    return True


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
    parser.add_argument("--skip-unchanged", action="store_true",
                        help="with --snapshot, leave the file alone when the "
                             "figures match what it already holds")
    args = parser.parse_args()
    if args.snapshot:
        if render_snapshot(args.snapshot, skip_unchanged=args.skip_unchanged):
            print(f"Snapshot written to {args.snapshot}")
        else:
            print("Snapshot unchanged, left as is")
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
