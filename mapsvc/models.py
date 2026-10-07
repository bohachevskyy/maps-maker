"""Data carried between the seams. Pure declarations -- importable by render."""

from dataclasses import dataclass, field


@dataclass
class HarvestResult:
    """What the harvester hands the renderer.

    rows       [{"id": "FRA", "geometry": {...}, "value": 38625.0}, ...]
    provenance {source, vintage, scale, region, variable, unit, year, normalize,
                normalize_unit} -- everything the mandatory footnote needs.
    dropped    [{"id": "ATA", "reason": "no_data", "geometry": {...}}, ...]

               Dropped rows keep their geometry: `missing: "hatch"` and
               `missing: "grey"` have to draw the features they exclude, and a
               country silently vanishing from a map is worse than one marked
               as having no data.
    overlays   feature layers (points, lines, flows) and their provenance.
    places     candidate place labels; the renderer picks which fit.
    """

    rows: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    dropped: list = field(default_factory=list)
    # Feature layers drawn above the polygons, one dict per manifest overlay
    # (see mapsvc/overlays/__init__.py for the shape).
    overlays: list = field(default_factory=list)
    # Context places for labelling -- Natural Earth populated places in the
    # window, largest first: [{"name", "lon", "lat", "pop", "capital"}]. Empty
    # unless overlays need orientation and bring no places of their own.
    places: list = field(default_factory=list)
