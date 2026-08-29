import json
import pathlib

import pytest

from mapsvc import registry, statistics
from mapsvc.manifest import Manifest
from mapsvc.models import HarvestResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def mini_geojson():
    return json.loads((FIXTURES / "mini.geojson").read_text())


@pytest.fixture
def mini_raw():
    return json.loads((FIXTURES / "mini_manifest.json").read_text())


def build_manifest(**overrides) -> Manifest:
    base = dict(
        region="testland", level="admin_0", variable_source="natural_earth",
        variable_id="GDP_MD", normalize=None, method="quantile", k=3,
        ramp="YlGnBu", projection="auto", missing="hatch",
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
                    "region": manifest.region,
                    "variable": manifest.variable_id, "unit": meta.unit,
                    "year": year, "normalize": manifest.normalize},
        dropped=dropped,
    )
