"""Lengths on the sphere, and route length with parallel tracks merged. Pure."""

import math

EARTH_R = 6371008.8   # mean radius, metres


def segment_m(a, b) -> float:
    """Metres between two (lon, lat) points; equirectangular, fine below ~10 km."""
    lat = math.radians((a[1] + b[1]) / 2.0)
    dx = math.radians(b[0] - a[0]) * math.cos(lat) * EARTH_R
    dy = math.radians(b[1] - a[1]) * EARTH_R
    return math.hypot(dx, dy)


def line_m(coords) -> float:
    return sum(segment_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def route_m(lines: list, radius: float = 20.0, step: float = 25.0) -> float:
    """Length of a network with parallel lines counted once.

    OpenStreetMap draws each track of a double-track railway as its own way,
    so summing ways counts the network twice: 5,282 track-km against 2,629 km
    of route for France's LGVs. Every line is cut into pieces of about `step`
    metres; a piece counts only if no piece of an *earlier, different* line
    lies within `radius` of its midpoint. Lines are visited in the order given,
    so the result is deterministic for a sorted input.

    Calibrated on that LGV data (2026-10): radius 20 m gives 2,637 km against
    2,629 km from buffering and dissolving at full resolution; 10 m gives
    2,745, 30 m 2,577.
    """
    cell = radius
    grid: dict = {}
    total = 0.0
    for index, coords in enumerate(lines):
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            length = segment_m(a, b)
            pieces = max(1, int(math.ceil(length / step)))
            for k in range(pieces):
                t = (k + 0.5) / pieces
                lon = a[0] + (b[0] - a[0]) * t
                lat = a[1] + (b[1] - a[1]) * t
                x = math.radians(lon) * math.cos(math.radians(lat)) * EARTH_R
                y = math.radians(lat) * EARTH_R
                cx, cy = int(x // cell), int(y // cell)
                covered = False
                for ix in (cx - 1, cx, cx + 1):
                    for iy in (cy - 1, cy, cy + 1):
                        for px, py, owner in grid.get((ix, iy), ()):
                            if owner != index and (px - x) ** 2 + (py - y) ** 2 < radius ** 2:
                                covered = True
                                break
                        if covered:
                            break
                    if covered:
                        break
                if not covered:
                    grid.setdefault((cx, cy), []).append((x, y, index))
                    total += length / pieces
    return total


def chain(lines: list) -> list:
    """Ways joined end to end into as few polylines as possible. Deterministic.

    A route relation lists every way it uses -- 4,171 of them for the
    Trans-Siberian -- and drawing each as its own subpath costs bytes and
    leaves joins unrounded. Lines are visited in the order given; each grows
    forward, then backward, by the earliest unused line sharing an endpoint
    (reversed if need be).
    """
    ends: dict = {}
    for index, coords in enumerate(lines):
        if len(coords) < 2:
            continue
        ends.setdefault(tuple(coords[0]), []).append(index)
        ends.setdefault(tuple(coords[-1]), []).append(index)
    used = [len(c) < 2 for c in lines]

    def take(point):
        for index in ends.get(point, ()):
            if not used[index]:
                used[index] = True
                coords = [tuple(c) for c in lines[index]]
                return coords if coords[0] == point else coords[::-1]
        return None

    out = []
    for index, coords in enumerate(lines):
        if used[index]:
            continue
        used[index] = True
        path = [tuple(c) for c in coords]
        while (nxt := take(path[-1])) is not None:
            path.extend(nxt[1:])
        while (prev := take(path[0])) is not None:
            path[:0] = prev[::-1][:-1]
        out.append(path)
    return out
