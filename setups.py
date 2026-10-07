#!/usr/bin/env python3
"""Session setups: a 09:50 map frozen once a day, one event per wall and setup,
and a forward-test log of how each one ended. Writes setups.json and
setups.html beside the dashboard's index.html.

    python setups.py --pub ../Gamma_X-snapshot

Why a separate page, and why events
-----------------------------------
The dashboard's plans were rebuilt from scratch on every update - "the nearest
wall right now" - so as price moved, setups appeared, vanished and came back:
noise in real time, too little if only read once a day. They also offered
trades the scorecard never measured: it scores ONE fade per wall per session,
on the first touch, and ONE breakout, on the first 5-minute close through.

So this page does exactly that and no more:

  map     frozen once per session from the first clean snapshot at or after
          09:50 (after Cboe's routine 09:45 outage): the nearest walls each
          side - the same walls, count and book the scorecard measured - and
          the session's gamma regime. Every symbol carries both setups as the
          trader's RULES (fixed 1:1 points, tools/score_levels.RULE_POINTS),
          each with its base rate in every regime and a verdict for today's;
          ES is shown in full but marked reference, not traded.
  events  each run replays the session's COMPLETED 5-minute futures bars from
          09:50 against that map, so a touch between two runs is never missed.
          Every wall/setup goes watching -> armed (within one stop) ->
          triggered -> won / lost / closed at 16:00, once.
  record  every resolved setup is appended to a forward-test history, so the
          page's live record can be set against the scorecard's backtest.

Two sessions run side by side, as tabs: the DAY above, and the EVENING - a
map frozen from the first clean snapshot after the 18:00 Globex roll, setups
run to 03:00 ET, base rates from the scorecard's evening trades.

Entry and the bar rules are the scorecard's own (tools/score_levels.py), and
the base rates are its score_rules() - the same trades, measured - so the page
never shows a trade nobody measured.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import gex_terminal as g          # noqa: E402
import score_levels as sl         # noqa: E402

MAP_AT = "09:50"
CLOSE = "16:00"
BAR_MIN = 5
BARS_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
            "{sym}?range=5d&interval=5m&includePrePost=true")
SETUPS = ("fade", "breakout")

# Two sessions, each with its own frozen map, events and record. The evening
# is the scorecard's evening window (tools/score_levels.py): its map is the
# first clean snapshot after the 18:00 Globex roll, its bars run from that
# snapshot to London's open, and its base rates are rules_evening. Sunday to
# Thursday evenings; Friday and Saturday have no session.
WINDOWS = {
    "evening": {"label": "Evening", "map_from": sl.EVENING_MAP_FROM,
                "map_to": sl.EVENING_MAP_TO, "end": sl.EVENING_END,
                "overnight": True, "rules": "rules_evening"},
    "day": {"label": "Day", "map_from": MAP_AT, "map_to": CLOSE, "end": CLOSE,
            "overnight": False, "rules": "rules"},
}


def hm(hhmm):
    return datetime.strptime(hhmm, "%H:%M").time()


def session_due(win, now):
    """The session whose map time has come by `now`, or None."""
    spec, day = WINDOWS[win], now.date()
    if now.strftime("%H:%M") < spec["map_from"]:
        return None
    if win == "day":
        return day if g.is_trading_day(day) else None
    return day if day.weekday() not in (4, 5) else None


def session_end(win, session):
    spec = WINDOWS[win]
    day = datetime.fromisoformat(session).date()
    return datetime.combine(day + timedelta(days=1 if spec["overnight"] else 0),
                            hm(spec["end"]))


def map_start(win, smap):
    """Where a map's bars begin: 09:50 for the day, as the scorecard; the
    evening snapshot's own time, as the scorecard's evening."""
    return MAP_AT if win == "day" else (smap.get("frozen_at") or WINDOWS[win]["map_from"])


# ---------------------------------------------------------------- inputs
def session_bars(series, day, now, start=MAP_AT, win="day"):
    """Completed 5-minute bars of one session, `start` to its end, ET.

    A bar still forming is left out: its high and low can still change, and a
    setup must not trigger on a print the next run would take back.
    """
    begin = datetime.combine(day, hm(start))
    end = session_end(win, day.isoformat())
    res = json.loads(g.http_get(BARS_URL.format(sym=series)))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    off = (res.get("meta") or {}).get("gmtoffset") or 0
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        hi, lo, cl = q["high"][i], q["low"][i], q["close"][i]
        if hi is None or lo is None or cl is None:
            continue
        t = datetime.fromtimestamp(ts + off, timezone.utc).replace(tzinfo=None)
        if not begin <= t < end:
            continue
        if t + timedelta(minutes=BAR_MIN) > now:
            continue                       # still forming
        rows.append((t.strftime("%H:%M"), float(hi), float(lo), float(cl)))
    return rows


def rule_verdict(stats):
    """A rule's verdict in one gamma context, from its measured base rate."""
    if not stats or (stats.get("n") or 0) < g.PLAN_THIN_N:
        return "thin"
    if stats.get("exp") is None or stats["exp"] <= g.PLAN_MIN_EXP:
        return "no edge"
    return "ok"


def wall_setups(side, rules, regime):
    up = side == "call"
    out = {}
    for setup in SETUPS:
        base = rules.get(setup) or {}
        out[setup] = {"direction": ((-1 if up else +1) if setup == "fade"
                                    else (+1 if up else -1)),
                      "base": base, "verdict": rule_verdict(base.get(regime))}
    return out


def with_rules(smap, calib, win):
    """A map frozen while its symbol had no rules (ES before 2026-10-07 was
    walls only) gets them, from the walls and regime it already froze."""
    name = smap["symbol"]
    if smap.get("points") or name not in sl.RULE_POINTS:
        return smap
    rules = ((calib.get(WINDOWS[win]["rules"]) or {}).get(name) or {})
    smap = dict(smap, points=sl.RULE_POINTS[name],
                traded=name not in sl.REFERENCE_ONLY)
    smap.pop("mode", None)
    smap["walls"] = [dict(w, setups=wall_setups(w["side"], rules, smap.get("regime")))
                     for w in smap["walls"]]
    return smap


def build_symbol_map(sym, inst, calib, stamp, win="day"):
    """The session map for one symbol.

    Every symbol gets both setups as rules - fixed 1:1 in points
    (sl.RULE_POINTS) - each carrying its base rate in every gamma context,
    measured in THIS window, and a verdict for the session's regime. ES is
    shown in full but flagged traded=False: a reference for NQ, not a trade.
    """
    top = calib.get("top") or 2
    lv = sl.levels_of(sym, g.PLAN_BOOK, top, "nearest")
    regime = ((sym.get("regimes") or {}).get(g.PLAN_BOOK) or {}).get("regime")
    name = sym["symbol"]
    pts = sl.RULE_POINTS.get(name)
    rules = ((calib.get(WINDOWS[win]["rules"]) or {}).get(name) or {})
    walls = [{"side": side, "level": round(level, 2),
              "setups": wall_setups(side, rules, regime)}
             for side in ("call", "put") for level in lv[side]]
    return {"symbol": name, "traded": name not in sl.REFERENCE_ONLY,
            "points": pts, "contract": (sym.get("contract") or {}).get("symbol"),
            "multiplier": inst["multiplier"], "frozen_at": stamp,
            "regime": regime, "spot_at_map": sym.get("spot"),
            "basis": sym.get("gamma_basis"),
            "flip": lv.get("flip"), "walls": walls}


# ----------------------------------------------------------------- events
def evaluate(smap, bars, session_over):
    """Every rule setup in the frozen map, against the session's bars.

    Entry and the bar rules are the scorecard's; stop and target are the
    rule's fixed points either side of the actual entry.
    """
    last = bars[-1][3] if bars else None
    open_px = bars[0][3] if bars else None
    pts = smap["points"]
    out = []
    for w in smap["walls"]:
        level, side = w["level"], w["side"]
        up = side == "call"
        for setup in SETUPS:
            st = w["setups"][setup]
            d = st["direction"]
            ev = {"side": side, "level": level, "setup": setup,
                  "verdict": st["verdict"], "regime": smap.get("regime"),
                  "base": (st.get("base") or {}).get(smap.get("regime"))}
            # The scorecard skips a wall price had already passed when the
            # session's bars began - a call wall below price at 09:50 is not a
            # cap being tested - so this does too, or the page fires trades
            # nobody measured. Found replaying 2026-10-06: two extra NQ trades.
            if open_px is not None and \
                    ((level <= open_px) if up else (level >= open_px)):
                ev["status"] = "passed at open"
                out.append(ev)
                continue
            trig = None
            for i, (_, hi, lo, cl) in enumerate(bars):
                if setup == "fade" and ((hi >= level) if up else (lo <= level)):
                    trig = (i, level, i)            # fills at the wall
                    break
                if setup == "breakout" and ((cl > level) if up else (cl < level)):
                    trig = (i, cl, i + 1)           # fills on the close
                    break
            if trig is None:
                near = last is not None and abs(last - level) <= pts
                ev["status"] = ("not triggered" if session_over
                                else "armed" if near else "watching")
                ev["dist_pts"] = round(level - last, 2) if last else None
                out.append(ev)
                continue
            i, entry, start = trig
            flip = smap.get("flip")
            ev.update({"time": bars[i][0], "entry": round(entry, 2),
                       "stop": round(entry - d * pts, 2),
                       "target": round(entry + d * pts, 2),
                       "flip_side": (("above" if entry > flip else "below")
                                     if flip else None)})
            status, r, at = "open", 0.0, None
            for k, (t, hi, lo, cl) in enumerate(bars[start:]):
                a = (entry - lo) if d > 0 else (hi - entry)
                f = (hi - entry) if d > 0 else (entry - lo)
                if a >= pts:                        # both in one bar = stop
                    status, r, at = "lost", -1.0, t
                    break
                if k == 0 and setup == "fade":
                    continue                        # fill bar counts against only
                if f >= pts:
                    status, r, at = "won", 1.0, t
                    break
            if status == "open" and last is not None:
                r = (last - entry) * d / pts
                if session_over:
                    status = "closed"
            ev.update({"status": status, "r": round(r, 2), "resolved_at": at,
                       "pnl_usd": round(r * pts * smap["multiplier"])})
            out.append(ev)
    return out


# ---------------------------------------------------------------- record
FINAL = ("won", "lost", "closed")


def summarise_record(history):
    """Forward test by session, symbol, setup and gamma regime, against the
    backtest."""
    groups = {}
    for h in history:
        if h.get("status") not in FINAL:
            continue
        key = (h.get("window") or "day", h["symbol"], h["setup"],
               h.get("regime") or "?")
        gr = groups.setdefault(key, {"n": 0, "won": 0, "r": 0.0, "usd": 0,
                                     "bt": None})
        gr["n"] += 1
        gr["won"] += h["status"] == "won"
        gr["r"] += h.get("r") or 0.0
        gr["usd"] += h.get("pnl_usd") or 0
        if (h.get("base") or {}).get("exp") is not None:
            gr["bt"] = h["base"]["exp"]
    return [{"window": win, "symbol": sym, "setup": setup, "regime": regime,
             "n": v["n"], "won": v["won"], "avg_r": round(v["r"] / v["n"], 2),
             "avg_usd": round(v["usd"] / v["n"]), "backtest_r": v["bt"]}
            for (win, sym, setup, regime), v in sorted(groups.items())]


def record_session(history, session, maps, now, win="day"):
    """Evaluate a session to its end and put its resolved setups in the
    record, replacing any earlier pass over the same session."""
    day = datetime.fromisoformat(session).date()
    keep = [h for h in history
            if not (h.get("date") == session and (h.get("window") or "day") == win)]
    for name, smap in maps.items():
        try:
            bars = session_bars(smap["contract"], day, now, map_start(win, smap), win)
            evs = evaluate(smap, bars, True)
        except Exception:
            continue
        for ev in evs:
            if ev.get("status") in FINAL:
                keep.append({"date": session, "window": win, "symbol": name, **{
                    k: ev.get(k) for k in ("side", "level", "setup", "verdict",
                                           "regime", "flip_side", "base", "time",
                                           "entry", "stop", "target", "r",
                                           "pnl_usd", "status")}})
    return keep


# ------------------------------------------------------------------ run
def step_window(win, ws, history, payload, calib, insts, now):
    """Advance one session window by one run. Returns (window, history)."""
    spec = WINDOWS[win]
    due = session_due(win, now)
    session = ws.get("session")
    maps = {k: with_rules(v, calib, win) for k, v in (ws.get("maps") or {}).items()}
    # A new session replaces the old one when its map time comes; one left
    # unfinished - the PC off at its end, say - is finished first so its
    # setups still reach the record. Yahoo keeps the bars for days.
    if due and (session or "") < due.isoformat():
        if session and not ws.get("session_over"):
            history = record_session(history, session, maps, now, win)
        session, maps = due.isoformat(), {}

    notes = []
    if due and session == due.isoformat():
        stamp = (payload.get("generated") or "").replace(" ET", "")
        fresh = (stamp[:10] == session
                 and spec["map_from"] <= stamp[11:16] < spec["map_to"])
        for sym in payload.get("symbols") or []:
            name = sym.get("symbol")
            if name in maps or name not in insts:
                continue
            # Freeze only from a clean read of THIS session: a held panel is
            # an earlier run's ladder, and a pre-09:50 one is pre-outage.
            if fresh and sym.get("ok") and not sym.get("regimes_from"):
                maps[name] = build_symbol_map(sym, insts[name], calib,
                                              stamp[11:16], win)
            elif now.strftime("%H:%M") < spec["map_to"] or win == "day":
                notes.append(f"{name}: map waiting for a clean snapshot")

    session_over = bool(session) and now >= session_end(win, session)
    symbols = []
    if session:
        day = datetime.fromisoformat(session).date()
        for name, smap in maps.items():
            entry = {"symbol": name, "map": smap, "events": []}
            try:
                bars = session_bars(smap["contract"], day, now,
                                    map_start(win, smap), win)
                entry["events"] = evaluate(smap, bars, session_over)
                entry["last"] = bars[-1][3] if bars else None
                entry["bars_to"] = bars[-1][0] if bars else None
            except Exception as exc:
                entry["error"] = f"bars unavailable ({type(exc).__name__})"
            symbols.append(entry)

    # The finished session goes into the record; a re-run replaces it.
    if session and session_over:
        history = record_session(history, session, maps, now, win)

    return ({"name": win, "label": spec["label"], "session": session,
             "session_over": session_over, "map_from": spec["map_from"],
             "map_to": spec["map_to"], "end": spec["end"],
             "symbols": symbols, "notes": notes, "maps": maps}, history)


def default_window(windows, now):
    """The tab a visitor lands on: a day session still trading, else the
    evening from the cash close until the next morning's map."""
    day = windows.get("day") or {}
    if day.get("session") == now.date().isoformat() and not day.get("session_over"):
        return "day"
    return "evening" if not (MAP_AT <= now.strftime("%H:%M") < CLOSE) else "day"


def run(pub, now=None, payload_path=None):
    pub = Path(pub)
    state_path = pub / "setups.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    history = state.get("history") or []
    for h in history:
        h.setdefault("window", "day")
    prior = {w["name"]: w for w in state.get("windows") or []}
    if not prior and state.get("session"):            # the one-session format
        prior["day"] = {"session": state["session"], "maps": state.get("maps"),
                        "session_over": state.get("session_over")}
    now = now or g.now_et().replace(tzinfo=None)
    calib = g.load_calibration()
    payload = json.loads(Path(payload_path or pub / "data.json")
                         .read_text(encoding="utf-8"))
    insts = {i["future"]: i for i in g.INSTRUMENTS}

    windows = {}
    for win in WINDOWS:
        windows[win], history = step_window(win, prior.get(win) or {}, history,
                                            payload, calib, insts, now)

    data = {"generated": now.strftime("%Y-%m-%d %H:%M ET"), "epoch": time.time(),
            "default_window": default_window(windows, now),
            "windows": list(windows.values()),
            "calibration": {k: calib.get(k) for k in ("from", "to", "days")},
            "record": summarise_record(history), "history": history}
    blob = json.dumps(data, separators=(",", ":"))
    state_path.write_text(blob, encoding="utf-8")
    (pub / "setups.html").write_text(
        PAGE.replace("const SETUPS_DATA = null;", f"const SETUPS_DATA = {blob};"),
        encoding="utf-8")
    return data


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Gamma Setups</title>
<style>
:root{
  --bg:#0b0f17; --panel:#131b28; --raised:#1a2434; --line:rgba(255,255,255,.08);
  --ink:#e7ecf4; --muted:#7f8da0;
  --brass:#d9a441; --jade:#4bbf8a; --verm:#e0603f; --stress:#ff5d63;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;font-size:13px}
.wrap{max-width:1180px;margin:0 auto;padding:22px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 18px;margin-bottom:18px}
h1{font-family:system-ui,sans-serif;font-weight:800;font-size:19px;letter-spacing:.04em;margin:0}
h1 .g{color:var(--brass)}
.meta{color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;gap:6px 16px}
.meta b{color:var(--ink);font-weight:500}
.navlink{color:var(--brass);text-decoration:none;border:1px solid var(--brass);
  border-radius:4px;padding:5px 10px;font-size:12px;letter-spacing:.06em;margin-left:auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:16px;margin-bottom:16px;min-width:0}
.phead{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px;margin-bottom:10px}
.sym{font-family:system-ui,sans-serif;font-weight:800;font-size:22px;letter-spacing:.06em}
.sub{color:var(--muted);font-size:11.5px}
.k{font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);
  margin:14px 0 5px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;font-size:12.5px;min-width:560px;width:100%}
th{text-align:left;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:500;padding:4px 10px 4px 0;border-bottom:1px solid var(--line)}
td{padding:5px 10px 5px 0;border-bottom:1px solid rgba(255,255,255,.04);font-variant-numeric:tabular-nums}
td.num,th.num{text-align:right}
.side-c{color:var(--jade)}.side-p{color:var(--verm)}
.pos{color:var(--jade)}.neg{color:var(--verm)}
.chip{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;padding:1px 6px;
  border:1px solid var(--line);border-radius:3px;color:var(--muted);white-space:nowrap}
.v-ok{color:var(--jade);border-color:rgba(75,191,138,.45)}
.v-thin,.v-room{color:var(--brass);border-color:rgba(217,164,65,.45)}
.v-noedge{color:var(--stress);border-color:rgba(255,93,99,.4)}
.s-armed{color:var(--brass);border-color:var(--brass)}
.s-open{color:var(--ink);border-color:var(--ink)}
.s-won{color:var(--jade);border-color:var(--jade)}
.s-lost{color:var(--verm);border-color:var(--verm)}
tr.dim td{opacity:.55}
.g-neg{color:#ffb38a;border-color:rgba(224,96,63,.5)}
.g-pos{color:#9fd8bd;border-color:rgba(75,191,138,.5)}
.empty{color:var(--muted);font-size:12px;padding:6px 0}
.err{color:var(--stress);font-size:12px}
.caveat{color:var(--muted);font-size:11px;line-height:1.55;max-width:80ch;margin-top:14px}
.caveat b{color:var(--brass);font-weight:500}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.tab{background:none;border:1px solid var(--line);color:var(--muted);border-radius:6px;
  padding:8px 14px;font:inherit;cursor:pointer;text-align:left}
.tab.on{color:var(--ink);border-color:var(--brass);background:rgba(217,164,65,.08)}
.tab small{display:block;font-size:10.5px;color:var(--muted)}
</style></head>
<body><div class="wrap">
<header>
  <h1>Gamma<span class="g">/</span>Setups<b style="color:var(--brass)">*</b></h1>
  <div class="meta" id="meta"></div>
  <a class="navlink" href="scorecard.html">Scorecard</a><a class="navlink" href="./" style="margin-left:0">Dashboard</a>
</header>
<div id="root"></div>
</div>
<script>
const SETUPS_DATA = null;
const VERDICT = {"ok":"v-ok","thin":"v-thin","no edge":"v-noedge"};
const ACTION = {fade:{call:"sell call wall",put:"buy put wall"},
                breakout:{call:"buy break above",put:"sell break below"}};
const esc = v => String(v==null?"":v).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fx = (v,d=2) => v==null ? "—" : (+v).toFixed(d);
const rFmt = v => v==null ? "—" : `${v>0?"+":""}${(+v).toFixed(2)}R`;
const usd = v => v==null ? "—" : `${v<0?"−":"+"}$${Math.abs(v).toLocaleString()}`;
const chip = (t,c) => `<span class="chip ${c||""}">${esc(t)}</span>`;
const regimeChip = r => r ? chip(`${r} gamma`, r==="negative"?"g-neg":"g-pos") : "";
const baseTxt = b => b ? `${b.won}/${b.n} won ${rFmt(b.exp)}` : "no history";

// One wall's rule in both regimes, today's first and bright, the other dim:
// the regime is the call the trader makes, so both rates sit side by side.
function ruleCell(st, regime){
  const other = regime==="negative" ? "positive" : "negative";
  const b = st.base||{};
  return `${chip(st.verdict, VERDICT[st.verdict])} <span>${esc(regime||"?")}: ${baseTxt(b[regime])}</span>`
    + `<div class="sub">${other}: ${baseTxt(b[other])} · all: ${baseTxt(b.all)}</div>`;
}

function mapTable(sm){
  const rows = sm.walls.map(w=>`<tr><td class="side-${w.side[0]}">${w.side.toUpperCase()}</td>`
    +`<td class="num">${fx(w.level)}</td><td>${ruleCell(w.setups.fade, sm.regime)}</td>`
    +`<td>${ruleCell(w.setups.breakout, sm.regime)}</td></tr>`).join("");
  return `<div class="scroll"><table><thead><tr><th>Wall</th><th class="num">Level</th>`
    +`<th>Fade it</th><th>Trade the break</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function eventRows(evs, mult){
  const live = evs.filter(e=>["armed","open","won","lost","closed"].includes(e.status));
  if(!live.length) return `<div class="empty">Nothing armed or triggered yet. Each wall fires once per setup per session.</div>`;
  const order = {open:0,armed:1,won:2,lost:2,closed:2};
  live.sort((a,b)=>(order[a.status]-order[b.status]) || ((a.time||"")<(b.time||"")?-1:1));
  const rows = live.map(e=>{
    const st = {open:"s-open",armed:"s-armed",won:"s-won",lost:"s-lost"}[e.status]||"";
    const res = e.status==="armed" ? `${e.dist_pts>0?"+":""}${fx(e.dist_pts)} pts away`
      : `<b class="${(e.r||0)>=0?"pos":"neg"}">${usd(e.pnl_usd)}</b> <span class="sub">${rFmt(e.r)}${e.status==="open"?" now":""}</span>`;
    const ctx = (e.flip_side?chip(`${e.flip_side} flip`, e.flip_side==="below"?"g-neg":"g-pos"):"");
    return `<tr class="${e.verdict!=="ok"?"dim":""}"><td class="side-${e.side[0]}">${ACTION[e.setup][e.side]}</td>`
      +`<td>${chip(e.status,st)}</td><td>${chip(e.verdict,VERDICT[e.verdict])} <span class="sub">${baseTxt(e.base)}</span></td>`
      +`<td>${ctx}</td><td class="num">${esc(e.time||"")}</td><td class="num">${fx(e.level)}</td>`
      +`<td class="num">${fx(e.entry)}</td><td class="num">${fx(e.stop)}</td>`
      +`<td class="num">${fx(e.target)}</td><td class="num">${res}</td></tr>`;
  }).join("");
  return `<div class="scroll"><table><thead><tr><th>Setup</th><th>Status</th>`
    +`<th>Verdict, today's regime</th><th>At entry</th><th class="num">Time</th>`
    +`<th class="num">Wall</th><th class="num">Entry</th><th class="num">Stop</th>`
    +`<th class="num">Target</th><th class="num">Result</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function recordTable(rec){
  if(!rec||!rec.length) return `<div class="empty">No finished sessions yet. The record starts with the first session this page sees through to the close.</div>`;
  const rows = rec.map(r=>`<tr><td>${esc(r.symbol)}</td><td>${esc(r.setup)}</td><td>${regimeChip(r.regime)}</td>`
    +`<td class="num">${r.n}</td><td class="num">${r.won}</td>`
    +`<td class="num"><b class="${r.avg_r>=0?"pos":"neg"}">${rFmt(r.avg_r)}</b></td>`
    +`<td class="num">${usd(r.avg_usd)}</td><td class="num">${rFmt(r.backtest_r)}</td></tr>`).join("");
  return `<div class="scroll"><table><thead><tr><th>Symbol</th><th>Setup</th><th>Regime</th>`
    +`<th class="num">Trades</th><th class="num">Won</th><th class="num">Live avg</th>`
    +`<th class="num">Per trade</th><th class="num">Backtest</th></tr></thead>`
    +`<tbody>${rows}</tbody></table></div>`;
}

function windowTabs(d, cur){
  return `<div class="tabs">` + d.windows.map(w=>{
    const st = !w.session ? "no map yet" : `${w.session} · ${w.session_over?"closed":"live"}`;
    return `<button class="tab ${w.name===cur?"on":""}" onclick="pickWin('${w.name}')">`
      + `${esc(w.label)} <span class="sub">${esc(w.map_from)}–${esc(w.end)} ET</span><small>${esc(st)}</small></button>`;
  }).join("") + `</div>`;
}

function renderSetups(d, win){
  if(!d) return `<div class="panel empty">No setups data yet.</div>`;
  if(!d.windows) return `<div class="panel empty">Updating to the new format…</div>`;
  win = win || d.default_window || "day";
  const w = d.windows.find(x=>x.name===win) || d.windows[0];
  const evening = w.name==="evening";
  const syms = (w.symbols||[]).slice().sort((a,b)=>
    ((a.map||{}).traded===false) - ((b.map||{}).traded===false));
  let html = windowTabs(d, w.name);
  if(!w.session) html += `<div class="panel empty">` + (evening
    ? `The evening map freezes at the first clean snapshot after ${esc(w.map_from)} ET, Sunday to Thursday, and its setups run to ${esc(w.end)} ET.`
    : `The session map freezes at ${esc(w.map_from)} ET on trading days.`) + `</div>`;
  for(const s of syms){
    const m = s.map||{};
    html += `<div class="panel"><div class="phead"><span class="sym">${esc(s.symbol)}</span>`
      + chip(`rules: 1:1, ${fx(m.points,0)} pts`)
      + (m.traded===false ? ` ${chip("reference, not traded")}` : "")
      + ` ${regimeChip(m.regime)}`
      + (m.basis==="live" ? ` ${chip("gamma at live price")}` : "")
      + `<span class="sub">map frozen ${esc(m.frozen_at)} ET · flip ${fx(m.flip)}`
      + (s.last!=null?` · last ${fx(s.last)} (${esc(s.bars_to)} bar)`:"")+`</span></div>`
      + (s.error?`<div class="err">${esc(s.error)}</div>`:"");
    html += `<div class="k">Setups ${evening?"tonight":"today"}</div>` + eventRows(s.events||[], m.multiplier)
      + `<div class="k">Session map: each rule's ${evening?"evening ":""}history in each gamma regime</div>` + mapTable(m) + `</div>`;
  }
  for(const n of (w.notes||[])) html += `<div class="sub">${esc(n)}</div>`;
  const c = d.calibration||{};
  const rec = (d.record||[]).filter(r=>(r.window||"day")===w.name);
  html += `<div class="panel"><div class="phead"><span class="sym" style="font-size:16px">Forward test, ${esc(w.label.toLowerCase())}</span>`
    + `<span class="sub">live results of these rules, by regime, against the backtest</span></div>`
    + recordTable(rec)
    + `<div class="caveat"><b>*</b> Rules: fixed 1:1 stop and target in points, fades on the first touch `
    + `of a wall and breakouts on the first 5-minute close through, one of each per wall per session, `
    + `the nearest walls each side, frozen ` + (evening
      ? `from the first clean snapshot after the ${esc(w.map_from)} Globex roll and run to ${esc(w.end)} ET. `
        + `After the cash close the chain stops printing, so evening maps read gamma repriced to the live `
        + `future (tagged on the panel); earlier evening sessions read it at the 4pm close. `
      : `at ${esc(w.map_from)} ET and run to the ${esc(w.end)} close. `)
    + `"Regime" is the near book's net gamma in that map; "above/below flip" is where the entry sat `
    + `against its flip. Base rates come from the scorecard's ${evening?"evening":"day"} trades over `
    + `${esc(c.days)} sessions (${esc(c.from)} to ${esc(c.to)}) and many are a handful of trades, so read `
    + `them as provisional; the forward test is the check. Only completed 5-minute bars are used, so a `
    + `trigger shows up to one bar plus one publish late. A bar touching both stop and target counts as `
    + `the stop. Rows without a measured edge in the session's regime are dimmed. Not advice.</div></div>`;
  return html;
}

let CUR = SETUPS_DATA, WIN = null;
function pickWin(w){ WIN = w; paint(CUR); }
function paint(d){
  CUR = d;
  document.getElementById("root").innerHTML = renderSetups(d, WIN);
  document.getElementById("meta").innerHTML = d
    ? `<span>updated <b>${esc(d.generated)}</b></span>` : "";
}

async function poll(){
  const get = async u => { try{ const r=await fetch(u,{cache:"no-store"}); if(r.ok) return await r.json(); }catch(e){} return null; };
  const t=Date.now(), m=location.hostname.match(/^([^.]+)\.github\.io$/);
  const repo=location.pathname.split("/").filter(Boolean)[0];
  const got=(await Promise.all([get("setups.json?t="+t),
    (m&&repo)?get(`https://raw.githubusercontent.com/${m[1]}/${repo}/snapshot/setups.json?t=${t}`):null]))
    .filter(Boolean);
  const best=got.reduce((a,b)=>((b.epoch||0)>(a.epoch||0)?b:a), CUR||{epoch:0});
  if(best && best!==CUR) paint(best);
}
paint(SETUPS_DATA);
setInterval(poll, 60000);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pub", required=True, help="folder holding index.html/data.json")
    ap.add_argument("--now", help="replay as of 'YYYY-MM-DD HH:MM' ET (testing)")
    ap.add_argument("--payload", help="data.json to map from (testing)")
    a = ap.parse_args()
    now = datetime.strptime(a.now, "%Y-%m-%d %H:%M") if a.now else None
    d = run(a.pub, now, a.payload)
    for w in d["windows"]:
        live = sum(1 for s in w["symbols"] for e in s["events"]
                   if e.get("status") in ("armed", "open"))
        done = sum(1 for s in w["symbols"] for e in s["events"]
                   if e.get("status") in FINAL)
        print(f"setups {w['name']}: session {w['session']}, "
              f"{len(w['symbols'])} maps, {live} live, {done} resolved")
    print(f"setups: {len(d['history'])} in the record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
