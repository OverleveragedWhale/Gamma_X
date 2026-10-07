#!/usr/bin/env python3
"""Execute the generated page's render functions and check what they emit.

    python tools/check_page.py <index.html>          # check an existing page
    python tools/check_page.py --generate            # build one first, then check

Why this exists
---------------
On 2026-10-01 the published page went completely blank. The cause was a
duplicate `const rk` inside panel(): a lexical redefinition, which JavaScript
treats as an EARLY ERROR, so the entire script failed to compile and nothing
rendered - not a broken panel, a blank document.

Every check the repo had passed it:

    balanced braces / parens / backticks        pass
    <div> balance, header-vs-row cell counts    pass
    payload parses, PAGE_BUILD matches          pass
    esprima parse                               pass

Even a real parser passes it, because redeclaration is a semantic early error
rather than a syntax error. Every check was structural, and the bug was not.

The only thing that catches this class is running the code. panel() and
everything it calls build strings and never touch the DOM, so they execute in
a bare engine with a handful of globals stubbed, against the real payload.

The engine is a DEVELOPMENT dependency, not a runtime one - the dashboard
itself is still pure stdlib plus tzdata. quickjs is used when present;
otherwise mini-racer (V8). quickjs only ships Windows wheels up to Python 3.12
and does not build with MSVC, so on a current Python mini-racer is the one
that installs - it has a single py3 wheel per platform. When neither is
present this exits 0 with a warning rather than blocking: a machine that has
not been provisioned should still be able to publish, it just publishes
unguarded.

    pip install mini-racer        (or quickjs, on Python 3.12 and older)
"""
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Enough of a browser for code that only concatenates strings. Anything the
# page genuinely needs beyond this belongs in load(), which this does not run.
STUB = """
var document = { getElementById: function(){ return {
  textContent:"", innerHTML:"", className:"", style:{},
  classList:{add:function(){},remove:function(){}},
  addEventListener:function(){}, appendChild:function(){},
  querySelectorAll:function(){ return []; } }; },
  querySelectorAll: function(){ return []; }, addEventListener: function(){} };
var window = { addEventListener:function(){}, matchMedia:function(){ return {matches:false}; },
               location:{ reload:function(){} } };
var navigator = { clipboard:{ writeText:function(){} } };
var setTimeout = function(){}; var setInterval = function(){};
var clearInterval = function(){};
var fetch = function(){ return { then:function(){ return {then:function(){}}; } }; };
var getSelection = function(){ return { selectAllChildren:function(){} }; };
var localStorage = { getItem:function(){ return null; }, setItem:function(){} };
"""

# Lines that kick the page off; they need a live DOM and are not what this
# checks. Every definition above them is kept.
BOOTSTRAP = ("load();", "setInterval(", "document.addEventListener",
             "window.addEventListener")


def js_context():
    """A JS context exposing eval(str), and the engine's name - or (None, None).

    Both engines take the same calls here: eval() returns JS strings as str
    and raises on a compile or runtime error with the line in the message.
    """
    try:
        import quickjs
        return quickjs.Context(), "quickjs"
    except ImportError:
        pass
    try:
        from py_mini_racer import MiniRacer
        return MiniRacer(), "V8 (mini-racer)"
    except ImportError:
        return None, None


def fail(msg):
    print(f"FAIL  {msg}")
    return 1


def ok(msg):
    print(f"ok    {msg}")
    return 0


def script_of(html):
    return html[html.index("<script>") + len("<script>"):html.rindex("</script>")]


def payload_of(html):
    marker = "const SNAPSHOT_DATA = "
    i = html.index(marker) + len(marker)
    return json.JSONDecoder().raw_decode(html, i)[0]


def check_setups(ctx, engine, html):
    """setups.html: same principle - compile it, then RUN its renderer on the
    data baked into it and check what comes out."""
    js = "\n".join(l for l in script_of(html).split("\n")
                   if not l.strip().startswith(("paint(", "setInterval(")))
    try:
        ctx.eval(STUB + "\n" + js)
    except Exception as exc:
        return fail(f"setups script does not compile: {str(exc)[:200]}")
    ok(f"setups script compiles in a real engine ({engine})")
    bad = 0
    try:
        out = ctx.eval("renderSetups(SETUPS_DATA)")
    except Exception as exc:
        return fail(f"renderSetups threw: {str(exc)[:200]}")
    if not out or len(out) < 200:
        bad += fail(f"renderSetups returned {len(out or '')} chars")
    if "undefined" in (out or ""):
        bad += fail("renderSetups: the string 'undefined' reached the page")
    if (out or "").count("<div") != (out or "").count("</div>"):
        bad += fail("renderSetups: unbalanced <div> in output")
    for i, table in enumerate(re.findall(r"<table>.*?</table>", out or "", re.S)):
        for problem in check_rows(f"setups table {i + 1}", table):
            bad += fail(problem)
    if not bad:
        ok(f"renderSetups renders, {len(out):,} chars, cells balanced")
    print()
    print("PAGE OK" if not bad else f"{bad} PROBLEM(S) - do not publish")
    return 1 if bad else 0


def check_rows(name, table_html):
    """Every row in a rendered table must carry the header's cell count.

    Checked on the OUTPUT rather than on the source that builds it, so a
    conditional branch that emits one cell too few is caught even when the
    template it came from looked symmetrical.
    """
    # EVERY table in the panel, not the first. A panel carries the book
    # summary, the ladder and max pain; checking only the first one passed a
    # ladder whose spot row was a cell short, which is exactly the mistake
    # this is here to catch.
    bad = []
    tables = re.findall(r"<table>.*?</table>", table_html, re.S)
    if not tables:
        return [f"{name}: no <table> in the output"]
    for t, table in enumerate(tables, 1):
        head = re.search(r"<thead>.*?</thead>", table, re.S)
        body = re.search(r"<tbody>(.*?)</tbody>", table, re.S)
        if not head or not body:
            bad.append(f"{name} table {t}: missing thead/tbody")
            continue
        want = len(re.findall(r"<th[\s>]", head.group(0)))
        for n, row in enumerate(re.findall(r"<tr.*?</tr>", body.group(1), re.S), 1):
            got = len(re.findall(r"<td[\s>]", row))
            if got != want:
                bad.append(f"{name} table {t} row {n}: {got} cells vs "
                           f"{want} in its header")
    return bad


def main():
    args = sys.argv[1:]
    tmp = None
    if "--generate" in args:
        tmp = Path(tempfile.mkdtemp()) / "index.html"
        r = subprocess.run([sys.executable, str(ROOT / "gex_terminal.py"),
                            "--snapshot", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return fail(f"snapshot generation failed: {r.stderr.strip()[:200]}")
        target = tmp
    else:
        rest = [a for a in args if not a.startswith("-")]
        if not rest:
            print(__doc__)
            return 2
        target = Path(rest[0])

    if not target.exists():
        return fail(f"no such page: {target}")
    html = io.open(target, encoding="utf-8").read()

    ctx, engine = js_context()
    if ctx is None:
        print("WARN  no JS engine installed - the page is NOT being checked.")
        print("WARN  pip install mini-racer    (development only; the dashboard")
        print("WARN  itself stays pure stdlib)")
        return 0

    if "function renderSetups" in html:
        return check_setups(ctx, engine, html)

    bad = 0
    js = "\n".join(l for l in script_of(html).split("\n")
                   if not l.strip().startswith(BOOTSTRAP))

    # 1. Does the script COMPILE? This is the check that would have caught the
    #    duplicate const, and the only one that catches an early error.
    try:
        ctx.eval(STUB + "\n" + js)
    except Exception as exc:
        line = re.search(r":(\d+)", str(exc))
        where = ""
        if line:
            n = int(line.group(1)) - STUB.count("\n") - 1
            src = js.split("\n")
            if 0 < n <= len(src):
                where = f"\n      near JS line {n}: {src[n - 1].strip()[:110]}"
            bad += fail(f"script does not compile: {str(exc)[:160]}{where}")
        else:
            bad += fail(f"script does not compile: {str(exc)[:200]}")
        return 1
    ok(f"script compiles in a real engine ({engine})")

    data = payload_of(html)

    # 2. Does panel() actually RUN, for every symbol, and emit something?
    for sym in data.get("symbols", []):
        name = sym.get("symbol", "?")
        ctx.eval("var __s = " + json.dumps(sym) + ";")
        try:
            out = ctx.eval("panel(__s)")
        except Exception as exc:
            bad += fail(f"panel({name}) threw: {str(exc)[:200]}")
            continue
        # A symbol whose data failed to load is MEANT to render a short error
        # panel. Treating that as a broken page blocked every symbol's good
        # figures on 2026-10-03 at 00:00 and 04:00 over one stalled download.
        # Still executed above, so a throw in the error path is caught; it is
        # a warning in the log, not a reason to withhold the page.
        if sym.get("ok") is False:
            if out and "error" in out:
                print(f"WARN  panel({name}) shows its data error: "
                      f"{str(sym.get('error'))[:120]}")
            else:
                bad += fail(f"panel({name}) failed to load and rendered no error")
            continue
        if not out or len(out) < 500:
            bad += fail(f"panel({name}) returned {len(out or '')} chars")
            continue

        # 3. Check the rendered output, not the template that made it.
        for problem in check_rows(name, out):
            bad += fail(problem)
        if out.count("<div") != out.count("</div>"):
            bad += fail(f"panel({name}): unbalanced <div> in output")
        if "undefined" in out:
            bad += fail(f"panel({name}): the string 'undefined' reached the page")
        if not bad:
            ok(f"panel({name}) renders, {len(out):,} chars, cells balanced")

    # 4. The footer touches the whole payload rather than one symbol.
    ctx.eval("var __d = " + json.dumps({k: v for k, v in data.items()
                                        if k != "symbols"}) + ";")
    try:
        ctx.eval('(__d.vol && __d.vol["30D"]) ? "y" : "n"')
        ok("footer payload access")
    except Exception as exc:
        bad += fail(f"footer threw: {str(exc)[:160]}")

    if tmp:
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass

    print()
    print("PAGE OK" if not bad else f"{bad} PROBLEM(S) - do not publish")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
