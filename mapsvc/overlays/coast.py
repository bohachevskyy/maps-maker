"""The sea, from OpenStreetMap's coastline, for city-scale maps.

Administrative polygons are generalised: Tokyo's wards have 30-70 vertices
each and the bay between them is a handful of straight edges. OSM's
`natural=coastline` ways are the unsimplified shore, drawn with land on the
left of the direction of travel. Clipped to the window and closed along its
edges they are the sea; painted over the land, the shoreline is the real one.

Pure geometry here (`sea_rings`); the one network call is `fetch`.
"""

import hashlib
import json
import time

from mapsvc import fetch

EPS = 1e-9


def query(box: tuple) -> str:
    west, south, east, north = box
    return (f"[out:json][timeout:120][maxsize:268435456];"
            f'way["natural"="coastline"]({south:g},{west:g},{north:g},{east:g});out geom;')


def fetch_ways(box: tuple) -> list:
    """Coastline ways crossing the box, as [(lon, lat), ...] in OSM direction."""
    from mapsvc.overlays import overpass

    ql = query(box)
    digest = hashlib.sha256(ql.encode()).hexdigest()[:16]
    target = fetch.path("overlays", "overpass", f"coast-{digest}.json")
    if target.exists() and time.time() - target.stat().st_mtime <= overpass.CACHE_FRESH_S:
        payload = target.read_bytes()
    else:
        try:
            payload = overpass._post(ql)
        except overpass.UpstreamUnavailable as exc:
            if not target.exists():
                raise OSError(str(exc)) from exc
            payload = target.read_bytes()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            temporary.write_bytes(payload)
            temporary.replace(target)
    ways = []
    for element in json.loads(payload).get("elements", []):
        points = [(g["lon"], g["lat"]) for g in element.get("geometry") or []]
        if element.get("type") == "way" and len(points) >= 2:
            ways.append((element["id"], points))
    ways.sort()
    return [points for _, points in ways]


def _chains(ways: list) -> list:
    """Ways joined head to tail into longer chains (closed ones repeat the first point)."""
    by_start: dict = {}
    for i, points in enumerate(ways):
        by_start.setdefault(points[0], []).append(i)
    used = [False] * len(ways)
    has_predecessor = {points[-1] for points in ways}
    order = sorted(range(len(ways)), key=lambda i: (ways[i][0] in has_predecessor, i))
    chains = []
    for i in order:
        if used[i]:
            continue
        used[i] = True
        chain = list(ways[i])
        while chain[-1] != chain[0]:
            nxt = next((j for j in by_start.get(chain[-1], ()) if not used[j]), None)
            if nxt is None:
                break
            used[nxt] = True
            chain.extend(ways[nxt][1:])
        chains.append(chain)
    return chains


def _clip_segment(a, b, box):
    """Liang-Barsky: the part of segment ab inside the box, or None."""
    west, south, east, north = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - west), (dx, east - a[0]),
                 (-dy, a[1] - south), (dy, north - a[1])):
        if abs(p) < 1e-15:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return ((a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy))


def _runs(chain: list, box: tuple) -> list:
    """The pieces of a chain inside the box, each a polyline."""
    runs, current = [], []
    for a, b in zip(chain, chain[1:]):
        part = _clip_segment(a, b, box)
        if part is None:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue
        start, end = part
        if current and _close(current[-1], start):
            current.append(end)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [start, end]
        if not _close(end, b):          # left the box
            runs.append(current)
            current = []
    if len(current) >= 2:
        runs.append(current)
    return runs


def _close(a, b) -> bool:
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


def _perimeter_t(point, box) -> float | None:
    """Distance along the box boundary counter-clockwise from its south-west
    corner, or None when the point is not on the boundary."""
    west, south, east, north = box
    x, y = point
    w, h = east - west, north - south
    if abs(y - south) < EPS:
        return x - west
    if abs(x - east) < EPS:
        return w + (y - south)
    if abs(y - north) < EPS:
        return w + h + (east - x)
    if abs(x - west) < EPS:
        return 2 * w + h + (north - y)
    return None


def _corners(t0: float, t1: float, box) -> list:
    """Box corners met going counter-clockwise from perimeter t0 to t1."""
    west, south, east, north = box
    w, h = east - west, north - south
    perimeter = 2 * (w + h)
    corners = [(w, (east, south)), (w + h, (east, north)),
               (2 * w + h, (west, north)), (perimeter, (west, south))]
    span = (t1 - t0) % perimeter
    found = []
    for position, point in [(0.0, (west, south))] + corners:
        distance = (position - t0) % perimeter
        if 0 < distance < span:
            found.append((distance, point))
    found.sort()
    return [p for _, p in found]


def _signed_area(ring) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])) / 2


def sea_rings(ways: list, box: tuple) -> list:
    """Rings (lon, lat) that together, under the even-odd rule, are the sea in `box`.

    OSM coastlines keep land on the left, so each open run is reversed (sea on
    the left) and closed counter-clockwise along the box edge; an island is a
    counter-clockwise closed chain and punches a hole. [] when no coastline
    crosses the box: there is then nothing to say about sea.
    """
    open_runs, rings = [], []
    for chain in _chains(ways):
        if chain[0] == chain[-1] and all(
                box[0] < x < box[2] and box[1] < y < box[3] for x, y in chain):
            ring = chain[:-1]
            if len(ring) >= 3:
                rings.append(ring)
            continue
        for run in _runs(chain, box):
            if (_perimeter_t(run[0], box) is not None
                    and _perimeter_t(run[-1], box) is not None):
                open_runs.append(list(reversed(run)))
    if not open_runs:
        # Islands alone: the box is sea around them. A clockwise ring alone is
        # a lake inside land, which is not this layer's business.
        islands = [r for r in rings if _signed_area(r) > 0]
        if not islands:
            return []
        west, south, east, north = box
        return [[(west, south), (east, south), (east, north), (west, north)]] + islands

    starts = sorted((_perimeter_t(r[0], box), i) for i, r in enumerate(open_runs))
    perimeter = 2 * ((box[2] - box[0]) + (box[3] - box[1]))
    unused = set(range(len(open_runs)))
    sea = []
    while unused:
        first = min(unused)
        ring, current = [], first
        while True:
            unused.discard(current)
            run = open_runs[current]
            ring.extend(run)
            exit_t = _perimeter_t(run[-1], box)
            # The next run to start counter-clockwise from where this one ends.
            following = min(starts, key=lambda s: ((s[0] - exit_t) % perimeter, s[1]))
            ring.extend(_corners(exit_t, following[0], box))
            current = following[1]
            if current == first or current not in unused:
                break
        sea.append(ring)
    return sea + [r for r in rings if _signed_area(r) > 0]
