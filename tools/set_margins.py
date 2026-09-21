#!/usr/bin/env python3
"""Set CME margin values in config.ini without disturbing its comments.

Rewriting through configparser would drop the guidance comments explaining
where the numbers come from and that they go stale, so this edits the value
lines in place instead.

Up and down legs are stored separately, since margin need not be symmetric;
gex_terminal falls back to the bare symbol key for a leg that is missing. Every
write stamps SET_AT, which the dashboard shows, so a band is never read without
knowing how old its inputs are.

    python tools/set_margins.py --es-up 25010 --es-down 24000
    python tools/set_margins.py --nq 39650          # both legs at once
"""
import argparse
import configparser
import datetime
import pathlib
import re
import sys

# Kept in step with INSTRUMENTS in gex_terminal.py by hand rather than by
# import, so this stays a standalone script with no network-capable module
# pulled in just to read a list of four strings.
SYMBOLS = ("ES", "NQ", "GC", "CL")
CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config.ini"
SECTION = "[margins]"
NL = chr(10)


def set_value(text, key, value):
    """Return text with `key` set to `value` under [margins]."""
    line = f"{key} = {value}"
    # Matches "ES_UP = 1", "es_up=1", and the commented-out examples.
    pattern = re.compile(r"^[ \t]*#?[ \t]*" + re.escape(key) + r"[ \t]*=.*$",
                         re.MULTILINE | re.IGNORECASE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if SECTION not in text:
        return text.rstrip(NL) + NL + NL + SECTION + NL + line + NL
    # Append at the end of the section so the guidance comments stay on top.
    start = text.index(SECTION) + len(SECTION)
    nxt = re.search(r"^\[", text[start:], re.MULTILINE)
    end = start + (nxt.start() if nxt else len(text) - start)
    body = text[start:end].rstrip(NL) + NL + line + NL
    return text[:start] + body + text[end:]


def positive(raw):
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be greater than zero")
    return f"{value:g}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for sym in (s.lower() for s in SYMBOLS):
        ap.add_argument(f"--{sym}", type=positive,
                        help=f"{sym.upper()} margin per contract, both legs")
        ap.add_argument(f"--{sym}-up", type=positive,
                        help=f"{sym.upper()} margin per contract, up leg")
        ap.add_argument(f"--{sym}-down", type=positive,
                        help=f"{sym.upper()} margin per contract, down leg")
    ap.add_argument("--path", type=pathlib.Path, default=CONFIG)
    args = ap.parse_args()

    updates = {}
    for sym in SYMBOLS:
        both = getattr(args, sym.lower())
        for side in ("UP", "DOWN"):
            value = getattr(args, f"{sym.lower()}_{side.lower()}") or both
            if value:
                updates[f"{sym}_{side}"] = value
    if not updates:
        ap.error("give at least one margin value")

    text = args.path.read_text(encoding="utf-8")
    for key, value in updates.items():
        text = set_value(text, key, value)
        print(f"  {key} = {value}")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = set_value(text, "SET_AT", stamp)
    print(f"  SET_AT = {stamp}")
    args.path.write_text(text, encoding="utf-8")

    parser = configparser.ConfigParser()
    parser.read(args.path)
    if not parser.has_section("margins"):
        print("ERROR: no [margins] section after edit", file=sys.stderr)
        return 1
    for key in sorted(updates):
        print(f"  verified {key} -> {float(parser['margins'][key]):,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
