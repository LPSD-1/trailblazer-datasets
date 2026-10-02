"""Every council code in the TRO table is GeoPlace's, and every park lane is
listed under the councils that regulate it.

tools/tro_authorities.csv puts a council's name beside the orders that carry
its GeoPlace SWA code, and tells the lane sheet which councils regulate a
lane's ground. A wrong code there is not a crash: it names one council beside
another council's orders, or says a council "does not publish" when the code
we gave it is somebody else's. test_tro_council_table.py pins six codes by
hand; the other 172 were pinned by nothing (Devon 1155 -> 1156 and Powys
losing "Brecon Beacons National Park" both passed every suite).

THE REFERENCE. tools/geoplace_swa_org_active_2026-09-30.csv is GeoPlace's
SWA_ORG_ACTIVE list exactly as downloaded on 1 Oct 2026 from
https://static.geoplace.co.uk/downloads/SWA_ORG_ACTIVE_2026-09-30.csv (linked
from geoplace.co.uk "View SWA codes"), the list the table was built from. It
is committed unedited (git stores it with LF line endings; the download has
CRLF) and its hash is pinned over the LF form, so the reference cannot be
edited to agree with a wrong table. When GeoPlace publishes a new list,
download it beside this one, update GEOPLACE and its hash, and let this file
say which rows of the table moved.

Run as `python tools/test_tro_council_codes_match_geoplace.py`.
"""
import csv
import hashlib
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_tro  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GEOPLACE = os.path.join(HERE, "geoplace_swa_org_active_2026-09-30.csv")
# SHA-256 of the file as downloaded, line endings normalised to LF so a
# Windows checkout (core.autocrlf) hashes the same as the committed bytes.
GEOPLACE_SHA256 = (
    "cba74f8af071cb1b3408098d882ada3e48c0fe5ae7625129e25705395d156819")

# The three national traffic authorities the table carries beside the local
# highway authorities. GeoPlace numbers every England and Wales local highway
# authority between 114 (Bath and North East Somerset) and 6955 (Wrexham);
# utilities and contractors are numbered above that.
NATIONAL = {"11", "16", "20"}
LOCAL_FIRST, LOCAL_LAST = 114, 6955

# The Welsh councils whose ground Bannau Brycheiniog (Brecon Beacons) National
# Park covers, each the highway authority for its part of the park.
BRECON_BEACONS = {
    "6825": "Carmarthenshire County Council",
    "6840": "Monmouthshire County Council",
    "6850": "Powys County Council",
    "6910": "Blaenau Gwent County Borough Council",
    "6925": "Merthyr Tydfil County Borough Council",
    "6940": "Rhondda Cynon Taf County Borough Council",
    "6945": "Torfaen County Borough Council",
}
# Westmorland and Furness has most of the Lake District, Cumberland the
# north-west. Both succeeded Cumbria County Council (abolished 1 April 2023),
# whose name rowmaps still files lanes under.
CUMBRIA = {
    "935": "Westmorland and Furness Council",
    "940": "Cumberland Council",
}

# Every lane name that is NOT simply its council's own name, and the rows it
# belongs on. Anything else must be the name of the one council it sits under.
SHARED_OR_SPELT_DIFFERENTLY = {
    "Brecon Beacons National Park": set(BRECON_BEACONS),
    "Lake District National Park": set(CUMBRIA),
    "Cumbria": set(CUMBRIA),
    "City of Kingston upon Hull": {"2004"},   # Hull City Council
    "Newcastle upon Tyne": {"4510"},           # Newcastle City Council
    "Rhondda Cynon Taff": {"6940"},            # rowmaps spells it with "ff"
}

_STOP = {"COUNCIL", "COUNTY", "CITY", "BOROUGH", "METROPOLITAN", "DISTRICT",
         "LONDON", "OF", "ROYAL", "THE", "AND"}


def geoplace_rows():
    """GeoPlace's list as {code: name}, plus the raw row count."""
    with open(GEOPLACE, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["swa_org_name_text", "swa_org_ref",
                                     "swa_org_prefix"], reader.fieldnames
        rows = list(reader)
    by_code = {}
    for row in rows:
        code = build_tro.normalise_tra(row["swa_org_ref"])
        assert code not in by_code, "GeoPlace lists %s twice" % code
        by_code[code] = row["swa_org_name_text"]
    return by_code, len(rows)


def same_name(name):
    """A council name as GeoPlace and the table should agree on it.

    GeoPlace writes names in capitals ("ST. HELENS COUNCIL", "TRANSPORT FOR
    LONDON (TFL)"); the table writes what a rider reads ("St Helens Council",
    "Transport for London"). Case, full stops, a bracketed abbreviation and
    spacing are all that may differ. Every word must still match.
    """
    text = re.sub(r"\([^)]*\)", " ", name).replace(".", "")
    return " ".join(text.upper().split())


def own_name_key(name):
    """The words that make a council's name its own, for lane matching."""
    text = name.upper().replace("-", " ").replace(".", "").replace("&", "AND")
    return " ".join(w for w in text.split() if w not in _STOP)


class TheReference(unittest.TestCase):
    def test_the_extract_is_the_file_geoplace_published(self):
        with open(GEOPLACE, "rb") as handle:
            raw = handle.read().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), GEOPLACE_SHA256,
                         "the committed GeoPlace list was edited")
        by_code, count = geoplace_rows()
        self.assertEqual(count, 581, "PREMISE: the 30 Sep 2026 active list")
        local = [c for c in by_code if LOCAL_FIRST <= int(c) <= LOCAL_LAST]
        self.assertEqual(len(local), 175,
                         "PREMISE: 175 England and Wales highway authorities")


class EveryCode(unittest.TestCase):
    def setUp(self):
        self.mapped, self.unmapped = build_tro.load_authority_table()
        self.geoplace, _ = geoplace_rows()

    def test_every_mapped_row_is_the_council_geoplace_gives_that_code(self):
        self.assertEqual(len(self.mapped), 178,
                         "PREMISE: 175 councils and 3 national authorities")
        wrong = []
        for row in self.mapped:
            listed = self.geoplace.get(row["swa"])
            if listed is None:
                wrong.append("%s %s: GeoPlace has no such code"
                             % (row["swa"], row["name"]))
            elif same_name(listed) != same_name(row["name"]):
                wrong.append("%s %s: GeoPlace says %s"
                             % (row["swa"], row["name"], listed))
        self.assertEqual(wrong, [], "codes that name the wrong council")

    def test_every_highway_authority_geoplace_lists_is_in_the_table(self):
        expected = NATIONAL | {c for c in self.geoplace
                               if LOCAL_FIRST <= int(c) <= LOCAL_LAST}
        have = {row["swa"] for row in self.mapped}
        self.assertEqual(sorted(expected - have, key=int), [],
                         "councils GeoPlace lists that the table lacks")
        self.assertEqual(sorted(have - expected, key=int), [],
                         "table codes that are not a highway authority")


class TheLanes(unittest.TestCase):
    def setUp(self):
        self.mapped, self.unmapped = build_tro.load_authority_table()
        self.by_lane = {}
        for row in self.mapped:
            for lane in row["lanes"]:
                self.by_lane.setdefault(lane, set()).add(row["swa"])

    def councils_for(self, lane):
        return {row["swa"]: row["name"] for row in self.mapped
                if lane in row["lanes"]}

    def test_brecon_beacons_is_listed_under_all_seven_councils(self):
        self.assertEqual(self.councils_for("Brecon Beacons National Park"),
                         BRECON_BEACONS)
        # Each keeps its own name too: the park is added, nothing replaced.
        for swa in BRECON_BEACONS:
            own = [r for r in self.mapped if r["swa"] == swa][0]["lanes"]
            self.assertEqual(len(own), 2, "%s lanes %r" % (swa, own))

    def test_the_lake_district_and_cumbria_are_listed_under_both_successors(
            self):
        self.assertEqual(self.councils_for("Lake District National Park"),
                         CUMBRIA)
        self.assertEqual(self.councils_for("Cumbria"), CUMBRIA)

    def test_the_park_authorities_hold_no_code(self):
        unmapped = {r["name"]: r["lanes"] for r in self.unmapped}
        self.assertEqual(unmapped, {
            "Bannau Brycheiniog National Park Authority":
                ["Brecon Beacons National Park"],
            "Lake District National Park Authority":
                ["Lake District National Park"],
        })

    def test_every_other_lane_name_is_its_own_councils_name(self):
        self.assertGreaterEqual(len(self.by_lane), 140,
                                "PREMISE: the table maps the rowmaps names")
        wrong = []
        for lane, codes in sorted(self.by_lane.items()):
            if lane in SHARED_OR_SPELT_DIFFERENTLY:
                if codes != SHARED_OR_SPELT_DIFFERENTLY[lane]:
                    wrong.append("%s under %s" % (lane, sorted(codes)))
                continue
            names = [r["name"] for r in self.mapped if r["swa"] in codes]
            if len(names) != 1 or own_name_key(lane) != own_name_key(names[0]):
                wrong.append("%s under %s" % (lane, names))
        self.assertEqual(wrong, [], "lane names on the wrong council")


if __name__ == "__main__":
    # BLIND, not failed, when the list is not here. GeoPlace's list is
    # fetched at run time by traffic-orders.yml from GeoPlace's own link and
    # never committed: it is their list, and this repository is public.
    if not os.path.exists(GEOPLACE):
        print("BLIND: GeoPlace's SWA list is not at %s (traffic-orders.yml "
              "fetches it; locally, download it from the link above)" % GEOPLACE)
        sys.exit(2)
    unittest.main()
