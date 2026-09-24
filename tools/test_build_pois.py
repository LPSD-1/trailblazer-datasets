#!/usr/bin/env python3
"""What a POI build must not get wrong.

Three of these exist because of a specific way this step can go quietly bad.

1. **The category list grows.** `docs/WAYS-SCHEMA.md` says nine and "nothing
   else - a general POI database would dwarf the ways beside it". A tenth
   category is a one-line edit with no visible consequence until a region
   container is twice the size it was. So the list is read OUT OF THE SCHEMA
   and compared, and a tag nobody asked for is asserted to categorise as None.

2. **An element matches two categories.** A filling station with a cafe in it.
   Left to dict order the answer is stable-looking and arbitrary; here it is a
   declared priority, and the test pins it.

3. **The rtree and the record disagree.** `lanes`/`lanes_bbox` had exactly this
   fault once - records present, findable, invisible - so the bbox hit is
   joined back to its row and the row is checked, rather than counting rows in
   each table and calling it agreement.

Run:  python tools/test_build_pois.py
"""
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_pois as B  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_DOC = os.path.join(ROOT, "docs", "WAYS-SCHEMA.md")

_passed = 0
_failed = []


def check(name, ok, detail=""):
    global _passed
    if ok:
        _passed += 1
    else:
        _failed.append("%s%s" % (name, (": " + detail) if detail else ""))


def node(uid, tags, lat=52.5, lon=-1.5):
    return {"type": "node", "id": uid, "lat": lat, "lon": lon, "tags": tags}


def way(uid, tags, lat=52.5, lon=-1.5):
    return {"type": "way", "id": uid, "center": {"lat": lat, "lon": lon},
            "tags": tags}


# ----------------------------------------------------- 1. the list is closed

def schema_categories():
    """The nine, read from the contract rather than from this file."""
    text = open(SCHEMA_DOC, encoding="utf-8").read()
    match = re.search(r"Categories:(.*?)Nothing else", text, re.S)
    if not match:
        return None
    return set(re.findall(r"`([a-z_]+)`", match.group(1)))


def test_categories_match_the_schema():
    from_doc = schema_categories()
    check("schema doc still names its categories", from_doc is not None)
    if from_doc is None:
        return
    ours = set(B.CATEGORY_NAMES)
    check("categories match docs/WAYS-SCHEMA.md exactly", ours == from_doc,
          "ours-doc=%s doc-ours=%s" % (sorted(ours - from_doc),
                                       sorted(from_doc - ours)))
    check("nine categories, no more", len(B.CATEGORY_NAMES) == 9,
          str(len(B.CATEGORY_NAMES)))
    check("no category is listed twice",
          len(set(B.CATEGORY_NAMES)) == len(B.CATEGORY_NAMES))


def test_the_rest_of_osm_is_not_carried():
    """The filter is the feature. Each of these is a large, popular tag that a
    general POI build would sweep in."""
    for tags in ({"amenity": "bank"}, {"shop": "supermarket"},
                 {"amenity": "school"}, {"highway": "bus_stop"},
                 {"amenity": "bench"}, {"amenity": "post_box"},
                 {"shop": "hairdresser"}, {"amenity": "place_of_worship"},
                 {"tourism": "hotel"}, {"amenity": "waste_basket"},
                 {"building": "house"}, {}):
        check("not carried: %s" % (tags or "untagged"),
              B.categorise(tags) is None, str(B.categorise(tags)))


def test_every_tag_the_spec_lists_is_carried():
    """Spec section 6.2's table, transcribed. If a row is dropped from
    CATEGORIES this goes red naming it."""
    expected = {
        ("amenity", "fuel"): "fuel",
        ("amenity", "cafe"): "food",
        ("amenity", "pub"): "food",
        ("amenity", "restaurant"): "food",
        ("amenity", "fast_food"): "food",
        ("amenity", "toilets"): "toilets",
        ("amenity", "drinking_water"): "water",
        ("amenity", "parking"): "parking",
        ("highway", "services"): "parking",
        ("tourism", "camp_site"): "camping",
        ("tourism", "caravan_site"): "camping",
        ("shop", "motorcycle"): "repair",
        ("shop", "car_repair"): "repair",
        ("shop", "tyres"): "repair",
        ("tourism", "viewpoint"): "viewpoint",
        ("amenity", "shelter"): "viewpoint",
        ("amenity", "atm"): "atm",
    }
    for (key, value), category in sorted(expected.items()):
        got = B.categorise({key: value})
        check("%s=%s -> %s" % (key, value, category), got == category,
              "got %s" % got)


# --------------------------------------------------------- 2. two categories

def test_priority_is_declared_not_accidental():
    # A filling station with a workshop is fuel to a rider who is nearly empty.
    check("fuel beats repair",
          B.categorise({"amenity": "fuel", "shop": "car_repair"}) == "fuel")
    # A tyre place with a cafe in it is a tyre place.
    check("repair beats food",
          B.categorise({"shop": "tyres", "amenity": "cafe"}) == "repair")
    # A viewpoint with a car park is the viewpoint; the car park is why you can
    # stop there, not what you came for.
    check("viewpoint beats parking",
          B.categorise({"tourism": "viewpoint", "amenity": "parking"})
          == "viewpoint")
    check("camping beats parking",
          B.categorise({"tourism": "camp_site", "amenity": "parking"})
          == "camping")


def test_access_private_is_dropped():
    check("access=private dropped",
          B.poi_of(node(1, {"amenity": "parking", "access": "private"}),
                   "2026-09-24") is None)
    check("access=no dropped",
          B.poi_of(node(2, {"amenity": "toilets", "access": "no"}),
                   "2026-09-24") is None)
    # A pub car park is customers-only and is exactly where a rider parks.
    check("access=customers kept",
          B.poi_of(node(3, {"amenity": "parking", "access": "customers"}),
                   "2026-09-24") is not None)
    check("no access tag kept",
          B.poi_of(node(4, {"amenity": "parking"}), "2026-09-24") is not None)


# --------------------------------------------------------------- the row

def test_row_shape():
    poi = B.poi_of(node(12345, {"amenity": "fuel", "name": "Bamford Filling",
                                "opening_hours": "Mo-Su 07:00-21:00"},
                        lat=53.3456789123, lon=-1.6987654321), "2026-09-24")
    check("uid is stable and typed", poi["poi_uid"] == "osm:n12345",
          poi["poi_uid"])
    check("name carried", poi["name"] == "Bamford Filling")
    check("opening_hours carried",
          poi["opening_hours"] == "Mo-Su 07:00-21:00")
    check("source_date carried", poi["source_date"] == "2026-09-24")
    check("lat rounded to 7dp", poi["lat"] == 53.3456789, repr(poi["lat"]))
    check("lon rounded to 7dp", poi["lon"] == -1.6987654, repr(poi["lon"]))
    # Unnamed toilets and viewpoints are most of what OSM has; dropping them
    # would delete the categories the spec says are "disproportionately valued".
    unnamed = B.poi_of(node(7, {"amenity": "toilets"}), "2026-09-24")
    check("an unnamed POI still ships", unnamed is not None)
    check("an unnamed POI has a NULL name",
          unnamed is not None and unnamed["name"] is None)


def test_a_way_uses_its_centre_and_a_node_its_own_position():
    w = B.poi_of(way(99, {"amenity": "parking"}, lat=52.1, lon=-2.2),
                 "2026-09-24")
    check("way -> centre", (w["lat"], w["lon"]) == (52.1, -2.2))
    check("way uid distinct from node uid", w["poi_uid"] == "osm:w99")
    n = B.poi_of(node(99, {"amenity": "parking"}, lat=52.1, lon=-2.2),
                 "2026-09-24")
    check("node 99 and way 99 are different POIs",
          n["poi_uid"] != w["poi_uid"])
    # A relation with no centre cannot be drawn, so it is not carried.
    check("no position -> not carried",
          B.poi_of({"type": "relation", "id": 5, "tags": {"amenity": "fuel"}},
                   "2026-09-24") is None)


def test_clipped_to_the_region():
    box = B.REGIONS["midlands"]
    inside = B.poi_of(node(1, {"amenity": "fuel"}, lat=52.9, lon=-1.5), "d")
    outside = B.poi_of(node(2, {"amenity": "fuel"}, lat=51.0, lon=-1.5), "d")
    check("inside the box", B.in_bbox(inside, box))
    check("outside the box", not B.in_bbox(outside, box))


def test_the_build_drops_what_falls_outside_the_region():
    """`in_bbox` being right is not the same as the build applying it. A split
    box is asked for by its own bounds, and Overpass returns a way whose centre
    lies outside it, so without the clip a region ships its neighbour's POIs -
    and the byte figure this step reports would be measuring two regions."""
    workdir = tempfile.mkdtemp()
    os.makedirs(os.path.join(workdir, "test"))
    blob = {"region": "test", "bbox": [0, 0, 1, 1], "category": "fuel",
            "fetched_at": "2026-09-24",
            "elements": [node(1, {"amenity": "fuel"}, lat=0.5, lon=0.5),
                         node(2, {"amenity": "fuel"}, lat=9.0, lon=9.0)]}
    for name, _ in B.CATEGORIES:
        blob["category"] = name
        payload = dict(blob, elements=blob["elements"] if name == "fuel"
                       else [])
        with open(B.cache_path(workdir, "test", name), "w",
                  encoding="utf-8") as handle:
            json.dump(payload, handle)
    pois, _ = B.load_cached(workdir, "test", (0, 0, 1, 1),
                            log=lambda *a: None)
    check("the POI outside the region is not carried", len(pois) == 1,
          str([p["poi_uid"] for p in pois]))
    check("and the one inside it is",
          pois and pois[0]["poi_uid"] == "osm:n1")


def test_regions_match_build_packages():
    """A POI region that is not a pack region publishes POIs nobody downloads."""
    try:
        import build_packages
    except SystemExit as error:
        check("build_packages importable", False, str(error))
        return
    theirs = {r: tuple(b) for r, _, b in build_packages.REGIONS}
    check("region boxes match build_packages.REGIONS",
          B.REGIONS == theirs,
          "ours=%s theirs=%s" % (sorted(B.REGIONS.items()),
                                 sorted(theirs.items())))


# -------------------------------------------------- 3. rtree and the record

def fixture_container(path):
    """A container in the shape the POI writer will meet: tiles, a records
    table and meta already there."""
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,
                          tile_row INTEGER, tile_data BLOB);
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    db.execute("INSERT INTO tiles VALUES (10,1,1,?)", (b"x" * 4096,))
    db.execute("INSERT INTO meta VALUES ('kind','area')")
    db.commit()
    db.close()


def sample_pois():
    return [B.poi_of(node(1, {"amenity": "fuel", "name": "A"},
                          lat=53.0, lon=-1.0), "2026-09-24"),
            B.poi_of(node(2, {"amenity": "toilets"},
                          lat=52.0, lon=-2.0), "2026-09-24"),
            B.poi_of(way(3, {"amenity": "parking", "name": "C"},
                         lat=52.5, lon=-1.5), "2026-09-24")]


def test_written_rows_and_their_bboxes_agree():
    workdir = tempfile.mkdtemp()
    path = os.path.join(workdir, "c.tbmap")
    fixture_container(path)
    pois = sorted(sample_pois(), key=lambda p: p["poi_uid"])
    B.write_pois(path, pois)

    db = sqlite3.connect(path)
    check("every POI is a row",
          db.execute("SELECT count(*) FROM pois").fetchone()[0] == 3)
    check("every POI is in the rtree",
          db.execute("SELECT count(*) FROM pois_bbox").fetchone()[0] == 3)
    check("the container's own tables survive",
          db.execute("SELECT count(*) FROM tiles").fetchone()[0] == 1)

    # THE ONE THAT MATTERS: search the rtree the way the app will, and check
    # the row it leads to is the right POI - not merely that a row exists.
    row = db.execute(
        "SELECT p.poi_uid, p.category, p.name FROM pois_bbox b"
        " JOIN pois p ON p.rowid = b.id"
        " WHERE b.min_lon >= ? AND b.max_lon <= ?"
        "   AND b.min_lat >= ? AND b.max_lat <= ?",
        (-1.05, -0.95, 52.95, 53.05)).fetchall()
    check("a bbox search finds exactly one POI", len(row) == 1, str(row))
    check("and it is the fuel station",
          row == [("osm:n1", "fuel", "A")], str(row))

    empty = db.execute(
        "SELECT count(*) FROM pois_bbox WHERE min_lon >= 10 AND max_lon <= 11"
    ).fetchone()[0]
    check("a search over empty sea finds nothing", empty == 0)
    check("opening_hours is NULL when OSM has none",
          db.execute("SELECT opening_hours FROM pois WHERE poi_uid='osm:n2'"
                     ).fetchone()[0] is None)
    db.close()


def test_the_uid_is_a_primary_key():
    workdir = tempfile.mkdtemp()
    path = os.path.join(workdir, "c.tbmap")
    fixture_container(path)
    twice = [B.poi_of(node(1, {"amenity": "fuel"}), "d"),
             B.poi_of(node(1, {"amenity": "fuel"}), "d")]
    try:
        B.write_pois(path, twice)
        check("a duplicate uid is refused", False, "it was accepted")
    except sqlite3.IntegrityError:
        check("a duplicate uid is refused", True)


def test_two_builds_of_one_cache_are_byte_identical():
    """Reproducibility, the property step 0.11 exists to protect."""
    workdir = tempfile.mkdtemp()
    digests = []
    for name in ("a.tbmap", "b.tbmap"):
        path = os.path.join(workdir, name)
        fixture_container(path)
        B.write_pois(path, sorted(sample_pois(), key=lambda p: p["poi_uid"]))
        digests.append(hashlib.sha256(open(path, "rb").read()).hexdigest())
    check("two writes give the same bytes", digests[0] == digests[1],
          "%s vs %s" % (digests[0][:12], digests[1][:12]))


# --------------------------------------------------------- fetching from OSM

def test_the_query_asks_overpass_the_right_question():
    query = B.query_for([("amenity", "fuel")], (-3.25, 51.90, 0.15, 53.60), 300)
    # Overpass wants south,west,north,east - transposing it silently fetches
    # the wrong part of the world, or nothing.
    check("bbox in Overpass order",
          "(51.900000,-3.250000,53.600000,0.150000)" in query, query)
    check("asks for nodes, ways and relations", "nwr[" in query, query)
    check("asks for tags and a centre", query.endswith("out tags center;"),
          query)
    check("carries the timeout", "[timeout:300]" in query, query)
    multi = B.query_for([("shop", "motorcycle"), ("shop", "tyres")],
                        (0, 0, 1, 1), 30)
    check("every selector in one query",
          multi.count("nwr[") == 2, multi)


def test_a_refused_box_is_split_and_not_lost():
    """Overpass 504s under load - it is what stopped three counties in step
    0.1. The big box must be split, and every element in the quarters kept."""
    calls = []

    class Response(object):
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        # The body is form-encoded, exactly as Overpass is posted to.
        query = urllib.parse.parse_qs(request.data.decode())["data"][0]
        calls.append(query)
        box = re.search(r"\(([-\d.,]+)\)", query).group(1)
        south = float(box.split(",")[0])
        north = float(box.split(",")[2])
        if north - south > 0.6:          # the whole box: refuse it
            raise IOError("HTTP Error 504: Gateway Time-out")
        return Response(json.dumps({"elements": [
            node(int(south * 1000), {"amenity": "fuel"})]}).encode())

    got = B.fetch_category("fuel", [("amenity", "fuel")], (0.0, 0.0, 1.0, 1.0),
                           log=lambda *a: None, opener=opener, pause=0.0)
    check("the refused box was retried then split", len(calls) > 4,
          "%d calls" % len(calls))
    check("all four quarters came back", len(got) == 4, str(len(got)))


def test_the_splitter_covers_the_whole_box():
    pieces = B.split((-4.0, 50.0, 0.0, 54.0))
    check("four pieces", len(pieces) == 4)
    area = sum((e - w) * (n - s) for w, s, e, n in pieces)
    check("the pieces tile the box exactly", abs(area - 16.0) < 1e-9,
          str(area))


def test_a_seam_duplicate_is_collapsed_once():
    """Split boxes overlap on their seams, so the same car park arrives twice.
    Two rows for one car park is a visible double-draw, and it also inflates
    the very number this step exists to measure."""
    class Args(object):
        region, bbox, refresh = "test", "0,0,1,1", False

    workdir = tempfile.mkdtemp()
    args = Args()
    args.cache = workdir
    payload = [node(42, {"amenity": "parking"}, lat=0.5, lon=0.5),
               node(42, {"amenity": "parking"}, lat=0.5, lon=0.5),
               node(43, {"amenity": "parking"}, lat=0.5, lon=0.5)]

    class Response(object):
        def read(self):
            return json.dumps({"elements": payload}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    B.do_fetch(args, log=lambda *a: None, pause=0.0,
               opener=lambda request, timeout=None: Response())
    blob = json.load(open(B.cache_path(workdir, "test", "parking"),
                          encoding="utf-8"))
    check("the seam duplicate is collapsed", len(blob["elements"]) == 2,
          str(len(blob["elements"])))

    pois, dates = B.load_cached(workdir, "test", (0, 0, 1, 1),
                                log=lambda *a: None)
    check("and two POIs reach the build", len(pois) == 2, str(len(pois)))
    check("one source date", len(dates) == 1, str(dates))
    check("sorted by uid",
          [p["poi_uid"] for p in pois] == sorted(p["poi_uid"] for p in pois))


def test_only_names_a_category_that_exists():
    """`--only` is the instrument for the density budget. A typo silently
    measuring nothing would report that a category is free."""
    check("--only defaults to off", B.wanted_categories(None) is None)
    check("--only takes a list",
          B.wanted_categories("fuel,food") == {"fuel", "food"})
    check("--only tolerates spacing",
          B.wanted_categories(" fuel , food ") == {"fuel", "food"})
    try:
        B.wanted_categories("diesel")
        check("a typo is refused, not measured as zero", False, "accepted")
    except SystemExit as error:
        check("a typo is refused, not measured as zero",
              "diesel" in str(error), str(error))


def test_gzip_size_is_the_compressed_length():
    workdir = tempfile.mkdtemp()
    path = os.path.join(workdir, "blob.bin")
    payload = (b"trailblazer" * 5000) + os.urandom(4096)
    open(path, "wb").write(payload)
    expected = len(gzip.compress(payload, 9, mtime=0))
    got = B.gzip_size(path)
    check("gzip_size matches gzip.compress", abs(got - expected) <= 2,
          "%d vs %d" % (got, expected))
    check("and it actually compresses", got < len(payload))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("%d passed, %d failed" % (_passed, len(_failed)))
    for failure in _failed:
        print("  FAIL %s" % failure)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
