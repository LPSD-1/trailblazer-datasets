"""The overview floor has to be one every zoom above it can live with.

A container built with floor F holds F..high, and tile size is NOT monotonic in
zoom: coalescing merges more the further out you go, so a z4 tile can be
smaller than the z5 tile above it.

`lowest_zoom_that_fits` measured the candidate zoom ALONE, so for a cyclist z4
fitted, 4 was returned, and the z5 tile the container also had to carry came
out at 535 kB against a 512 kB ceiling. The publish guard refused the build -
correctly - but the builder should never have offered it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_map_container as B

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def floor_for(sizes, low=4, high=10, ceiling=512):
    """Run the real chooser against a made-up size profile."""
    calls = []

    def fake(features, zoom, coalesced, on_tile, ids=None):
        calls.append(zoom)
        on_tile(zoom, 0, 0, b"x" * sizes[zoom], 1)

    real = B.build_tiles
    B.build_tiles = fake
    try:
        return B.lowest_zoom_that_fits([], low, high, max_tile=ceiling), calls
    finally:
        B.build_tiles = real


def test_a_zoom_above_the_floor_can_veto_it():
    # The measured failure: z4 fits, z5 does not, and the container holds both.
    got, _ = floor_for({4: 100, 5: 600, 6: 200, 7: 150, 8: 100, 9: 90, 10: 80})
    check("a fat zoom above the floor vetoes it", got == 6, "got %r" % got)


def test_the_easy_case_still_takes_the_lowest():
    # A motorcyclist's data fits everywhere, so it should go as low as allowed.
    got, _ = floor_for({z: 40 for z in range(4, 11)})
    check("all fitting means the lowest floor", got == 4, "got %r" % got)


def test_a_dataset_that_fits_nowhere_gets_no_overview():
    got, _ = floor_for({z: 9000 for z in range(4, 11)})
    check("too fat everywhere means no overview", got == 11, "got %r" % got)


def test_a_walker_lands_where_it_fits():
    # Dense data: nothing below 9 fits, so 9 it is.
    got, _ = floor_for({4: 9000, 5: 9000, 6: 9000, 7: 9000, 8: 9000,
                        9: 300, 10: 200})
    check("a dense set lands at the first workable floor", got == 9,
          "got %r" % got)


def test_each_zoom_is_cut_once():
    # The chooser used to re-cut the whole set per candidate. Measuring each
    # zoom once and picking from the measurements is the same answer for less.
    _, calls = floor_for({z: 40 for z in range(4, 11)})
    check("each zoom measured exactly once",
          sorted(calls) == list(range(4, 11)), "cut zooms %r" % sorted(calls))


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
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
