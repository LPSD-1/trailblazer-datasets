#!/usr/bin/env python3
"""Which of our byways a council's record is about.

Every source added beside D-TRO - a council's closures layer, its list of
permanent and seasonal orders, a Planning Inspectorate decision, a Street
Manager works record - describes a path in its own terms: a line in British
National Grid, a parish and path number, a route code. This module turns any
of those into the `way_uid`s of the byways riders actually have, read from
the published ways containers (`containers/ways-*.tbmap`), so the answer is
always about a lane that is on somebody's phone.

TWO WAYS TO MATCH, in the order the merge rule in the sources report gives
(data-sources-report section 2.5):

  * by GEOMETRY: at least half of the shorter of the two lines lies within
    25 m of the other. 25 m because that is the app's own lane-closure
    tolerance (lane_closures.dart), so a record matched here falls on the same
    lane the phone will draw it against; half of the SHORTER line so a closure
    of 200 m of a 3 km byway matches the byway, and a 3 km order does not
    match a 40 m spur it merely touches.
  * by REFERENCE: authority + parish + path number, read out of the way's
    own name ("Byway open to all traffic (BOAT) Debden 75"). Used for sources
    that carry no geometry (Devon's and Somerset's notice pages, the order
    register). Path numbers are compared with leading zeros removed, so a
    council's "BOAT 024" is our "24".

Nothing here fetches anything. Pure standard library apart from the
containers it reads, which are SQLite.
"""
import glob
import math
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TOLERANCE_M = 25.0
MIN_SHARE = 0.5
STEP_M = 10.0
_CELL = 0.01  # degrees; about 1.1 km north-south

GEOMETRY_SCALE = 10 ** 7


def _read_varint(blob, i):
    value = shift = 0
    while True:
        b = blob[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _unzigzag(n):
    return (n >> 1) if not n & 1 else -((n + 1) >> 1)


def unpack_geometry(blob, scale=None):
    """`build_map_container.pack_geometry`'s inverse: lines of (lon, lat)."""
    scale = scale or GEOMETRY_SCALE
    i = 0
    count, i = _read_varint(blob, i)
    lines = []
    for _ in range(count):
        points, i = _read_varint(blob, i)
        lon = lat = 0
        line = []
        for _ in range(points):
            dlon, i = _read_varint(blob, i)
            dlat, i = _read_varint(blob, i)
            lon += _unzigzag(dlon)
            lat += _unzigzag(dlat)
            line.append((lon / scale, lat / scale))
        lines.append(line)
    return lines


# ---------------------------------------------------------------- references

_DESIGNATION = re.compile(r"^\s*byway open to all traffic\s*\(boat\)\s*", re.I)

#: An unsurfaced unclassified road's name (build_packages.normalise_ucr):
#: "Rocky Lane (Abbotsham UCR 301)", or, where the council names no road,
#: "Unsurfaced unclassified road (UCR) Abbotsham 301".
_UCR_NAMED = re.compile(r"\((.+?)\s+UCR\s+(\S+)\)\s*$", re.I)
_UCR_DESIGNATION = re.compile(
    r"^\s*unsurfaced unclassified road\s*\(ucr\)\s*", re.I)


def norm_parish(text):
    """A parish (or parish code) as compared: lower case, letters and digits.

    "Abbess Beauchamp & Berners Roding" and "ABBESS BEAUCHAMP AND BERNERS
    RODING" are the same parish; so are "Leigh CP" and "Leigh".
    """
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"\b(cp|parish|civil parish|town council|pc)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def norm_number(text):
    """A path number as compared: "024/0" -> "24", "13A" -> "13a".

    A trailing "/0" (Suffolk's "no suffix") and "BOAT"/"byway"/"No." words
    are dropped - and Devon's "uUCR"/"UCR" status words, so its "uUCR 306"
    is our UCR 306; every numeric run loses its leading zeros.
    """
    text = (text or "").lower()
    text = re.sub(r"\b(boat|byway|by|bw|u?ucr|u?uct|no\.?|number)\b", " ",
                  text)
    text = re.sub(r"^u?uc[rt](?=\d)", "", text.strip())
    text = text.strip(" ./-")
    parts = [p for p in re.split(r"[^a-z0-9]+", text) if p]
    parts = [(p.lstrip("0") or "0") if p.isdigit() else
             re.sub(r"^0+(?=\d)", "", p) for p in parts]
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return "/".join(parts)


def split_name(name):
    """(parish part, number part) of a container way name, normalised.

    A UCR's name carries its reference in brackets after the road's own
    name, or after the designation where the council names no road; either
    reads as the council's parish and number, as a byway's name does.
    """
    m = _UCR_NAMED.search(name or "")
    if m:
        return norm_parish(m.group(1)), norm_number(m.group(2))
    name = _UCR_DESIGNATION.sub("", name or "")
    rest = _DESIGNATION.sub("", name or "").strip()
    if not rest:
        return "", ""
    tokens = rest.split()
    if len(tokens) == 1:
        return "", norm_number(tokens[0])
    return norm_parish(" ".join(tokens[:-1])), norm_number(tokens[-1])


# ------------------------------------------------------------------ geometry


def _project(points, lat0, lon0):
    """Degrees to local metres around (lat0, lon0). Good to well under a
    metre over the few kilometres any one comparison spans."""
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110574.0
    return [((lon - lon0) * kx, (lat - lat0) * ky) for lon, lat in points]


def _densify(line, step=STEP_M):
    if len(line) < 2:
        return list(line)
    out = [line[0]]
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        d = math.hypot(x1 - x0, y1 - y0)
        n = int(d // step)
        for k in range(1, n + 1):
            t = k * step / d
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        if out[-1] != (x1, y1):
            out.append((x1, y1))
    return out


def _seg_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy)
                     / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


END_TOL_M = 3.0


def _near(point, lines, tol, end_tol=END_TOL_M):
    """Whether `point` lies within `tol` of `lines` ALONG them.

    BEYOND A LINE'S END DOES NOT COUNT. A point is near when its nearest
    place on the line is somewhere along it; when the nearest place is one of
    the line's two terminal ends, only `end_tol` counts. Without this, a 40 m
    spur meeting a closed byway end-on, or at a T, lies "within 25 m" of the
    closure for more than half its length - and was closed with it.
    """
    for line in lines:
        if len(line) == 1:
            if math.hypot(point[0] - line[0][0], point[1] - line[0][1]) <= tol:
                return True
            continue
        last = len(line) - 2
        for k, (a, b) in enumerate(zip(line, line[1:])):
            (px, py), (ax, ay), (bx, by) = point, a, b
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = ((px - ax) * dx + (py - ay) * dy) / seg2 if seg2 else 0.0
            if (t <= 0 and k == 0) or (t >= 1 and k == last):
                end = a if (t <= 0 and k == 0) else b
                if math.hypot(px - end[0], py - end[1]) <= end_tol:
                    return True
                continue
            if _seg_dist(point, a, b) <= tol:
                return True
    return False


def _length(lines):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for line in lines for a, b in zip(line, line[1:]))


def share_within(lines_a, lines_b, tol=TOLERANCE_M):
    """Fraction of `lines_a` (metres) lying within `tol` of `lines_b`."""
    pts = [p for line in lines_a for p in _densify(line)]
    if not pts:
        return 0.0
    hit = sum(1 for p in pts if _near(p, lines_b, tol))
    return hit / float(len(pts))


def overlap(lines_a, lines_b, tol=TOLERANCE_M):
    """(share of the SHORTER line within tol of the other, metres overlapped).

    Both in local metres. A point (a one-point line) counts as length zero,
    so it is always the shorter and matches when it lies within `tol`.
    """
    la, lb = _length(lines_a), _length(lines_b)
    if la <= lb:
        share = share_within(lines_a, lines_b, tol)
        return share, share * la
    share = share_within(lines_b, lines_a, tol)
    return share, share * lb


def bbox(lines):
    xs = [p[0] for line in lines for p in line]
    ys = [p[1] for line in lines for p in line]
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------- ways


class Way(object):
    __slots__ = ("uid", "authority", "name", "parish", "number", "lines",
                 "box", "way_class")

    def __init__(self, uid, authority, name, lines, way_class="boat"):
        self.uid = uid
        self.authority = authority
        self.name = name
        # `boat` or `ucr`. A reference names one or the other ("Byway 6",
        # "uUCR 306"), and the two numberings are the council's to keep
        # apart, not ours to assume apart: see match_ref.
        self.way_class = way_class
        self.parish, self.number = split_name(name)
        self.lines = lines
        self.box = bbox(lines)


class Byways(object):
    """Every published byway, with a coarse grid for finding the near ones."""

    def __init__(self, ways):
        self.ways = {}
        for way in ways:
            if way.uid not in self.ways and way.lines and any(way.lines):
                self.ways[way.uid] = way
        self._grid = {}
        for way in self.ways.values():
            for cell in self._cells(way.box, pad=0.0005):
                self._grid.setdefault(cell, []).append(way)
        self._by_ref = {}
        for way in self.ways.values():
            key = (way.authority, way.parish, way.number)
            self._by_ref.setdefault(key, []).append(way)

    def __len__(self):
        return len(self.ways)

    def count(self, way_class="boat"):
        """How many ways of one class: the byways alone by default. The
        "a broken checkout has under 1,000 byways" floors count these, not
        len(), which counts the unsurfaced roads too."""
        return sum(1 for w in self.ways.values() if w.way_class == way_class)

    @staticmethod
    def _cells(box, pad=0.0):
        w, s, e, n = box
        for i in range(int(math.floor((w - pad) / _CELL)),
                       int(math.floor((e + pad) / _CELL)) + 1):
            for j in range(int(math.floor((s - pad) / _CELL)),
                           int(math.floor((n + pad) / _CELL)) + 1):
                yield (i, j)

    def near(self, lines_wgs84, pad_m=TOLERANCE_M + 5):
        """Ways whose box comes within `pad_m` of these lines' box."""
        w, s, e, n = bbox(lines_wgs84)
        pad = pad_m / 111000.0 * 2
        seen, out = set(), []
        for cell in self._cells((w, s, e, n), pad=pad):
            for way in self._grid.get(cell, ()):
                if way.uid in seen:
                    continue
                seen.add(way.uid)
                ww, ws, we, wn = way.box
                if we >= w - pad and ww <= e + pad and wn >= s - pad \
                        and ws <= n + pad:
                    out.append(way)
        return out

    def match_geometry(self, lines_wgs84, authorities=None,
                       tol=TOLERANCE_M, min_share=MIN_SHARE):
        """[(way_uid, share, metres)] for every byway this geometry is on.

        `authorities`, when given, is the set of container authority names
        the source can speak for: a council's closure layer can only close
        its own byways, and a line along a county boundary must not close
        the neighbour's lane that runs beside it.
        """
        lines_wgs84 = [list(l) for l in lines_wgs84 if l]
        if not lines_wgs84:
            return []
        lat0 = sum(p[1] for l in lines_wgs84 for p in l) / sum(
            len(l) for l in lines_wgs84)
        lon0 = sum(p[0] for l in lines_wgs84 for p in l) / sum(
            len(l) for l in lines_wgs84)
        src = [_project(l, lat0, lon0) for l in lines_wgs84]
        out = []
        for way in self.near(lines_wgs84):
            if authorities and way.authority not in authorities:
                continue
            wl = [_project(l, lat0, lon0) for l in way.lines]
            share, metres = overlap(src, wl, tol)
            if share >= min_share:
                out.append((way.uid, round(share, 3), round(metres, 1)))
        out.sort()
        return out

    def match_ref(self, authority, parish, number, way_class="boat"):
        """Way uids recorded as this authority's parish + path number.

        OF ONE CLASS, a byway's by default. A council numbering its byways
        and its unsurfaced roads in two series could give "Bere Ferrers 6"
        to both, and a notice closing Byway 6 must not close UCR 6 with it;
        a source naming a UCR asks for `ucr`.
        """
        key = (authority, norm_parish(parish), norm_number(number))
        return sorted(w.uid for w in self._by_ref.get(key, ())
                      if w.way_class == way_class)

    def match_number(self, authority, number, way_class="boat"):
        """Way uids with this path number anywhere in the authority.

        For sources whose parish wording cannot be compared (a route code,
        a parish number); only safe when the caller confirms by geometry.
        """
        n = norm_number(number)
        return sorted(w.uid for w in self.ways.values()
                      if w.authority == authority and w.number == n
                      and w.way_class == way_class)

    def geometry(self, uids):
        """The lines of these ways, for a feature drawn from a reference."""
        lines = []
        for uid in uids:
            way = self.ways.get(uid)
            if way:
                lines.extend(way.lines)
        return lines

    def authorities(self):
        return sorted(set(w.authority for w in self.ways.values()))


def mark_on(item, ways):
    """Say on `item` what it was matched to, where every way is an
    unsurfaced road: item["on"] = "ucr", so council_orders.label_for says
    "Road closed", not "Byway closed". Absent for a byway or a mix, so a
    byway's item is unchanged. `ways` are Way objects (None, for a uid no
    longer published, counts as a byway). Every matcher calls this wherever
    it makes a match: council_sources, order_register, street_manager and
    mod_ranges."""
    ways = list(ways or [])
    if ways and all(getattr(w, "way_class", "boat") == "ucr" for w in ways):
        item["on"] = "ucr"
    return item


def load_byways(pattern=None, include_ucr=True):
    """Every BOAT in the published regional containers - and, unless
    [include_ucr] is False, every unsurfaced unclassified road.

    THE ROADS TOO, because an order closes a road exactly as it closes a
    byway: a council's closure notice, a register entry, a Street Manager
    closure or a firing range can each be about a UCR, and a matcher that
    sees only byways would publish none of them against the lane a rider
    has. UCRs live in their own table (`ucr_ways`, which an app before 119
    never reads), so this reads both. The definitive-map processes - a DMMO
    application, a Planning Inspectorate decision - are about the definitive
    map, which no UCR is on; their callers pass False.

    The overview container is skipped: it holds simplified copies of the
    same ways, and matching against a simplified line would move a record
    by tens of metres.
    """
    pattern = pattern or os.path.join(ROOT, "containers", "ways-*.tbmap")
    ways = []
    paths = sorted(p for p in glob.glob(pattern)
                   if not p.endswith("ways-overview.tbmap"))
    for path in paths:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                               uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            scale = float(meta.get("geometry_scale") or GEOMETRY_SCALE)
            tables = set(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))
            queries = ["SELECT way_uid, authority, name, geometry, way_class "
                       "FROM ways WHERE way_class = 'boat'"]
            if include_ucr and "ucr_ways" in tables:
                queries.append("SELECT way_uid, authority, name, geometry, "
                               "way_class FROM ucr_ways "
                               "WHERE way_class = 'ucr'")
            for query in queries:
                for uid, authority, name, blob, klass in conn.execute(query):
                    ways.append(Way(uid, authority, name,
                                    unpack_geometry(blob, scale), klass))
        finally:
            conn.close()
    return Byways(ways)
