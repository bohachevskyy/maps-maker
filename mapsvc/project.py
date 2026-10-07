"""Map projections and canvas fitting. Pure trig, no I/O.

Each projection takes (lon, lat) in degrees and returns (x, y) in abstract
projected units. Scaling to the canvas is a separate step so that the one-scale-
factor rule stays in a single place.
"""

import math

Extent = tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat

# Beyond this the Mercator y blows up and the poles eat the canvas.
MERCATOR_LAT_LIMIT = 85.0

# A vertex this close to +/-180 means the ring was clipped against the
# antimeridian rather than genuinely ending there.
ANTIMERIDIAN_EPS = 179.99


def choose(extent: Extent) -> str:
    """`projection: "auto"` -- an equal-area projection suited to the window.

    Every choice is equal-area, because a choropleth compares areas by eye and
    any other projection inflates some units' share of the ink.

    * Nearly the whole globe: Equal Earth.
    * A mid-latitude window much wider east-west than tall (the conterminous
      US, Russia, a strip of the Sahel): Albers, whose cone follows the band.
    * Anything else -- a continent, a country, a city: Lambert azimuthal
      equal-area centred on the window, as EPSG:3035 does for Europe.

    The rules describe the window's shape, not named places. "Wide" is measured
    in ground distance, so a degree of longitude counts for cos(latitude).
    """
    min_lon, min_lat, max_lon, max_lat = extent
    lon_span = max_lon - min_lon
    lat_span = max(max_lat - min_lat, 1e-9)
    lat0 = (min_lat + max_lat) / 2.0
    if lon_span >= 240.0 or (lon_span >= 180.0 and lat_span >= 90.0):
        return "equal_earth"
    ground_aspect = lon_span * math.cos(math.radians(lat0)) / lat_span
    if abs(lat0) >= 25.0 and ground_aspect >= 1.5:
        return "albers"
    return "laea"


def _mercator(lon0: float):
    def project(lon: float, lat: float) -> tuple[float, float]:
        phi = math.radians(max(-MERCATOR_LAT_LIMIT, min(MERCATOR_LAT_LIMIT, lat)))
        return math.radians(lon - lon0), math.log(math.tan(math.pi / 4 + phi / 2))
    return project


def _mollweide(lon0: float):
    def project(lon: float, lat: float) -> tuple[float, float]:
        phi = math.radians(lat)
        # Solve 2*theta + sin(2*theta) = pi*sin(phi) for theta.
        theta = phi
        target = math.pi * math.sin(phi)
        for _ in range(8):
            denom = 2 + 2 * math.cos(2 * theta)
            if abs(denom) < 1e-12:  # at the poles theta is already pi/2
                break
            delta = (2 * theta + math.sin(2 * theta) - target) / denom
            theta -= delta
            if abs(delta) < 1e-12:
                break
        x = (2 * math.sqrt(2) / math.pi) * math.radians(lon - lon0) * math.cos(theta)
        y = math.sqrt(2) * math.sin(theta)
        return x, y
    return project


def _equal_earth(lon0: float):
    """Equal Earth (Šavrič, Patterson & Jenny, 2018), unit sphere."""
    a1, a2, a3, a4 = 1.340264, -0.081106, 0.000893, 0.003796

    def project(lon: float, lat: float) -> tuple[float, float]:
        lam = math.radians(lon - lon0)
        phi = math.radians(max(-90.0, min(90.0, lat)))
        theta = math.asin(math.sqrt(3.0) * math.sin(phi) / 2.0)
        theta2 = theta * theta
        theta6 = theta2 * theta2 * theta2
        denominator = 3.0 * (
            9.0 * a4 * theta6 * theta2
            + 7.0 * a3 * theta6
            + 3.0 * a2 * theta2
            + a1
        )
        x = 2.0 * math.sqrt(3.0) * lam * math.cos(theta) / denominator
        y = theta * (a4 * theta6 * theta2 + a3 * theta6 + a2 * theta2 + a1)
        return x, y

    return project


def _laea(lon0: float, lat0: float):
    """Spherical Lambert azimuthal equal-area centred on the extent."""
    lambda0, phi0 = math.radians(lon0), math.radians(lat0)
    sin_phi0, cos_phi0 = math.sin(phi0), math.cos(phi0)

    def project(lon: float, lat: float) -> tuple[float, float]:
        lam, phi = math.radians(lon), math.radians(lat)
        delta = lam - lambda0
        sin_phi, cos_phi = math.sin(phi), math.cos(phi)
        denominator = 1.0 + sin_phi0 * sin_phi + cos_phi0 * cos_phi * math.cos(delta)
        # The antipode is outside any regional viewport, but keeping the
        # function finite makes malformed/coarse rings harmless.
        k = math.sqrt(2.0 / max(denominator, 1e-12))
        return (
            k * cos_phi * math.sin(delta),
            k * (cos_phi0 * sin_phi - sin_phi0 * cos_phi * math.cos(delta)),
        )

    return project


def _albers(lon0: float, lat0: float, lat1: float, lat2: float):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    n = (math.sin(p1) + math.sin(p2)) / 2
    if abs(n) < 1e-6:
        # Standard parallels straddle the equator symmetrically; the cone
        # degenerates into a cylinder. Mercator is the honest fallback.
        return _mercator(lon0)
    c = math.cos(p1) ** 2 + 2 * n * math.sin(p1)
    rho0 = math.sqrt(max(c - 2 * n * math.sin(math.radians(lat0)), 0.0)) / n

    def project(lon: float, lat: float) -> tuple[float, float]:
        rho = math.sqrt(max(c - 2 * n * math.sin(math.radians(lat)), 0.0)) / n
        theta = n * math.radians(lon - lon0)
        return rho * math.sin(theta), rho0 - rho * math.cos(theta)
    return project


def _shortest(delta_lon: float) -> float:
    """Shortest signed longitude difference, in (-180, 180]."""
    return (delta_lon + 180.0) % 360.0 - 180.0


def unwrap_ring(coords, lon0: float) -> list[tuple[float, float]]:
    """Put a ring's longitudes on one continuous branch near `lon0`.

    Natural Earth splits countries at the antimeridian, so a ring can run to
    exactly +/-180. Wrapping each vertex independently sends one endpoint to the
    opposite edge of the canvas and draws a spike across the whole map. Walking
    the ring and keeping each vertex on the branch nearest its predecessor
    preserves the shape; shifting the finished ring by whole turns puts it on
    the side of the map it belongs to.
    """
    out: list[tuple[float, float]] = []
    prev: float | None = None
    for lon, lat in coords:
        if prev is not None:
            lon = prev + _shortest(lon - prev)
        prev = lon
        out.append((lon, lat))
    lons = [lon for lon, _ in out]
    midpoint = (min(lons) + max(lons)) / 2.0
    shift = -360.0 * round((midpoint - lon0) / 360.0)
    if shift:
        out = [(lon + shift, lat) for lon, lat in out]
    return out


def split_at_seam(ring, lon0: float) -> list[list[tuple[float, float]]]:
    """A ring (already unwrapped near lon0) cut where a world map's edges are.

    A world map centred away from 0° -- on the Pacific, say -- has its seam at
    lon0 ± 180, through land that a Greenwich-centred map never cuts:
    Greenland and Antarctica at 150°E. Each copy of the ring a whole turn
    apart is clipped to the visible branch, so the pieces meet at the two
    edges instead of one of them being drawn across the whole map.
    """
    lons = [lon for lon, _ in ring]
    west, east = lon0 - 180.0, lon0 + 180.0
    if not lons or (min(lons) >= west - 1e-9 and max(lons) <= east + 1e-9):
        return [list(ring)]
    pieces = []
    for shift in (-360.0, 0.0, 360.0):
        moved = [(lon + shift, lat) for lon, lat in ring]
        if max(lon for lon, _ in moved) <= west or min(lon for lon, _ in moved) >= east:
            continue
        clipped = clip_to_extent(moved, (west, -90.0, east, 90.0))
        if len(clipped) >= 3:
            pieces.append(clipped)
    return pieces


def central_meridian(longitudes, step: float = 30.0, concentration: float = 0.35):
    """Where a world map of these points should be centred, or None for 0°.

    The circular mean of the longitudes, rounded to `step` degrees, when the
    points are concentrated enough for a centre to mean anything (mean
    resultant length above `concentration`). 2025's M6+ earthquakes: mean
    157.8°E, resultant 0.51, so 150°E -- the Pacific in one piece and the seam
    in the Atlantic at 30°W.
    """
    values = list(longitudes)
    if not values:
        return None
    s = sum(math.sin(math.radians(v)) for v in values)
    c = sum(math.cos(math.radians(v)) for v in values)
    if math.hypot(s, c) / len(values) < concentration:
        return None
    centre = step * round(math.degrees(math.atan2(s, c)) / step)
    centre = (centre + 180.0) % 360.0 - 180.0
    return None if centre == 0 else centre


def make(name: str, extent: Extent):
    """Build a projection function parameterised by the map extent."""
    min_lon, min_lat, max_lon, max_lat = extent
    lon0 = (min_lon + max_lon) / 2.0
    lat0 = (min_lat + max_lat) / 2.0
    if name == "mercator":
        return _mercator(lon0)
    if name == "mollweide":
        return _mollweide(lon0)
    if name == "equal_earth":
        return _equal_earth(lon0)
    if name == "laea":
        return _laea(lon0, lat0)
    if name == "albers":
        # Standard parallels at the sixth-points of the latitude span keeps
        # distortion balanced across the map (Deetz & Adams's rule of thumb).
        span = max_lat - min_lat
        return _albers(lon0, lat0, min_lat + span / 6.0, max_lat - span / 6.0)
    raise ValueError(f"unknown projection {name!r}")


def fit(bounds: tuple[float, float, float, float], width: float, height: float,
        margin: float):
    """Return a transform mapping projected coords into the canvas box.

    One scale factor for both axes -- two would stretch the map and destroy the
    projection's area properties.
    """
    min_x, min_y, max_x, max_y = bounds
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    # Centre whatever slack the single scale factor leaves on the other axis.
    off_x = (width - scale * span_x) / 2.0
    off_y = (height - scale * span_y) / 2.0

    def transform(x: float, y: float) -> tuple[float, float]:
        # SVG y grows downward; projected y grows north.
        return off_x + (x - min_x) * scale, height - off_y - (y - min_y) * scale
    return transform


def _ring_area(ring) -> float:
    """Unsigned shoelace area in square degrees. Only used to rank rings."""
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(total) / 2.0


def outer_rings(geometry) -> list:
    """Exterior rings of a Polygon or MultiPolygon."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"][0]]
    return [part[0] for part in geometry["coordinates"]]


def extent_rings(geometries) -> list:
    """The one ring per feature that is allowed to define the viewport.

    Applies both filters described in `extent_of`: largest ring only, and
    nothing that was clipped at the antimeridian.
    """
    kept = []
    for geometry in geometries:
        rings = outer_rings(geometry)
        if not rings:
            continue
        ring = max(rings, key=_ring_area)
        lons = [lon for lon, _ in unwrap_ring(ring, 0.0)]
        if any(abs(lon) >= ANTIMERIDIAN_EPS for lon in lons):
            continue
        if max(lons) - min(lons) > 180.0:
            continue
        kept.append(ring)
    return kept


def viewport_bounds(geometries, projector, extent: Extent) -> tuple[float, float, float, float]:
    """Projected bounds of the extent-defining rings.

    Fitting the lon/lat extent *rectangle* instead would waste a lot of canvas:
    under a conic the rectangle's southern corners bow far west of any land, so
    a map of Europe ends up with a third of its width empty. Fitting the rings
    themselves tracks the shape the projection actually produces.
    """
    rings = extent_rings(geometries)
    if extent == (-180.0, -90.0, 180.0, 90.0):
        # Whole globe: fit the projection's own outline, not the land, so the
        # oceans and the clipped-out features keep their place.
        rings = [[(lon, lat) for lat in range(-90, 91, 5)] for lon in (-180, 0, 180)]
        rings += [[(lon, lat) for lon in range(-180, 181, 5)] for lat in (-90, 0, 90)]
    xs: list[float] = []
    ys: list[float] = []
    for ring in rings:
        for lon, lat in ring:
            x, y = projector(lon, lat)
            xs.append(x)
            ys.append(y)
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def extent_of(geometries) -> Extent:
    """Map viewport for a set of features.

    Two departures from a naive bbox of every vertex, both needed to get a
    recognisable regional map:

    * Only each feature's *largest* ring counts. Otherwise France reaches the
      extent to French Guiana, Norway to Bouvet Island, Portugal to the Azores,
      and a map of Europe becomes a map of the Atlantic.
    * Rings cut at the antimeridian are skipped. Natural Earth files all of
      Russia under CONTINENT='Europe' and clips its ring against the 180th
      meridian, leaving vertices sitting exactly on it; letting that define the
      viewport stretches a map of Europe out to 180 degrees east. Such features
      are still drawn and still coloured -- they are just clipped by the viewport
      rather than defining it.
    """
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    fallback: list = []

    for geometry in geometries:
        rings = outer_rings(geometry)
        if not rings:
            continue
        ring = max(rings, key=_ring_area)
        lons = [lon for lon, _ in unwrap_ring(ring, 0.0)]
        lats = [lat for _, lat in ring]
        cut_at_antimeridian = any(abs(lon) >= ANTIMERIDIAN_EPS for lon in lons)
        if cut_at_antimeridian or max(lons) - min(lons) > 180.0:
            fallback.append((min(lons), min(lats), max(lons), max(lats)))
            continue
        min_lon, max_lon = min(min_lon, min(lons)), max(max_lon, max(lons))
        min_lat, max_lat = min(min_lat, min(lats)), max(max_lat, max(lats))

    if min_lon == float("inf"):
        # Every feature was a wide crosser (e.g. region="RUS"); use them anyway.
        if not fallback:
            return (-180.0, -90.0, 180.0, 90.0)
        min_lon = min(b[0] for b in fallback)
        min_lat = min(b[1] for b in fallback)
        max_lon = max(b[2] for b in fallback)
        max_lat = max(b[3] for b in fallback)

    # If what survived the filters already wraps most of the way round, the
    # request was for the whole globe. Snapping to it restores the features the
    # antimeridian filter set aside -- otherwise a world map clips Antarctica to
    # a sliver, having taken its southern limit from Tierra del Fuego.
    if max_lon - min_lon > 300.0:
        return (-180.0, -90.0, 180.0, 90.0)

    # A zero-width or zero-height extent (single point feature) would divide by
    # nothing downstream; give it a degree of breathing room.
    if max_lon - min_lon < 1e-6:
        min_lon, max_lon = min_lon - 0.5, max_lon + 0.5
    if max_lat - min_lat < 1e-6:
        min_lat, max_lat = min_lat - 0.5, max_lat + 0.5
    return (min_lon, min_lat, max_lon, max_lat)


# ------------------------------------------------------------ the window ---

def window_outline(extent: Extent, step: float = 1.0) -> list[tuple[float, float]]:
    """The extent rectangle as a closed lon/lat ring, densified.

    Under any projection but Mercator a parallel or a meridian is a curve, so
    four corners are not enough to draw the window. For the whole globe this
    ring *is* the projection's outline: two pole lines and the two 180th
    meridians.
    """
    min_lon, min_lat, max_lon, max_lat = extent

    def run(a: float, b: float) -> list[float]:
        n = max(1, math.ceil(abs(b - a) / step))
        return [a + (b - a) * i / n for i in range(n)]

    ring = [(lon, min_lat) for lon in run(min_lon, max_lon)]
    ring += [(max_lon, lat) for lat in run(min_lat, max_lat)]
    ring += [(lon, max_lat) for lon in run(max_lon, min_lon)]
    ring += [(min_lon, lat) for lat in run(max_lat, min_lat)]
    ring.append(ring[0])
    return ring


def graticule_interval(extent: Extent) -> float:
    """A round spacing that puts a handful of lines across the window."""
    span = max(extent[2] - extent[0], extent[3] - extent[1])
    for interval in (1.0, 2.0, 5.0, 10.0, 15.0):
        if span / interval <= 8:
            return interval
    return 30.0


def graticule(extent: Extent, interval: float, step: float = 1.0) -> list[list]:
    """Interior meridians and parallels of the window, as lon/lat polylines."""
    min_lon, min_lat, max_lon, max_lat = extent
    fine = min(step, interval / 4.0)
    lines: list[list] = []

    def ticks(lo: float, hi: float) -> list[float]:
        first = math.floor(lo / interval) * interval + interval
        out = []
        v = first
        while v < hi - 1e-9:
            if v > lo + 1e-9:
                out.append(round(v, 9))
            v += interval
        return out

    def run(a: float, b: float) -> list[float]:
        n = max(1, math.ceil((b - a) / fine))
        return [a + (b - a) * i / n for i in range(n + 1)]

    for lon in ticks(min_lon, max_lon):
        lines.append([(lon, lat) for lat in run(min_lat, max_lat)])
    for lat in ticks(min_lat, max_lat):
        lines.append([(lon, lat) for lon in run(min_lon, max_lon)])
    return lines


def clip_to_extent(ring, extent: Extent) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a lon/lat ring against the extent rectangle.

    Used only to decide where a symbol goes: a unit cut by the window should
    carry its symbol on the part that is visible.
    """
    min_lon, min_lat, max_lon, max_lat = extent
    edges = (
        (lambda p: p[0] >= min_lon, lambda a, b: _cross_x(a, b, min_lon)),
        (lambda p: p[0] <= max_lon, lambda a, b: _cross_x(a, b, max_lon)),
        (lambda p: p[1] >= min_lat, lambda a, b: _cross_y(a, b, min_lat)),
        (lambda p: p[1] <= max_lat, lambda a, b: _cross_y(a, b, max_lat)),
    )
    points = list(ring)
    for inside, cross in edges:
        if not points:
            break
        out = []
        prev = points[-1]
        for cur in points:
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
            prev = cur
        points = out
    return points


def _cross_x(a, b, x):
    t = (x - a[0]) / (b[0] - a[0])
    return (x, a[1] + t * (b[1] - a[1]))


def _cross_y(a, b, y):
    t = (y - a[1]) / (b[1] - a[1])
    return (a[0] + t * (b[0] - a[0]), y)


# ------------------------------------------------------- planar geometry ---
# Everything below works in canvas pixels, after projection.

def planar_area(ring) -> float:
    """Unsigned shoelace area of a ring of (x, y) points."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def point_in_ring(x: float, y: float, ring) -> bool:
    """Even-odd ray cast."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _segment_distance_sq(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx or dy:
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        if t > 1:
            ax, ay = bx, by
        elif t > 0:
            ax, ay = ax + dx * t, ay + dy * t
    dx, dy = px - ax, py - ay
    return dx * dx + dy * dy


def _signed_distance(x: float, y: float, ring, holes=()) -> float:
    """Distance to the polygon's edge, positive inside and negative outside.

    A hole's edge is an edge, and a point in a hole is outside: Kyiv oblast
    surrounds Kyiv city, and the oblast's symbol must not land on the city.
    """
    best = float("inf")
    for r in (ring, *holes):
        n = len(r)
        for i in range(n):
            ax, ay = r[i]
            bx, by = r[(i + 1) % n]
            best = min(best, _segment_distance_sq(x, y, ax, ay, bx, by))
    d = math.sqrt(best)
    inside = point_in_ring(x, y, ring) and not any(point_in_ring(x, y, h) for h in holes)
    return d if inside else -d


def simplify_ring(ring, tolerance: float) -> list:
    """Douglas-Peucker in page pixels, after a radial-distance pre-pass.

    Applied after projection, so the tolerance means the same thing on a map
    of Kyiv and a map of the world. Each ring is simplified on its own: a
    shared border can drift by up to `tolerance` between its two units, which
    at 0.3 px is below what a 2x raster resolves. The first and last points are
    kept, so a closed ring stays closed. Deterministic: no randomness, no
    dependence on anything but the points.
    """
    n = len(ring)
    if n <= 4 or tolerance <= 0:
        return list(ring)
    tol2 = tolerance * tolerance
    # Radial pass: drop runs of vertices within tolerance of the last kept one.
    # Cheap, and on dense coastlines it removes most points before DP.
    kept = [ring[0]]
    for point in ring[1:-1]:
        dx, dy = point[0] - kept[-1][0], point[1] - kept[-1][1]
        if dx * dx + dy * dy >= tol2:
            kept.append(point)
    kept.append(ring[-1])
    if len(kept) <= 4:
        return kept

    keep = [False] * len(kept)
    keep[0] = keep[-1] = True
    stack = [(0, len(kept) - 1)]
    if kept[0] == kept[-1]:
        # A closed ring: split it at its farthest vertex from the start, or DP
        # measures every point against a zero-length segment and keeps too few.
        ax, ay = kept[0]
        far = max(range(1, len(kept) - 1),
                  key=lambda i: (kept[i][0] - ax) ** 2 + (kept[i][1] - ay) ** 2)
        keep[far] = True
        stack = [(0, far), (far, len(kept) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        ax, ay = kept[first]
        bx, by = kept[last]
        best, index = -1.0, -1
        for i in range(first + 1, last):
            d = _segment_distance_sq(kept[i][0], kept[i][1], ax, ay, bx, by)
            if d > best:
                best, index = d, i
        if best > tol2:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [point for point, flag in zip(kept, keep) if flag]


def _decimate(ring, limit: int) -> list:
    if len(ring) <= limit:
        return list(ring)
    stride = math.ceil(len(ring) / limit)
    return list(ring[::stride])


def pole_of_inaccessibility(ring, precision: float = 1.0,
                            max_vertices: int = 160, holes=()) -> tuple[float, float]:
    """The interior point farthest from the ring's edge (Mapbox's polylabel).

    A centroid can fall outside a crescent or a C-shaped unit -- Croatia's
    centroid is in Bosnia -- and a symbol there would be read as Bosnia's.
    Quadtree search over cells, best potential first; ties resolve by
    insertion order so the result is deterministic. The ring is thinned to at
    most `max_vertices` points first: a symbol position does not need every
    coastline wiggle, and the search is quadratic in vertex count.
    """
    import heapq

    ring = _decimate(ring, max_vertices)
    holes = [_decimate(h, max_vertices) for h in holes if len(h) >= 3]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
    size = min(max_x - min_x, max_y - min_y)
    if size <= precision or len(ring) < 3:
        return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)

    counter = 0
    heap: list = []

    def push(cx: float, cy: float, h: float):
        nonlocal counter
        d = _signed_distance(cx, cy, ring, holes)
        heapq.heappush(heap, (-(d + h * math.sqrt(2)), counter, cx, cy, h, d))
        counter += 1

    h = size / 2.0
    x = min_x
    while x < max_x:
        y = min_y
        while y < max_y:
            push(x + h, y + h, h)
            y += size
        x += size

    # Start from the vertex-mean, which is usually close and often inside.
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    best = (_signed_distance(mx, my, ring, holes), mx, my)

    evaluated = 0
    while heap and evaluated < 4000:
        neg_potential, _, cx, cy, h, d = heapq.heappop(heap)
        if d > best[0]:
            best = (d, cx, cy)
        if -neg_potential - best[0] <= precision:
            continue
        h /= 2.0
        for ox in (-h, h):
            for oy in (-h, h):
                push(cx + ox, cy + oy, h)
                evaluated += 1
    return best[1], best[2]
