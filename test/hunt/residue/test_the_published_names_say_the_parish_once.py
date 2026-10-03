"""The PUBLISHED containers name a byway's parish once.

FEATURES-14 TRIAGE of the features-13 parish verifier.

466719c ("Gwynedd names its parish once") fixed `council_reference` in
tools/build_packages.py, and the f13 verifier widened it to a parish ending in
a bracket (Nottinghamshire's "Gamston (B)"). Both are in the builder and
tools/test_gwynedd_names_its_parish_once.py proves the builder. But the lane
data riders download was refreshed BEFORE that commit (c825e54, 05:19 UTC on
3 Oct; the fix landed at 08:00 UTC), and has not been rebuilt since: the live
containers still carry 31 Gwynedd names such as "Byway open to all traffic
(BOAT) Abermaw Prow Abermaw Rhif 2" and Gamston twice, in midlands and north.
The app collapses the Gwynedd repeat on the sheet, the card and in search
(`laneNameWithParishOnce`), but not on a dozen other screens, and not
Gamston's bracket at all - so until the data is rebuilt, riders read the
parish twice.

This reads the committed containers/ways-*.tbmap themselves: it goes green
when a refresh built from the fixed builder is published.

Run from the repository root (or under pytest):
    python test/hunt/residue/test_the_published_names_say_the_parish_once.py
"""
import glob
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# The designation the publisher puts before the council reference.
_DESIGNATION = re.compile(r"^.*?\(BOAT\)\s+|^Byway open to all traffic\s+")


def _parish_twice(name):
    """The repeated parish in [name], or None.

    The reference after the designation starts with the parish; it is said
    twice when the same one to four words follow at once, or after one word
    ("Prow"), with the council number still to come.
    """
    rest = _DESIGNATION.sub("", name or "", count=1)
    words = rest.split(" ")
    for k in range(1, 5):
        if len(words) < 2 * k:
            break
        head = " ".join(words[:k])
        if not head[:1].isupper():
            break
        after = rest[len(head) + 1:]
        # A whole word at the repeat's end where that end is a letter ("ST"
        # is not repeated in "ST STEPHEN"); a parish ending in a bracket may
        # run straight on into the number ("Gamston (B)BOAT4"), as
        # build_packages.council_reference now allows.
        end = r"(?![A-Za-z])" if head[-1:].isalpha() else ""
        if re.match(r"(?:[A-Z]\w* )?" + re.escape(head) + end, after):
            if re.search(r"\d", after):
                return head
    return None


def _published():
    paths = sorted(glob.glob(os.path.join(ROOT, "containers", "ways-*.tbmap")))
    names = []
    for path in paths:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            has = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'ways'").fetchone()
            if not has:
                continue
            for (uid, name) in db.execute("SELECT way_uid, name FROM ways"):
                names.append((os.path.basename(path), uid, name))
        finally:
            db.close()
    return names


def test_control_the_rule_finds_both_shapes_and_leaves_ordinary_names():
    assert _parish_twice(
        "Byway open to all traffic (BOAT) Abermaw Prow Abermaw Rhif 2") \
        == "Abermaw"
    assert _parish_twice(
        "Byway open to all traffic (BOAT) Gamston (B) Gamston (B)BOAT4") \
        == "Gamston (B)"
    assert _parish_twice(
        "Byway open to all traffic (BOAT) Prow Abermaw Rhif 2") is None
    assert _parish_twice(
        "Byway open to all traffic (BOAT) Abney and Abney Grange 4") is None
    assert _parish_twice(
        "Byway open to all traffic (BOAT) Llanfachreth Prow Brithdir a "
        "Llanfachreth Rhif 3") is None
    assert _parish_twice("Byway open to all traffic (BOAT) AW 339") is None


def test_the_published_containers_name_each_parish_once():
    names = _published()
    assert len(names) > 1000, (
        "PREMISE: the published ways containers were read (%d rows)"
        % len(names))
    twice = [
        "%s %s: %r" % (f, uid, name)
        for (f, uid, name) in names if _parish_twice(name)
    ]
    assert not twice, (
        "%d published byway names still say the parish twice - the fixed "
        "builder (466719c) has not been run into the published data:\n  %s"
        % (len(twice), "\n  ".join(twice[:12])))


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                failed += 1
                print("FAIL", name)
                print("    ", str(e).replace("\n", "\n     "))
    sys.exit(1 if failed else 0)
