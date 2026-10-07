#!/usr/bin/env python3
"""The scorecard lab: every scored rule trade, replayable in the browser at any
stop, target, filter and time window. Writes scorecard.html beside index.html.

    python lab.py --pub ../Gamma_X-snapshot

Why
---
The rule sizes (NQ 40, GC 10) were picked by running one-off scripts. Checking
whether another value does better should not need a script: the scorecard now
stores each trade's PATH - how far it went for and against, bar by bar, and
its open result every half hour (tools/score_levels.trade_path) - and the page
replays those exactly as the scorecard's replay() does, so a figure read off
the page is the figure the scorecard would print.

Two windows, as tabs:
  day       levels at 09:50, traded to the 16:00 cash close
  evening   levels from the first snapshot after the 18:00 Globex roll,
            traded to 03:00 ET (London open)

The data is tools/score_levels.py --trades, refreshed by the weekly calibration
and merged rather than replaced, so sessions older than Yahoo's 60 days of
5-minute bars stay in.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import gex_terminal as g          # noqa: E402

TRADES_PATH = ROOT / "rules_trades.json"


def write(pub, trades_path=TRADES_PATH):
    try:
        data = json.loads(Path(trades_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if data is not None:
        data["generated"] = g.now_et().strftime("%Y-%m-%d %H:%M ET")
        data["epoch"] = time.time()
    blob = json.dumps(data, separators=(",", ":"))
    out = Path(pub) / "scorecard.html"
    out.write_text(PAGE.replace("const LAB_DATA = null;", f"const LAB_DATA = {blob};"),
                   encoding="utf-8")
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Gamma Scorecard</title>
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
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 12px;margin-bottom:18px}
h1{font-family:system-ui,sans-serif;font-weight:800;font-size:19px;letter-spacing:.04em;margin:0}
h1 .g{color:var(--brass)}
.meta{color:var(--muted);font-size:12px}
.navs{margin-left:auto;display:flex;gap:8px}
.navlink{color:var(--brass);text-decoration:none;border:1px solid var(--brass);
  border-radius:4px;padding:5px 10px;font-size:12px;letter-spacing:.06em}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.tab{background:none;border:1px solid var(--line);color:var(--muted);border-radius:6px;
  padding:8px 14px;font:inherit;cursor:pointer}
.tab.on{color:var(--ink);border-color:var(--brass);background:rgba(217,164,65,.08)}
.tab small{display:block;font-size:10.5px;color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:16px;margin-bottom:16px;min-width:0}
.controls{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px 14px}
.controls label{display:flex;flex-direction:column;gap:4px;font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.controls select,.controls input{background:var(--raised);color:var(--ink);border:1px solid var(--line);
  border-radius:4px;padding:6px 8px;font:inherit;font-size:13px;min-width:0;width:100%}
.controls .chk{flex-direction:row;align-items:center;gap:8px;padding-top:16px}
.controls .chk input{width:auto}
.k{font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);margin:16px 0 6px}
.k:first-child{margin-top:0}
.sub{color:var(--muted);font-size:11.5px}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:10px}
.stat{background:var(--raised);border-radius:6px;padding:9px 11px}
.stat .v{font-size:18px;font-weight:600;margin-top:3px}
.stat .l{font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;font-size:12.5px;width:100%}
th{text-align:left;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:500;padding:4px 10px 4px 0;border-bottom:1px solid var(--line)}
td{padding:5px 10px 5px 0;border-bottom:1px solid rgba(255,255,255,.04);font-variant-numeric:tabular-nums}
td.num,th.num{text-align:right}
tr.cur td{background:rgba(217,164,65,.10)}
tr.thin td{opacity:.5}
tr.pick{cursor:pointer}
tr.pick:hover td{background:rgba(255,255,255,.04)}
.pos{color:var(--jade)}.neg{color:var(--verm)}
.side-c{color:var(--jade)}.side-p{color:var(--verm)}
.chip{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;padding:1px 6px;
  border:1px solid var(--line);border-radius:3px;color:var(--muted);white-space:nowrap}
.g-neg{color:#ffb38a;border-color:rgba(224,96,63,.5)}
.g-pos{color:#9fd8bd;border-color:rgba(75,191,138,.5)}
.best{color:var(--brass);border-color:var(--brass)}
.heat{border-collapse:separate;border-spacing:2px;width:auto;font-size:10.5px}
.heat td,.heat th{padding:3px 4px;text-align:center;border:0;min-width:34px}
.heat td{cursor:pointer;border-radius:3px}
.heat td.cur{outline:2px solid var(--brass)}
.heat .rowh{text-align:right;padding-right:8px;color:var(--muted);cursor:default}
.warn{color:var(--brass);font-size:12px;margin-top:8px}
.empty{color:var(--muted);font-size:12px;padding:6px 0}
.caveat{color:var(--muted);font-size:11px;line-height:1.55;max-width:84ch;margin-top:14px}
.caveat b{color:var(--brass);font-weight:500}
</style></head>
<body><div class="wrap">
<header>
  <h1>Gamma<span class="g">/</span>Scorecard<b style="color:var(--brass)">*</b></h1>
  <div class="meta" id="meta"></div>
  <div class="navs"><a class="navlink" href="setups.html">Setups</a><a class="navlink" href="./">Dashboard</a></div>
</header>
<div class="tabs" id="tabs"></div>
<div class="panel"><div class="controls" id="controls"></div></div>
<div id="out"></div>
</div>
<script>
const LAB_DATA = null;
// Sweep grids, in points: the values the tables try for each symbol.
const GRID = {NQ:[10,120,5], GC:[2,30,1], ES:[4,40,2]};
const MIN_N = 10;                 // fewer trades than this: shown, but dimmed
const WIN_LABEL = {evening:"Evening", day:"Day"};
const esc = v => String(v==null?"":v).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fx = (v,d=2) => v==null||!isFinite(v) ? "—" : (+v).toFixed(d);
const sgn = (v,d=2) => v==null||!isFinite(v) ? "—" : `${v>0?"+":""}${(+v).toFixed(d)}`;
const rFmt = v => v==null||!isFinite(v) ? "—" : `${sgn(v)}R`;
const usd = v => v==null||!isFinite(v) ? "—" : `${v<0?"−":v>0?"+":""}$${Math.round(Math.abs(v)).toLocaleString()}`;
const pc = v => v==null||!isFinite(v) ? "—" : `${Math.round(v*100)}%`;
const cls = v => v>0 ? "pos" : v<0 ? "neg" : "";
const chip = (t,c) => `<span class="chip ${c||""}">${esc(t)}</span>`;
const regimeChip = r => r ? chip(r, r==="negative"?"g-neg":"g-pos") : "";

function windowsOf(d){ return d ? ["evening","day"].filter(w=>d[w]) : []; }
function times(d, win){
  const w = (d||{})[win]||{};
  return win==="evening" ? {start:w.map_from||"18:00", end:w.end||"03:00"}
                         : {start:w.map_at||"09:50", end:w.end||"16:00"};
}
// Minutes into the window; the evening runs past midnight.
function ord(hhmm, win){
  const [h,m] = String(hhmm).split(":").map(Number);
  return h*60 + m + (win==="evening" && h<12 ? 1440 : 0);
}
function halfHours(d, win){
  const t = times(d, win), out = [];
  let m = ord(t.start, win), e = ord(t.end, win);
  m = Math.ceil(m/30)*30;
  for(; m<=e; m+=30){ const h=Math.floor(m/60)%24, mm=m%60;
    out.push(`${String(h).padStart(2,"0")}:${String(mm).padStart(2,"0")}`); }
  return out;
}
function defaultState(d){
  const pts = (d&&d.points)||{};
  const sym = "NQ";
  return {win: windowsOf(d)[0]||"evening", sym, setup:"all", regime:"all", flip:"all",
          side:"all", basis:"all", from:"", to:"", enter_from:"", enter_to:"", flat:"",
          stop: pts[sym]||40, target: pts[sym]||40, link:true};
}

// --- replay: tools/score_levels.replay(), plus an optional flat-by time --------
const first = (steps, x) => { for(const [k,v] of steps) if(v>=x) return k; return null; };
function markAt(t, flat, win){
  if(!flat) return t.marks[t.marks.length-1];
  let m = null;
  for(const x of t.marks){ if(ord(x[0],win) <= ord(flat,win)) m = x; else break; }
  return m;
}
function replay(t, stop, target, flat, win){
  const m = markAt(t, flat, win);
  if(!m) return null;                       // entered after flat-by: no trade
  const ks = first(t.adv, stop), kt = first(t.fav, target);
  if(ks!=null && ks<=m[1] && (kt==null || ks<=kt)) return {how:"stop", pts:-stop};
  if(kt!=null && kt<=m[1]) return {how:"target", pts:target};
  return {how:"close", pts:m[2]};
}

function filtered(d, S){
  const win = S.win;
  return ((d&&d.trades)||[]).filter(t =>
    (t.window||"day")===win && t.symbol===S.sym
    && (S.setup==="all" || t.setup===S.setup)
    && (S.regime==="all" || t.regime===S.regime)
    && (S.flip==="all" || t.flip_side===S.flip)
    && (S.side==="all" || t.side===S.side)
    && (S.basis==="all" || (t.basis||"close")===S.basis)
    && (!S.from || t.date>=S.from) && (!S.to || t.date<=S.to)
    && (!S.enter_from || ord(t.time,win) >= ord(S.enter_from,win))
    && (!S.enter_to || ord(t.time,win) <= ord(S.enter_to,win))
    && (!S.flat || ord(t.time,win) < ord(S.flat,win)));
}
function run(trades, stop, target, S, mult){
  const rows = [];
  for(const t of trades){
    const r = replay(t, stop, target, S.flat, S.win);
    if(r) rows.push({t, ...r, usd: r.pts*mult});
  }
  const n = rows.length;
  const st = {n, won:0, lost:0, closed:0, pts:0, usd:0, dd:0};
  let eq = 0, peak = 0;
  for(const r of rows){
    st[r.how==="target"?"won":r.how==="stop"?"lost":"closed"]++;
    st.pts += r.pts; eq += r.usd; peak = Math.max(peak, eq); st.dd = Math.min(st.dd, eq-peak);
  }
  st.usd = eq;
  st.avg = n ? st.pts/n : null;
  st.r = n ? st.avg/stop : null;
  st.per = n ? st.usd/n : null;
  st.win = n ? st.won/n : null;
  st.rows = rows;
  return st;
}
function gridOf(sym){
  const [lo,hi,step] = GRID[sym] || [5,100,5], out=[];
  for(let v=lo; v<=hi+1e-9; v+=step) out.push(+v.toFixed(2));
  return out;
}

// --- render -------------------------------------------------------------------
function statBox(l, v, c){ return `<div class="stat"><div class="l">${l}</div><div class="v ${c||""}">${v}</div></div>`; }

function sweepTable(trades, S, mult){
  const grid = gridOf(S.sym);
  const res = grid.map(v=>({v, st: run(trades, v, v, S, mult)}));
  const ok = res.filter(x=>x.st.n>=MIN_N);
  const best = ok.length ? ok.reduce((a,b)=>b.st.usd>a.st.usd?b:a).v : null;
  const rows = res.map(({v,st})=>{
    const c = [S.link && v===+S.stop ? "cur" : "", st.n<MIN_N ? "thin" : "", "pick"].join(" ");
    return `<tr class="${c}" onclick="pick(${v},${v})"><td class="num">${fx(v, v%1?1:0)}${v===best?" "+chip("best","best"):""}</td>`
      +`<td class="num">${st.n}</td><td class="num">${st.won}/${st.lost}/${st.closed}</td>`
      +`<td class="num">${pc(st.win)}</td><td class="num ${cls(st.r)}">${rFmt(st.r)}</td>`
      +`<td class="num ${cls(st.avg)}">${sgn(st.avg)}</td><td class="num ${cls(st.per)}">${usd(st.per)}</td>`
      +`<td class="num ${cls(st.usd)}">${usd(st.usd)}</td><td class="num neg">${usd(st.dd)}</td></tr>`;
  }).join("");
  return `<div class="scroll"><table><thead><tr><th class="num">Points 1:1</th><th class="num">Trades</th>`
    +`<th class="num">Won/lost/time</th><th class="num">Win</th><th class="num">Avg R</th>`
    +`<th class="num">Avg pts</th><th class="num">Per trade</th><th class="num">Total</th>`
    +`<th class="num">Max DD</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function heatmap(trades, S, mult){
  const grid = gridOf(S.sym);
  const cells = grid.map(s=>grid.map(t=>run(trades, s, t, S, mult)));
  let mx = 0;
  for(const row of cells) for(const st of row) if(st.n) mx = Math.max(mx, Math.abs(st.per));
  const head = grid.map(t=>`<th>${fx(t, t%1?1:0)}</th>`).join("");
  const body = grid.map((s,i)=>`<tr><td class="rowh">${fx(s, s%1?1:0)}</td>`+cells[i].map((st,j)=>{
    const v = st.per, a = mx ? Math.min(1, Math.abs(v||0)/mx) : 0;
    const bg = v>0 ? `rgba(75,191,138,${(.08+.6*a).toFixed(2)})` : v<0 ? `rgba(224,96,63,${(.08+.6*a).toFixed(2)})` : "transparent";
    const cur = s===+S.stop && grid[j]===+S.target ? "cur" : "";
    return `<td class="${cur}" style="background:${bg}" title="stop ${s} / target ${grid[j]}: ${st.n} trades, ${pc(st.win)} won, ${usd(v)} per trade" onclick="pick(${s},${grid[j]})">${v==null?"":Math.round(v)}</td>`;
  }).join("")+`</tr>`).join("");
  return `<div class="scroll"><table class="heat"><thead><tr><th class="rowh">stop ↓ target →</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function breakdown(d, S, mult){
  const base = Object.assign({}, S, {setup:"all", regime:"all"});
  const all = filtered(d, base);
  const rows = [];
  for(const setup of ["fade","breakout"]) for(const regime of ["negative","positive"]){
    const ts = all.filter(t=>t.setup===setup && t.regime===regime);
    const st = run(ts, +S.stop, +S.target, S, mult);
    const on = (S.setup==="all"||S.setup===setup) && (S.regime==="all"||S.regime===regime);
    rows.push(`<tr class="${on?"":"thin"} ${st.n<MIN_N?"thin":""}"><td>${setup}</td><td>${regimeChip(regime)}</td>`
      +`<td class="num">${st.n}</td><td class="num">${pc(st.win)}</td><td class="num ${cls(st.r)}">${rFmt(st.r)}</td>`
      +`<td class="num ${cls(st.per)}">${usd(st.per)}</td><td class="num ${cls(st.usd)}">${usd(st.usd)}</td></tr>`);
  }
  return `<div class="scroll"><table><thead><tr><th>Setup</th><th>Regime</th><th class="num">Trades</th>`
    +`<th class="num">Win</th><th class="num">Avg R</th><th class="num">Per trade</th><th class="num">Total</th>`
    +`</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function tradeTable(st){
  if(!st.rows.length) return `<div class="empty">No trades match.</div>`;
  const rows = st.rows.slice().reverse().map(r=>{
    const t = r.t;
    return `<tr><td>${esc(t.date)}</td><td class="num">${esc(t.time)}</td><td>${esc(t.setup)}</td>`
      +`<td class="side-${t.side[0]}">${esc(t.side)}</td><td>${regimeChip(t.regime)}</td>`
      +`<td>${esc(t.flip_side||"")}</td><td class="num">${fx(t.level)}</td><td class="num">${fx(t.entry)}</td>`
      +`<td>${r.how==="close"?"time":r.how}</td><td class="num ${cls(r.pts)}">${sgn(r.pts)}</td>`
      +`<td class="num ${cls(r.usd)}">${usd(r.usd)}</td></tr>`;
  }).join("");
  return `<div class="scroll"><table><thead><tr><th>Date</th><th class="num">Time</th><th>Setup</th><th>Wall</th>`
    +`<th>Regime</th><th>Flip</th><th class="num">Wall</th><th class="num">Entry</th><th>Exit</th>`
    +`<th class="num">Pts</th><th class="num">$</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderLab(d, S){
  if(!d || !(d.trades||[]).length)
    return `<div class="panel empty">No scored trades yet. The weekly calibration writes them.</div>`;
  const mult = (d.multipliers||{})[S.sym] || 1;
  const trades = filtered(d, S);
  const st = run(trades, +S.stop, +S.target, S, mult);
  const t = times(d, S.win);
  const dates = new Set(trades.map(x=>x.date));
  let html = `<div class="panel"><div class="k">${WIN_LABEL[S.win]} · ${esc(S.sym)} · stop ${fx(S.stop,S.stop%1?1:0)} / target ${fx(S.target,S.target%1?1:0)} pts`
    + (S.flat?` · flat by ${esc(S.flat)}`:` · held to ${esc(t.end)}`)+`</div><div class="stats">`
    + statBox("Trades", `${st.n}<span class="sub"> / ${dates.size} sessions</span>`)
    + statBox("Won / lost / time", `${st.won} / ${st.lost} / ${st.closed}`)
    + statBox("Win rate", pc(st.win))
    + statBox("Avg R", rFmt(st.r), cls(st.r))
    + statBox("Avg points", sgn(st.avg), cls(st.avg))
    + statBox("Per trade, 1 lot", usd(st.per), cls(st.per))
    + statBox("Total, 1 lot", usd(st.usd), cls(st.usd))
    + statBox("Max drawdown", usd(st.dd), "neg")
    + `</div>`
    + (st.n && st.n<MIN_N ? `<div class="warn">Only ${st.n} trades: too few to tell a value from luck.</div>` : "")
    + `<div class="k">By setup and gamma regime, at these values</div>` + breakdown(d, S, mult)
    + `</div>`;
  html += `<div class="panel"><div class="k">1:1 sweep: stop = target</div>`
    + `<div class="sub">Compare rows by points or dollars, not R: R is per unit of risk, and the risk is what changes down this table. Click a row to use it.</div>`
    + sweepTable(trades, S, mult)
    + `<div class="k">Every stop and target: average $ per trade, 1 lot</div>`
    + `<div class="sub">Rows are stops, columns targets. Click a cell to use it.</div>`
    + heatmap(trades, S, mult) + `</div>`;
  html += `<div class="panel"><div class="k">Trades at these values, newest first</div>` + tradeTable(st)
    + `<div class="caveat"><b>*</b> Every figure here is the past, replayed. The best row of a sweep is the `
    + `value that fitted these particular trades best, and on a few dozen trades it will rarely be the best on `
    + `the next few dozen: prefer a value inside a broad region that works over a lone peak, and rows under `
    + `${MIN_N} trades (dimmed) say almost nothing. Trades are the scorecard's: one fade (filled at the wall on `
    + `first touch) and one breakout (on the first 5-minute close through) per wall, the nearest two walls `
    + `each side, skipping walls price had already passed when the map was taken. Day: map at ${esc(times(d,"day").start)} ET, `
    + `held to ${esc(times(d,"day").end)}. Evening: the first snapshot after the ${esc(times(d,"evening").start)} Globex `
    + `roll, held to ${esc(times(d,"evening").end)} ET. A 5-minute bar touching both stop and target counts as the stop; `
    + `a trade neither stopped nor paid by the end (or by flat-by) is closed at that bar's close, "time" `
    + `above. "Regime" is the map's near-book net gamma. No commission or slippage. Not advice.</div></div>`;
  return html;
}

// --- page wiring (needs a DOM; not run by the render check) ---------------------
let S = null;
function opts(list, cur){ return list.map(([v,l])=>`<option value="${esc(v)}"${String(v)===String(cur)?" selected":""}>${esc(l)}</option>`).join(""); }
function controls(d){
  const syms = [...new Set(((d&&d.trades)||[]).map(t=>t.symbol))].sort();
  const hh = halfHours(d, S.win).map(h=>[h,h]);
  const step = (GRID[S.sym]||[0,0,1])[2] < 1 ? 0.5 : (S.sym==="GC" ? 0.5 : 1);
  const sel = (k, label, list) => `<label>${label}<select onchange="set('${k}',this.value)">${opts(list, S[k])}</select></label>`;
  const num = (k, label) => `<label>${label}<input type="number" min="0" step="${step}" value="${esc(S[k])}" onchange="set('${k}',this.value)"></label>`;
  const date = (k, label) => `<label>${label}<input type="date" value="${esc(S[k])}" onchange="set('${k}',this.value)"></label>`;
  document.getElementById("controls").innerHTML =
      sel("sym","Symbol", syms.map(s=>[s, s + ((d.reference||[]).includes(s)?" (reference, not traded)":"")]))
    + sel("setup","Setup", [["all","Both"],["fade","Fade"],["breakout","Breakout"]])
    + sel("regime","Gamma regime", [["all","Any"],["negative","Negative"],["positive","Positive"]])
    + sel("flip","Entry vs flip", [["all","Any"],["below","Below flip"],["above","Above flip"]])
    + sel("side","Wall", [["all","Both"],["call","Call walls"],["put","Put walls"]])
    + sel("basis","Gamma read at", [["all","Any"],["close","4pm close (old)"],["live","Live future"],["chain","Chain, cash hours"]])
    + num("stop","Stop, pts") + num("target","Target, pts")
    + `<label class="chk"><input type="checkbox" ${S.link?"checked":""} onchange="set('link',this.checked)">Keep 1:1</label>`
    + sel("enter_from","Enter from", [["","Map time"], ...hh])
    + sel("enter_to","No entries after", [["","End"], ...hh])
    + sel("flat","Flat by", [["",`Window end (${times(d,S.win).end})`], ...hh])
    + date("from","Sessions from") + date("to","Sessions to");
}
function tabs(d){
  document.getElementById("tabs").innerHTML = windowsOf(d).map(w=>{
    const t = times(d, w), n = (d.trades||[]).filter(x=>(x.window||"day")===w && x.symbol===S.sym).length;
    return `<button class="tab ${w===S.win?"on":""}" onclick="set('win','${w}')">${WIN_LABEL[w]}<small>${esc(t.start)}–${esc(t.end)} ET · ${n} ${esc(S.sym)} trades</small></button>`;
  }).join("");
}
function set(k, v){
  if(k==="stop" || k==="target"){
    v = Math.max(0.1, +v || 0.1);
    if(S.link){ S.stop = v; S.target = v; } else S[k] = v;
  } else if(k==="link"){
    S.link = v; if(v) S.target = S.stop;
  } else {
    S[k] = v;
    if(k==="sym"){ const p=(LAB_DATA.points||{})[v] || (GRID[v]||[0,40])[1]/3; S.stop = S.target = +(+p).toFixed(1); }
    if(k==="win"){ S.enter_from = S.enter_to = S.flat = ""; }
  }
  paint();
}
function pick(s, t){ S.stop = s; S.target = t; S.link = s===t; paint(); }
function paint(){
  const d = LAB_DATA;
  try{ history.replaceState(null, "", "#" + new URLSearchParams(Object.entries(S).map(([k,v])=>[k,String(v)])).toString()); }catch(e){}
  if(d){
    document.getElementById("meta").textContent = `trades scored ${d.scored_at||"—"} · page ${d.generated||"—"}`;
    tabs(d); controls(d);
  }
  document.getElementById("out").innerHTML = renderLab(d, S);
}
function boot(){
  S = defaultState(LAB_DATA);
  try{
    const h = new URLSearchParams(location.hash.slice(1));
    for(const [k,v] of h) if(k in S) S[k] = k==="link" ? v==="true" : (k==="stop"||k==="target") ? +v : v;
  }catch(e){}
  paint();
}
boot();
</script></body></html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pub", required=True, help="folder holding index.html")
    ap.add_argument("--trades", default=str(TRADES_PATH))
    a = ap.parse_args()
    print(f"scorecard -> {write(a.pub, a.trades)}")
