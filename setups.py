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
          side - the same walls, count and book the scorecard measured - with
          each setup's verdict, stop and wall-to-wall target fixed there.
  events  each run replays the session's COMPLETED 5-minute futures bars from
          09:50 against that map, so a touch between two runs is never missed.
          Every wall/setup goes watching -> armed (within one stop) ->
          triggered -> won / lost / closed at 16:00, once.
  record  every resolved setup is appended to a forward-test history, so the
          page's live record can be set against the scorecard's backtest.

The rules - entry, stop, target, bar touching both is the stop - are the
scorecard's own (tools/score_levels.py) and the verdict is the dashboard's
(gex_terminal.plan_verdict), so the page never shows a trade nobody measured.
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


# ---------------------------------------------------------------- inputs
def session_bars(series, day, now):
    """Completed 5-minute bars of `day`, MAP_AT to the close, ET.

    A bar still forming is left out: its high and low can still change, and a
    setup must not trigger on a print the next run would take back.
    """
    res = json.loads(g.http_get(BARS_URL.format(sym=series)))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    off = (res.get("meta") or {}).get("gmtoffset") or 0
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        hi, lo, cl = q["high"][i], q["low"][i], q["close"][i]
        if hi is None or lo is None or cl is None:
            continue
        t = datetime.fromtimestamp(ts + off, timezone.utc).replace(tzinfo=None)
        if t.date() != day:
            continue
        hhmm = t.strftime("%H:%M")
        if not (MAP_AT <= hhmm < CLOSE):
            continue
        if t + timedelta(minutes=BAR_MIN) > now:
            continue                       # still forming
        rows.append((hhmm, float(hi), float(lo), float(cl)))
    return rows


def first_target(structure, ref, direction, stop_pts):
    """First level in the trade's direction at least a stop away - the rule
    the scorecard measured and the dashboard used."""
    ahead = sorted((abs(k - ref), k) for k in structure
                   if (k - ref) * direction >= stop_pts)
    return ahead[0] if ahead else (None, None)


# ------------------------------------------------------------------- map
def build_symbol_map(sym, inst, calib, stamp):
    top = calib.get("top") or 2
    lv = sl.levels_of(sym, g.PLAN_BOOK, top, "nearest")
    structure = sl.structure_of(sym, g.PLAN_BOOK)
    cal = (calib.get("symbols") or {}).get(sym["symbol"]) or {}
    walls = []
    for side in ("call", "put"):
        up = side == "call"
        side_cal = cal.get(side) if isinstance(cal.get(side), dict) else {}
        for level in lv[side]:
            setups = {}
            for setup in SETUPS:
                basis = g.plan_basis(calib, side_cal, setup)
                stop_pct = (side_cal.get(setup) or {}).get("stop_pct")
                direction = ((-1 if up else +1) if setup == "fade"
                             else (+1 if up else -1))
                stop_pts = level * stop_pct / 100.0 if stop_pct else None
                tgt_pts, tgt = (first_target(structure, level, direction, stop_pts)
                                if stop_pts else (None, None))
                setups[setup] = {
                    "direction": direction, "stop_pct": stop_pct,
                    "verdict": g.plan_verdict(basis, tgt is not None),
                    "planned_rr": round(tgt_pts / stop_pts, 2) if tgt else None,
                    "basis": {k: basis.get(k) for k in
                              ("n", "win", "exp", "measure")}}
            walls.append({"side": side, "level": round(level, 2),
                          "setups": setups})
    return {"symbol": sym["symbol"],
            "contract": (sym.get("contract") or {}).get("symbol"),
            "multiplier": inst["multiplier"], "frozen_at": stamp,
            "spot_at_map": sym.get("spot"), "flip": lv.get("flip"),
            "structure": [round(k, 2) for k in structure], "walls": walls}


# ----------------------------------------------------------------- events
def evaluate(smap, bars, session_over):
    """Every wall/setup in the frozen map, against the session's bars."""
    last = bars[-1][3] if bars else None
    out = []
    for w in smap["walls"]:
        level, side = w["level"], w["side"]
        up = side == "call"
        for setup in SETUPS:
            st = w["setups"][setup]
            ev = {"side": side, "level": level, "setup": setup,
                  "verdict": st["verdict"], "direction": st["direction"],
                  "basis": st["basis"], "planned_rr": st["planned_rr"]}
            if not st["stop_pct"]:
                ev["status"] = "unmeasured"
                out.append(ev)
                continue
            # Trigger: the scorecard's rules exactly. A fade fills at the wall
            # on the first touch; a breakout on the first CLOSE through.
            trig = None
            for i, (_, hi, lo, cl) in enumerate(bars):
                if setup == "fade" and ((hi >= level) if up else (lo <= level)):
                    trig = (i, level, i)            # start on the fill bar
                    break
                if setup == "breakout" and ((cl > level) if up else (cl < level)):
                    trig = (i, cl, i + 1)           # start on the bar after
                    break
            if trig is None:
                stop_pts = level * st["stop_pct"] / 100.0
                near = last is not None and abs(last - level) <= stop_pts
                ev["status"] = ("not triggered" if session_over
                                else "armed" if near else "watching")
                ev["dist_pct"] = (round(100 * (level - last) / last, 2)
                                  if last else None)
                out.append(ev)
                continue
            i, entry, start = trig
            d = st["direction"]
            stop_pts = entry * st["stop_pct"] / 100.0
            tgt_pts, tgt = first_target(smap["structure"], entry, d, stop_pts)
            ev.update({"time": bars[i][0], "entry": round(entry, 2),
                       "stop": round(entry - d * stop_pts, 2),
                       "risk_usd": round(stop_pts * smap["multiplier"])})
            if tgt is None:
                # Nothing to target from where it actually filled: not a
                # trade, as the scorecard would have skipped it.
                ev["status"] = "no room"
                out.append(ev)
                continue
            ev.update({"target": round(tgt, 2),
                       "rr": round(tgt_pts / stop_pts, 2),
                       "reward_usd": round(tgt_pts * smap["multiplier"])})
            status, r, at = "open", 0.0, None
            for k, (t, hi, lo, cl) in enumerate(bars[start:]):
                a = (entry - lo) if d > 0 else (hi - entry)
                f = (hi - entry) if d > 0 else (entry - lo)
                if a >= stop_pts:                   # both in one bar = stop
                    status, r, at = "lost", -1.0, t
                    break
                if k == 0 and setup == "fade":
                    continue                        # fill bar counts against only
                if f >= tgt_pts:
                    status, r, at = "won", tgt_pts / stop_pts, t
                    break
            if status == "open" and last is not None:
                r = (last - entry) * d / stop_pts
                if session_over:
                    status = "closed"
            ev.update({"status": status, "r": round(r, 2), "resolved_at": at})
            out.append(ev)
    return out


# ---------------------------------------------------------------- record
FINAL = ("won", "lost", "closed")


def summarise_record(history):
    """Forward test: realized R by setup and verdict, against the backtest."""
    groups = {}
    for h in history:
        if h.get("status") not in FINAL:
            continue
        key = (h["setup"], "ok" if h.get("verdict") == "ok" else "other")
        gr = groups.setdefault(key, {"n": 0, "won": 0, "r": 0.0, "bt": []})
        gr["n"] += 1
        gr["won"] += h["status"] == "won"
        gr["r"] += h.get("r") or 0.0
        if (h.get("basis") or {}).get("exp") is not None:
            gr["bt"].append(h["basis"]["exp"])
    out = []
    for (setup, grp), v in sorted(groups.items()):
        out.append({"setup": setup, "group": grp, "n": v["n"], "won": v["won"],
                    "avg_r": round(v["r"] / v["n"], 2),
                    "backtest_r": (round(sum(v["bt"]) / len(v["bt"]), 2)
                                   if v["bt"] else None)})
    return out


def record_session(history, session, maps, now):
    """Evaluate a session to its close and put its resolved setups in the
    record, replacing any earlier pass over the same session."""
    day = datetime.fromisoformat(session).date()
    keep = [h for h in history if h.get("date") != session]
    for name, smap in maps.items():
        try:
            evs = evaluate(smap, session_bars(smap["contract"], day, now), True)
        except Exception:
            continue
        for ev in evs:
            if ev.get("status") in FINAL:
                keep.append({"date": session, "symbol": name, **{
                    k: ev.get(k) for k in ("side", "level", "setup", "verdict",
                                           "basis", "time", "entry", "stop",
                                           "target", "rr", "r", "status")}})
    return keep


# ------------------------------------------------------------------ run
def run(pub, now=None, payload_path=None):
    pub = Path(pub)
    state_path = pub / "setups.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    history = state.get("history") or []
    now = now or g.now_et().replace(tzinfo=None)
    today = now.date()
    hhmm = now.strftime("%H:%M")
    calib = g.load_calibration()
    payload = json.loads(Path(payload_path or pub / "data.json")
                         .read_text(encoding="utf-8"))
    insts = {i["future"]: i for i in g.INSTRUMENTS}

    # A session left open - the PC was off at the close, say - is finished
    # now, before today's map replaces it, so its setups still reach the
    # record. Yahoo keeps the bars for days, so a late finish is exact.
    prev = state.get("session")
    if prev and prev != today.isoformat() and not state.get("session_over"):
        history = record_session(history, prev, state.get("maps") or {}, now)

    session = prev if prev == today.isoformat() else None
    maps = (state.get("maps") or {}) if session else {}
    notes = []
    if g.is_trading_day(today) and hhmm >= MAP_AT:
        session = today.isoformat()
        stamp = (payload.get("generated") or "").replace(" ET", "")
        fresh = stamp[:10] == session and stamp[11:16] >= MAP_AT
        for sym in payload.get("symbols") or []:
            name = sym.get("symbol")
            if name in maps or name not in insts:
                continue
            # Freeze only from a clean read of THIS session: a held panel is
            # an earlier run's ladder, and a pre-09:50 one is pre-outage.
            if fresh and sym.get("ok") and not sym.get("regimes_from"):
                maps[name] = build_symbol_map(sym, insts[name], calib, stamp[11:16])
            else:
                notes.append(f"{name}: map waiting for a clean snapshot")

    session_over = bool(session) and (today.isoformat() > session or hhmm >= CLOSE)
    symbols = []
    if session:
        for name, smap in maps.items():
            entry = {"symbol": name, "map": smap, "events": []}
            try:
                bars = session_bars(smap["contract"], datetime.fromisoformat(session).date(), now)
                entry["events"] = evaluate(smap, bars, session_over)
                entry["last"] = bars[-1][3] if bars else None
                entry["bars_to"] = bars[-1][0] if bars else None
            except Exception as exc:
                entry["error"] = f"bars unavailable ({type(exc).__name__})"
            symbols.append(entry)

    # The finished session goes into the record; a re-run replaces it.
    if session and session_over:
        history = record_session(history, session, maps, now)

    data = {"generated": now.strftime("%Y-%m-%d %H:%M ET"), "epoch": time.time(),
            "session": session, "session_over": session_over,
            "map_at": MAP_AT, "symbols": symbols, "notes": notes,
            "calibration": {k: calib.get(k) for k in ("from", "to", "days")},
            "record": summarise_record(history), "history": history,
            "maps": maps}
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
.empty{color:var(--muted);font-size:12px;padding:6px 0}
.err{color:var(--stress);font-size:12px}
.caveat{color:var(--muted);font-size:11px;line-height:1.55;max-width:80ch;margin-top:14px}
.caveat b{color:var(--brass);font-weight:500}
</style></head>
<body><div class="wrap">
<header>
  <h1>Gamma<span class="g">/</span>Setups<b style="color:var(--brass)">*</b></h1>
  <div class="meta" id="meta"></div>
  <a class="navlink" href="./">&larr; Dashboard</a>
</header>
<div id="root"></div>
</div>
<script>
const SETUPS_DATA = null;
const VERDICT = {"ok":"v-ok","thin":"v-thin","no edge":"v-noedge","no room":"v-room"};
const ACTION = {fade:{call:"sell call wall",put:"buy put wall"},
                breakout:{call:"buy break above",put:"sell break below"}};
const esc = v => String(v==null?"":v).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fx = (v,d=2) => v==null ? "—" : (+v).toFixed(d);
const rFmt = v => v==null ? "—" : `${v>0?"+":""}${(+v).toFixed(2)}R`;
const chip = (t,c) => `<span class="chip ${c||""}">${esc(t)}</span>`;

function mapTable(sm){
  const rows = sm.walls.map(w=>{
    const cell = s => { const x=w.setups[s]; return chip(x.verdict, VERDICT[x.verdict])
      + (x.basis&&x.basis.exp!=null?` <span class="sub">${rFmt(x.basis.exp)}</span>`:""); };
    return `<tr><td class="side-${w.side[0]}">${w.side.toUpperCase()}</td>`
      +`<td class="num">${fx(w.level)}</td><td>${cell("fade")}</td><td>${cell("breakout")}</td></tr>`;
  }).join("");
  return `<div class="scroll"><table><thead><tr><th>Wall</th><th class="num">Level</th>`
    +`<th>Fade it</th><th>Trade the break</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function eventRows(evs){
  const live = evs.filter(e=>["armed","open","won","lost","closed","no room"].includes(e.status));
  if(!live.length) return `<div class="empty">Nothing armed or triggered yet. Each wall fires once per setup per session.</div>`;
  const order = {open:0,armed:1,won:2,lost:2,closed:2,"no room":3};
  live.sort((a,b)=>(order[a.status]-order[b.status]) || ((a.time||"")<(b.time||"")?-1:1));
  const rows = live.map(e=>{
    const dim = e.verdict!=="ok";
    const st = {open:"s-open",armed:"s-armed",won:"s-won",lost:"s-lost"}[e.status]||"";
    const res = e.status==="armed" ? `${e.dist_pct>0?"+":""}${fx(e.dist_pct)}% away`
      : e.status==="no room" ? "no target"
      : `<b class="${(e.r||0)>=0?"pos":"neg"}">${rFmt(e.r)}</b>${e.status==="open"?" now":""}`;
    return `<tr class="${dim?"dim":""}"><td class="side-${e.side[0]}">${ACTION[e.setup][e.side]}</td>`
      +`<td>${chip(e.status,st)}</td><td>${chip(e.verdict,VERDICT[e.verdict])}</td>`
      +`<td class="num">${esc(e.time||"")}</td><td class="num">${fx(e.level)}</td>`
      +`<td class="num">${fx(e.entry)}</td><td class="num">${fx(e.stop)}</td>`
      +`<td class="num">${fx(e.target)}</td><td class="num">${e.rr!=null?fx(e.rr):"—"}</td>`
      +`<td class="num">${res}</td></tr>`;
  }).join("");
  return `<div class="scroll"><table><thead><tr><th>Setup</th><th>Status</th><th>Verdict</th>`
    +`<th class="num">Time</th><th class="num">Wall</th><th class="num">Entry</th>`
    +`<th class="num">Stop</th><th class="num">Target</th><th class="num">R:R</th>`
    +`<th class="num">Result</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function recordTable(rec){
  if(!rec||!rec.length) return `<div class="empty">No finished sessions yet. The record starts with the first session this page sees through to the close.</div>`;
  const rows = rec.map(r=>`<tr><td>${esc(r.setup)}</td><td>${chip(r.group==="ok"?"ok":"other",r.group==="ok"?"v-ok":"")}</td>`
    +`<td class="num">${r.n}</td><td class="num">${r.won}</td>`
    +`<td class="num"><b class="${r.avg_r>=0?"pos":"neg"}">${rFmt(r.avg_r)}</b></td>`
    +`<td class="num">${rFmt(r.backtest_r)}</td></tr>`).join("");
  return `<div class="scroll"><table><thead><tr><th>Setup</th><th>Verdict</th><th class="num">Trades</th>`
    +`<th class="num">Won</th><th class="num">Live avg</th><th class="num">Backtest</th></tr></thead>`
    +`<tbody>${rows}</tbody></table></div>`;
}

function renderSetups(d){
  if(!d) return `<div class="panel empty">No setups data yet.</div>`;
  const syms = d.symbols||[];
  let html = "";
  if(!d.session) html += `<div class="panel empty">The session map freezes at ${esc(d.map_at)} ET on trading days.</div>`;
  for(const s of syms){
    const m = s.map||{};
    html += `<div class="panel"><div class="phead"><span class="sym">${esc(s.symbol)}</span>`
      +`<span class="sub">map frozen ${esc(m.frozen_at)} ET · flip ${fx(m.flip)}`
      +(s.last!=null?` · last ${fx(s.last)} (${esc(s.bars_to)} bar)`:"")+`</span></div>`
      + (s.error?`<div class="err">${esc(s.error)}</div>`:"")
      + `<div class="k">Setups today</div>` + eventRows(s.events||[])
      + `<div class="k">Session map</div>` + mapTable(m) + `</div>`;
  }
  for(const n of (d.notes||[])) html += `<div class="sub">${esc(n)}</div>`;
  const c = d.calibration||{};
  html += `<div class="panel"><div class="phead"><span class="sym" style="font-size:16px">Forward test</span>`
    + `<span class="sub">live results of setups this page fired, against the scorecard's backtest</span></div>`
    + recordTable(d.record)
    + `<div class="caveat"><b>*</b> A setup here is exactly a trade the scorecard measured: one fade per `
    + `wall per session on the first touch, one breakout on the first 5-minute close through, the `
    + `nearest walls each side, frozen at ${esc(d.map_at)} ET. Stops are fitted to past trades and `
    + `targets are the first wall or flip beyond a stop, as measured; a bar touching both counts as `
    + `the stop. Verdicts come from ${esc(c.days)} sessions (${esc(c.from)} to ${esc(c.to)}), so they `
    + `are provisional, and the forward test is the check on them. Bars are 5-minute and only `
    + `completed ones are used, so a trigger shows up to one bar plus one publish late. `
    + `Rows without a measured edge are dimmed. Not advice.</div></div>`;
  return html;
}

function paint(d){
  document.getElementById("root").innerHTML = renderSetups(d);
  document.getElementById("meta").innerHTML = d
    ? `<span>session <b>${esc(d.session||"—")}</b></span><span>updated <b>${esc(d.generated)}</b></span>`
      + (d.session_over?`<span>session closed</span>`:"") : "";
}

async function poll(){
  const get = async u => { try{ const r=await fetch(u,{cache:"no-store"}); if(r.ok) return await r.json(); }catch(e){} return null; };
  const t=Date.now(), m=location.hostname.match(/^([^.]+)\.github\.io$/);
  const repo=location.pathname.split("/").filter(Boolean)[0];
  const got=(await Promise.all([get("setups.json?t="+t),
    (m&&repo)?get(`https://raw.githubusercontent.com/${m[1]}/${repo}/snapshot/setups.json?t=${t}`):null]))
    .filter(Boolean);
  const best=got.reduce((a,b)=>((b.epoch||0)>(a.epoch||0)?b:a), SETUPS_DATA||{epoch:0});
  if(best && best!==SETUPS_DATA) paint(best);
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
    live = sum(1 for s in d["symbols"] for e in s["events"]
               if e.get("status") in ("armed", "open"))
    done = sum(1 for s in d["symbols"] for e in s["events"]
               if e.get("status") in FINAL)
    print(f"setups: session {d['session']}, {len(d['symbols'])} maps, "
          f"{live} live, {done} resolved, {len(d['history'])} in the record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
