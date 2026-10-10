#!/usr/bin/env python3
"""The public data status file (tools/build_status.py), from small fixtures.

    python tools/test_build_status.py

Every input is a file written here into a throwaway checkout, or a fake
handed in: no network, no real containers, and never the live GitHub API.
The schema is the one the app is built against (published/status.json,
schema 1); its keys are written out HERE, not read from build_status.py, so
a key the builder stops writing is a key this file notices.

Each test names what it guards; each fails if that thing is removed:
  * every schema key present, with the right types and values;
  * no personal data reaches the file, and an "@", a phone number or a
    postcode is refused outright;
  * the D-TRO label is neutral, and "dormant" or "inactive" is refused;
  * honours need two criteria;
  * "no byways" rows are left out;
  * Wales and England, councils and parks;
  * a lane name shared by several rows counts where the table says;
  * the API's conclusions mapped to the four results the app knows;
  * the file is rewritten only when more than `generated` moved;
  * the catalogue carries `status`, and status.yml is wired as described.

Standard library only: status.yml installs nothing.
"""
import csv
import datetime
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_status as bs  # noqa: E402

# ---------------------------------------------------------------- the schema

TOP_KEYS = ["schema", "generated", "datasets", "authorities", "honours",
            "notes"]
DATASET_KEYS = ["id", "name", "what", "schedule", "last_run", "result",
                "last_ok", "data_as_of"]
DATASET_IDS = ["lanes", "orders", "council-closures", "council-roads",
               "street-works", "order-lists", "status-changes", "height",
               "imagery", "routing"]
AUTHORITY_KEYS = ["name", "full_name", "country", "kind", "byways",
                  "unsurfaced_roads", "dtro_records", "dtro", "sources",
                  "honours", "activity"]
DTRO_LABELS = {None, "Publishes to D-TRO", "Not on D-TRO yet"}
SOURCE_KEYS = ["what", "from", "as_of"]
HONOUR_KEYS = ["name", "reasons"]
NOTE_KEYS = ["date", "text"]
RESULTS = {"ok", "problem", "running", "unknown"}
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# ------------------------------------------------------------- the fixtures

# Personal details as a council's own records carry them. None of these may
# reach the published file.
CONTACT_NAME = "Jane Smith"
CONTACT_EMAIL = "jane.smith@devon.example.gov.uk"
CONTACT_PHONE = "01392 000000"
OFFICER = "Bob Jones"
ADDRESS = "County Hall, Topsham Road, Exeter EX2 4QD"

TABLE = [
    ("swa_code", "display_name", "lane_names", "note"),
    ("11", "National Highways", "", "Strategic road network; no byways."),
    ("1155", "Devon County Council", "Devon", ""),
    ("1265", "Dorset Council", "Dorset", ""),
    ("3300", "Somerset Council", "Somerset", ""),
    ("2745", "North Yorkshire Council", "North Yorkshire", ""),
    ("3940", "Wiltshire Council", "Wiltshire", ""),
    ("935", "Westmorland and Furness Council",
     "Cumbria;Lake District National Park;Westmorland and Furness", ""),
    ("940", "Cumberland Council", "Cumbria;Lake District National Park", ""),
    ("6850", "Powys County Council", "Brecon Beacons National Park;Powys",
     ""),
    ("6815", "City of Cardiff Council", "Cardiff", ""),
    ("", "Lake District National Park Authority",
     "Lake District National Park", "UNMAPPED"),
    ("", "Bannau Brycheiniog National Park Authority",
     "Brecon Beacons National Park", "UNMAPPED"),
]

WAYS = (
    [("Devon", "boat", "council:devon")] * 3 +
    [("Devon", "ucr", "highway-records:devon-county-council")] * 2 +
    [("Dorset", "boat", "rowmaps:dorset")] * 4 +
    [("Wiltshire", "boat", "rowmaps:wiltshire")] * 2 +
    [("Cumbria", "boat", "rowmaps:cumbria")] * 5 +
    [("Westmorland and Furness", "boat",
      "rowmaps:westmorland-and-furness")] * 7 +
    [("Lake District National Park", "boat",
      "rowmaps:lake-district-national-park")] * 11 +
    [("Powys", "boat", "rowmaps:powys")] * 13 +
    [("Brecon Beacons National Park", "boat",
      "rowmaps:brecon-beacons-national-park")] * 17
)

# council_sources.SOURCES as the builder sees it (by id), with the contact
# fields a careless adapter might one day carry.
CLOSURE_SOURCES = {
    "devon-closures": {
        "id": "devon-closures", "authority": "Devon",
        "name": "Devon County Council - temporary path closures",
        "licence": "Open Government Licence v3.0",
        "contact": CONTACT_NAME, "email": CONTACT_EMAIL},
    "dorset-closures": {
        "id": "dorset-closures", "authority": "Dorset",
        "name": "Dorset Council - rights of way closures (WFS)",
        "licence": "Published by the council (no licence stated)"},
    "wiltshire-closures": {
        "id": "wiltshire-closures", "authority": "Wiltshire",
        "name": "Wiltshire Council - rights of way closures register",
        "licence": "Open Government Licence v3.0"},
}
BYWAY_LAYERS = [{"code": "DN", "council": "Devon County Council",
                 "licence": None}]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _json(path, obj):
    _write(path, json.dumps(obj, indent=1) + "\n")


def make_checkout(tmp, publishers=True):
    """A checkout holding only what build_status reads."""
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(TABLE)
    _write(os.path.join(tmp, "tools", "tro_authorities.csv"), out.getvalue())
    _json(os.path.join(tmp, "council-ways", "status.json"), {
        "DN": {"council": "Devon County Council", "last_ok": "2026-10-01",
               "error": "write to " + CONTACT_EMAIL}})
    _json(os.path.join(tmp, "council-ucrs", "status.json"), {
        "DN": {"council": "Devon County Council", "last_ok": "2026-10-02"}})
    _json(os.path.join(tmp, "council-ucrs", "DN.json"), {"source": {
        "code": "DN", "authority": "Devon", "council": "Devon County Council",
        "licence": None, "contact": CONTACT_NAME, "phone": CONTACT_PHONE,
        "address": ADDRESS}})
    _json(os.path.join(tmp, "tro", "council", "status.json"), {
        "devon-closures": {
            "authority": "Devon", "last_ok": "2026-10-03",
            "name": "Devon County Council - temporary path closures",
            "error": "ask %s on %s" % (CONTACT_NAME, CONTACT_PHONE)},
        "dorset-closures": {
            "authority": "Dorset", "last_ok": "2026-10-04",
            "name": "Dorset Council - rights of way closures (WFS)"},
        "wiltshire-closures": {
            "authority": "Wiltshire", "last_ok": "2026-10-05",
            "name": "Wiltshire Council - rights of way closures register"},
        # Never read successfully: speaks for nobody.
        "somerset-closures": {
            "authority": "Somerset", "name": "Somerset Council - closures",
            "blocked": "bot challenge"}})
    _json(os.path.join(tmp, "tro", "council", "devon-closures.json"), {
        "items": [{"title": "Path closed", "contact": CONTACT_NAME,
                   "email": CONTACT_EMAIL, "phone": CONTACT_PHONE}]})
    _json(os.path.join(tmp, "tro", "register", "orders.json"), [
        {"council": "North Yorkshire Council", "status": "approved",
         "review_note": "confirmed by %s (%s)" % (OFFICER, CONTACT_EMAIL)},
        {"council": "Dorset Council", "status": "needs-review"},
        {"council": "Dorset Council", "status": "listed-only"}])
    _json(os.path.join(tmp, "tro", "register", "pages.json"), [
        {"council": "North Yorkshire Council", "url": "https://x.invalid/"}])
    if publishers:
        _json(os.path.join(tmp, "tro", "publishers.json"), {
            "generated": "2026-10-08", "authorities": [
                {"swa": "3940", "name": "Wiltshire Council", "records": 5,
                 "newest": "2026-09-01"},
                {"swa": "1155", "name": "Devon County Council",
                 "records": 0, "newest": None}]})
    _json(os.path.join(tmp, "tro", "index.json"),
          {"generated": "2026-10-08", "cut": "2026-09-06", "packs": []})
    _json(os.path.join(tmp, "containers", "manifest.json"), {"containers": [
        {"dataset": "ways", "waysCut": "2026-10-07T12:00:00Z"},
        {"dataset": "ways", "waysCut": "2026-10-08T12:26:02Z"}]})
    _json(os.path.join(tmp, "tro", "streetworks", "orders",
                       "street-manager.json"),
          {"months": ["2026-09", "2026-08", "2026-02"]})
    for name, cron in (("refresh-data.yml", "23 3,9,15,21 * * *"),
                       ("council-ways.yml", "17 3 * * *")):
        _write(os.path.join(tmp, ".github", "workflows", name),
               "on:\n  schedule:\n    - cron: '%s'\n" % cron)
    _json(os.path.join(tmp, "tools", "status_notes.json"), [
        {"date": "2026-09-%02d" % d, "text": "Note %d." % d}
        for d in range(1, 13)])
    return tmp


def no_api(_workflow):
    return None


NOW = datetime.datetime(2026, 10, 8, 21, 17, tzinfo=datetime.timezone.utc)


class Fixture(unittest.TestCase):
    publishers = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        make_checkout(self.tmp, publishers=self.publishers)

    def build(self, runs_for=no_api, previous=None, ways=WAYS,
              closures=CLOSURE_SOURCES):
        return bs.build(self.tmp, runs_for, list(ways), closures,
                        BYWAY_LAYERS, previous=previous, now=NOW)

    def authority(self, status, full_name):
        rows = [a for a in status["authorities"]
                if a["full_name"] == full_name]
        self.assertEqual(len(rows), 1, "%s listed %d times"
                         % (full_name, len(rows)))
        return rows[0]


# ------------------------------------------------------------------ schema

class TheFileMatchesTheSchema(Fixture):
    def test_every_key_is_present_and_no_other(self):
        s = self.build()
        self.assertEqual(list(s), TOP_KEYS)
        self.assertEqual(s["schema"], 1)
        self.assertRegex(s["generated"], ISO_TIME)
        self.assertEqual(s["generated"], "2026-10-08T21:17:00Z")

        self.assertEqual([d["id"] for d in s["datasets"]], DATASET_IDS)
        for d in s["datasets"]:
            self.assertEqual(list(d), DATASET_KEYS, d["id"])
            for key in ("name", "what", "schedule"):
                self.assertTrue(isinstance(d[key], str) and d[key], key)
            self.assertIn(d["result"], RESULTS)
            for key in ("last_run", "last_ok"):
                self.assertTrue(d[key] is None or ISO_TIME.match(d[key]))
            self.assertTrue(d["data_as_of"] is None
                            or ISO_DAY.match(d["data_as_of"]), d)

        self.assertTrue(s["authorities"], "PREMISE: no authorities")
        for a in s["authorities"]:
            self.assertEqual(list(a), AUTHORITY_KEYS, a["full_name"])
            self.assertIn(a["country"], ("England", "Wales"))
            self.assertIn(a["kind"], ("council", "park"))
            for key in ("byways", "unsurfaced_roads"):
                self.assertIsInstance(a[key], int)
            self.assertTrue(a["dtro_records"] is None
                            or isinstance(a["dtro_records"], int))
            self.assertIn(a["dtro"], DTRO_LABELS, a["full_name"])
            self.assertIsInstance(a["activity"], list)
            for src in a["sources"]:
                self.assertEqual(list(src), SOURCE_KEYS)
                self.assertTrue(src["as_of"] is None
                                or ISO_DAY.match(src["as_of"]), src)
            for reason in a["honours"]:
                self.assertIsInstance(reason, str)

        self.assertTrue(s["honours"], "PREMISE: nobody honoured")
        for h in s["honours"]:
            self.assertEqual(list(h), HONOUR_KEYS)
        for n in s["notes"]:
            self.assertEqual(list(n), NOTE_KEYS)
            self.assertRegex(n["date"], ISO_DAY)

    def test_authorities_by_country_then_name(self):
        s = self.build()
        keys = [(a["country"], a["name"].lower()) for a in s["authorities"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(s["authorities"][-1]["country"], "Wales")
        self.assertEqual(s["authorities"][0]["country"], "England")

    def test_the_counts_and_where_they_came_from(self):
        s = self.build()
        devon = self.authority(s, "Devon County Council")
        self.assertEqual((devon["byways"], devon["unsurfaced_roads"]), (3, 2))
        self.assertEqual(devon["name"], "Devon")
        self.assertEqual(devon["sources"], [
            {"what": "Byways", "from": "Devon County Council",
             "as_of": "2026-10-01"},
            {"what": "Unsurfaced roads", "from": "Devon County Council",
             "as_of": "2026-10-02"},
            {"what": "Closures",
             "from": "Devon County Council - temporary path closures",
             "as_of": "2026-10-03"}])
        dorset = self.authority(s, "Dorset Council")
        # rowmaps' fetch date is not committed anywhere: null, not a guess.
        self.assertIn({"what": "Byways",
                       "from": "Dorset Council, via rowmaps.com",
                       "as_of": None}, dorset["sources"])
        # A source never read successfully speaks for nobody.
        self.assertFalse([a for a in s["authorities"]
                          if any("Somerset" in x["from"]
                                 for x in a["sources"])])

    def test_dataset_dates_and_schedules_from_the_checkout(self):
        s = self.build()
        by_id = dict((d["id"], d) for d in s["datasets"])
        self.assertEqual(by_id["lanes"]["data_as_of"], "2026-10-08")
        self.assertEqual(by_id["orders"]["data_as_of"], "2026-10-08")
        self.assertEqual(by_id["council-closures"]["data_as_of"],
                         "2026-10-05")
        self.assertEqual(by_id["council-roads"]["data_as_of"], "2026-10-02")
        self.assertEqual(by_id["street-works"]["data_as_of"], "2026-09-30")
        self.assertIsNone(by_id["imagery"]["data_as_of"])
        self.assertEqual(by_id["lanes"]["schedule"], "Every 6 hours")
        self.assertEqual(by_id["council-roads"]["schedule"], "Daily")
        # No workflow file in this checkout: said, not invented.
        self.assertEqual(by_id["routing"]["schedule"], "When needed")

    def test_notes_are_the_ten_newest_first(self):
        notes = self.build()["notes"]
        self.assertEqual(len(notes), 10)
        self.assertEqual(notes[0], {"date": "2026-09-12", "text": "Note 12."})
        self.assertEqual(notes[-1]["date"], "2026-09-03")

    def test_the_real_notes_file_reads(self):
        notes = bs.load_notes(os.path.join(HERE, "status_notes.json"))
        self.assertTrue(notes, "tools/status_notes.json gives no notes")
        self.assertLessEqual(len(notes), 10)
        for n in notes:
            self.assertNotIn("@", n["text"])


# ----------------------------------------------------------- personal data

class NoPersonalData(Fixture):
    def test_no_contact_detail_reaches_the_file(self):
        text = json.dumps(self.build(), ensure_ascii=False)
        self.assertIn("Devon County Council - temporary path closures", text,
                      "PREMISE: the source carrying contact fields was not "
                      "read at all, so its contacts could not leak")
        self.assertIn("North Yorkshire Council - byway orders", text,
                      "PREMISE: the register entry with a review note was "
                      "not read")
        self.assertNotIn("@", text)
        for detail in (CONTACT_NAME, CONTACT_EMAIL, CONTACT_PHONE, OFFICER,
                       "jane.smith"):
            self.assertNotIn(detail, text)

    def test_an_address_in_a_source_name_is_refused(self):
        leaky = dict(CLOSURE_SOURCES)
        leaky["dorset-closures"] = dict(
            leaky["dorset-closures"],
            name="Dorset Council - closures (ask %s)" % CONTACT_EMAIL)
        with self.assertRaises(ValueError):
            self.build(closures=leaky)

    def test_no_phone_number_or_postcode_anywhere(self):
        text = json.dumps(self.build(), ensure_ascii=False)
        # Written here, not borrowed from build_status, so a guard that is
        # loosened there is not loosened here too.
        self.assertFalse(re.search(r"(?:\+44|\b0)\d{2,4}[\s-]?\d{3,4}", text))
        self.assertFalse(re.search(
            r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", text, re.I))
        for detail in (ADDRESS, "EX2 4QD", "Topsham"):
            self.assertNotIn(detail, text)

    def test_a_phone_number_in_a_source_name_is_refused(self):
        for leak in (CONTACT_PHONE, "07700 900123", "+44 20 7946 0000"):
            leaky = dict(CLOSURE_SOURCES)
            leaky["dorset-closures"] = dict(
                leaky["dorset-closures"],
                name="Dorset Council - closures (ring %s)" % leak)
            with self.assertRaises(ValueError, msg=leak):
                self.build(closures=leaky)

    def test_a_postcode_in_a_source_name_is_refused(self):
        for leak in ("EX2 4QD", "SW1A 1AA", "dt1 1xj"):
            leaky = dict(CLOSURE_SOURCES)
            leaky["dorset-closures"] = dict(
                leaky["dorset-closures"],
                name="Dorset Council - closures, County Hall %s" % leak)
            with self.assertRaises(ValueError, msg=leak):
                self.build(closures=leaky)

    def test_the_real_strings_are_not_mistaken_for_either(self):
        # Dates, times, counts and names must pass the check, or the guard
        # would stop every hourly run.
        bs.public_check(self.build())
        bs.public_check({"datasets": [], "x": [
            "2026-10-08T18:05:00Z", "2026-09-30", "Every 6 hours",
            "the 2016 Sentinel-2 cloudless mosaic", "A1 and M6 junctions"]})

    def test_public_check_refuses_an_unknown_result(self):
        s = self.build()
        s["datasets"][0]["result"] = "failed"
        with self.assertRaises(ValueError):
            bs.public_check(s)


# ----------------------------------------------------------------- honours

class Honours(Fixture):
    def test_two_criteria_are_honoured(self):
        s = self.build()
        devon = self.authority(s, "Devon County Council")
        # Its own layers and closures, one of them OGL; 0 D-TRO records.
        self.assertEqual(devon["honours"], [bs.OPEN_DATA, bs.OGL])
        self.assertIn({"name": "Devon", "reasons": [bs.OPEN_DATA, bs.OGL]},
                      s["honours"])

    def test_three_criteria_are_all_given(self):
        s = self.build()
        wilts = self.authority(s, "Wiltshire Council")
        self.assertEqual(wilts["dtro_records"], 5)
        self.assertEqual(wilts["honours"], [bs.OPEN_DATA, bs.OGL, bs.DTRO])

    def test_one_criterion_is_not_an_honour(self):
        s = self.build()
        honoured = set(h["name"] for h in s["honours"])
        # Dorset: its own closures feed, no OGL stated, no D-TRO count.
        dorset = self.authority(s, "Dorset Council")
        self.assertTrue(any(src["what"] == "Closures"
                            for src in dorset["sources"]),
                        "PREMISE: Dorset meets one criterion")
        self.assertEqual(dorset["honours"], [])
        self.assertNotIn("Dorset", honoured)
        # North Yorkshire: an approved register entry, and nothing else.
        nyc = self.authority(s, "North Yorkshire Council")
        self.assertTrue(nyc["sources"], "PREMISE: its register entry")
        self.assertEqual(nyc["honours"], [])
        self.assertNotIn("North Yorkshire", honoured)

    def test_only_approved_register_entries_count(self):
        s = self.build()
        dorset = self.authority(s, "Dorset Council")
        self.assertFalse([x for x in dorset["sources"]
                          if "byway orders listed" in x["from"]])

    def test_top_level_honours_are_exactly_the_honoured_authorities(self):
        s = self.build()
        self.assertEqual(
            s["honours"],
            [{"name": a["name"], "reasons": a["honours"]}
             for a in s["authorities"] if len(a["honours"]) >= 2])


# ---------------------------------------------------------- the authorities

class WhoIsListed(Fixture):
    def test_no_byways_rows_are_left_out(self):
        s = self.build()
        names = [a["full_name"] for a in s["authorities"]]
        self.assertNotIn("National Highways", names)
        self.assertEqual(len(names), len(TABLE) - 2,
                         "every other row is listed")

    def test_wales_and_england(self):
        s = self.build()
        for full, country, kind in (
                ("Powys County Council", "Wales", "council"),
                ("City of Cardiff Council", "Wales", "council"),
                ("Bannau Brycheiniog National Park Authority", "Wales",
                 "park"),
                ("Devon County Council", "England", "council"),
                ("Cumberland Council", "England", "council"),
                ("Lake District National Park Authority", "England", "park")):
            a = self.authority(s, full)
            self.assertEqual((a["country"], a["kind"]), (country, kind), full)
        self.assertEqual(self.authority(s, "City of Cardiff Council")["name"],
                         "Cardiff")

    def test_a_shared_lane_name_counts_where_the_table_says(self):
        s = self.build()
        count = lambda full: self.authority(s, full)["byways"]
        # The parks' own lanes are the parks', not every council's.
        self.assertEqual(count("Lake District National Park Authority"), 11)
        self.assertEqual(count("Bannau Brycheiniog National Park Authority"),
                         17)
        self.assertEqual(count("Powys County Council"), 13)
        # Cumbria belongs to both successors, as the table says.
        self.assertEqual(count("Westmorland and Furness Council"), 5 + 7)
        self.assertEqual(count("Cumberland Council"), 5)

    def test_dtro_records(self):
        s = self.build()
        self.assertEqual(self.authority(s, "Devon County Council")
                         ["dtro_records"], 0)
        # Not in the file at all (a park has no code): unknown, not zero.
        self.assertIsNone(self.authority(
            s, "Lake District National Park Authority")["dtro_records"])
        self.assertIsNone(self.authority(s, "Dorset Council")["dtro_records"])
        wilts = self.authority(s, "Wiltshire Council")
        self.assertIn({"what": "Traffic orders",
                       "from": "Wiltshire Council, via the Department for "
                               "Transport's D-TRO service",
                       "as_of": "2026-09-01"}, wilts["sources"])


class TheDtroLabelIsNeutral(Fixture):
    def test_the_label_follows_the_count(self):
        s = self.build()
        label = lambda full: self.authority(s, full)["dtro"]
        self.assertEqual(label("Wiltshire Council"), "Publishes to D-TRO")
        self.assertEqual(label("Devon County Council"), "Not on D-TRO yet")
        # Not in the publishers file: we cannot say, so we say nothing.
        self.assertIsNone(label("Dorset Council"))
        self.assertIsNone(label("Lake District National Park Authority"))

    def test_never_dormant_or_inactive(self):
        text = json.dumps(self.build(), ensure_ascii=False).lower()
        self.assertIn("not on d-tro yet", text, "PREMISE: no label written")
        for word in ("dormant", "inactive", "overdue", "failed"):
            self.assertNotIn(word, text)

    def test_a_word_of_blame_is_refused(self):
        for word in ("Dormant on D-TRO", "Inactive", "Reply overdue"):
            s = self.build()
            s["authorities"][0]["activity"] = [{"text": word,
                                                "since": "2026-10"}]
            with self.assertRaises(ValueError, msg=word):
                bs.public_check(s)


class WithoutThePublishersFile(Fixture):
    publishers = False

    def test_every_dtro_count_is_unknown(self):
        s = self.build()
        self.assertEqual(set(a["dtro_records"] for a in s["authorities"]),
                         {None})
        self.assertEqual(set(a["dtro"] for a in s["authorities"]), {None})
        # And so nobody is honoured for D-TRO.
        self.assertFalse([h for h in s["honours"] if bs.DTRO in h["reasons"]])


class TheRealTable(unittest.TestCase):
    """WALES against the real council table and GeoPlace's code ranges."""

    def test_wales_is_every_68xx_and_69xx_row_and_the_park(self):
        rows = bs.load_table(os.path.join(HERE, "tro_authorities.csv"))
        names = set(r["full_name"] for r in rows)
        self.assertEqual(sorted(bs.WALES - names), [],
                         "WALES names a row the table does not have")
        for r in rows:
            welsh_code = bool(r["swa"]) and r["swa"][:2] in ("68", "69")
            if r["swa"]:
                self.assertEqual(r["full_name"] in bs.WALES, welsh_code,
                                 r["full_name"])
        self.assertEqual(len([n for n in bs.WALES
                              if bs.kind_of(n) == "council"]), 22)

    def test_short_names_are_unique_and_plain(self):
        rows = bs.load_table(os.path.join(HERE, "tro_authorities.csv"))
        short = [bs.short_name(r["full_name"]) for r in rows]
        self.assertEqual(len(short), len(set(short)))
        for full, want in (("Devon County Council", "Devon"),
                           ("London Borough of Barnet", "Barnet"),
                           ("City of London Corporation", "City of London"),
                           ("Council of the Isles of Scilly",
                            "Isles of Scilly"),
                           ("City and County of Swansea Council", "Swansea"),
                           ("Lake District National Park Authority",
                            "Lake District National Park")):
            self.assertEqual(bs.short_name(full), want)

    def test_no_byways_rows_are_dropped_from_the_real_table(self):
        rows = bs.load_table(os.path.join(HERE, "tro_authorities.csv"))
        names = set(r["full_name"] for r in rows)
        for gone in ("National Highways", "Welsh Government",
                     "Transport for London"):
            self.assertNotIn(gone, names)
        self.assertIn("Devon County Council", names)


# ---------------------------------------------------------------- the runs

def run(status, conclusion, started):
    return {"status": status, "conclusion": conclusion,
            "run_started_at": started, "created_at": started}


class TheRunResults(Fixture):
    def test_conclusions_map_to_the_four_results(self):
        for status, conclusion, want in (
                ("completed", "success", "ok"),
                ("completed", "failure", "problem"),
                ("completed", "timed_out", "problem"),
                ("completed", "startup_failure", "problem"),
                ("in_progress", None, "running"),
                ("queued", None, "running"),
                ("completed", "cancelled", "unknown"),
                ("completed", "skipped", "unknown"),
                ("completed", "action_required", "unknown"),
                ("completed", "neutral", "unknown"),
                ("waiting", None, "unknown"),
                (None, None, "unknown")):
            self.assertEqual(bs.result_of({"status": status,
                                           "conclusion": conclusion}), want,
                             (status, conclusion))

    def test_newest_run_is_the_result_and_last_ok_the_newest_success(self):
        runs = [run("completed", "failure", "2026-10-08T14:00:00Z"),
                run("in_progress", None, "2026-10-08T20:00:00Z"),
                run("completed", "success", "2026-10-08T02:00:00Z"),
                run("completed", "success", "2026-10-08T08:00:00Z")]
        self.assertEqual(bs.summarise_runs(runs),
                         ("2026-10-08T20:00:00Z", "running",
                          "2026-10-08T08:00:00Z"))

    def test_no_answer_is_unknown_and_keeps_what_was_true(self):
        before = {"last_run": "2026-10-08T08:00:00Z", "result": "ok",
                  "last_ok": "2026-10-08T08:00:00Z"}
        self.assertEqual(bs.summarise_runs(None, before),
                         ("2026-10-08T08:00:00Z", "unknown",
                          "2026-10-08T08:00:00Z"))
        self.assertEqual(bs.summarise_runs(None), (None, "unknown", None))
        # A success older than the ten runs read is still the last one.
        runs = [run("completed", "failure", "2026-10-09T00:00:00Z")]
        self.assertEqual(bs.summarise_runs(runs, before)[2],
                         "2026-10-08T08:00:00Z")

    def test_the_fake_api_is_asked_for_every_dataset_workflow(self):
        asked = []

        def fake(workflow):
            asked.append(workflow)
            if workflow == "traffic-orders.yml":
                return [run("completed", "failure", "2026-10-08T20:23:05Z"),
                        run("completed", "success", "2026-10-08T14:23:01Z")]
            return []
        s = self.build(runs_for=fake)
        self.assertEqual(asked, [w for _, w, _, _ in bs.DATASETS])
        orders = [d for d in s["datasets"] if d["id"] == "orders"][0]
        self.assertEqual((orders["last_run"], orders["result"],
                          orders["last_ok"]),
                         ("2026-10-08T20:23:05Z", "problem",
                          "2026-10-08T14:23:01Z"))

    def test_the_token_is_sent_and_never_printed(self):
        sent = {}

        class Answer(object):
            def __enter__(self):
                return io.BytesIO(json.dumps({"workflow_runs": [
                    run("completed", "success", "2026-10-08T00:00:00Z")]})
                    .encode("utf-8"))

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["auth"] = req.get_header("Authorization")
            return Answer()
        saved = bs.urllib.request.urlopen
        bs.urllib.request.urlopen = fake_urlopen
        try:
            runs = bs.github_runs("council-ways.yml", token="t0ken")
        finally:
            bs.urllib.request.urlopen = saved
        self.assertEqual(len(runs), 1)
        self.assertEqual(sent["auth"], "Bearer t0ken")
        self.assertEqual(sent["url"],
                         "https://api.github.com/repos/lpsd-1/"
                         "trailblazer-datasets/actions/workflows/"
                         "council-ways.yml/runs?per_page=10")


class TheRealSchedules(unittest.TestCase):
    """schedule_text against the crons the data workflows actually have."""

    def test_each_workflow_reads_as_a_rider_would_say_it(self):
        want = {"refresh-data.yml": "Every 6 hours",
                "traffic-orders.yml": "Every 6 hours",
                "council-orders.yml": "Every 6 hours",
                "council-ways.yml": "Daily",
                "street-manager.yml": "Monthly",
                "order-register.yml": "Every 3 months",
                "status-changes.yml": "Weekly",
                "height.yml": "Daily",
                "satellite.yml": "Twice a day",
                "mirror-routing.yml": "Weekly"}
        for _, workflow, _, _ in bs.DATASETS:
            crons = bs.workflow_crons(os.path.join(
                ROOT, ".github", "workflows", workflow))
            self.assertTrue(crons, "PREMISE: %s has no cron" % workflow)
            self.assertEqual(bs.schedule_text(crons), want[workflow],
                             "%s %r" % (workflow, crons))


# ------------------------------------------------------- written on change

class WrittenOnlyWhenSomethingMoved(Fixture):
    def test_a_new_time_alone_is_not_written(self):
        path = os.path.join(self.tmp, "published", "status.json")
        first = self.build()
        self.assertTrue(bs.write_if_changed(path, first))
        later = dict(first, generated="2026-10-08T22:17:00Z")
        self.assertFalse(bs.write_if_changed(path, later))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["generated"],
                             "2026-10-08T21:17:00Z")

    def test_anything_else_moving_is_written(self):
        path = os.path.join(self.tmp, "published", "status.json")
        first = self.build()
        bs.write_if_changed(path, first)
        moved = json.loads(json.dumps(first))
        moved["generated"] = "2026-10-08T22:17:00Z"
        moved["datasets"][0]["result"] = "running"
        self.assertTrue(bs.write_if_changed(path, moved))
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        self.assertEqual(got["datasets"][0]["result"], "running")
        self.assertEqual(got["generated"], "2026-10-08T22:17:00Z")

    def test_written_with_lf_only(self):
        path = os.path.join(self.tmp, "published", "status.json")
        bs.write_if_changed(path, self.build())
        with open(path, "rb") as fh:
            self.assertNotIn(b"\r", fh.read())


# --------------------------------------------------- the containers, for real

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _geometry(points, scale=10 ** 7):
    """byway_match.unpack_geometry's input, for one line."""
    blob = _varint(1) + _varint(len(points))
    lon0 = lat0 = 0
    for lon, lat in points:
        x, y = int(round(lon * scale)), int(round(lat * scale))
        for d in (x - lon0, y - lat0):
            blob += _varint((d << 1) if d >= 0 else ((-d << 1) - 1))
        lon0, lat0 = x, y
    return blob


def make_container(path, ways, ucrs=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for table in ("ways", "ucr_ways"):
        conn.execute("CREATE TABLE %s (way_uid TEXT PRIMARY KEY, "
                     "way_class TEXT, name TEXT, authority TEXT, "
                     "source TEXT, geometry BLOB)" % table)
    line = _geometry([(-3.5, 50.7), (-3.49, 50.71)])
    for table, rows in (("ways", ways), ("ucr_ways", ucrs)):
        for uid, klass, authority, source in rows:
            conn.execute("INSERT INTO %s VALUES (?, ?, ?, ?, ?, ?)" % table,
                         (uid, klass, "Byway open to all traffic (BOAT) X 1",
                          authority, source, line))
    conn.commit()
    conn.close()


class TheContainersAreRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="status-ways-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_each_way_once_with_its_source_and_the_overview_skipped(self):
        # ways-north sorts BEFORE ways-overview, as the real east-anglia,
        # midlands and north do: an overview read after it would win.
        make_container(os.path.join(self.tmp, "ways-north.tbmap"),
                       [("DN-1", "boat", "Devon", "council:devon"),
                        ("DN-2", "boat", "Devon", "council:devon")],
                       [("DN-U1", "ucr", "Devon",
                         "highway-records:devon-county-council")])
        # A way on a region boundary is in both regions' containers.
        make_container(os.path.join(self.tmp, "ways-wales.tbmap"),
                       [("DN-2", "boat", "Devon", "council:devon"),
                        ("PW-1", "boat", "Powys", "rowmaps:powys")])
        # The overview's simplified copies are never read, not even for a
        # source (here one that disagrees, so reading it would show).
        make_container(os.path.join(self.tmp, "ways-overview.tbmap"),
                       [("DN-1", "boat", "Devon", "rowmaps:devon"),
                        ("XX-1", "boat", "Devon", "rowmaps:devon")])
        ways = sorted(bs.load_ways(os.path.join(self.tmp, "ways-*.tbmap")))
        self.assertEqual(ways, [
            ("Devon", "boat", "council:devon"),
            ("Devon", "boat", "council:devon"),
            ("Devon", "ucr", "highway-records:devon-county-council"),
            ("Powys", "boat", "rowmaps:powys")])

    def test_main_end_to_end_then_unchanged(self):
        root = make_checkout(os.path.join(self.tmp, "checkout"))
        os.makedirs(os.path.join(root, "containers"), exist_ok=True)
        make_container(os.path.join(root, "containers",
                                    "ways-south-west.tbmap"),
                       [("DN-1", "boat", "Devon", "council:devon")])
        out = os.path.join(root, "published", "status.json")
        argv = ["--root", root, "--out", out, "--no-api"]
        self.assertEqual(bs.main(argv), 0)
        with open(out, encoding="utf-8") as fh:
            first = json.load(fh)
        devon = [a for a in first["authorities"]
                 if a["name"] == "Devon"][0]
        self.assertEqual(devon["byways"], 1)
        # As if written an hour ago: a rerun now differs in `generated`
        # whatever the clock says, and in nothing else.
        first["generated"] = "2026-01-01T00:00:00Z"
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(first, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        with open(out, "rb") as fh:
            before = fh.read()
        self.assertEqual(bs.main(argv), 0)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "a run where only the time moved rewrote the "
                             "file, so the hourly job would commit hourly")


# --------------------------------------------------- the catalogue, and CI

class TheCatalogueCarriesStatus(unittest.TestCase):
    def test_status_is_the_published_file_under_base_url(self):
        import build_catalogue as K
        saved = K.routing_index
        K.routing_index = lambda: {"W5_N50": 1}
        tmp = tempfile.mkdtemp(prefix="status-cat-")
        self.addCleanup(shutil.rmtree, tmp, True)
        stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            cat = K.build(None, "https://lpsd-1.github.io/"
                          "trailblazer-datasets/", "2026-10-08T00:00:00Z",
                          conditions_dir=tmp)
        finally:
            K.routing_index = saved
            sys.stdout = stdout
        self.assertEqual(cat.get("status"),
                         "https://lpsd-1.github.io/trailblazer-datasets/"
                         "published/status.json")
        self.assertEqual(os.path.normpath(os.path.join(ROOT, *K.STATUS_PATH
                                                       .split("/"))),
                         os.path.normpath(bs.OUT),
                         "the catalogue names a different file from the "
                         "one build_status writes")


def _section(text, key, indent):
    """The lines under `key:` at `indent`, up to the next key at that
    indent or less."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line == " " * indent + key + ":":
            out = []
            for row in lines[i + 1:]:
                if row.strip() and not row.lstrip().startswith("#") and \
                        len(row) - len(row.lstrip()) <= indent:
                    break
                out.append(row)
            return "\n".join(out)
    raise AssertionError("PREMISE: no %r at indent %d" % (key, indent))


class TheStatusWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, ".github", "workflows", "status.yml"),
                  "rb") as fh:
            cls.raw = fh.read()
        cls.text = cls.raw.decode("utf-8")

    def test_it_runs_after_every_dataset_workflow_by_its_name(self):
        names = []
        for _, workflow, _, _ in bs.DATASETS:
            with open(os.path.join(ROOT, ".github", "workflows", workflow),
                      encoding="utf-8") as fh:
                m = re.search(r"^name:\s*(.+?)\s*$", fh.read(), re.M)
            self.assertTrue(m, "PREMISE: %s has no name:" % workflow)
            names.append(m.group(1))
        listed = re.findall(r"^\s+-\s+(.+?)\s*$",
                            _section(self.text, "workflows", 4), re.M)
        self.assertEqual(sorted(listed), sorted(names),
                         "workflow_run matches a workflow's name:, and "
                         "these must be exactly the datasets shown")
        self.assertIn("types: [completed]", _section(self.text,
                                                     "workflow_run", 2))
        self.assertIn("workflow_dispatch:", self.text)

    def test_hourly_off_the_hour_and_half_hour(self):
        crons = bs.workflow_crons(os.path.join(ROOT, ".github", "workflows",
                                               "status.yml"))
        self.assertEqual(len(crons), 1)
        minute, hour = crons[0].split()[:2]
        self.assertEqual(hour, "*")
        self.assertTrue(minute.isdigit() and int(minute) not in (0, 30),
                        crons[0])

    def test_permissions_and_concurrency(self):
        perms = _section(self.text, "permissions", 0)
        self.assertRegex(perms, r"(?m)^\s+contents:\s*write\b")
        self.assertRegex(perms, r"(?m)^\s+actions:\s*read\b")
        conc = _section(self.text, "concurrency", 0)
        self.assertRegex(conc, r"(?m)^\s+group:\s*status\s*$")
        self.assertRegex(conc, r"(?m)^\s+cancel-in-progress:\s*false\b")

    def test_it_builds_then_commits_only_the_status_file(self):
        self.assertIn("python tools/build_status.py", self.text)
        self.assertIn("git add -A published/status.json", self.text)
        self.assertEqual(re.findall(r"git add[^\n]*", self.text),
                         ["git add -A published/status.json"])
        self.assertIn("for attempt in 1 2 3", self.text)


if __name__ == "__main__":
    unittest.main()
