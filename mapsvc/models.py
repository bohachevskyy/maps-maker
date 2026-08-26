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
    """

    rows: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    dropped: list = field(default_factory=list)
