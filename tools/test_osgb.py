#!/usr/bin/env python3
"""Does the National Grid conversion actually land in the right place?

    python -m unittest tools.test_osgb        (from the repo root)
    python tools/test_osgb.py

Every D-TRO geometry is British National Grid. If this is wrong, every order in
the country is drawn somewhere it is not, and it is wrong CONSISTENTLY — which
is the kind of wrong that looks plausible on a map and is only caught by
checking against a published answer. So the first test does exactly that.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from osgb import grid_to_osgb36, grid_to_wgs84, osgb36_to_wgs84  # noqa: E402


def _dms(deg, minutes, seconds):
    return deg + minutes / 60.0 + seconds / 3600.0


class TransverseMercator(unittest.TestCase):
    """Against Ordnance Survey's own worked example.

    From *A guide to coordinate systems in Great Britain*, which gives a test
    point in both forms so that an implementation can be checked without
    trusting any other implementation.
    """

    # 52 deg 39' 27.2531" N, 1 deg 43' 4.5177" E on OSGB36 ...
    OS_LAT = _dms(52, 39, 27.2531)
    OS_LON = _dms(1, 43, 4.5177)
    # ... is this National Grid easting and northing.
    OS_E = 651409.903
    OS_N = 313177.270

    def test_matches_the_published_answer(self):
        lat, lon = grid_to_osgb36(self.OS_E, self.OS_N)
        lat, lon = math.degrees(lat), math.degrees(lon)
        # A ten-thousandth of a second of arc is about three millimetres. The
        # projection is a closed series and should be far better than the
        # datum shift that follows it, so it is held to a tight bound here:
        # any real error in the series will be metres, not millimetres.
        self.assertAlmostEqual(lat, self.OS_LAT, places=7)
        self.assertAlmostEqual(lon, self.OS_LON, places=7)

    def test_the_true_origin_is_where_it_says(self):
        # The false origin is 400000E, -100000N against a true origin of
        # 49N 2W. Feed in the true origin's grid coordinates and the latitude
        # and longitude must come back out.
        lat, lon = grid_to_osgb36(400000.0, -100000.0)
        self.assertAlmostEqual(math.degrees(lat), 49.0, places=8)
        self.assertAlmostEqual(math.degrees(lon), -2.0, places=8)


class DatumShift(unittest.TestCase):
    """Against Ordnance Survey's published ETRS89 value for the same point.

    ETRS89 and WGS84 differ by centimetres in Britain, so OS's ETRS89 figure is
    the right thing to check a WGS84 conversion against — and it is published
    for the SAME physical point as the National Grid worked example above,
    which makes the pair a complete end-to-end check of both halves.
    """

    ETRS_LAT = _dms(52, 39, 28.8282)
    ETRS_LON = _dms(1, 42, 57.8663)

    def test_lands_within_the_accuracy_this_transform_claims(self):
        lon, lat = grid_to_wgs84(TransverseMercator.OS_E, TransverseMercator.OS_N)
        dy = (lat - self.ETRS_LAT) * 111320.0
        dx = (lon - self.ETRS_LON) * 111320.0 * math.cos(math.radians(lat))
        off = math.hypot(dx, dy)
        # osgb.py says Helmert is good to about five metres. This is the
        # measurement behind that sentence: if it ever exceeds it, either the
        # claim in the docstring is wrong or the transform is.
        self.assertLess(off, 5.0,
                        "%.2f m from the published ETRS89 position" % off)

    def test_moves_the_point_the_right_way(self):
        # A sign error in the Helmert rotations moves the point by a similar
        # SIZE in a different DIRECTION, so distance alone would not catch it.
        # In eastern England the shift from OSGB36 to WGS84 is north and west.
        lat0, lon0 = grid_to_osgb36(TransverseMercator.OS_E,
                                    TransverseMercator.OS_N)
        lat1, lon1 = osgb36_to_wgs84(lat0, lon0)
        self.assertGreater(lat1, lat0, "WGS84 should sit NORTH of OSGB36 here")
        self.assertLess(lon1, lon0, "and WEST of it")

    def test_the_shift_is_a_sensible_size(self):
        lat0, lon0 = math.radians(53.0), math.radians(-1.7)
        lat1, lon1 = osgb36_to_wgs84(lat0, lon0)
        dy = math.degrees(lat1 - lat0) * 111320.0
        dx = math.degrees(lon1 - lon0) * 111320.0 * math.cos(lat0)
        shift = math.hypot(dx, dy)
        # Tens of metres, not metres and not kilometres. Catches a transform
        # that has quietly become the identity, which is the failure mode that
        # looks most like success.
        self.assertTrue(20 < shift < 200,
                        "datum shift of %.1f m is not credible" % shift)


class GeoJsonOrder(unittest.TestCase):
    def test_longitude_comes_first(self):
        # Getting it the other way round puts Great Britain in the Indian
        # Ocean - obvious, but only if something checks.
        lon, lat = grid_to_wgs84(TransverseMercator.OS_E,
                                 TransverseMercator.OS_N)
        self.assertTrue(-9 < lon < 2, "first value should be a longitude")
        self.assertTrue(49 < lat < 61, "second value should be a latitude")


class SelfConsistentWithTheApp(unittest.TestCase):
    """The app converts the other way; the two must agree.

    `lib/core/osgrid.dart` turns WGS84 into OSGB36 to show a grid reference.
    This file turns D-TRO's National Grid geometry into WGS84. If the two used
    even slightly different Helmert parameters, a TRO and the grid reference a
    rider reads off the same screen would disagree by a few metres, and
    nothing would ever say why. So this is the app's transform, ported, and
    the round trip has to come back to where it started.
    """

    @staticmethod
    def _wgs84_to_osgb36(lat, lon):
        a, b = 6378137.0, 6356752.314245
        lat_r, lon_r = math.radians(lat), math.radians(lon)
        e2 = (a * a - b * b) / (a * a)
        nu = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
        x = nu * math.cos(lat_r) * math.cos(lon_r)
        y = nu * math.cos(lat_r) * math.sin(lon_r)
        z = (1 - e2) * nu * math.sin(lat_r)
        tx, ty, tz, s = -446.448, 125.157, -542.060, 20.4894e-6
        rx = math.radians(-0.1502 / 3600.0)
        ry = math.radians(-0.2470 / 3600.0)
        rz = math.radians(-0.8421 / 3600.0)
        x2 = tx + x * (1 + s) - y * rz + z * ry
        y2 = ty + x * rz + y * (1 + s) - z * rx
        z2 = tz - x * ry + y * rx + z * (1 + s)
        a, b = 6377563.396, 6356256.909
        e2 = (a * a - b * b) / (a * a)
        p = math.sqrt(x2 * x2 + y2 * y2)
        lat2 = math.atan2(z2, p * (1 - e2))
        for _ in range(12):
            nu = a / math.sqrt(1 - e2 * math.sin(lat2) ** 2)
            lat2 = math.atan2(z2 + e2 * nu * math.sin(lat2), p)
        return lat2, math.atan2(y2, x2)

    def test_round_trip_comes_back_to_the_same_place(self):
        for lat, lon in [(51.5, -0.12), (53.0, -1.73), (56.8, -5.0),
                         (50.0, -5.2), (54.5, 0.5)]:
            o_lat, o_lon = self._wgs84_to_osgb36(lat, lon)
            b_lat, b_lon = osgb36_to_wgs84(o_lat, o_lon)
            dy = (math.degrees(b_lat) - lat) * 111320.0
            dx = ((math.degrees(b_lon) - lon) * 111320.0
                  * math.cos(math.radians(lat)))
            self.assertLess(math.hypot(dx, dy), 0.01,
                            "round trip at %.2f,%.2f lost %.3f m"
                            % (lat, lon, math.hypot(dx, dy)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
