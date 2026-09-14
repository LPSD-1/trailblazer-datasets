#!/usr/bin/env python3
"""Ordnance Survey National Grid (EPSG:27700) to WGS84 latitude and longitude.

D-TRO publishes every geometry as `SRID=27700;LINESTRING(easting northing ...)`
— British National Grid, in metres. The app works in WGS84 degrees, so every
TRO has to be converted before it can be drawn or matched against a lane.

Pure standard library on purpose. `pyproj` would do this in one line and it is
the right tool for a desktop GIS; it is a compiled dependency with a bundled
PROJ database, and this repository's build tooling is otherwise stdlib plus
`cryptography`. Adding a wheel that has to resolve on every CI runner, to do
arithmetic that fits on one screen and never changes, is a bad trade.

ACCURACY, stated plainly because it decides what this data may be used for.
The datum shift here is a seven-parameter Helmert transformation, which is what
the app's own `lib/core/osgrid.dart` uses in the opposite direction. Helmert is
a best fit across the whole of Great Britain and is good to roughly **5 metres**
— locally better, and worse at the edges. It is NOT the centimetre-accurate
OSTN15 transformation, which needs a multi-megabyte shift grid.

Five metres is fine for what this is for: saying which road an order applies to,
and drawing it. It is not fine for anything that turns on exactly where a
boundary falls, and nothing downstream should treat it as though it were.
"""
import math

# Airy 1830 — the ellipsoid the National Grid is defined on.
_AIRY_A = 6377563.396
_AIRY_B = 6356256.909

# GRS80 / WGS84.
_WGS_A = 6378137.0
_WGS_B = 6356752.314245

# National Grid projection constants.
_F0 = 0.9996012717          # scale factor on the central meridian
_LAT0 = math.radians(49.0)  # true origin
_LON0 = math.radians(-2.0)
_E0 = 400000.0              # false origin, metres
_N0 = -100000.0

# OSGB36 -> WGS84 Helmert. The same numbers as lib/core/osgrid.dart, with the
# signs reversed because that file goes the other way. Keeping one set of
# parameters matters more than which set: two different fits would put a TRO
# and the lane it applies to a few metres apart for no reason anybody could
# find later.
_TX, _TY, _TZ = 446.448, -125.157, 542.060
_S = -20.4894e-6
_RX = math.radians(0.1502 / 3600.0)
_RY = math.radians(0.2470 / 3600.0)
_RZ = math.radians(0.8421 / 3600.0)


def grid_to_osgb36(easting, northing):
    """National Grid metres to OSGB36 latitude and longitude, in radians.

    The inverse Transverse Mercator series from OS's *A guide to coordinate
    systems in Great Britain*. Exact to well under a millimetre; the datum
    shift afterwards is where the real error lives.
    """
    a, b = _AIRY_A, _AIRY_B
    e2 = (a * a - b * b) / (a * a)
    n = (a - b) / (a + b)
    n2, n3 = n * n, n * n * n

    lat = _LAT0
    m = 0.0
    # Iterate northing -> footpoint latitude. Converges in three or four
    # passes; ten is free and leaves no doubt.
    for _ in range(10):
        lat = (northing - _N0 - m) / (a * _F0) + lat
        dlat = lat - _LAT0
        slat = lat + _LAT0
        m = b * _F0 * (
            (1 + n + 1.25 * n2 + 1.25 * n3) * dlat
            - (3 * n + 3 * n2 + 2.625 * n3) * math.sin(dlat) * math.cos(slat)
            + (1.875 * n2 + 1.875 * n3) * math.sin(2 * dlat)
            * math.cos(2 * slat)
            - (35.0 / 24.0) * n3 * math.sin(3 * dlat) * math.cos(3 * slat)
        )
        if abs(northing - _N0 - m) < 1e-5:
            break

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    tan_lat = math.tan(lat)

    nu = a * _F0 / math.sqrt(1 - e2 * sin_lat * sin_lat)
    rho = a * _F0 * (1 - e2) / pow(1 - e2 * sin_lat * sin_lat, 1.5)
    eta2 = nu / rho - 1

    t2 = tan_lat * tan_lat
    t4 = t2 * t2
    t6 = t4 * t2

    vii = tan_lat / (2 * rho * nu)
    viii = tan_lat / (24 * rho * nu ** 3) * (5 + 3 * t2 + eta2 - 9 * t2 * eta2)
    ix = tan_lat / (720 * rho * nu ** 5) * (61 + 90 * t2 + 45 * t4)
    x = 1 / (cos_lat * nu)
    xi = 1 / (cos_lat * 6 * nu ** 3) * (nu / rho + 2 * t2)
    xii = 1 / (cos_lat * 120 * nu ** 5) * (5 + 28 * t2 + 24 * t4)
    xiia = 1 / (cos_lat * 5040 * nu ** 7) * (
        61 + 662 * t2 + 1320 * t4 + 720 * t6)

    de = easting - _E0
    de2 = de * de
    lat_out = lat - vii * de2 + viii * de2 * de2 - ix * de2 * de2 * de2
    lon_out = (_LON0 + x * de - xi * de2 * de + xii * de2 * de2 * de
               - xiia * de2 * de2 * de2 * de)
    return lat_out, lon_out


def osgb36_to_wgs84(lat, lon):
    """OSGB36 radians to WGS84 radians, by seven-parameter Helmert."""
    a, b = _AIRY_A, _AIRY_B
    e2 = (a * a - b * b) / (a * a)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    nu = a / math.sqrt(1 - e2 * sin_lat * sin_lat)

    x = nu * cos_lat * math.cos(lon)
    y = nu * cos_lat * math.sin(lon)
    z = (1 - e2) * nu * sin_lat

    x2 = _TX + x * (1 + _S) - y * _RZ + z * _RY
    y2 = _TY + x * _RZ + y * (1 + _S) - z * _RX
    z2 = _TZ - x * _RY + y * _RX + z * (1 + _S)

    a, b = _WGS_A, _WGS_B
    e2 = (a * a - b * b) / (a * a)
    p = math.sqrt(x2 * x2 + y2 * y2)
    lat2 = math.atan2(z2, p * (1 - e2))
    for _ in range(12):
        nu = a / math.sqrt(1 - e2 * math.sin(lat2) ** 2)
        lat2 = math.atan2(z2 + e2 * nu * math.sin(lat2), p)
    return lat2, math.atan2(y2, x2)


def grid_to_wgs84(easting, northing):
    """National Grid metres to (longitude, latitude) in degrees.

    Longitude first, because that is GeoJSON's order and every consumer of
    this is writing GeoJSON. Getting it the other way round puts Britain in
    the Indian Ocean, which at least fails loudly.
    """
    lat, lon = grid_to_osgb36(easting, northing)
    lat, lon = osgb36_to_wgs84(lat, lon)
    return math.degrees(lon), math.degrees(lat)
