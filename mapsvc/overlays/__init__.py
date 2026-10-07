"""Overlays: feature data drawn above the basemap -- points, lines, flows.

Statistics are values keyed to polygons; an overlay is geometry of its own: an
earthquake's epicentre, a railway, an army's route. The manifest names *what*
to fetch -- `{"source": "usgs", "id": "start=2025-01-01;end=2026-01-01;
minmagnitude=6"}` -- and nothing about how to draw it. Whether a layer is
points, lines or a flow comes from the data; which attribute drives size and
which drives colour is declared by the source, so the encoding is derived the
same way a choropleth's ramp is.

Registering a source
--------------------
Write `mapsvc/overlays/<name>.py` with:

    CAPABILITIES = Capabilities(source=..., license=..., attribution=...,
                                description=..., id_help=...)
    def parse(overlay_id) -> str          # pure; canonical id or OverlayError
    def load(overlay_id, window) -> dict  # I/O; a layer, see below
    def search(query, limit) -> list      # searchable sources only

`parse` runs inside `manifest.validate`, before any I/O, so it must not fetch.

A layer is a plain dict, JSON-serialisable so it can be cached and handed to
the pure renderer:

    kind        "points" | "lines" | "flow" | "places"
    title       what the layer shows, for a title when there is no variable
    label       legend heading
    size        {"field", "label", "unit", "scale"} or None
                scale "magnitude": area ∝ 10^(M/2); "sqrt": area ∝ value;
                "linear": width ∝ value (flows)
    category    {"field", "label", "classes": [{"value", "label"}...]} or None
    features    points  {"id", "lon", "lat", "size", "category", "name"}
                lines   {"id", "coords": [[lon, lat], ...], "category"}
                flow    {"group", "order", "lon", "lat", "size", "category"}
                places  {"id", "lon", "lat", "name"}
    stats       {name: number} -- machine-readable facts stated on the map
    summary     one phrase for the subtitle
    encoding    one sentence for the footnote
    excluded    [{"count", "reason"}] -- features fetched but not drawn
    provenance  {"name", "url", "license", "attribution", "vintage"}
"""

import functools
from dataclasses import dataclass

_MODULES = ("usgs", "overpass")

# A map with more layers than this stops being readable before it stops
# being drawable.
MAX_OVERLAYS = 3

UNKNOWN_LICENSE = "licence not stated by source"


@dataclass(frozen=True)
class Capabilities:
    source: str
    license: str
    attribution: str
    description: str
    # How to write an id, shown to the agent and in validation errors.
    id_help: str
    searchable: bool = False


@dataclass(frozen=True)
class Window:
    """Where to fetch: what the manifest framed, and what the basemap covers.

    `extent` is the boundaries' own extent, so a `within`-only map still has a
    box to hand a source that only understands boxes.
    """

    bbox: tuple | None
    within: tuple | None
    extent: tuple
    # A plain name for the place, for titles: "France". None when the window
    # is not one named country.
    name: str | None = None


class OverlayError(ValueError):
    def __init__(self, message: str, field: str = "overlays"):
        super().__init__(message)
        self.field = field


@functools.lru_cache(maxsize=1)
def _providers() -> dict:
    from importlib import import_module

    loaded = {}
    for name in _MODULES:
        module = import_module(f"mapsvc.overlays.{name}")
        loaded[module.CAPABILITIES.source] = module
    return loaded


def sources() -> tuple:
    return tuple(_providers())


def provider(name: str):
    providers = _providers()
    if name not in providers:
        raise OverlayError(
            f"unknown overlay source {name!r}; expected one of "
            f"{', '.join(providers)}", "overlays.source")
    return providers[name]


def capabilities(name: str) -> Capabilities:
    return provider(name).CAPABILITIES


def all_capabilities() -> list:
    return [m.CAPABILITIES for m in _providers().values()]


def parse(source: str, overlay_id) -> str:
    """The canonical form of an overlay id, checked without any I/O."""
    if not isinstance(overlay_id, str) or not overlay_id.strip():
        raise OverlayError(f"{source} overlay needs an id: "
                           f"{capabilities(source).id_help}", "overlays.id")
    return provider(source).parse(overlay_id.strip())


def load(source: str, overlay_id: str, window: Window) -> dict:
    layer = provider(source).load(overlay_id, window)
    layer.setdefault("source", source)
    layer.setdefault("id", overlay_id)
    return layer


def search(source: str, query: str, limit: int = 8) -> list:
    module = provider(source)
    if not module.CAPABILITIES.searchable:
        raise OverlayError(f"{source} is not searchable; write its id directly",
                           "overlays.source")
    return module.search(query, limit)
