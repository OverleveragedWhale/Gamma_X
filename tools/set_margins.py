#!/usr/bin/env python3
"""Set CME margin values in config.ini without disturbing its comments.

Rewriting the file through configparser would drop the guidance comments that
explain where the numbers come from and that they go stale, so this edits the
value lines in place instead: an existing entry - including the commented-out
examples the file ships with - is replaced, otherwise the entry is inserted
under [margins].

    python tools/set_margins.py --es 25010 --nq 39650
"""
import argparse
import pathlib
import re
import sys

CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config.ini"
SECTION = "[margins]"


def set_value(text, key, value):
    """Return text with `key` set to `value` under [margins]."""
    line = f"{key} = {value}"
    # Matches "ES = 1", "es=1", and the shipped "# ES  = 25010" examples.
    pattern = re.compile(rf"^[ \t]*#?[ \t]*{re.escape(key)}[ \t]*=.*$",
                         re.MULTILINE | re.IGNORECASE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if SECTION not in text:
        return text.rstrip("\n") + f"\n\n{SECTION}\n{line}\n"
    head, _, tail = text.partition(SECTION)
    return f"{head}{SECTION}\n{line}{tail}"


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
    ap.add_argument("--es", type=positive, help="ES initial margin per contract, USD")
    ap.add_argument("--nq", type=positive, help="NQ initial margin per contract, USD")
    ap.add_argument("--path", type=pathlib.Path, default=CONFIG)
    args = ap.parse_args()

    if not args.es and not args.nq:
        ap.error("give --es and/or --nq")

    text = args.path.read_text(encoding="utf-8")
    for key, value in (("ES", args.es), ("NQ", args.nq)):
        if value:
            text = set_value(text, key, value)
            print(f"  {key} = {value}")
    args.path.write_text(text, encoding="utf-8")

    # Prove the result is loadable the same way the dashboard loads it.
    import configparser
    parser = configparser.ConfigParser()
    parser.read(args.path)
    if not parser.has_section("margins"):
        print("ERROR: no [margins] section after edit", file=sys.stderr)
        return 1
    for key in ("ES", "NQ"):
        if key in parser["margins"]:
            print(f"  verified {key} -> {float(parser['margins'][key]):,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
