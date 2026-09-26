#!/usr/bin/env python3
"""Every meta key a region container carries is named in WAYS-SCHEMA.md.

    python tools/test_meta_keys_are_documented.py

THE DEFECT. `build_pois.write_pois` writes `meta.pois_checked` and
`stamp_build.py` writes `meta.ways_cut`, and the app reads both (the POI
sheet's "checked 3 days ago, unchanged since ..." and the Data screen's
"cut <date>"). WAYS-SCHEMA.md named neither: its Meta section documented
`built_at` and `evidence_dates` only, its `stamp_build` rule said the content
digest skips "every meta key but `built_at`" when it also skips `ways_cut`,
and its POIs section never said that `pois.source_date` now means "unchanged
since" rather than "read on". A reader of the schema had no way to learn the
two dates the app shows riders.

THE CHECK drives the real writers - build_map_container.write_container,
build_pois.write_pois, evidence_age.write_meta and a stamp_build rule-3
restamp - collects every key they leave in `meta`, and requires each one to
be named, in backticks, in the doc's Meta section. So a key a writer gains
later fails here until the doc says what it is.
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_map_container as B  # noqa: E402
import build_pois as PO          # noqa: E402
import evidence_age as EA        # noqa: E402
import stamp_build as SB         # noqa: E402

DOC = os.path.join(os.path.dirname(HERE), "docs", "WAYS-SCHEMA.md")

WAYS_STAMP = "2026-09-24T22:40:20Z"
POI_RUN = "2026-10-24T06:00:00Z"

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + str(detail)) if detail else ""))


def _way(uid, lon, lat):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
            "properties": {"lane_uid": uid, "class": "boat",
                           "authority": "Derbyshire", "source_date": "2026-03-04",
                           "motorbike_ok": 1, "fourxfour_ok": 1,
                           "access_reason": "x", "access_evidence": "statutory",
                           "lengthKm": 0.4,
                           # A way another authority also records, so the
                           # builder writes meta.also_recorded_by.
                           "also_recorded_by": [
                               {"way_uid": "PW-1-0", "authority": "Powys",
                                "authority_code": "PW",
                                "name": "Byway open to all traffic 1"}]}}


def _poi(uid):
    return {"poi_uid": uid, "category": "fuel", "name": None, "lat": 52.501,
            "lon": -1.699, "opening_hours": None, "source_date": "2026-09-01"}


def _build(path, pois, previous=None):
    """A region container as the pipeline writes one, up to the stamp, with
    every optional meta key the builder can write switched on."""
    B.write_container(path, [_way("W1", -1.70, 52.50)], "area", (11, 11),
                      WAYS_STAMP, context_scope="none",
                      context_note="Bridleways are not carried.")
    PO.write_pois(path, pois, previous=previous)
    EA.write_meta(path)
    return path


def _meta_keys(path):
    db = sqlite3.connect(path)
    try:
        return {row[0] for row in db.execute("SELECT key FROM meta")}
    finally:
        db.close()


def _section(text, heading):
    """The body of `## heading`, up to the next level-2 heading."""
    match = re.search(r"^## %s[ \t]*\r?$(.*?)(?=^## |\Z)" % re.escape(heading),
                      text, re.S | re.M)
    return match.group(1) if match else ""


def _named(text, key):
    return ("`%s`" % key) in text


def _doc():
    with open(DOC, encoding="utf-8") as fh:
        return fh.read()


def collected_keys():
    """Every meta key of a region container that rule 3 has restamped: the
    one shape that carries all of them at once."""
    tmp = tempfile.mkdtemp()
    try:
        published = _build(os.path.join(tmp, "published.tbmap"),
                           [_poi("osm:n200")])
        built = _build(os.path.join(tmp, "built.tbmap"),
                       [_poi("osm:n100"), _poi("osm:n200")],
                       previous=published)
        verdict, stamp = SB.stamp(published, built, POI_RUN)
        check("PREMISE: the build met rule 3",
              verdict == "changed" and stamp == POI_RUN, (verdict, stamp))
        return _meta_keys(built)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_meta_key_is_named_in_the_meta_section():
    keys = collected_keys()
    # BLIND otherwise: an empty set, or one missing the keys this test was
    # written for, passes against any doc at all.
    check("PREMISE: the writers left keys to check", len(keys) >= 10,
          sorted(keys))
    for key in ("pois_checked", "ways_cut", "built_at", "evidence_dates",
                "also_recorded_by"):
        check("PREMISE: the pipeline wrote %s" % key, key in keys,
              sorted(keys))
    meta = _section(_doc(), "Meta")
    check("PREMISE: the doc has a Meta section", len(meta) > 200, len(meta))
    missing = sorted(k for k in keys if not _named(meta, k))
    check("every meta key is named in WAYS-SCHEMA.md's Meta section",
          not missing, missing)


def test_the_digest_rule_names_every_key_it_skips():
    """The doc's 'same content' rule must list what content_digest leaves
    out, or a reader expects a ways_cut difference to count as a change."""
    meta = _section(_doc(), "Meta")
    match = re.search(r"^- \*\*same content\*\*(.*?)(?=^- \*\*|^\s*$)", meta,
                      re.S | re.M)
    check("PREMISE: the Meta section states the same-content rule",
          match is not None)
    rule = match.group(1) if match else ""
    check("PREMISE: stamp_build skips at least built_at",
          "built_at" in SB.UNHASHED_META, SB.UNHASHED_META)
    missing = [k for k in SB.UNHASHED_META if not _named(rule, k)]
    check("the rule names every key content_digest skips", not missing,
          missing)


def test_the_pois_section_says_what_source_date_means():
    """A POI's source_date is kept from the published row while nothing about
    it moves, so it reads 'unchanged since'; when the region was read is
    meta.pois_checked. The POIs section must say both."""
    pois = _section(_doc(), "POIs")
    check("PREMISE: the doc has a POIs section", "CREATE TABLE pois" in pois)
    check("POIs: source_date is 'unchanged since'",
          "unchanged since" in pois)
    check("POIs: the read date is meta.pois_checked",
          _named(pois, "pois_checked") or "`meta.pois_checked`" in pois)


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
