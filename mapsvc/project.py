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
    """`projection: "auto"` -- pick by latitude span, then by latitude band."""
    min_lon, min_lat, max_lon, max_lat = extent
    lat_span = max_lat - min_lat
    if lat_span > 90.0:
        return "mollweide"
    if abs((min_lat + max_lat) / 2.0) >= 25.0:
        return "albers"
    return "mercator"


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


def make(name: str, extent: Extent):
    """Build a projection function parameterised by the map extent."""
    min_lon, min_lat, max_lon, max_lat = extent
    lon0 = (min_lon + max_lon) / 2.0
    lat0 = (min_lat + max_lat) / 2.0
    if name == "mercator":
        return _mercator(lon0)
    if name == "mollweide":
        return _mollweide(lon0)
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

    # A zero-width or zero-height extent (single point feature) would divide by
    # nothing downstream; give it a degree of breathing room.
    if max_lon - min_lon < 1e-6:
        min_lon, max_lon = min_lon - 0.5, max_lon + 0.5
    if max_lat - min_lat < 1e-6:
        min_lat, max_lat = min_lat - 0.5, max_lat + 0.5
    return (min_lon, min_lat, max_lon, max_lat)
