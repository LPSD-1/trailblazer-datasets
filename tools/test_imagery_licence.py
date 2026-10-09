#!/usr/bin/env python3
"""The imagery we ship must be imagery we may ship in a paid app.

    python tools/test_imagery_licence.py

WHY THIS EXISTS
---------------
EOX publish a Sentinel-2 cloudless mosaic for most years, and the years are
NOT licensed alike. From EOX's licence page
(https://cloudless.eox.at/license-non-commercial) and the layer abstracts in
its WMTS capabilities (https://tiles.maps.eox.at/wmts/1.0.0/
WMTSCapabilities.xml), both read 9 Oct 2026 and quoted, dated, in
docs/licences/eox-s2cloudless-2026-10-09.md:

  s2cloudless_3857        2016   CC BY 4.0 on the licence page AND abstract
  s2cloudless-2017_3857   2017   CC BY 4.0 in the abstract only
  s2cloudless-2018_3857   2017 data, CC BY-NC-SA 4.0   (sic: 2018 layer)
  s2cloudless-2019 .. -2025      CC BY-NC-SA 4.0

Trail Blazer is sold, so NonCommercial rules every year from 2018 out, and
only 2016, the year both sources agree on, is relied on. This repository
shipped the 2024 layer with an attribution saying "CC BY 4.0", which was
simply wrong. The checks below hold the source to the 2016 layer, and hold
every place that names the imagery to the one ATTRIBUTION constant,
so the year cannot drift in one file and not the others.

CC BY 4.0 s.3(a) needs the creator, the licence with a link to it, and a
statement that the material was modified. We sharpen and recompress every
tile, so it was.
"""
import contextlib
import datetime as dt
import email.message
import hashlib
import html
import http.server
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The ONLY layer relied on for a paid app: (mosaic year, the attribution EOX's
# licence page requires for it, verbatim). s2cloudless_3857 is the one year
# both the licence page and the WMTS <Abstract> call CC BY 4.0; 2017 is CC BY
# in its abstract alone. Note s2cloudless-2018_3857 holds 2017 data but is
# NC, so a year alone is not enough: it is the LAYER that is allowed.
CC_BY_LAYERS = {"s2cloudless_3857": (
    2016, "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
          "(Contains modified Copernicus Sentinel data 2016 & 2017)")}


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


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Answers each request from a script: bytes for a 200, or an HTTP
    status code to raise. Records every request it is asked to make."""

    def __init__(self, script, default=b"tile"):
        self.script, self.default, self.calls = list(script), default, []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, req.get_header("User-agent")))
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            code, headers = item
            msg = email.message.Message()
            for k, v in headers.items():
                msg[k] = v
            raise urllib.error.HTTPError(req.full_url, code, "no", msg, None)
        return _Response(item)


class _Clock:
    """Time that moves only when the code under test sleeps."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def service_checks(bs):
    # (c) Who we are, with no email address.
    check(bs.USER_AGENT ==
          "TrailBlazer-data/1.0 (+https://lpsd-1.github.io/trailblazer-help/)",
          "USER_AGENT is %r" % bs.USER_AGENT)

    def fetcher(script, retries=4):
        clock = _Clock()
        opener = _Opener(script)
        f = bs.Fetcher(sharpen=False, retries=retries, opener=opener,
                       sleep=clock.sleep, clock=clock)
        return f, opener, clock

    try:
        # A 429 that says when to come back is waited out, then the tile
        # arrives; the request carried the User-Agent.
        f, op, clock = fetcher([(429, {"Retry-After": "7"}), b"ok"])
        got = f.get(13, 1, 2)
        check(got == b"ok" and 7 in clock.slept and f.stopped is None,
              "a 429 with Retry-After 7 was not waited out: got %r, slept %s"
              % (got, clock.slept))
        check(all(ua == bs.USER_AGENT for _u, ua in op.calls),
              "a request went without the User-Agent: %r" % op.calls)

        # (a) Refused every time: a bounded number of tries, then the whole
        # run stops - no other tile is asked for.
        for code in (301, 302, 403, 429, 500, 503):
            f, op, clock = fetcher([(code, {})] * 10, retries=3)
            first = f.get(13, 1, 2)
            asked = len(op.calls)
            second = f.get(13, 1, 3)
            check(first is None and asked == 3 and f.stopped,
                  "HTTP %d: %d requests, stopped=%r; expected 3 and a stop"
                  % (code, asked, f.stopped))
            check(second is None and len(op.calls) == asked,
                  "HTTP %d: the run went on asking after it was refused"
                  % code)

        # Retry-After is honoured but capped, so a hostile header cannot
        # hold a runner all day; and backoff grows between refusals.
        f, op, clock = fetcher([(503, {"Retry-After": "999999"}), b"ok"])
        f.get(13, 1, 2)
        check(clock.slept and max(clock.slept) <= bs.RETRY_AFTER_CAP,
              "Retry-After was not capped: slept %s" % clock.slept)
        f, op, clock = fetcher([(503, {})] * 3 + [b"ok"])
        f.get(13, 1, 2)
        waits = [s for s in clock.slept if s >= 1]
        check(len(waits) == 3 and waits == sorted(waits) and
              waits[0] < waits[-1],
              "backoff without Retry-After does not grow: %s" % clock.slept)

        # A network fault is not a refusal: retried after a short, paced
        # wait (through the injected clock, so it is the fetcher's own).
        f, op, clock = fetcher([urllib.error.URLError("reset"), b"ok"])
        check(f.get(13, 1, 2) == b"ok" and 1.5 in clock.slept
              and f.stopped is None,
              "a network fault was not retried after 1.5 s: slept %s"
              % clock.slept)

        # A 404 is a tile that is not there: recorded, not retried, and it
        # does not stop the run.
        f, op, clock = fetcher([(404, {})])
        check(f.get(13, 1, 2) is None and len(op.calls) == 1
              and f.stopped is None and len(f.failed) == 1,
              "a 404 was retried or stopped the run: %d calls, stopped=%r"
              % (len(op.calls), f.stopped))

        # (b) A polite rate, stated: request starts are spaced out.
        rate = bs.MAX_REQUESTS_PER_SECOND
        check(0 < rate <= 5, "MAX_REQUESTS_PER_SECOND is %r" % rate)
        f, op, clock = fetcher([])
        starts = []
        real_open = op.open

        def timed(req, timeout=None):
            starts.append(clock.now)
            return real_open(req, timeout)
        op.open = timed
        for y in range(6):
            f.get(13, 1, y)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        check(len(gaps) == 5 and min(gaps) >= 1.0 / rate - 1e-9,
              "requests were not paced to %s a second: gaps %s"
              % (rate, gaps))
    except Exception as e:  # noqa: BLE001
        check(False, "the fetcher could not be driven: %r" % e)

    # (a) A redirect is never followed, through the opener the build uses.
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/heavyload"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"busy")
                return
            self.send_response(302)
            self.send_header("Location", "/heavyload")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        clock = _Clock()
        f = bs.Fetcher(sharpen=False, retries=2, sleep=clock.sleep,
                       clock=clock, source="http://127.0.0.1:%d/{z}/{x}/{y}"
                       % server.server_address[1])
        got = f.get(13, 1, 2)
        check(got is None and f.stopped and
              not any(h.startswith("/heavyload") for h in hits),
              "a redirect was followed or not treated as a refusal: "
              "got %r, stopped=%r, requests %s" % (got, f.stopped, hits))
    except Exception as e:  # noqa: BLE001
        check(False, "the redirect check could not run: %r" % e)
    finally:
        server.shutdown()
        server.server_close()

    # (a, d) The build end to end over a tiny area: staged tiles are never
    # fetched again, and a refusal stops the area with no pack written, so
    # the published one stays.
    box = ["--bbox", "-1.0", "51.0", "-0.9", "51.1", "--min-zoom", "0",
           "--max-zoom", "2", "--id", "t", "--label", "T"]
    wanted = list(bs.tiles_in((-1.0, 51.0, -0.9, 51.1), 0, 2))
    real_fetcher, argv = bs.Fetcher, sys.argv
    try:
        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "staging")
            bs.stage(staging, *wanted[0], b"old")
            opened = []

            def make(script):
                def factory(sharpen):
                    op = _Opener(script)
                    opened.append(op)
                    clock = _Clock()
                    return real_fetcher(sharpen=sharpen, opener=op,
                                        sleep=clock.sleep, clock=clock)
                return factory

            bs.Fetcher = make([])
            sys.argv = ["build_satellite.py"] + box + ["--staging", staging]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bs.main()
            asked = [u for op in opened for u, _ua in op.calls]
            first = "/%d/%d/%d.jpg" % (wanted[0][0], wanted[0][2],
                                       wanted[0][1])
            check(rc == 0 and len(asked) == len(wanted) - 1 and
                  not any(u.endswith(first) for u in asked),
                  "a staged tile was fetched again, or a tile was missed: "
                  "%d asked of %d wanted, rc %r" % (len(asked), len(wanted),
                                                      rc))
            opened.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bs.main()
            again = sum(len(op.calls) for op in opened)
            check(rc == 0 and again == 0,
                  "a complete staging set was fetched again: %d requests"
                  % again)

            staging2 = os.path.join(tmp, "staging2")
            out = os.path.join(tmp, "t.pmtiles")
            bs.Fetcher = make([(429, {})] * 100)
            sys.argv = (["build_satellite.py"] + box +
                        ["--staging", staging2, "--package", "--out", out])
            log = io.StringIO()
            with contextlib.redirect_stdout(log), \
                    contextlib.redirect_stderr(log):
                rc = bs.main()
            check(rc == 3 and not os.path.exists(out) and
                  "STOPPED" in log.getvalue() and
                  len(opened[-1].calls) <= 4,
                  "a refused area was not stopped cleanly: rc %r, pack %s, "
                  "%d requests, log %r" % (rc, os.path.exists(out),
                                           len(opened[-1].calls),
                                           log.getvalue()[-300:]))
    except Exception as e:  # noqa: BLE001
        check(False, "the build could not be driven: %r" % e)
    finally:
        bs.Fetcher, sys.argv = real_fetcher, argv


def main():
    bs = load("build_satellite")
    layer = layer_of(bs.SOURCE)
    year, required = CC_BY_LAYERS.get(layer, (None, None))

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
    check(required is not None and required in a,
          "ATTRIBUTION does not carry EOX's required words for the layer "
          "(%r): %r" % (required, a))
    # No other year may appear in it: "2016 ... data 2024" is the mistake
    # this replaces, half corrected.
    years = set(int(y) for y in re.findall(r"\b(20\d\d)\b", a))
    check(years == {2016, 2017},
          "ATTRIBUTION names years %s, expected the 2016 mosaic's data "
          "years 2016 and 2017" % sorted(years))

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

    # 4a. Every plate is regenerated from LAYER: the six the tool makes, no
    #     more, each recorded with its bytes in satellite/samples/index.json.
    #     The plates published before 9 Oct 2026 were cut from 2024 (NC).
    names = list(getattr(si, "SAMPLE_NAMES", ()))
    check(sorted(names) == sorted([
        "standard", "standard-sharpened", "detailed",
        "zoom-standard", "zoom-sharpened", "zoom-detailed"]),
        "sample_imagery does not make all six plates: %r" % names)
    samples = os.path.join(ROOT, "satellite", "samples")
    try:
        with open(os.path.join(samples, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError) as e:
        index = {}
        check(False, "satellite/samples/index.json unreadable: %s" % e)
    check(index.get("layer") == layer,
          "the plates record layer %r, not %r" % (index.get("layer"), layer))
    check(index.get("attribution") == a,
          "the plates' index does not carry ATTRIBUTION")
    files = index.get("files", {})
    on_disk = sorted(n for n in os.listdir(samples) if n.endswith(".jpg"))
    check(sorted(files) == on_disk == sorted(n + ".jpg" for n in names),
          "plates on disk %s, indexed %s, made %s"
          % (on_disk, sorted(files), names))
    for n in on_disk:
        with open(os.path.join(samples, n), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        check(files.get(n) == digest,
              "%s is not the plate the index records" % n)

    # 4b. The plates themselves, from synthetic tiles. The z13 tile is blue
    #     with a red centre quarter, the z14 mosaic green with a yellow one,
    #     so each plate's corner says which tiles it came from and whether it
    #     is the whole ground or the centre quarter blown up.
    from PIL import Image

    def target(px, ground, centre):
        img = Image.new("RGB", (px, px), ground)
        img.paste(Image.new("RGB", (px // 2, px // 2), centre),
                  (px // 4, px // 4))
        return img

    blue, red = (0, 0, 255), (255, 0, 0)
    green, yellow = (0, 255, 0), (255, 255, 0)
    want = {"standard": blue, "standard-sharpened": blue, "detailed": green,
            "zoom-standard": red, "zoom-sharpened": red,
            "zoom-detailed": yellow}
    try:
        plates = si.plates(target(256, blue, red),
                           target(512, green, yellow), 128)
    except Exception as e:  # noqa: BLE001
        plates = {}
        check(False, "sample_imagery.plates failed: %r" % e)
    check(sorted(plates) == sorted(want),
          "plates() made %s, not %s" % (sorted(plates), sorted(want)))
    for n, img in plates.items():
        check(img.size == (128, 128), "%s is %s, not 128px" % (n, img.size))
        got = img.convert("RGB").getpixel((5, 5))
        check(all(abs(g - w) < 40 for g, w in zip(got, want.get(n, got))),
              "%s corner is %s, expected %s" % (n, got, want.get(n)))
    if "standard" in plates and "standard-sharpened" in plates:
        check(plates["standard"].tobytes()
              != plates["standard-sharpened"].tobytes(),
              "standard-sharpened is not sharpened")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            si.write_plates(tmp, plates)
            with open(os.path.join(tmp, "index.json"), encoding="utf-8") as f:
                made = json.load(f)
        except Exception as e:  # noqa: BLE001
            made = {}
            check(False, "sample_imagery.write_plates failed: %r" % e)
        check(made.get("layer") == layer and made.get("attribution") == a,
              "write_plates does not record LAYER and ATTRIBUTION: %r"
              % {k: made.get(k) for k in ("layer", "attribution")})
        for n in names:
            path = os.path.join(tmp, n + ".jpg")
            ok = os.path.exists(path)
            check(ok, "write_plates did not write %s.jpg" % n)
            if ok:
                with open(path, "rb") as f:
                    check(made.get("files", {}).get(n + ".jpg")
                          == hashlib.sha256(f.read()).hexdigest(),
                          "write_plates recorded the wrong hash for %s" % n)

    # 4b'. main() end to end, with the network replaced by flat tiles: it
    #      must write all six plates and the index, with where they are of.
    with tempfile.TemporaryDirectory() as tmp:
        fetch, argv = si.fetch, sys.argv
        si.fetch = lambda z, x, y: Image.new("RGB", (256, 256), blue)
        sys.argv = ["sample_imagery.py", "--lat", "52.12", "--lon", "1.40",
                    "--out", tmp, "--size", "64"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                si.main()
            with open(os.path.join(tmp, "index.json"), encoding="utf-8") as f:
                ran = json.load(f)
        except Exception as e:  # noqa: BLE001
            ran = {"error": repr(e)}
        finally:
            si.fetch, sys.argv = fetch, argv
        check(ran.get("layer") == layer and ran.get("lat") == 52.12
              and ran.get("z13_tile") == [4127, 2701]
              and sorted(ran.get("files", {})) == sorted(
                  n + ".jpg" for n in names),
              "sample_imagery main() did not write the plates and index: %r"
              % ran)

    # 4b''. The plates are fetched the way the packs are: the build's
    #       fetcher, its User-Agent, and a refusal ends the run.
    jpeg = io.BytesIO()
    Image.new("RGB", (256, 256), blue).save(jpeg, format="JPEG")
    real = si.FETCHER
    try:
        clock = _Clock()
        op = _Opener([jpeg.getvalue()])
        si.FETCHER = bs.Fetcher(sharpen=False, opener=op, sleep=clock.sleep,
                                clock=clock)
        img = si.fetch(13, 1, 2)
        check(img.size == (256, 256) and op.calls and
              op.calls[0][1] == bs.USER_AGENT,
              "sample_imagery.fetch does not use the build's fetcher: %r"
              % op.calls)
        si.FETCHER = bs.Fetcher(sharpen=False, retries=2,
                                opener=_Opener([(429, {})] * 5),
                                sleep=clock.sleep, clock=clock)
        try:
            si.fetch(13, 1, 2)
            check(False, "sample_imagery.fetch carried on after a refusal")
        except SystemExit as e:
            check("429" in str(e), "the refusal is not reported: %s" % e)
    except Exception as e:  # noqa: BLE001
        check(False, "sample_imagery.fetch could not be driven: %r" % e)
    finally:
        si.FETCHER = real

    # 4c. A pack published under any other attribution is due NOW, however
    #     young: otherwise the NC 2024 packs stay served for up to 25 days.
    sp = load("satellite_plan")
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def area(aid, note):
        return {"id": aid, "label": aid, "bounds": {
            "west": -1, "south": 51, "east": 0, "north": 52},
            "packs": [{"kind": "lanes", "id": aid + "-ways"}] + [
                {"kind": "basemap", "id": "%s-satellite-%s" % (aid, t),
                 "maxZoom": z, "generated": fresh, "note": note}
                for t, z in (("standard", 13), ("high", 14))]}

    old_note = ("Sentinel-2 cloudless 2024 by EOX IT Services GmbH, CC BY "
                "4.0. Contains modified Copernicus Sentinel data 2024.")

    def plan(*areas):
        """satellite_plan's own main(), as the workflow runs it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalogue.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"continents": [{"countries": [
                    {"label": "GB", "areas": list(areas)}]}]}, f)
            argv, out = sys.argv, io.StringIO()
            sys.argv = ["satellite_plan.py", "--catalogue", path]
            try:
                with contextlib.redirect_stdout(out):
                    sp.main()
            except Exception as e:  # noqa: BLE001
                return {"error": repr(e)}
            finally:
                sys.argv = argv
            return json.loads(out.getvalue())

    due = plan(area("gb-a", a), area("gb-b", old_note))
    check(due.get("work") is True and due.get("id") == "gb-b-satellite",
          "a day-old pack under the old attribution is not due: %r" % due)
    idle = plan(area("gb-a", a), area("gb-b", a))
    check(idle.get("work") is False,
          "packs under ATTRIBUTION and a day old were rebuilt: %r" % idle)

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

    # 8b. The dated evidence for the licence relied on: each section of
    #     docs/licences/eox-s2cloudless-2026-10-09.md, by what it must quote.
    try:
        ev = read("docs/licences/eox-s2cloudless-2026-10-09.md")
    except OSError as e:
        ev = ""
        check(False, "the licence evidence is missing: %s" % e)
    blocks = [b for b in re.split(r"\n\s*\n", ev) if b.strip()]
    wants = [
        ("the introduction", ["s2cloudless_3857", "robots.txt"]),
        ("the WMTS abstract", [
            "Source: https://tiles.maps.eox.at/wmts/1.0.0/"
            "WMTSCapabilities.xml", "Fetched: 9 October 2026",
            "> EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
            "(Contains modified Copernicus Sentinel data 2016) released "
            "under &lt;a rel=\"license\" href=\"https://creativecommons.org/"
            "licenses/by/4.0/\"&gt;Creative Commons Attribution 4.0 "
            "International License&lt;/a&gt;."]),
        ("the 2016 licence sentence", [
            "Source: https://cloudless.eox.at/license-non-commercial",
            "Fetched: 9 October 2026",
            "> For the year 2016, EOxCloudless is licensed under the "
            "Creative Commons Attribution 4.0 International License."]),
        ("the required attribution", ["> \"%s\"" % required]
         if required else ["(no layer)"]),
        ("the NonCommercial years", [
            "> For the years 2018 to 2025, EOxCloudless WM(T)S layers is "
            "licensed under the Creative Commons Attribution-NonCommercial-"
            "ShareAlike 4.0 International License."]),
        ("how it is used", ["> " + a]),
    ]
    check(len(blocks) == len(wants),
          "the licence evidence has %d sections, expected %d"
          % (len(blocks), len(wants)))
    for block, (what, needles) in zip(blocks, wants):
        flat = re.sub(r"\s*\n\s*", " ", block)
        for needle in needles:
            check(needle in flat, "the licence evidence's %s lacks %r"
                  % (what, needle))

    # 9. Nothing in the tools, workflows or docs still names a NonCommercial
    #    layer or year as the source, or says that the CC BY licence covers
    #    "the data" as if every year were licensed alike.
    stale = [
        r"(?i)CC(&nbsp;| )BY licence covers the data",
        r"s2cloudless-20(1[7-9]|2\d)_3857",
        r"(?i)cloudless 20(1[7-9]|2\d)\b",
        r"\b20(1[7-9]|2\d) Sentinel-2 cloudless",
        r"Copernicus Sentinel data 20(1[89]|2\d)",
        r"Copernicus Sentinel data 2017[;.)]",
        r"mosaic is 20(1[7-9]|2\d)",
        r"\b20(1[7-9]|2\d) mosaic's CC",
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

    # 10. EOX's tile service: no bulk terms, but it rate-limits with HTTP
    #     errors and redirects to a "heavyload" page. We never work around
    #     a block.
    service_checks(bs)

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("%d failure(s)" % len(failures))
        return 1
    print("imagery licence: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
