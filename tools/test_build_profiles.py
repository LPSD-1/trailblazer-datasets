#!/usr/bin/env python3
"""The routing profiles are built here, and the app ships what is built here.

    python tools/test_build_profiles.py

Two things these checks hold:

1. THE GENERATOR APPLIES EVERY STEP. `insert_vehicle_model` was written and
   never called from `build()`, so this generator's output had no vehicle
   model while the app's profiles carried one by hand - and a re-run would
   have dropped it, and with it every refusal that keeps a motorbike off a
   footpath.

2. THE REFUSALS ARE IN THE OUTPUT, in all three profiles: a path is not a
   road whatever generic tag it carries, a motorbike is asked its own access
   question, a ford mapped as a point is refused when fords are avoided,
   stepping stones mapped as a point are refused whatever the switches say,
   and no profile grants a track that carries no access tag at all.

The stock base is vendored in tools/profiles_base/car-vario.brf (BRouter,
MIT) so these run without a BRouter checkout. The comparison with the app's
own files runs when the app is checked out beside this repository (or at
TRAILBLAZER_APP_ROUTING); where it is not - CI - it says so, by name.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_profiles as bp    # noqa: E402

BASE_DIR = os.path.join(HERE, "profiles_base")
APP_ROUTING = os.environ.get(
    "TRAILBLAZER_APP_ROUTING",
    os.path.join(HERE, "..", "..", "greenroadmap-app", "assets", "routing"))
KEYS = ("fast", "fun", "lanes")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + repr(detail)) if detail else ""))


def built():
    base = bp.read_base(BASE_DIR)
    return {k: bp.profile_text(base, k) for k in KEYS}


def assigned(text, name):
    m = re.search(r"^assign\s+%s\s+=\s*(\S+)" % re.escape(name), text, re.M)
    return m.group(1) if m else None


def block(text, name):
    """The body of a multi-line `assign name =`, up to the next blank line."""
    m = re.search(r"^assign %s\s*=?\s*\n((?:.+\n)+)" % re.escape(name),
                  text, re.M)
    return m.group(1) if m else ""


def exits(fn, *args):
    try:
        fn(*args)
    except SystemExit:
        return True
    return False


def test_the_vehicle_model_is_in_every_profile():
    for key, text in built().items():
        # Premise: a profile, not an empty string that asserts nothing.
        check("%s is a profile" % key,
              "---context:way" in text and "---context:node" in text)
        check("%s blocks on the vehicle rules" % key,
              "  else if tb_vehicle_blocked then 10000\n" in text)
        check("%s costs the vehicle penalty" % key,
              "  add tb_vehicle_penalty\n" in text)
        check("%s reads barriers at nodes" % key,
              "switch tb_barrier_impassable" in text)


def test_no_profile_grants_a_track_nothing_opens():
    # The stock `switch and highway=track use_offroad 1` is reached only when
    # a track carries no access tag of any kind.
    for key, text in built().items():
        check("%s: premise, the stock clause the switch drives" % key,
              "switch and highway=track use_offroad" in text)
        check("%s: use_offroad is false" % key,
              assigned(text, "use_offroad") == "false",
              assigned(text, "use_offroad"))


def test_a_path_is_not_a_road_whatever_generic_tag_it_carries():
    for key, text in built().items():
        check("%s: tb_nonmotor_way blocks" % key,
              "or tb_nonmotor_way" in block(text, "tb_vehicle_blocked"))
        check("%s: the path classes" % key,
              "highway=path|footway|bridleway|cycleway|pedestrian"
              in block(text, "tb_nonmotor_way"))
        named = block(text, "tb_motor_named")
        check("%s: premise, tb_motor_named exists" % key, bool(named))
        # Only tags that NAME a motor vehicle open one of these ways; never
        # the generic access or vehicle, which a bicycle satisfies too.
        bare = named.replace("motor_vehicle=", "")
        check("%s: no generic tag opens a path" % key,
              "access=" not in bare and "vehicle=" not in bare, named)


def test_a_motorbike_is_asked_its_own_access_question():
    for key, text in built().items():
        permits = block(text, "tb_motorcycle_permits")
        found = [permits.find(t) for t in
                 ("switch motorcycle=", "switch motor_vehicle=",
                  "switch vehicle=", "switch access=")]
        check("%s: the motorcycle hierarchy, most specific first" % key,
              -1 not in found and found == sorted(found), found)
        check("%s: motorcar is not in it" % key, "motorcar" not in permits)
        check("%s: tb_denied_here asks it" % key,
              "not tb_motorcycle_permits" in block(text, "tb_denied_here"))


def test_a_ford_mapped_as_a_point_is_refused_when_fords_are_avoided():
    for key, text in built().items():
        node = text.split("---context:node", 1)[1]
        lines = [l.strip() for l in block(node, "initialcost").splitlines()]
        avoid = [i for i, l in enumerate(lines)
                 if l.startswith("switch and avoid_fords ford=yes")]
        access = [i for i, l in enumerate(lines)
                  if l.startswith("switch caraccess")]
        check("%s: premise, both clauses found" % key,
              len(avoid) == 1 and len(access) == 1, lines)
        if len(avoid) != 1 or len(access) != 1:
            continue
        # Before caraccess, or a ford node signed `access=yes` is let through.
        check("%s: the refusal is before caraccess" % key,
              avoid[0] < access[0])
        check("%s: and it is a refusal" % key,
              lines[avoid[0]].endswith("1000000"))
        check("%s: an allowed point ford is costed" % key,
              "tb_node_ford_cost" in lines[access[0]])
        m = re.search(r"if vehicle_is_4x4 then (\d+) else (\d+)",
                      block(node, "tb_node_ford_cost"))
        check("%s: more on a bike than in a 4x4" % key,
              m is not None and int(m.group(2)) > int(m.group(1)) > 0)


def test_stepping_stones_on_a_point_are_refused_whatever_the_switches_say():
    # The way half refuses `ford=stepping_stones` outright; the node half must
    # too. A clause that names the value but hangs on a switch - `and
    # avoid_fords ford=stepping_stones`, `and vehicle_is_4x4 ...` - still
    # routes a rider with fords allowed (the default) over the stones, so the
    # refusal is checked for being UNCONDITIONAL, not merely present.
    for key, text in built().items():
        node = text.split("---context:node", 1)[1]
        lines = [l.strip() for l in block(node, "initialcost").splitlines()]
        stones = [i for i, l in enumerate(lines) if "stepping_stones" in l]
        access = [i for i, l in enumerate(lines)
                  if l.startswith("switch caraccess")]
        check("%s: premise, caraccess found" % key, len(access) == 1, lines)
        check("%s: the node half names stepping stones" % key,
              len(stones) == 1, lines)
        if len(stones) != 1 or len(access) != 1:
            continue
        check("%s: stepping stones are refused on no condition" % key,
              re.fullmatch(r"switch ford=stepping_stones\s+1000000",
                           lines[stones[0]]) is not None, lines[stones[0]])
        check("%s: the stepping-stones refusal is before caraccess" % key,
              stones[0] < access[0])


def test_every_step_is_applied_to_the_output():
    with tempfile.TemporaryDirectory() as out:
        bp.build(BASE_DIR, out)
        for key in KEYS:
            with open(os.path.join(out, "trailblazer-%s.brf" % key),
                      encoding="utf8", newline="") as fh:
                text = fh.read()
            check("%s is LF, like the app's copy" % key, "\r" not in text)
            check("%s carries the vehicle model" % key,
                  "assign tb_nonmotor_way" in text)
            check("%s carries the travel model" % key,
                  "assign totalweight      = 300" in text)


def test_a_hand_edited_profile_fails_the_check():
    with tempfile.TemporaryDirectory() as out:
        bp.build(BASE_DIR, out)
        check("premise: an untouched build passes the check",
              not exits(bp.check, BASE_DIR, out))
        path = os.path.join(out, "trailblazer-lanes.brf")
        with open(path, encoding="utf8") as fh:
            text = fh.read()
        edited = text.replace("assign use_offroad         = false",
                              "assign use_offroad         = true")
        check("premise: the edit changed something", edited != text)
        with open(path, "w", encoding="utf8", newline="\n") as fh:
            fh.write(edited)
        check("a hand edit fails the check", exits(bp.check, BASE_DIR, out))


def test_the_app_ships_what_this_builds():
    if not os.path.isdir(APP_ROUTING):
        print("NOT RUN: the app is not checked out at %s (set "
              "TRAILBLAZER_APP_ROUTING) - the app's profiles were not "
              "compared with this build" % APP_ROUTING)
        return
    check("the app's profiles are this generator's",
          not exits(bp.check, BASE_DIR, APP_ROUTING))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_") \
                and fn.__module__ == __name__:
            fn()
    if _failed:
        print("FAILED:")
        for f in _failed:
            print("  " + f)
        return 1
    print("ok: %d checks" % _passed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
