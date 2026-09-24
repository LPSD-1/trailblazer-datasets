#!/usr/bin/env python3
"""Checks on `gradient.py`, run by hand and by the ways workflow.

    python tools/test_gradient.py

WHAT THIS IS REALLY GUARDING
----------------------------
Four silent failures. None of them throws; every one of them ships a wrong
number to a rider deciding whether their bike will get up something.

1. **THE F4 FAULT COMING BACK.** F4 measured that the obvious gradient measure
   is noise: two of the five steepest Midlands lanes read 28% over TWO METRES
   of total climb, because z12 is ~23 m/pixel and cannot resolve a track on a
   shelf. Three things keep that out - a run rather than a point, a low pass at
   the DEM's own resolution, and bilinear rather than nearest-pixel sampling -
   and each of the three is undone by a change that looks like a tidy-up. So
   each has a test that fails if it is undone, with the number it must beat.

2. **THE DIRECTION OF THE HILL.** `climb_m` read as ascent in the stored
   direction is arbitrary, because a council digitises a line whichever way it
   likes. Measured on `motor-midlands`, six of the twenty steepest lanes read a
   total ascent under 20 m while descending 37 m to 133 m. That is the plan's
   own top-twenty gate failing on six real hills.

3. **THE TERRARIUM DECODE AND THE PNG UNDER IT.** Height is in the colour
   channels, so an error here does not fail - it renders a different country.
   The PNG filters are the sharp edge: filter 4 (Paeth) is the one a
   from-memory implementation gets subtly wrong, and a subtly wrong predictor
   gives plausible heights that are not the ground.

4. **A WAY TOO SHORT TO MEASURE, ANSWERED ANYWAY.** A 15 m way over a 23 m
   pixel has no gradient to report. Reporting one is how a noise spike gets to
   the top of a "steepest lanes" list.
"""
import importlib.util
import math
import os
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, "%s.py" % name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gr = load("gradient")
bmc = load("build_map_container")
bs = gr._repack.bs

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def close(got, want, tol, what):
    check(got is not None and abs(got - want) <= tol,
          "%s: got %s, wanted %s +/- %s" % (what, got, want, tol))


# --------------------------------------------------------------------------
# Fixtures, standard library only
# --------------------------------------------------------------------------
def encode_png(width, height, pixels, filter_type=0):
    """An 8-bit RGB PNG using one filter type for every row.

    Written here so the decoder is checked against a second implementation
    rather than against itself.
    """
    stride = width * 3
    raw = bytearray()
    prev = bytes(stride)
    for row in range(height):
        line = pixels[row * stride:(row + 1) * stride]
        out = bytearray()
        for k in range(stride):
            a = line[k - 3] if k >= 3 else 0
            b = prev[k]
            c = prev[k - 3] if k >= 3 else 0
            if filter_type == 0:
                v = line[k]
            elif filter_type == 1:
                v = line[k] - a
            elif filter_type == 2:
                v = line[k] - b
            elif filter_type == 3:
                v = line[k] - ((a + b) >> 1)
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                v = line[k] - pr
            out.append(v & 0xFF)
        raw.append(filter_type)
        raw += out
        prev = line

    def chunk(kind, body):
        return (len(body).to_bytes(4, "big") + kind + body
                + zlib.crc32(kind + body).to_bytes(4, "big"))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                         0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def terrarium_tile(size, height_at):
    """A `size` x `size` Terrarium tile; `height_at(x, y)` gives metres."""
    px = bytearray()
    for y in range(size):
        for x in range(size):
            v = int(round((height_at(x, y) + 32768.0) * 256.0))
            px += bytes(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
    return bytes(px)


def an_archive(path, zoom, tiles_by_xy, bbox, metadata=None):
    """A real PMTiles archive written by the builder's own writer."""
    tiles = {}
    for (x, y), pixels in tiles_by_xy.items():
        blob = encode_png(gr.TILE_SIZE, gr.TILE_SIZE, pixels, filter_type=4)
        tiles[bs.tile_id(zoom, x, y)] = blob
    bs.write_pmtiles(path, tiles, bbox, zoom, zoom,
                     metadata or {"encoding": "terrarium",
                                  "vertical_step_m": 0.5},
                     tile_type=2)
    return path


def flat_profile(n, step=25.0, base=100.0):
    return [base] * n, [step * i for i in range(n)]


# --------------------------------------------------------------------------
def main():
    # === 1. Terrarium decode, the published formula ==========================
    close(gr.decode_terrarium(128, 0, 0), 0.0, 1e-9, "Terrarium zero")
    close(gr.decode_terrarium(128, 10, 128), 10.5, 1e-9, "Terrarium 10.5 m")
    close(gr.decode_terrarium(130, 100, 0), 612.0, 1e-9, "Terrarium 612 m")
    close(gr.decode_terrarium(127, 200, 0), -56.0, 1e-9, "Terrarium below sea")

    # === 2. The PNG under it, every filter type ==============================
    # Filter 4 is the one that is wrong from memory, and a wrong predictor
    # gives heights that are plausible and are not the ground.
    w = h = 9
    pixels = bytes(((x * 29 + y * 7) % 251, (x * 13) % 251, (y * 31) % 251
                    )[k] for y in range(h) for x in range(w) for k in range(3))
    for ftype in range(5):
        got = gr._decode_png(encode_png(w, h, pixels, ftype))
        check(got[:3] == (w, h, 3) and got[3] == pixels,
              "PNG filter type %d did not round-trip" % ftype)
    try:
        gr._decode_png(b"not a png at all")
        check(False, "_decode_png accepted something that is not a PNG")
    except ValueError:
        pass

    # === 3. The archive reader agrees with the archive writer ================
    # A ramp of 1 m per pixel eastwards, on the tile holding the Peak District.
    zoom = 12
    lon, lat = -1.8, 53.35
    n = 1 << zoom
    tx = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    ty = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi)
             / 2.0 * n)
    ramp = terrarium_tile(gr.TILE_SIZE, lambda x, y: 100.0 + x)
    with tempfile.TemporaryDirectory() as tmp:
        path = an_archive(os.path.join(tmp, "ramp.pmtiles"), zoom,
                          {(tx, ty): ramp}, (-3.25, 51.9, 0.15, 53.6))
        with gr.HeightSource(path) as src:
            check(src.max_zoom == zoom, "max_zoom not read from the header")
            check(src.vertical_step_m == 0.5,
                  "vertical_step_m not read from the archive metadata")

            # -- on a pixel centre, the pixel's own height -------------------
            px = (tx * gr.TILE_SIZE) + 40 + 0.5
            span = float(gr.TILE_SIZE << zoom)
            centre_lon = px / span * 360.0 - 180.0
            close(src.height(centre_lon, lat), 140.0, 0.02,
                  "height at a pixel centre")

            # -- BILINEAR, NOT NEAREST. This is F4's cause. -----------------
            # Halfway between two pixel centres on a 1 m/pixel ramp is x.5.
            # A nearest-pixel reader returns a whole number, and steps by a
            # whole 23 m pixel at a time - which is what manufactures the
            # jump F4 caught.
            half_lon = (px + 0.5) / span * 360.0 - 180.0
            got = src.height(half_lon, lat)
            close(got, 140.5, 0.02, "height halfway between pixel centres")
            check(got is not None and abs(got - round(got)) > 0.25,
                  "sampling is nearest-pixel, not bilinear: %s" % got)

            # -- outside the pack is None, not zero -------------------------
            check(src.height(20.0, 53.35) is None,
                  "a point outside the pack got a height instead of None")

            # -- measure() reads a real archive through the real path -------
            step_lon = 360.0 / span      # one pixel of longitude
            line = [(centre_lon + step_lon * k, lat) for k in range(41)]
            got = gr.measure([line], src, step_m=10.0)
            check(got["climb_m"] is not None and got["sustained_pct"] is not None,
                  "measure() returned nothing over a 40-pixel ramp")
            close(got["climb_m"], 40.0, 2.0, "climb over a 40 m ramp")

            # ...and measure() must CARRY the direction fix, not just own a
            # function that has it. The same ramp, digitised downhill: a
            # survivor of exactly this mutation is how DY-61/1 shipped a
            # 57 m hill as 0.0 m of climb.
            down = gr.measure([list(reversed(line))], src, step_m=10.0)
            close(down["climb_m"], got["climb_m"], 1e-9,
                  "measure() gave a different climb_m for the same ramp "
                  "digitised the other way")
            check(down["ascent_m"] != down["descent_m"],
                  "the fixture is not one-way, so this check proves nothing")

            # measure() must take its hysteresis from the pack's own vertical
            # step. Zero here is a fen out-climbing a fell.
            close(got["climb_threshold_m"], 2.0, 1e-9,
                  "measure() did not derive a climb threshold from the "
                  "archive's 0.5 m vertical step")

    # === 4. THE F4 REGRESSION: one bad sample must not become a gradient =====
    # F4's shape exactly: flat ground, one spurious sample 28 m out, 25 m
    # spacing. The numbers below were measured, and each is a different
    # defence being present.
    heights, distances = flat_profile(17)
    heights[8] += 28.0
    raw_max = max(abs(heights[k + 1] - heights[k]) / 25.0 * 100.0
                  for k in range(len(heights) - 1))
    close(raw_max, 112.0, 0.1, "the raw adjacent maximum F4 rejected")

    unsmoothed, _ = gr.steepest_run(heights, distances)
    close(unsmoothed, 28.0, 0.1,
          "a 100 m run alone should already cut 112% to 28%")

    one_pass, _ = gr.steepest_run(gr.smooth(heights, 1), distances)
    close(one_pass, 14.0, 0.1, "one smoothing pass")

    shipped, run = gr.steepest_run(
        gr.smooth(heights, gr.SMOOTH_PASSES), distances)
    check(shipped is not None and shipped <= 11.0,
          "the shipped settings let one spurious sample read %s%% - F4's "
          "fault is back" % shipped)
    close(run, 100.0, 1e-9, "the sustained run is not 100 m")

    # ...and the price of that filtering is nothing on a real slope, because a
    # straight line is invariant under a symmetric kernel.
    ramp_h = [100.0 + 0.12 * d for d in distances]
    for passes in (0, 1, gr.SMOOTH_PASSES, 3):
        pct, _ = gr.steepest_run(gr.smooth(ramp_h, passes), distances)
        close(pct, 12.0, 0.001,
              "smoothing moved a genuine 12%% ramp at %d passes" % passes)

    # === 5. The run is a RUN =================================================
    # A 200 m climb of 20% followed by 200 m of flat is 20% sustained, not
    # 10% averaged over the lot and not 20% claimed over 400 m.
    d = [25.0 * i for i in range(17)]
    hs = [100.0 + 0.20 * min(x, 200.0) for x in d]
    pct, run = gr.steepest_run(hs, d)
    close(pct, 20.0, 0.01, "sustained over a 200 m climb then flat")
    check(run is not None and gr.MIN_RUN_M <= run <= gr.MAX_RUN_M,
          "the run used was %s m, outside [%s, %s]"
          % (run, gr.MIN_RUN_M, gr.MAX_RUN_M))

    # Both directions count: ridden the other way it is the same hill.
    down = [100.0 - 0.20 * min(x, 200.0) for x in d]
    pct_down, _ = gr.steepest_run(down, d)
    close(pct_down, 20.0, 0.01, "a 20% descent read as something other than 20%")

    # === 6. A way too short to measure is not answered =======================
    pct, run = gr.steepest_run([100.0, 104.0], [0.0, 20.0])
    check(pct is None and run is None,
          "a 20 m way - under one DEM pixel - was given a gradient of %s" % pct)
    # ...but a short way above the floor is answered, and SAYS what on.
    pct, run = gr.steepest_run([100.0, 112.0], [0.0, 60.0])
    close(pct, 20.0, 0.01, "a 60 m way's gradient")
    close(run, 60.0, 1e-9,
          "sustained_run_m must report the 60 m it was actually measured over")

    # === 7. climb_m: hysteresis, and which way is up =========================
    # Quantisation wander is not ascent. Without the threshold this returns
    # ~50 m and a fen out-climbs a fell.
    saw = [100.0 + (0.5 if k % 2 else 0.0) for k in range(200)]
    close(gr.total_ascent(saw, gr.CLIMB_THRESHOLD_M), 0.5, 0.01,
          "a +/-0.5 m sawtooth booked as ascent")
    # A real climb is not eaten by it.
    real = [100.0 + x * 0.1 for x in range(1000)]
    close(gr.total_ascent(real, gr.CLIMB_THRESHOLD_M), 99.9, 0.2,
          "a genuine 100 m climb after hysteresis")

    # THE DY-61/1 REGRESSION. 57 m of hill, digitised downhill, read as zero.
    descent = [400.0 - 2.0 * k for k in range(30)]
    close(gr.total_ascent(descent, gr.CLIMB_THRESHOLD_M), 0.0, 1e-9,
          "ascent in the stored direction of a pure descent")
    close(gr.harder_direction_climb(descent, gr.CLIMB_THRESHOLD_M), 58.0, 0.01,
          "climb_m of a lane digitised downhill - this is DY-61/1, which read "
          "0.0 m of climb against 57.4 m of descent")
    check(gr.harder_direction_climb(descent, gr.CLIMB_THRESHOLD_M)
          == gr.harder_direction_climb(list(reversed(descent)),
                                       gr.CLIMB_THRESHOLD_M),
          "climb_m depends on which way the authority drew the line")

    # === 8. Geometry: the inverse really is the inverse ======================
    lines = [[(-1.8, 53.35), (-1.79995, 53.35012), (-1.7998, 53.3502)],
             [(0.0001, 52.0), (-0.0001, 52.00005)]]
    back = gr.unpack_geometry(bmc.pack_geometry(lines))
    check(len(back) == len(lines), "line count did not survive the round trip")
    worst = max(abs(a - b)
                for one, two in zip(lines, back)
                for (p, q) in zip(one, two)
                for a, b in ((p[0], q[0]), (p[1], q[1])))
    check(worst <= 1.0 / bmc.GEOMETRY_SCALE + 1e-12,
          "geometry round trip moved a point by %g degrees" % worst)

    # === 9. Resampling =======================================================
    # 0.01 degrees of latitude is about 1,111 m.
    pts, ds = gr.resample([(-1.8, 53.30), (-1.8, 53.31)], 100.0)
    close(ds[-1], 1112.0, 3.0, "resampled length of a 0.01 degree line")
    check(all(abs((ds[k + 1] - ds[k]) - 100.0) < 1.0
              for k in range(len(ds) - 2)),
          "resample did not keep to its step")
    check(ds[-1] > ds[-2], "resample dropped the end of the line")
    check(gr.resample([], 25.0) == ([], []), "resample of nothing")
    check(gr.resample([(0.0, 0.0)], 25.0) == ([(0.0, 0.0)], [0.0]),
          "resample of a single point")

    # A line whose points repeat must not divide by zero.
    pts, ds = gr.resample([(-1.8, 53.3), (-1.8, 53.3), (-1.8, 53.301)], 50.0)
    check(len(pts) == len(ds) and ds[-1] > 100.0,
          "a repeated point broke resampling")

    # === 10. measure() refuses rather than guesses ===========================
    class NoCoverage:
        vertical_step_m = 0.5

        def height(self, lon, lat, zoom=None):
            return None

    got = gr.measure([[(-1.8, 53.3), (-1.8, 53.31)]], NoCoverage())
    check(got["climb_m"] is None and got["sustained_pct"] is None,
          "a way with no DEM coverage was given numbers: %s" % got)

    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        print("%d of the checks failed" % len(failures), file=sys.stderr)
        return 1
    print("gradient: Terrarium and PNG decode exact across all five filters, "
          "sampling bilinear, one spurious sample cuts 112%% -> %.1f%% while a "
          "genuine 12%% ramp is untouched, climb_m independent of digitising "
          "direction, short ways refused"
          % gr.steepest_run(gr.smooth(heights, gr.SMOOTH_PASSES),
                            distances)[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
