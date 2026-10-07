"""Data carried between the seams. Pure declarations -- importable by render."""

from dataclasses import dataclass, field

from mapsvc import registry


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


@dataclass(frozen=True)
class Manifest:
    # min_lon, min_lat, max_lon, max_lat, or None when `within` scopes the map.
    bbox: tuple | None
    level: str
    # None for a base map: boundaries with nothing painted on them.
    variable_source: str | None
    variable_id: str | None
    # Country codes and/or ISO 3166-2 codes. A bbox is spatial and returns
    # whatever overlaps it; this is the political filter a box cannot be. A list
    # names a region with no code of its own: the Balkans is however many
    # countries you say it is.
    within: tuple | None = None
    basemap_source: str = registry.DEFAULT_BASEMAP
    basemap_detail: str = "simplified"
    # (source, id) of a divisor: diesel *per person*, income *per household*.
    per: tuple | None = None
    # (from_year, to_year): percent change of the variable between two years.
    change: tuple | None = None
    # ((source, id), ...): feature layers drawn above the basemap. What to
    # fetch, never how to draw it -- points, lines or a flow is the data's
    # own geometry, and size and colour follow from what the source declares.
    overlays: tuple = ()

    def data_key(self) -> dict:
        """The data-relevant fields only.

        Everything else about a manifest is now derived, so this is very nearly
        the whole thing -- the exception being that rendering choices cannot
        invalidate a fetch because there are none to make.
        """
        key = {
            "bbox": None if self.bbox is None else [round(v, 6) for v in self.bbox],
            "within": None if self.within is None else list(self.within),
            "level": self.level,
            "basemap": {"source": self.basemap_source, "detail": self.basemap_detail},
            "variable": (None if self.variable_id is None
                         else self._variable_key()),
        }
        # Only present when used, so a manifest without overlays hashes as it
        # always did.
        if self.overlays:
            key["overlays"] = [{"source": s, "id": i} for s, i in self.overlays]
        return key

    def _variable_key(self) -> dict:
        key = {"source": self.variable_source, "id": self.variable_id}
        # Only present when used, so a plain manifest hashes as it always did.
        if self.per is not None:
            key["per"] = {"source": self.per[0], "id": self.per[1]}
        if self.change is not None:
            key["change"] = {"from": self.change[0], "to": self.change[1]}
        return key
