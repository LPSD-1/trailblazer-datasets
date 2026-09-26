#!/usr/bin/env python3
"""No HTML entity reaches a rider: cleaned where text enters, refused at publish.

    python tools/test_text_clean.py

THE DEFECT, FOUND ON A REAL TABLET ON 26 SEP 2026: 2,012 published ways said
their authority was `North&nbsp;Lincolnshire`. rowmaps' authority index is an
HTML page and the names were scraped out of it with the entities still in.
Ten POI names carried a raw no-break space from OpenStreetMap.

Each entry point is driven through its real function with the encoded text
the sources actually carry, and the publish gate is run over containers the
real writer builds - one clean, and one per place an entity can hide.
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_fords as F            # noqa: E402
import build_map_container as B    # noqa: E402
import build_packages as P         # noqa: E402
import build_pois as PO            # noqa: E402
import build_tro as T              # noqa: E402
import build_wet as W              # noqa: E402
import check_containers as CC      # noqa: E402
import ea_flood as EA              # noqa: E402
import fetch_rights_of_way as FR   # noqa: E402
import test_build_fords as TF      # noqa: E402  (its container fixture)
from text_clean import clean_text, entities_in  # noqa: E402

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + repr(detail)) if detail else ""))


# ------------------------------------------------------------ the cleaner

def test_the_cleaner():
    cases = [
        ("North&nbsp;Lincolnshire", "North Lincolnshire"),
        ("Bournemouth,&nbsp;Christchurch&nbsp;and&nbsp;Poole",
         "Bournemouth, Christchurch and Poole"),
        ("Tea &amp; Cake", "Tea & Cake"),
        ("Tea &#38; Cake", "Tea & Cake"),
        ("Tea &#x26; Cake", "Tea & Cake"),
        ("Café Bar", "Café Bar"),
        ("Double&amp;nbsp;encoded", "Double encoded"),
        # TEXT, NOT MARKUP. html.unescape alone resolves the legacy forms
        # without a semicolon, and would turn the second into "Fish ¬ Chips".
        ("K&S Fuels", "K&S Fuels"),
        ("Fish &not Chips", "Fish &not Chips"),
        ("M&S Cafe", "M&S Cafe"),
        ("10 K&S", "10 K&S"),
        # An unknown name is not an entity; it stays as the source wrote it.
        ("Smith &partners; Ltd", "Smith &partners; Ltd"),
    ]
    for raw, want in cases:
        got = clean_text(raw)
        check("clean_text(%r)" % raw, got == want, (got, want))
        check("and nothing is left for the gate in %r" % raw,
              entities_in(got) == [], entities_in(got))
    check("None passes through", clean_text(None) is None)
    check("a number passes through", clean_text(3) == 3)
    check("the gate sees &nbsp;", entities_in("North&nbsp;L") == ["&nbsp;"])
    check("the gate does not see K&S", entities_in("K&S Fuels") == [])
    check("the gate does not see an unknown name",
          entities_in("Smith &partners; Ltd") == [])


# ------------------------------------------------- where the text enters

def _row(ref="NI|1|12", desc="BO|NI:1|0.200|none|-0.5|53.6|-0.49|53.61"):
    return {"type": "Feature",
            "properties": {"Name": ref, "Description": desc},
            "geometry": {"type": "LineString",
                         "coordinates": [[-0.50, 53.60], [-0.49, 53.61]]}}


def test_a_way_takes_its_authority_decoded():
    got = P.normalise(_row(), "NI", "North&nbsp;Lincolnshire",
                      "byway_open_to_all_traffic")
    props = got["properties"]
    check("authority decoded", props["authority"] == "North Lincolnshire",
          props["authority"])
    check("county decoded", props["county"] == "North Lincolnshire",
          props["county"])
    check("source slug unchanged in shape",
          props["source"] == "rowmaps:north-lincolnshire", props["source"])
    # THE ID DOES NOT MOVE for text that had nothing to decode: the cleaning
    # must not renumber the 12,702 ways already on riders' phones.
    import hashlib
    want = "NI-12-%s" % hashlib.sha1(json.dumps(
        [[-0.50, 53.60], [-0.49, 53.61]],
        separators=(",", ":")).encode()).hexdigest()[:10]
    check("the id of a clean record is what it always was",
          props["lane_uid"] == want, (props["lane_uid"], want))


def test_a_way_takes_its_path_number_and_note_decoded():
    got = P.normalise(_row(ref="NI|1|12&nbsp;A",
                           desc="BO|NI:1|0.200|gate&amp;stile|-0.5|53.6"),
                      "NI", "North Lincolnshire", "byway_open_to_all_traffic")
    props = got["properties"]
    check("path number decoded into the name",
          props["name"].endswith("12 A"), props["name"])
    check("the council's note decoded",
          props.get("description") == "gate&stile", props.get("description"))


def test_the_authority_list_is_decoded_as_it_is_scraped():
    tmp = tempfile.mkdtemp()
    real_get, real_dir = FR.get, FR.cache_dir
    try:
        page = ('<a href="NI/">North&nbsp;Lincolnshire</a>'
                '<a href="KT/">Kent</a>').encode("utf8")
        FR.get = lambda url, timeout=60: page
        FR.cache_dir = lambda: tmp
        with contextlib.redirect_stdout(io.StringIO()):
            got = FR.authorities()
        check("a scraped name is decoded",
              got.get("NI") == "North Lincolnshire", got)
        with open(os.path.join(tmp, "authorities.json"), encoding="utf8") as fh:
            stored = json.load(fh)
        check("and cached decoded", stored.get("NI") == "North Lincolnshire",
              stored)
        # A cache written BEFORE the fix is what CI holds today.
        with open(os.path.join(tmp, "authorities.json"), "w",
                  encoding="utf8") as fh:
            json.dump({"NI": "North&nbsp;Lincolnshire"}, fh)
        got = FR.authorities()
        check("an old cache is read decoded",
              got.get("NI") == "North Lincolnshire", got)
    finally:
        FR.get, FR.cache_dir = real_get, real_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_poi_takes_its_name_decoded():
    got = PO.poi_of({"type": "node", "id": 7, "lat": 52.0, "lon": -1.0,
                     "tags": {"amenity": "fuel",
                              "name": "Tea&nbsp;&amp; Fuel",
                              "opening_hours": "Mo-Fr&nbsp;08:00-18:00"}},
                    "2026-09-26")
    check("a POI is made", got is not None, got)
    check("its name decoded", got and got["name"] == "Tea & Fuel",
          got and got["name"])
    check("its hours decoded",
          got and got["opening_hours"] == "Mo-Fr 08:00-18:00",
          got and got["opening_hours"])


def test_a_ford_takes_its_name_decoded():
    got = F.ford_of(TF.node(1, 53.0, -1.6, ford="yes",
                            name="Watersplash&nbsp;Lane"), "2026-09-26")
    check("a ford's name decoded", got["name"] == "Watersplash Lane",
          got["name"])


def test_a_station_takes_its_text_decoded():
    got = EA.parse_station({"notation": "S1", "lat": 53.0, "long": -1.6,
                            "label": "Mill&nbsp;Lane",
                            "riverName": "River&nbsp;Dove",
                            "catchmentName": "Trent&amp;Dove"})
    check("the label decoded", got["label"] == "Mill Lane", got["label"])
    check("the river decoded", got["river"] == "River Dove", got["river"])
    check("the catchment decoded", got["catchment"] == "Trent&Dove",
          got["catchment"])


def test_a_cached_station_is_decoded_into_the_ford_gauges():
    """The station list is CACHED across runs, so a label parsed before the
    fix reaches the container unless the row is cleaned too."""
    tmp = tempfile.mkdtemp()
    try:
        db = sqlite3.connect(TF.container(
            os.path.join(tmp, "c.tbmap"),
            [("w1", [(-1.60, 53.00), (-1.59, 53.00)])]))
        fords = [F.ford_of(TF.node(1, 53.00001, -1.595, ford="yes"), "x")]
        station = TF.gauge("G1", 53.01, -1.595, river="River&nbsp;Dove")
        station["label"] = "Mill&nbsp;Lane"
        _rows, gauges, _c = F.build_rows(db, fords, [station])
        db.close()
        check("a gauge is carried", len(gauges) == 1, gauges)
        check("its label decoded", gauges and gauges[0]["label"] == "Mill Lane",
              gauges)
        check("its river decoded", gauges and gauges[0]["river"] == "River Dove",
              gauges)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_cached_station_is_decoded_into_the_wet_gauges():
    station = {"id": "R1", "label": "Moor&nbsp;Top", "lat": 53.0,
               "lon": -1.6, "river": None, "catchment": None,
               "typical_low_m": None, "typical_high_m": None,
               "measures": ["R1-rainfall"]}
    _rows, gauges, _c = W.assign([(1, "w1", None, None, 53.0, -1.6)],
                                 [station])
    check("a wet gauge is carried", len(gauges) == 1, gauges)
    check("its label decoded", gauges and gauges[0]["label"] == "Moor Top",
          gauges)


def test_an_order_takes_its_text_decoded_and_keeps_its_id():
    raw = {"code": "dimensionMaximumWeight", "label": "Weight limit",
           "name": "Mill&nbsp;Lane", "where": "Bridge&amp;approach",
           "ref": "TRO&nbsp;1", "start": None, "end": None}
    plain = dict(raw, name="Mill Lane", where="Bridge&approach",
                 ref="TRO 1")
    geometry = {"type": "Point", "coordinates": [-1.6, 53.0]}
    got = T._wrap(geometry, [-1.6, 53.0], raw, None)["properties"]
    check("the road name decoded", got.get("name") == "Mill Lane", got)
    check("the place decoded", got.get("where") == "Bridge&approach", got)
    check("the reference decoded", got.get("ref") == "TRO 1", got)
    # The id is seeded from the text AS PUBLISHED, so an order that carried
    # an entity keeps the id riders already hold for it.
    again = T._wrap(geometry, [-1.6, 53.0], plain, None)["properties"]
    check("the id is still seeded from the published text",
          got["tro_uid"] != again["tro_uid"], (got["tro_uid"],
                                               again["tro_uid"]))


# --------------------------------------------------------- the publish gate

def _feature(uid, authority, lon=-1.70, lat=52.50):
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [(lon, lat), (lon + 0.004, lat + 0.003)]},
            "properties": {"lane_uid": uid, "class": "boat",
                           "authority": authority, "county": authority,
                           "name": "Byway 10 K&S", "legal_tier": "statutory",
                           "source": "rowmaps:x", "source_date": "2026-09-26",
                           "motorbike_ok": 1, "fourxfour_ok": 1,
                           "access_reason": "BOAT",
                           "access_evidence": "statutory",
                           "lengthKm": 0.4}}


def _gate(path):
    problems = []
    db = sqlite3.connect(path)
    try:
        read = CC.check_text(db, os.path.basename(path), problems)
    finally:
        db.close()
    return read, problems


def test_the_gate_passes_clean_text_and_refuses_every_hiding_place():
    tmp = tempfile.mkdtemp()
    try:
        clean = os.path.join(tmp, "ways-clean.tbmap")
        B.write_container(clean, [_feature("W1", "North Lincolnshire")],
                          "area", (11, 12), "2026-09-26")
        read, problems = _gate(clean)
        check("the gate read something", read > 0, read)
        check("clean text - K&S included - passes", problems == [], problems)

        # The shipped defect, through the real writer: ways, meta and tiles.
        dirty = os.path.join(tmp, "ways-dirty.tbmap")
        B.write_container(dirty, [_feature("W1", "North&nbsp;Lincolnshire")],
                          "area", (11, 12), "2026-09-26")
        _read, problems = _gate(dirty)
        text = " | ".join(problems)
        for where in ("ways.authority", "ways.county", "meta.value",
                      "tiles(lanes).county"):
            check("an entity in %s is refused" % where, where in text,
                  problems)

        # THE OVERVIEW HAS NO RECORDS - only tiles - and is the first
        # container every rider downloads.
        overview = os.path.join(tmp, "ways-overview.tbmap")
        B.write_container(overview,
                          [_feature("W1", "North&nbsp;Lincolnshire")],
                          "overview", (8, 8), "2026-09-26")
        _read, problems = _gate(overview)
        check("an entity in an overview tile is refused",
              any("tiles(lanes).county" in p for p in problems), problems)

        # A table the ways builder never wrote: POIs, added later.
        PO.write_pois(clean, [{"poi_uid": "osm:n1", "category": "fuel",
                               "name": "Tea &amp; Fuel", "lat": 52.501,
                               "lon": -1.699, "opening_hours": None,
                               "source_date": "2026-09-26"}])
        _read, problems = _gate(clean)
        check("an entity in a POI name is refused",
              any("pois.name" in p for p in problems), problems)

        # The DECODED form is the same defect: ten published POI names
        # carried a raw no-break space, which the entity test cannot see.
        raw = os.path.join(tmp, "ways-raw.tbmap")
        B.write_container(raw, [_feature("W1", "North Lincolnshire")],
                          "area", (11, 12), "2026-09-26")
        PO.write_pois(raw, [{"poi_uid": "osm:n2", "category": "fuel",
                             "name": "Oliver's Coffee", "lat": 52.501,
                             "lon": -1.699, "opening_hours": None,
                             "source_date": "2026-09-26"}])
        _read, problems = _gate(raw)
        text = " | ".join(problems)
        for where in ("ways.authority", "pois.name", "tiles(lanes).county"):
            check("a raw no-break space in %s is refused" % where,
                  where in text, problems)

        # And through the command the workflow runs.
        real_argv = sys.argv
        try:
            sys.argv = ["check_containers.py", dirty]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = CC.main()
        finally:
            sys.argv = real_argv
        check("check_containers.py exits non-zero on it", rc == 1, rc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
