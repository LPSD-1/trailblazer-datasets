#!/usr/bin/env python3
"""The imagery we ship must be imagery we may ship in a paid app.

    python tools/test_imagery_licence.py

WHY THIS EXISTS
---------------
EOX publish a Sentinel-2 cloudless mosaic for most years, and the years are
NOT licensed alike. From the abstracts in EOX's WMTS capabilities
(https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml, read 9 Oct 2026)
and https://cloudless.eox.at/license-non-commercial:

  s2cloudless_3857        2016 data   CC BY 4.0
  s2cloudless-2017_3857   2017 data   CC BY 4.0
  s2cloudless-2018_3857   2017 data   CC BY-NC-SA 4.0   (sic: 2018 layer)
  s2cloudless-2019 .. -2025           CC BY-NC-SA 4.0

Trail Blazer is sold, so NonCommercial rules every year from 2018 out. This
repository shipped the 2024 layer with an attribution saying "CC BY 4.0",
which was simply wrong. The checks below hold the source to the CC BY years,
and hold every place that names the imagery to the one ATTRIBUTION constant,
so the year cannot drift in one file and not the others.

CC BY 4.0 s.3(a) needs the creator, the licence with a link to it, and a
statement that the material was modified. We sharpen and recompress every
tile, so it was.
"""
import html
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The ONLY layers whose licence allows a paid app, with the year of the
# Copernicus data each contains. From the <Abstract> of each layer in EOX's
# WMTS capabilities: these two say "CC BY 4.0"; every later year says
# "CC BY-NC-SA 4.0". Note s2cloudless-2018_3857 holds 2017 data but is NC, so
# a year alone is not enough: it is the LAYER that is allowed.
CC_BY_LAYERS = {"s2cloudless_3857": 2016, "s2cloudless-2017_3857": 2017}


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def layer_of(source):
    m = re.search(r"/wmts/1\.0\.0/([^/]+)/", source)
    return m.group(1) if m else None


def main():
    bs = load("build_satellite")
    layer = layer_of(bs.SOURCE)
    year = CC_BY_LAYERS.get(layer)

    # 1. The source is a CC BY layer.
    check(year is not None,
          "SOURCE uses %r, which is not a CC BY layer (allowed: %s)"
          % (layer, sorted(CC_BY_LAYERS)))

    # 2. The attribution says what CC BY 4.0 s.3(a) requires.
    a = bs.ATTRIBUTION
    for needed in ("CC BY 4.0",
                   "https://creativecommons.org/licenses/by/4.0/",
                   "EOX IT Services GmbH",
                   "https://cloudless.eox.at",
                   "modified"):
        check(needed in a, "ATTRIBUTION lacks %r: %r" % (needed, a))
    check(year is not None and
          "Copernicus Sentinel data %d" % year in a,
          "ATTRIBUTION does not credit Copernicus Sentinel data of the "
          "layer's year (%s): %r" % (year, a))
    check(year is not None and "EOxCloudless %d" % year in a,
          "ATTRIBUTION does not name the mosaic and its year (%s): %r"
          % (year, a))
    # No other year may appear in it: "2017 ... data 2024" is the mistake
    # this replaces, half corrected.
    years = set(int(y) for y in re.findall(r"\b(20\d\d)\b", a))
    check(year is not None and years == {year},
          "ATTRIBUTION names years %s, expected only %s" % (sorted(years), year))

    # 3. Staged tiles are kept per layer, so a staging cache filled from an
    #    older layer can never be resumed into a pack labelled with this one.
    staged = bs.staged_path("staging", 13, 1, 2)
    check(layer is not None and layer in staged.replace("\\", "/").split("/"),
          "staged tiles are not kept per layer: %r" % staged)

    # 4. The sample plates come from the same layer.
    si = load("sample_imagery")
    check(si.SOURCE == bs.SOURCE,
          "sample_imagery.SOURCE %r differs from build_satellite.SOURCE"
          % si.SOURCE)

    # 5. The comparison page credits the plates with the same words.
    dp = load("make_detail_page")
    text = re.sub(r"\s+", " ",
                  html.unescape(re.sub(r"<[^>]+>", "", dp.PAGE))
                  .replace(" ", " "))
    check(a in text, "make_detail_page's footer is not ATTRIBUTION")

    # 6. The release notes and commit bodies the workflow writes.
    wf = read(".github/workflows/satellite.yml")
    notes = re.findall(r'^\s*NOTES="([^"]*)"\s*$', wf, re.M)
    check(len(notes) == 1 and notes[0].startswith(a),
          "satellite.yml's release notes are not ATTRIBUTION: %r" % notes)
    # And set on the release that already exists, not only on creating it:
    # the release was made under the 2024 mosaic and kept its notes.
    check(re.search(r'^\s*gh release edit satellite --notes "\$NOTES"\s*$',
                    wf, re.M) is not None,
          "satellite.yml never updates an existing release's notes")
    for n, line in enumerate(wf.splitlines(), 1):
        if re.search(r"cloudless", line, re.I) and year is not None:
            check("%d" % year in line,
                  "satellite.yml:%d names the mosaic without its year %d: %s"
                  % (n, year, line.strip()))

    # 7. The public status page says which mosaic.
    st = load("build_status")
    rows = [r for r in st.DATASETS if r[0] == "imagery"]
    check(len(rows) == 1 and year is not None and "%d" % year in rows[0][3],
          "build_status's imagery row does not name year %s: %r"
          % (year, rows))

    # 8. The builder's own account of the licence cites where it was read.
    for cite in ("WMTSCapabilities.xml", "license-non-commercial",
                 "CC BY-NC-SA 4.0"):
        check(cite in (bs.__doc__ or ""),
              "build_satellite's docstring does not cite %r" % cite)

    # 9. Nothing in the tools, workflows or docs still names a NonCommercial
    #    layer or year as the source, or says that the CC BY licence covers
    #    "the data" as if every year were licensed alike.
    stale = [
        r"(?i)CC(&nbsp;| )BY licence covers the data",
        r"s2cloudless-20(1[89]|2\d)_3857",
        r"cloudless 20(1[89]|2\d)\b",
        r"\b20(1[89]|2\d) Sentinel-2 cloudless",
        r"Copernicus Sentinel data 20(1[89]|2\d)",
        r"mosaic is 20(1[89]|2\d)",
    ]
    me = os.path.basename(__file__)
    paths = []
    for d, exts in (("tools", (".py",)), (".github/workflows", (".yml",)),
                    ("docs", (".md",)), ("", (".md",))):
        full = os.path.join(ROOT, d)
        for name in sorted(os.listdir(full)):
            if name.endswith(exts) and name != me:
                paths.append(os.path.join(d, name) if d else name)
    for rel in paths:
        for n, line in enumerate(read(rel).splitlines(), 1):
            for pat in stale:
                if re.search(pat, line):
                    check(False, "%s:%d still names a NonCommercial mosaic: %s"
                          % (rel, n, line.strip()))

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("%d failure(s)" % len(failures))
        return 1
    print("imagery licence: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
