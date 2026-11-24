import json
import os
import pathlib

import pytest

from mapsvc import registry, statistics
from mapsvc.manifest import Manifest
from mapsvc.models import HarvestResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# The suite is offline: never ask Overture's STAC catalog which release is latest.
os.environ.setdefault("OVERTURE_RELEASE", "2026-10-21.0")
# ...and never fall back to Overture when a test makes Overpass fail; the
# fallback tests switch it on with recorded rows.
os.environ["MAPSVC_OVERTURE_FALLBACK"] = "0"
# ...and never go looking on the web when the agent refuses; the research
# tests switch it on with every dependency faked.
os.environ["MAPSVC_RESEARCH"] = "0"


@pytest.fixture
def mini_geojson():
    return json.loads((FIXTURES / "mini.geojson").read_text())


@pytest.fixture
def mini_raw():
    return json.loads((FIXTURES / "mini_manifest.json").read_text())


# The fixture geometry lives here; every offline test uses this window.
FIXTURE_BBOX = (-5.0, 35.0, 40.0, 60.0)


def build_manifest(**overrides) -> Manifest:
    base = dict(
        bbox=FIXTURE_BBOX, level="admin_0",
        variable_source="natural_earth", variable_id="GDP_MD",
        # These tests stub Natural Earth's file loader, so they pin the basemap
        # rather than inheriting the Overture default and reaching for S3.
        basemap_source="natural_earth", basemap_detail="simplified",
    )
    base.update(overrides)
    return Manifest(**base)


def build_result(geojson, manifest) -> HarvestResult:
    """A HarvestResult assembled without touching the network.

    Mirrors what harvest.py produces so the renderer can be tested in isolation.
    """
    meta = statistics.variables_for(manifest.level, manifest.variable_source)[manifest.variable_id]
    rows, dropped, years = [], [], set()
    for feature in geojson["features"]:
        props = feature["properties"]
        gid = props[registry.id_property(manifest.level)]
        value = props.get(manifest.variable_id)
        if value is None or value == -99 or (meta.level == "count" and value == 0):
            dropped.append({"id": gid, "reason": "no_data", "geometry": feature["geometry"]})
            continue
        rows.append({"id": gid, "geometry": feature["geometry"], "value": value})
        if meta.year_col:
            years.add(props[meta.year_col])
    year = None
    if years:
        year = str(min(years)) if len(years) == 1 else f"{min(years)}–{max(years)}"
    return HarvestResult(
        rows=rows,
        provenance={"source": registry.SOURCE_NAME, "vintage": registry.SOURCE_VINTAGE,
                    "scale": f"1:{registry.scale_for(manifest.level)}",
                    "bbox": list(manifest.bbox),
                    "variable": manifest.variable_id, "unit": meta.unit,
                    "measurement": meta.level,
                    "year": year},
        dropped=dropped,
    )


@pytest.fixture(autouse=True)
def _no_context_fetch(monkeypatch):
    """`within` maps fetch Natural Earth countries as context land. Offline
    tests must not; a test of the context layer installs its own."""
    from mapsvc import harvest
    monkeypatch.setattr(harvest, "context_countries", lambda: [])
    # Sea names likewise: a test of them installs its own marine areas.
    monkeypatch.setattr(harvest, "marine_areas", lambda: [])
    # A landfall's region name comes from Natural Earth admin-1; offline tests
    # install their own.
    from mapsvc.overlays import web
    monkeypatch.setattr(web, "_landfall_region", lambda lon, lat: "")
