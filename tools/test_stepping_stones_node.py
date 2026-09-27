#!/usr/bin/env python3
"""Stepping stones mapped as a POINT are refused, as they are on a way.

    python tools/test_stepping_stones_node.py     (or: pytest tools/...)

The way half of every generated profile blocks `ford=stepping_stones`
outright, whatever `avoid_fords` says: no motor vehicle of any width gets over
one. Round 13 taught the node half about fords mapped as a point, but only
`ford=yes`. The engine's lookup table carries `ford=stepping_stones` in the
node context too (310 nodes in the shipped lookups.dat), and nothing in the
node half reads it, so a stepping-stones node on a way a profile otherwise
opens is priced like any other point and routed over.

Plain asserts, so pytest reports it, and a main, so CI's
`python tools/test_*.py` loop runs it.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_profiles as bp    # noqa: E402

BASE_DIR = os.path.join(HERE, "profiles_base")
KEYS = ("fast", "fun", "lanes")


def _initialcost(text):
    node = text.split("---context:node", 1)[1]
    m = re.search(r"^assign initialcost\s*=(.*?)(?=^assign )", node,
                  re.M | re.S)
    assert m, "premise: the node half assigns initialcost"
    return [l.strip() for l in m.group(1).splitlines() if l.strip()]


def test_stepping_stones_on_a_node_are_refused_before_caraccess():
    base = bp.read_base(BASE_DIR)
    for key in KEYS:
        text = bp.profile_text(base, key)
        way = text.split("---context:node", 1)[0]
        assert "ford=stepping_stones" in way, \
            "%s: premise, the way half refuses stepping stones" % key
        lines = _initialcost(text)
        access = [i for i, l in enumerate(lines)
                  if l.startswith("switch caraccess")]
        assert len(access) == 1, "%s: premise, caraccess found" % key
        refusal = [i for i, l in enumerate(lines)
                   if re.match(r"switch\s+(?:\w+\s+)*"
                               r"ford=(?:[\w|]*\|)?stepping_stones[\w|]*"
                               r"\s+1000000$", l)]
        assert refusal, ("%s: no clause of the node initialcost refuses "
                         "ford=stepping_stones: %r" % (key, lines))
        assert refusal[0] < access[0], \
            "%s: the refusal must come before caraccess" % key


def main():
    try:
        test_stepping_stones_on_a_node_are_refused_before_caraccess()
    except AssertionError as e:
        print("FAILED: %s" % e)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
