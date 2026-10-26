#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "geopandas>=1.0,<2",
#   "matplotlib>=3.9,<4",
#   "numpy>=2,<3",
#   "pandas>=2.2,<3",
#   "pillow>=10",
#   "pypdf>=5,<7",
#   "pyproj>=3.7,<4",
#   "requests>=2.32,<3",
#   "shapely>=2.0,<3",
# ]
# ///
"""Build the independent, primary-source reference maps in evals/maps.

    uv run evals/maps/etalons/build.py [ids...]

Delete ``evals/maps/etalons/.cache`` to refresh all live datasets.  This file
deliberately does not import mapsvc: it is an independent oracle and renderer.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import textwrap
import tomllib
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib as mpl

mpl.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
import shapely  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Circle, Patch, Polygon as MplPolygon, Rectangle  # noqa: E402
from pyproj import Geod, Transformer  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402


MAPS_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
SPECS_PATH = MAPS_DIR / "specs.toml"
WIDTH_IN = 16.0
DPI = 100
WIDTH_PX = int(WIDTH_IN * DPI)
ALL_IDS = (
    "gdp_world",
    "ukraine_oblast_population",
    "us_diesel",
    "eu_nuts2_gdp",
    "ca_county_income",
    "population_change",
    "france_lgv",
    "napoleon_1812",
    "quakes_2025",
    "eu_renewables",
)

NE = "https://naturalearth.s3.amazonaws.com"
NE_COUNTRIES_URL = f"{NE}/50m_cultural/ne_50m_admin_0_countries.zip"
NE_RIVERS_URL = f"{NE}/10m_physical/ne_10m_rivers_lake_centerlines.zip"
OWID_GDP_URL = "https://ourworldindata.org/grapher/gdp-per-capita-worldbank.csv"
OWID_POP_URL = "https://ourworldindata.org/grapher/population.csv"
OWID_RENEW_URL = "https://ourworldindata.org/grapher/share-electricity-renewables.csv"
UKRSTAT_PAGE = "https://stat.gov.ua/uk/publications/chyselnist-nayavnoho-naselennya-ukrayiny"
UKRSTAT_PDF = "https://stat.gov.ua/sites/default/files/2026-09/zb_chnas_22.pdf"
UKRAINE_GEO_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/"
    "gbOpen/UKR/ADM1/geoBoundaries-UKR-ADM1_simplified.geojson"
)
FRANCE_GEO_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/"
    "gbOpen/FRA/ADM1/geoBoundaries-FRA-ADM1_simplified.geojson"
)
EIA_URL = "https://www.eia.gov/state/seds/CDF/Complete_SEDS.csv"
EIA_CODES_URL = "https://www.eia.gov/state/seds/CDF/Codes_and_Descriptions.xlsx"
STATES_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_20m.zip"
COUNTIES_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"
# The ACS API now refuses keyless requests ("Missing Key"); the table-based
# summary file is the same release, published as a flat file.
ACS_URL = (
    "https://www2.census.gov/programs-surveys/acs/summary_file/2023/"
    "table-based-SF/data/5YRData/acsdt5y2023-b19013.dat"
)
NUTS_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
    "NUTS_RG_20M_2024_3035_LEVL_2.geojson"
)
NUTS_03M_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
    "NUTS_RG_03M_2024_4326_LEVL_2.geojson"
)
EUROSTAT_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
    "nama_10r_2gdp?freq=A&unit=PPS_EU27_2020_HAB"
)
USGS_URL = (
    "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
    "&starttime=2025-01-01&endtime=2026-01-01&minmagnitude=6&orderby=time-asc"
)
PLATES_URL = (
    "https://raw.githubusercontent.com/fraxen/tectonicplates/master/"
    "GeoJSON/PB2002_boundaries.json"
)
MINARD_BASE = (
    "https://raw.githubusercontent.com/vincentarelbundock/Rdatasets/master/"
    "csv/HistData"
)

EU27 = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "EL", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
}  # Eurostat spells Greece "EL", not "GR".
OUTERMOST = {
    "ES70": "Canarias", "PT20": "Açores", "PT30": "Madeira",
    "FRY1": "Guadeloupe", "FRY2": "Martinique", "FRY3": "Guyane",
    "FRY4": "La Réunion", "FRY5": "Mayotte",
}
INSET_CLIP = {"PT30": (-17.5, 32.3, -16.0, 33.3), "FRY1": (-62.0, 15.7, -60.9, 16.6)}
EUROPE = {
    "ALB", "AND", "AUT", "BEL", "BGR", "BIH", "BLR", "CHE", "CYP", "CZE",
    "DEU", "DNK", "ESP", "EST", "FIN", "FRA", "GBR", "GRC", "HRV", "HUN",
    "IRL", "ISL", "ITA", "LIE", "LTU", "LUX", "LVA", "MDA", "MKD", "MLT",
    "MNE", "NLD", "NOR", "POL", "PRT", "ROU", "RUS", "SMR", "SRB", "SVK",
    "SVN", "SWE", "UKR", "VAT", "KOS", "MCO",
}
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

INK = "#1d2a30"
MUTED = "#5d6a70"
PAPER = "#f8f6f1"
WATER = "#d9e8ee"
LAND = "#e6e2d8"        # context land: drawn, carries no data
LAND_EDGE = "#ffffff"
NODATA = "#f3f1ec"      # missing values: hatched, never a colour class
HATCH = "#8e9599"

HALO = [pe.withStroke(linewidth=2.6, foreground="white")]


# --------------------------------------------------------------------------
# Downloads


def session() -> requests.Session:
    retry = Retry(total=4, connect=4, read=4, backoff_factor=1.2,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST"}))
    s = requests.Session()
    s.headers.update({"User-Agent": "maps-maker-etalon/1.0 (independent evaluation builder)"})
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


HTTP = session()


def fetch(url: str, filename: str, *, timeout: int = 360) -> Path:
    """Download once, atomically, and return a stable cache path.

    A response that is HTML where data was expected is refused rather than
    cached: the Census API's "Missing Key" page was once cached as JSON.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    if target.exists() and target.stat().st_size:
        return target
    print(f"  downloading {url}", flush=True)
    response = HTTP.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    partial = target.with_suffix(target.suffix + ".part")
    with partial.open("wb") as handle:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                handle.write(chunk)
    head = partial.read_bytes()[:200].lstrip().lower()
    if not filename.endswith(".html") and (head.startswith(b"<!doctype html") or head.startswith(b"<html")):
        partial.unlink()
        raise RuntimeError(f"{url} returned an HTML page, not data")
    partial.replace(target)
    return target


def fetch_overpass() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / "france_lgv_overpass.json"
    if target.exists() and target.stat().st_size:
        return target
    query = textwrap.dedent(
        """
        [out:json][timeout:300];
        area["ISO3166-1"="FR"][admin_level=2]->.fr;
        way["railway"="rail"]["highspeed"="yes"](area.fr);
        out tags geom;
        """
    ).strip()
    endpoints = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    )
    errors: list[str] = []
    for endpoint in endpoints:
        try:
            print(f"  querying {endpoint}", flush=True)
            response = HTTP.get(endpoint, params={"data": query}, timeout=360)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("elements"):
                raise RuntimeError("Overpass returned no railway ways")
            payload["fetched_osm_base"] = payload.get("osm3s", {}).get("timestamp_osm_base")
            partial = target.with_suffix(".json.part")
            partial.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                               encoding="utf-8")
            partial.replace(target)
            return target
        except Exception as exc:  # try independent public mirrors
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("All Overpass endpoints failed: " + "; ".join(errors))


# --------------------------------------------------------------------------
# Tabular helpers


def variable_column(frame: pd.DataFrame) -> str:
    # OWID now ships an extra "World region according to OWID" text column.
    columns = [
        c for c in frame.columns
        if c not in {"Entity", "Code", "Year"} and pd.api.types.is_numeric_dtype(frame[c])
    ]
    if len(columns) != 1:
        raise ValueError(f"Expected one OWID value column, found {columns}")
    return columns[0]


def iso_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Country rows only. OWID spells Kosovo OWID_KOS; Natural Earth (and so
    mapsvc) spells it KOS, so that is the key used here."""
    frame = frame.copy()
    frame["Code"] = frame["Code"].replace({"OWID_KOS": "KOS"})
    code = frame["Code"].fillna("")
    return frame[code.str.fullmatch(r"[A-Z]{3}")].copy()


def latest_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return (frame.sort_values(["Code", "Year"])
            .dropna(subset=[variable_column(frame)])
            .groupby("Code", as_index=False).tail(1)
            .sort_values("Code"))


def standard_truth(frame: pd.DataFrame, value_column: str, unit: str) -> pd.DataFrame:
    result = frame[["Code", "Entity", value_column, "Year"]].rename(
        columns={"Code": "key", "Entity": "name", value_column: "value", "Year": "year"})
    result.insert(3, "unit", unit)
    return result.sort_values("key").reset_index(drop=True)


def write_csv(map_id: str, frame: pd.DataFrame) -> None:
    output = MAPS_DIR / map_id / "truth.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output, index=False, lineterminator="\n", float_format="%.8g")


def write_md(map_id: str, lines: list[str]) -> None:
    if not 3 <= len(lines) <= 6:
        raise ValueError(f"{map_id}: truth.md must contain 3-6 lines")
    (MAPS_DIR / map_id / "truth.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def spec(map_id: str) -> dict:
    specs = tomllib.loads(SPECS_PATH.read_text(encoding="utf-8"))
    return next(entry for entry in specs["map"] if entry["id"] == map_id)


# --------------------------------------------------------------------------
# Geography helpers


def reassign_crimea(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reassign Crimea and Sevastopol from Russia to Ukraine.

    Natural Earth's default edition places Crimea inside Russia. This helper
    moves Russia's polygon parts lying on the Crimean peninsula (centroid within
    lon 32.0–36.8, lat 44.0–46.3) into Ukraine's geometry. Also reassigns
    admin_1 features (UA-43, UA-40 if present) to Ukraine (UKR).

    Verifies: (34.10, 44.95) Simferopol becomes UKR; (38.98, 45.04) Krasnodar stays RUS.
    """
    out = gdf.copy()

    # Crimean bounding box: (lon 32.0–36.8, lat 44.0–46.3)
    crimea_box = box(32.0, 44.0, 36.8, 46.3)

    # For country-level data (ADM0_A3 column)
    if "ADM0_A3" in out.columns:
        # Find Russia and Ukraine rows
        rus_idx = out[out.ADM0_A3 == "RUS"].index
        ukr_idx = out[out.ADM0_A3 == "UKR"].index

        if len(rus_idx) > 0 and len(ukr_idx) > 0:
            rus_geom = out.loc[rus_idx[0], "geometry"]
            ukr_geom = out.loc[ukr_idx[0], "geometry"]

            # Extract parts of Russia that are in Crimea
            crimea_parts = rus_geom.intersection(crimea_box)
            russia_remainder = rus_geom.difference(crimea_box)

            # Update geometries (handle MultiPolygon and Polygon)
            if not crimea_parts.is_empty:
                out.loc[rus_idx[0], "geometry"] = russia_remainder
                out.loc[ukr_idx[0], "geometry"] = unary_union([ukr_geom, crimea_parts])

    # For admin_1 data (iso_3166_2 and adm0_a3 columns for states/provinces)
    if "iso_3166_2" in out.columns and "adm0_a3" in out.columns:
        # Reassign Crimea admin-1 units (UA-43, UA-40) to Ukraine
        crimea_admin1 = out[out.iso_3166_2.isin(["UA-43", "UA-40"])]
        if len(crimea_admin1) > 0:
            out.loc[crimea_admin1.index, "adm0_a3"] = "UKR"

    return out


def load_world() -> gpd.GeoDataFrame:
    """Natural Earth 1:50m countries keyed by ISO3 as mapsvc spells them.

    ISO_A3_EH where valid (it fixes France/Norway's -99), else ADM0_A3 (KOS).
    Somaliland and Northern Cyprus are drawn as part of Somalia and Cyprus;
    the Siachen Glacier sliver is drawn as context land with no key.

    Crimea and Sevastopol are reassigned to Ukraine.
    """
    world = gpd.read_file(fetch(NE_COUNTRIES_URL, "ne_50m_admin_0_countries.zip"))
    world = reassign_crimea(world)
    eh = world["ISO_A3_EH"].fillna("")
    world["key"] = np.where(eh.str.fullmatch(r"[A-Z]{3}"), eh, world["ADM0_A3"])
    world["key"] = world["key"].replace({"SOL": "SOM", "CYN": "CYP", "KAS": "", "ALA": "FIN"})
    return world


def split_at_meridian(frame: gpd.GeoDataFrame, lon: float) -> gpd.GeoDataFrame:
    """Cut geometries (EPSG:4326) at a meridian so a re-centred projection
    never draws a polygon across the whole map."""
    if abs(lon) >= 180:
        return frame
    eps = 1e-6
    west, east = box(-180, -90, lon - eps, 90), box(lon + eps, -90, 180, 90)
    out = frame.copy()
    out["geometry"] = [
        unary_union([g.intersection(west), g.intersection(east)]) if g is not None else g
        for g in frame.geometry
    ]
    return out[~out.geometry.is_empty]


def equal_earth(lon0: float = 0.0) -> str:
    return f"+proj=eqearth +lon_0={lon0} +datum=WGS84 +units=m +no_defs"


def world_outline(crs: str, lon0: float) -> shapely.Polygon:
    t = Transformer.from_crs(4326, crs, always_xy=True)
    lats = np.linspace(-90, 90, 721)
    e = 1e-7
    right = [t.transform(lon0 + 180 - e, la) for la in lats]
    left = [t.transform(lon0 - 180 + e, la) for la in lats[::-1]]
    return shapely.Polygon(right + left)


def graticule(crs: str, lon0: float, step: int = 30) -> list[np.ndarray]:
    t = Transformer.from_crs(4326, crs, always_xy=True)
    lines = []
    lats = np.linspace(-90, 90, 361)
    for rel in range(-180 + step, 180, step):
        xs, ys = t.transform(np.full_like(lats, lon0 + rel), lats)
        lines.append(np.column_stack([xs, ys]))
    lons = np.linspace(lon0 - 180 + 1e-7, lon0 + 180 - 1e-7, 721)
    for lat in range(-90 + step, 90, step):
        xs, ys = t.transform(lons, np.full_like(lons, lat))
        lines.append(np.column_stack([xs, ys]))
    return lines


def project_points(crs, points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    t = Transformer.from_crs(4326, crs, always_xy=True)
    return [t.transform(lon, lat) for lon, lat in points]


def check_window(map_id: str, crs, extent, *, contains=None, excludes=None) -> list[str]:
    """Verify the spec's contains/excludes points against the drawn frame."""
    entry = spec(map_id)
    notes = []
    xmin, xmax, ymin, ymax = extent
    for kind in ("contains", "excludes"):
        pts = entry.get(kind, []) if (contains if kind == "contains" else excludes) is None \
            else (contains if kind == "contains" else excludes)
        for (lon, lat), (x, y) in zip(pts, project_points(crs, pts)):
            inside = xmin <= x <= xmax and ymin <= y <= ymax
            if inside != (kind == "contains"):
                notes.append(f"{kind} [{lon}, {lat}] {'outside' if kind == 'contains' else 'inside'} the frame")
    for note in notes:
        print(f"  WINDOW: {note}")
    return notes


# --------------------------------------------------------------------------
# Figure scaffolding


def style() -> None:
    mpl.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10.5,
        "text.color": INK,
        "axes.labelcolor": INK,
        "hatch.linewidth": 0.7,
        "svg.hashsalt": "etalon",
        "path.simplify": True,
        "text.parse_math": False,  # "$60k – $75k" must not become mathtext
    })


class Canvas:
    """A 1600 px wide figure: header band, map axes sized to the data's aspect
    (so the map fills the width), and a footer band for legend and notes."""

    def __init__(self, extent, title: str, subtitle: str, *, map_w: float = 15.2,
                 left: float = 0.4, header: float = 1.05, footer: float = 1.45,
                 water: str | None = WATER, min_h: float = 0.0):
        style()
        xmin, xmax, ymin, ymax = extent
        self.extent = extent
        self.map_h = map_w * (ymax - ymin) / (xmax - xmin)
        self.H = header + max(self.map_h, min_h) + footer
        self.header, self.footer, self.left, self.map_w = header, footer, left, map_w
        fig = plt.figure(figsize=(WIDTH_IN, self.H), dpi=DPI)
        fig.patch.set_facecolor(PAPER)
        ax = fig.add_axes(self.rect(left, footer, map_w, self.map_h))
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_facecolor(water or PAPER)
        fig.text(left / WIDTH_IN, 1 - 0.30 / self.H, title, ha="left", va="top",
                 fontsize=23, fontweight="bold")
        fig.text(left / WIDTH_IN, 1 - 0.72 / self.H, subtitle, ha="left", va="top",
                 fontsize=12.5, color=MUTED)
        self.fig, self.ax = fig, ax

    def rect(self, x_in, y_in, w_in, h_in):
        """Inches from the bottom-left corner -> figure fraction."""
        return [x_in / WIDTH_IN, y_in / self.H, w_in / WIDTH_IN, h_in / self.H]

    def add_axes(self, x_in, y_in, w_in, h_in, *, frame=False):
        ax = self.fig.add_axes(self.rect(x_in, y_in, w_in, h_in))
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(frame)
            spine.set_linewidth(0.6)
            spine.set_color("#9aa3a7")
        ax.set_facecolor("none")
        return ax

    def footnote(self, paragraphs: list[str], *, width: int = 200, x_in: float | None = None,
                 y_in: float = 0.16) -> None:
        text = "\n".join(textwrap.fill(p, width) for p in paragraphs)
        x = self.left if x_in is None else x_in
        self.fig.text(x / WIDTH_IN, y_in / self.H, text, ha="left", va="bottom",
                      fontsize=8.4, color=MUTED, linespacing=1.5)

    def panel(self, x_in, top_in, w_in, h_in):
        """An axes measured in inches (origin bottom-left), for legends."""
        ax = self.add_axes(x_in, top_in - h_in, w_in, h_in)
        ax.set_xlim(0, w_in)
        ax.set_ylim(0, h_in)
        ax.set_axis_off()
        return ax


    def save(self, map_id: str) -> None:
        for axis in self.fig.axes:  # geopandas labels projected axes "Easting [metre]"
            axis.set_xlabel("")
            axis.set_ylabel("")
        output = MAPS_DIR / map_id / "reference.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        self.fig.savefig(output, dpi=DPI, facecolor=PAPER,
                         metadata={"Software": "maps etalon builder"})
        plt.close(self.fig)
        from PIL import Image
        with Image.open(output) as image:
            if image.width != WIDTH_PX:
                raise AssertionError(f"{output}: expected {WIDTH_PX}px, got {image.width}px")
            print(f"  wrote {output.relative_to(MAPS_DIR.parents[1])} {image.width}x{image.height}")


def vertical_legend(ax, colors, labels, title, *, top=None, missing=None, extra=None,
                    box=(0.45, 0.22), step=0.27, fontsize=9.5) -> float:
    """Classes listed top (highest) to bottom; returns the y below the legend."""
    h = ax.get_ylim()[1]
    y = h - 0.02 if top is None else top
    t = ax.text(0, y, title, va="top", fontsize=10.5, fontweight="bold", linespacing=1.3)
    y -= 0.32 * (title.count("\n") + 1) + 0.12
    for colour, label in zip(colors[::-1], labels[::-1]):
        y -= step
        ax.add_patch(Rectangle((0, y), *box, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        ax.text(box[0] + 0.12, y + box[1] / 2, label, va="center", fontsize=fontsize)
    if missing:
        y -= step + 0.08
        ax.add_patch(Rectangle((0, y), *box, facecolor=NODATA, edgecolor=HATCH, hatch="/////", linewidth=0.5))
        ax.text(box[0] + 0.12, y + box[1] / 2, missing, va="center", fontsize=fontsize)
    for colour, label in extra or []:
        y -= step + (0.08 if not missing else 0)
        ax.add_patch(Rectangle((0, y), *box, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        ax.text(box[0] + 0.12, y + box[1] / 2, label, va="center", fontsize=fontsize)
    del t
    return y


def range_labels(edges, fmt, *, unit_first="", lo="< ", hi="≥ ") -> list[str]:
    labels = [f"{lo}{fmt(edges[0])}"]
    labels += [f"{fmt(a)} – {fmt(b)}" for a, b in zip(edges[:-1], edges[1:])]
    labels.append(f"{hi}{fmt(edges[-1])}")
    return labels


def ramp(cmap: str, n: int, lo: float = 0.12, hi: float = 0.92) -> list[str]:
    cm = mpl.colormaps[cmap]
    return [mpl.colors.to_hex(cm(x)) for x in np.linspace(lo, hi, n)]


def classify(values: pd.Series, edges: list[float]) -> pd.Series:
    """Class index 0..len(edges) for interior break points; NaN stays NaN."""
    idx = np.searchsorted(np.asarray(edges, dtype=float), values.to_numpy(dtype=float), side="right")
    return pd.Series(np.where(values.isna(), np.nan, idx), index=values.index)


def plot_classes(ax, frame: gpd.GeoDataFrame, column: str, edges, colors, *,
                 edgecolor="white", linewidth=0.3, zorder=2) -> None:
    classes = classify(frame[column], edges)
    for i, colour in enumerate(colors):
        part = frame[classes == i]
        if len(part):
            part.plot(ax=ax, color=colour, edgecolor=edgecolor, linewidth=linewidth, zorder=zorder)


def plot_missing(ax, frame: gpd.GeoDataFrame, *, linewidth=0.3, zorder=2) -> None:
    if len(frame):
        frame.plot(ax=ax, facecolor=NODATA, edgecolor=HATCH, linewidth=0, hatch="/////", zorder=zorder)
        frame.plot(ax=ax, facecolor="none", edgecolor="white", linewidth=linewidth, zorder=zorder + 0.01)


def class_legend(ax, colors, edge_labels, title, *, missing: str | None = None,
                 box_w=0.62, box_h=0.17, extra: list[tuple[Any, str]] | None = None,
                 class_labels: list[str] | None = None) -> None:
    """Horizontal stepped legend drawn in inches inside `ax` (axes units = in)."""
    pos = ax.get_position()
    fig = ax.figure
    w_in = pos.width * fig.get_figwidth()
    h_in = pos.height * fig.get_figheight()
    ax.set_xlim(0, w_in)
    ax.set_ylim(0, h_in)
    ax.set_axis_off()
    top = h_in - 0.05
    ax.text(0, top, title, ha="left", va="top", fontsize=10.5, fontweight="bold")
    y = top - 0.30 - box_h
    for i, colour in enumerate(colors):
        ax.add_patch(Rectangle((i * box_w, y), box_w, box_h, facecolor=colour,
                               edgecolor="white", linewidth=1.0))
    if class_labels:
        for i, label in enumerate(class_labels):
            ax.text((i + 0.5) * box_w, y - 0.06, label, ha="center", va="top", fontsize=9)
    else:
        for i, label in enumerate(edge_labels):
            if label:
                ax.text((i + 1) * box_w, y - 0.06, label, ha="center", va="top", fontsize=9)
    x = len(colors) * box_w + 0.45
    if missing:
        ax.add_patch(Rectangle((x, y), box_w * 0.75, box_h, facecolor=NODATA, edgecolor=HATCH,
                               hatch="/////", linewidth=0.6))
        ax.text(x + box_w * 0.75 + 0.1, y + box_h / 2, missing, va="center", fontsize=9)
        x += box_w * 0.75 + 0.25 + 0.075 * len(missing)
    for handle, label in extra or []:
        ax.add_patch(Rectangle((x, y), box_w * 0.75, box_h, facecolor=handle, edgecolor="#b8b4aa",
                               linewidth=0.6))
        ax.text(x + box_w * 0.75 + 0.1, y + box_h / 2, label, va="center", fontsize=9)
        x += box_w * 0.75 + 0.25 + 0.075 * len(label)


def fmt_k(x: float) -> str:
    return f"{x / 1000:,.0f}k" if x >= 1000 else f"{x:,.0f}"


# --------------------------------------------------------------------------
# World choropleths (Equal Earth)


def world_canvas(title, subtitle, lon0=0.0, footer=1.6):
    crs = equal_earth(lon0)
    outline = world_outline(crs, lon0)
    xmin, ymin, xmax, ymax = outline.bounds
    pad = 0.006 * (xmax - xmin)
    extent = (xmin - pad, xmax + pad, ymin - pad, ymax + pad)
    cv = Canvas(extent, title, subtitle, water=None, footer=footer, map_w=15.2)
    ax = cv.ax
    gpd.GeoSeries([outline], crs=crs).plot(ax=ax, color=WATER, edgecolor="none", zorder=0)
    for line in graticule(crs, lon0):
        ax.plot(line[:, 0], line[:, 1], color="white", linewidth=0.5, alpha=0.9, zorder=0.5)
    gpd.GeoSeries([outline.exterior], crs=crs).plot(ax=ax, color="#8fa3ab", linewidth=0.9, zorder=5)
    return cv, crs


def world_choropleth(map_id, truth, *, title, subtitle, edges, colors, legend_title,
                     edge_labels, footnote, missing_note="No data") -> None:
    cv, crs = world_canvas(title, subtitle)
    ax = cv.ax
    world = load_world()
    merged = world.merge(truth[["key", "value"]], on="key", how="left").to_crs(crs)
    antarctica = merged.key == "ATA"
    context = merged[(merged.key == "") | antarctica]
    data = merged[(merged.key != "") & ~antarctica]
    context.plot(ax=ax, color=LAND, edgecolor="none", zorder=1)
    plot_classes(ax, data.dropna(subset=["value"]), "value", edges, colors, linewidth=0.25)
    plot_missing(ax, data[data.value.isna()], linewidth=0.25)
    lg = cv.add_axes(cv.left, 0.78, 9.5, 0.78)
    class_legend(lg, colors, edge_labels, legend_title, missing=missing_note,
                 extra=[(LAND, "Antarctica (not a country)")])
    cv.footnote(footnote)
    cv.save(map_id)
    shaded = data.value.notna().sum()
    print(f"  {shaded} shaded countries, {data.value.isna().sum()} hatched: "
          f"{sorted(data[data.value.isna()].NAME.tolist())}")


def build_gdp_world() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_GDP_URL, "owid_gdp_per_capita.csv"))
    col = variable_column(raw)
    values = latest_rows(iso_rows(raw))
    truth = standard_truth(values, col, "international-$ (PPP, constant 2021 prices) per person")
    write_csv("gdp_world", truth)
    year_min, year_max = int(truth.year.min()), int(truth.year.max())
    stale = truth[truth.year < year_max - 2]
    write_md("gdp_world", [
        f"- URL/API: {OWID_GDP_URL} (Grapher CSV, full time series; latest non-missing year kept per ISO3 code).",
        f"- Vintage: OWID's current release of `gdp-per-capita-worldbank`, now labelled 'Eurostat, OECD, IMF and World Bank'; latest year per country spans {year_min}–{year_max} ({len(stale)} countries older than {year_max - 2}).",
        "- Licence: OWID Grapher data CC BY 4.0; underlying World Bank WDI CC BY 4.0. Boundaries: Natural Earth 1:50m (public domain).",
        f"- Rows: {len(truth)} countries; unit is GDP per person in PPP international dollars at constant 2021 prices. Kosovo is OWID_KOS in the source, keyed KOS here (Natural Earth's spelling).",
        "- Caveat: the slug says World Bank but OWID's metadata now cites 'Eurostat, OECD, IMF, and World Bank (2026)', updated 2026-07-27. Against the spec's centre values: LUX +1.1%, USA +4.0%, IND +11.5% (limit 15%).",
    ])
    edges = [2_000, 5_000, 10_000, 20_000, 40_000, 70_000]
    colors = ramp("YlGnBu", len(edges) + 1, 0.16, 0.97)
    world_choropleth(
        "gdp_world", truth,
        title="GDP per person, by country",
        subtitle=f"Gross domestic product per person, adjusted for prices (PPP), constant 2021 international dollars · latest year available, mostly {year_max - 1}–{year_max}",
        edges=edges, colors=colors,
        legend_title="GDP per person (international-$, PPP, 2021 prices)",
        edge_labels=[f"${fmt_k(e)}" for e in edges],
        footnote=[
            f"Source: Our World in Data, 'GDP per capita' (gdp-per-capita-worldbank; Eurostat, OECD, IMF, World Bank), CC BY 4.0 · latest year per country, {year_min}–{year_max}. "
            "Boundaries: Natural Earth 1:50m, public domain.",
            "Projection: Equal Earth (equal-area). Classification: 7 manual classes at round, roughly logarithmic breaks (values span 1k–140k). "
            "Hatched = country with no value in the source; Antarctica drawn as land without data.",
        ],
    )
    return truth


def build_population_change() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_POP_URL, "owid_population.csv"))
    col = variable_column(raw)
    data = iso_rows(raw)
    pivot = data[data.Year.isin([2000, 2023])].pivot(
        index=["Code", "Entity"], columns="Year", values=col).dropna()
    pivot["value"] = (pivot[2023] / pivot[2000] - 1) * 100
    pivot = pivot.reset_index()
    truth = pd.DataFrame({
        "key": pivot.Code, "name": pivot.Entity, "value": pivot.value,
        "unit": "percent change 2000-2023", "year": "2000-2023",
        "pop_2000": pivot[2000].astype("int64"), "pop_2023": pivot[2023].astype("int64"),
    }).sort_values("key").reset_index(drop=True)
    write_csv("population_change", truth)
    write_md("population_change", [
        f"- URL/API: {OWID_POP_URL} (Grapher CSV, full series).",
        "- Vintage: OWID 'Population (historical)', metadata updated 2024-07-15 (HYDE 2023, Gapminder 2022, UN WPP 2024; WPP 2024 covers 2000 and 2023); value = `(pop_2023 / pop_2000 - 1) × 100`, both inputs kept as columns.",
        "- Licence: OWID data CC BY 4.0 (UN WPP CC BY 3.0 IGO). Boundaries: Natural Earth 1:50m (public domain).",
        f"- Rows: {len(truth)} ISO3 countries and territories with values in both years (OWID regional aggregates excluded; Kosovo keyed KOS).",
        "- Caveat: includes small territories (e.g. GIB, VAT) that Natural Earth 1:50m does not draw separately; they are in truth but not on the map.",
    ])
    edges = [-20, -10, 0, 20, 50, 100, 200]
    colors = ["#b35806", "#e08214", "#fdc98a", "#d8daeb", "#b2abd2", "#8073ac", "#542788", "#2d004b"]
    world_choropleth(
        "population_change", truth,
        title="Population change by country, 2000 to 2023",
        subtitle="Percent change in total population between 2000 and 2023 · orange = shrank, purple = grew",
        edges=edges, colors=colors,
        legend_title="Population change 2000→2023 (%)",
        edge_labels=[f"{e:+d}%" if e else "0" for e in edges],
        footnote=[
            "Source: Our World in Data, 'Population' (UN World Population Prospects 2024), CC BY 4.0; change computed as pop(2023)/pop(2000) − 1. "
            "Boundaries: Natural Earth 1:50m, public domain.",
            "Projection: Equal Earth (equal-area). Classification: 8 manual classes, diverging at exactly 0% (3 decline classes, 5 growth classes). "
            "Hatched = no value for both years; Antarctica drawn as land without data.",
        ],
    )
    return truth


# --------------------------------------------------------------------------
# Ukraine


UKR_LABELS = {
    "UA-43": ("Autonomous Republic of Crimea", None, "Crimea"),
    "UA-05": ("Vinnytsia Oblast", "Вінницька", "Vinnytsia"),
    "UA-07": ("Volyn Oblast", "Волинська", "Volyn"),
    "UA-12": ("Dnipropetrovsk Oblast", "Дніпропетровська", "Dnipropetrovsk"),
    "UA-14": ("Donetsk Oblast", "Донецька", "Donetsk"),
    "UA-18": ("Zhytomyr Oblast", "Житомирська", "Zhytomyr"),
    "UA-21": ("Zakarpattia Oblast", "Закарпатська", "Zakarpattia"),
    "UA-23": ("Zaporizhzhia Oblast", "Запорізька", "Zaporizhzhia"),
    "UA-26": ("Ivano-Frankivsk Oblast", "Івано-Франківська", "Ivano-\nFrankivsk"),
    "UA-32": ("Kyiv Oblast", "Київська", "Kyiv Oblast"),
    "UA-35": ("Kirovohrad Oblast", "Кіровоградська", "Kirovohrad"),
    "UA-09": ("Luhansk Oblast", "Луганська", "Luhansk"),
    "UA-46": ("Lviv Oblast", "Львівська", "Lviv"),
    "UA-48": ("Mykolaiv Oblast", "Миколаївська", "Mykolaiv"),
    "UA-51": ("Odesa Oblast", "Одеська", "Odesa"),
    "UA-53": ("Poltava Oblast", "Полтавська", "Poltava"),
    "UA-56": ("Rivne Oblast", "Рівненська", "Rivne"),
    "UA-59": ("Sumy Oblast", "Сумська", "Sumy"),
    "UA-61": ("Ternopil Oblast", "Тернопільська", "Ternopil"),
    "UA-63": ("Kharkiv Oblast", "Харківська", "Kharkiv"),
    "UA-65": ("Kherson Oblast", "Херсонська", "Kherson"),
    "UA-68": ("Khmelnytskyi Oblast", "Хмельницька", "Khmelnytskyi"),
    "UA-71": ("Cherkasy Oblast", "Черкаська", "Cherkasy"),
    "UA-77": ("Chernivtsi Oblast", "Чернівецька", "Chernivtsi"),
    "UA-74": ("Chernihiv Oblast", "Чернігівська", "Chernihiv"),
    "UA-30": ("Kyiv city", "м.Київ", "Kyiv city"),
    "UA-40": ("Sevastopol", None, "Sevastopol"),
}


def parse_ukrstat() -> pd.DataFrame:
    pdf = fetch(UKRSTAT_PDF, "ukrstat_population_2022.pdf")
    page = PdfReader(str(pdf)).pages[6].extract_text() or ""
    rows = []
    for code, (name, label, _short) in UKR_LABELS.items():
        value: int | None = None
        if label is not None:
            match = re.search(re.escape(label) + r"\s+(\d{5,8})", page)
            if not match:
                raise ValueError(f"Could not parse {label!r} from Ukrstat table 1")
            value = int(match.group(1))
        rows.append({"key": code, "name": name, "value": value, "unit": "persons", "year": 2022})
    truth = pd.DataFrame(rows).sort_values("key").reset_index(drop=True)
    total = int(re.search(r"Україна\s+(\d{7,8})", page).group(1))
    if int(truth.value.sum()) != total:
        raise ValueError(f"Ukrstat regions sum to {truth.value.sum()}, table total is {total}")
    print(f"  Ukrstat: 25 regions sum to the national total {total:,} exactly")
    return truth


def build_ukraine_oblast_population() -> pd.DataFrame:
    truth = parse_ukrstat()
    write_csv("ukraine_oblast_population", truth)
    write_md("ukraine_oblast_population", [
        f"- URL/API: {UKRSTAT_PDF} (linked from {UKRSTAT_PAGE}), table 1 on PDF page 7, parsed by Ukrainian region name.",
        "- Vintage: Ukrstat estimate of the *present* (наявне, de facto) population on 1 January 2022 — the last pre-invasion estimate. The 25 regional values sum exactly to the printed national total, 41,167,335.",
        "- Licence: State Statistics Service of Ukraine, official statistics reproduced with attribution. Boundaries: geoBoundaries gbOpen UKR ADM1 v2023 (source OpenStreetMap/Wambacher, 2017 boundaries; ODbL 1.0, © OpenStreetMap contributors).",
        f"- Rows: {len(truth)} — 24 oblasts, the Autonomous Republic of Crimea, Kyiv city and Sevastopol; key is ISO 3166-2.",
        "- Caveat: the table prints '…' for Crimea and Sevastopol (no estimate since 2014), so both values are blank — not zero — and the units are hatched on the map.",
    ])
    crs = "+proj=laea +lat_0=48.5 +lon_0=31.2 +datum=WGS84 +units=m +no_defs"
    shapes = gpd.read_file(fetch(UKRAINE_GEO_URL, "ukraine_adm1.geojson"))
    shapes = shapes.rename(columns={"shapeISO": "key"})
    if set(shapes.key) != set(truth.key):
        raise ValueError(f"Ukraine join mismatch: {set(shapes.key) ^ set(truth.key)}")
    merged = shapes.merge(truth[["key", "value"]], on="key", how="left").to_crs(crs)
    xmin, ymin, xmax, ymax = merged.total_bounds
    extent = (xmin - 15_000, xmax + 40_000, ymin - 45_000, ymax + 40_000)
    check_window("ukraine_oblast_population", crs, extent)

    cv = Canvas(extent, "Population of Ukraine’s regions, 1 January 2022",
                "Estimated present population of the 24 oblasts, Kyiv city, Crimea and Sevastopol · last estimate before the full-scale invasion",
                footer=0.95)
    ax = cv.ax
    world = load_world()
    neighbours = world[world.key != "UKR"].to_crs(crs)
    neighbours.plot(ax=ax, color="#ebe8e1", edgecolor="#cfcac0", linewidth=0.6, zorder=1)
    known = merged.dropna(subset=["value"])
    known.plot(ax=ax, color="#dfe3dc", edgecolor="white", linewidth=0.9, zorder=2)
    plot_missing(ax, merged[merged.value.isna()], linewidth=0.9)
    gpd.GeoSeries([merged.union_all().boundary], crs=crs).plot(ax=ax, color="#7d8a80", linewidth=1.0, zorder=3)

    for name, lon, lat in (("POLAND", 23.0, 51.85), ("BELARUS", 27.6, 52.1), ("RUSSIA", 37.6, 51.6),
                           ("MOLDOVA", 28.6, 47.45), ("ROMANIA", 25.4, 46.6), ("HUNGARY", 22.0, 47.75),
                           ("SLOVAKIA", 22.0, 48.95)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=9, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("Black Sea", 32.3, 44.75), ("Sea of Azov", 36.6, 46.1)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", zorder=2)

    # Circles: area proportional to population. Anchors are interior points;
    # Kyiv Oblast's is moved off the city so the two circles read separately.
    s_max = 64.0 ** 2           # marker area (pt^2) for the largest value
    v_max = 4_100_000
    anchors = {"UA-32": (30.05, 49.8), "UA-30": (30.52, 50.45), "UA-14": (37.75, 48.05),
               "UA-09": (39.2, 48.85), "UA-51": (30.1, 46.9), "UA-23": (35.6, 47.25)}
    pts = {}
    for row in known.itertuples():
        if row.key in anchors:
            (x, y), = project_points(crs, [anchors[row.key]])
        else:
            p = row.geometry.representative_point()
            x, y = p.x, p.y
        pts[row.key] = (x, y, row.value)
    for key, (x, y, v) in sorted(pts.items(), key=lambda kv: -kv[1][2]):
        ax.scatter([x], [y], s=v / v_max * s_max, color="#2f6f9f", alpha=0.78,
                   edgecolor="white", linewidth=1.1, zorder=4)
    for key, (x, y, v) in pts.items():
        short = UKR_LABELS[key][2]
        if key == "UA-30":
            ax.annotate(f"Kyiv city\n{v / 1e6:.2f} M", (x, y), xytext=(28, -30),
                        textcoords="offset points", fontsize=9, fontweight="bold", zorder=6,
                        arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.7), path_effects=HALO)
            continue
        r_pt = (v / v_max * s_max) ** 0.5 / 2
        ax.annotate(f"{short}\n{v / 1e6:.2f} M", (x, y), xytext=(0, -r_pt - 4), textcoords="offset points",
                    ha="center", va="top", fontsize=8.2, zorder=6, linespacing=1.05, path_effects=HALO)
    for key, lon, lat in (("UA-43", 34.2, 45.3), ("UA-40", 33.52, 44.6)):
        (x, y), = project_points(crs, [(lon, lat)])
        label = "Crimea\nno estimate" if key == "UA-43" else "Sevastopol\nno estimate"
        ax.annotate(label, (x, y), xytext=(0, 0) if key == "UA-43" else (-46, -6), textcoords="offset points",
                    ha="center", va="center", fontsize=8.2, color="#4c5559", zorder=6, path_effects=HALO,
                    arrowprops=None if key == "UA-43" else dict(arrowstyle="-", color=INK, linewidth=0.6))

    # Legend sits on the empty Romanian corner of the frame, on a paper card.
    lg = cv.panel(cv.left + 0.2, cv.footer + 2.45, 4.3, 2.3)
    lg.add_patch(Rectangle((0, 0), 4.3, 2.3, facecolor=PAPER, edgecolor="#c9c4b8", linewidth=0.6, alpha=0.95))
    lg.text(0.18, 2.15, "Present population\n1 January 2022", va="top", fontsize=10.5, fontweight="bold",
            linespacing=1.3)
    cx, base = 0.75, 0.6
    for v in (4_000_000, 2_000_000, 1_000_000):
        r_in = (v / v_max * s_max) ** 0.5 / 72 / 2
        lg.add_patch(Circle((cx, base + r_in), r_in, facecolor="#2f6f9f" if v == 4_000_000 else "none",
                            alpha=0.78 if v == 4_000_000 else 1, edgecolor="white", linewidth=1.0))
        lg.plot([cx, cx + 0.8], [base + 2 * r_in, base + 2 * r_in], color=INK, linewidth=0.5)
        lg.text(cx + 0.85, base + 2 * r_in, f"{v // 1_000_000} million people", va="center", fontsize=9)
    lg.text(2.75, 1.18, "circle area ∝\npopulation", va="center", fontsize=8.5, color=MUTED)
    lg.add_patch(Rectangle((0.18, 0.18), 0.45, 0.22, facecolor=NODATA, edgecolor=HATCH, hatch="/////", linewidth=0.6))
    lg.text(0.73, 0.29, "No Ukrstat estimate (Crimea, Sevastopol)", va="center", fontsize=9)

    cv.footnote([
        "Source: State Statistics Service of Ukraine (Ukrstat), 'Чисельність наявного населення України на 1 січня 2022', table 1. Boundaries: geoBoundaries gbOpen UKR ADM1 from © OpenStreetMap contributors (ODbL 1.0); "
        "neighbours: Natural Earth 1:50m.",
        "Projection: Lambert azimuthal equal-area centred on 48.5°N 31.2°E. Encoding: proportional circles (area ∝ population), not classed. 25 of 27 units valued; "
        "Crimea and Sevastopol hatched because Ukrstat has published no estimate for them since 2014.",
    ])
    cv.save("ukraine_oblast_population")
    return truth


# --------------------------------------------------------------------------
# United States


def load_eia() -> pd.DataFrame:
    source = fetch(EIA_URL, "Complete_SEDS.csv", timeout=600)
    selected: list[pd.DataFrame] = []
    for chunk in pd.read_csv(source, chunksize=250_000, usecols=["MSN", "StateCode", "Year", "Data"]):
        keep = chunk[chunk.MSN.isin(["DFTCP", "TPOPP"]) & chunk.StateCode.isin(STATE_CODES)]
        if len(keep):
            selected.append(keep)
    data = pd.concat(selected, ignore_index=True)
    common = set(data.loc[data.MSN == "DFTCP", "Year"]) & set(data.loc[data.MSN == "TPOPP", "Year"])
    year = max(common)
    latest = data[data.Year == year].pivot(index="StateCode", columns="MSN", values="Data")
    if set(latest.index) != STATE_CODES:
        raise ValueError(f"EIA state mismatch: missing {sorted(STATE_CODES - set(latest.index))}")
    return latest.reset_index().rename(columns={"StateCode": "key"}).assign(year=year)


def build_us_diesel() -> pd.DataFrame:
    eia = load_eia()
    states = gpd.read_file(fetch(STATES_URL, "cb_2024_us_state_20m.zip"))
    names = states.set_index("STUSPS").NAME.to_dict()
    truth = pd.DataFrame({
        "key": eia.key, "name": eia.key.map(names), "value": eia.DFTCP,
        "unit": "thousand barrels per year", "year": eia.year,
        "total_thousand_barrels": eia.DFTCP,
        "population_thousands": eia.TPOPP,
        "per_capita_barrels": eia.DFTCP / eia.TPOPP,
    }).sort_values("key").reset_index(drop=True)
    write_csv("us_diesel", truth)
    year = int(truth.year.iloc[0])
    write_md("us_diesel", [
        f"- URL/API: {EIA_URL} (series definitions: {EIA_CODES_URL}); rows MSN=DFTCP and MSN=TPOPP.",
        f"- Vintage: EIA SEDS complete release, data year {year} (latest year with both series). DFTCP = distillate fuel oil total consumption, thousand barrels; TPOPP = resident population, thousands.",
        "- Licence: U.S. Energy Information Administration, U.S. government work (public domain). Boundaries: Census cartographic boundary file 2024, 1:20M (public domain).",
        f"- Rows: {len(truth)} — 50 states plus DC. `value` = `total_thousand_barrels` (annual); `per_capita_barrels` = DFTCP ÷ TPOPP (both in thousands, so the ratio is barrels per resident).",
        "- Caveat: 'distillate fuel oil' is on-road and off-road diesel plus heating oil; EIA SEDS does not publish diesel alone by state, so this is the closest primary series.",
    ])

    crs = "EPSG:5070"
    shapes = states[states.STUSPS.isin(STATE_CODES)].rename(columns={"STUSPS": "key"})
    shapes = shapes.merge(truth[["key", "per_capita_barrels"]], on="key", how="left")
    conus = shapes[~shapes.key.isin(["AK", "HI"])].to_crs(crs)
    xmin, ymin, xmax, ymax = conus.total_bounds
    extent = (xmin - 120_000, xmax + 260_000, ymin - 420_000, ymax + 80_000)
    check_window("us_diesel", crs, extent, contains=spec("us_diesel")["contains"][:2])
    by_total = truth.sort_values("total_thousand_barrels", ascending=False).reset_index(drop=True)
    by_pc = truth.sort_values("per_capita_barrels", ascending=False).reset_index(drop=True)
    top, second = by_total.iloc[0], by_total.iloc[1]
    top_rank = int(by_pc.index[by_pc.key == top.key][0]) + 1
    cv = Canvas(extent, f"Distillate fuel (diesel and heating oil) use per resident, {year}",
                f"Barrels per resident per year, by state · {top['name']} burns the most in total "
                f"({top.total_thousand_barrels / 1000:,.0f} M barrels) but ranks {top_rank}th of 51 per resident",
                footer=1.6)
    ax = cv.ax
    world = load_world()
    world[world.key.isin(["CAN", "MEX", "CUB", "BHS"])].to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="none", zorder=1)
    edges = [3, 4, 5, 7, 10, 15]
    colors = ramp("YlOrBr", len(edges) + 1, 0.08, 0.92)
    plot_classes(ax, conus, "per_capita_barrels", edges, colors, linewidth=0.8)

    classes = dict(zip(truth.key, classify(truth.per_capita_barrels, edges)))

    def label_colour(key):
        return "white" if classes[key] >= 5 else INK

    offsets = {"VT": (-46, 34), "NH": (-18, 60), "MA": (45, 12), "RI": (40, -6), "CT": (40, -24),
               "NJ": (36, -10), "DE": (36, -14), "MD": (40, -30), "DC": (34, -46)}
    nudges = {"FL": (60_000, 0), "LA": (-60_000, 0), "MI": (60_000, -120_000), "CA": (-30_000, 0),
              "KY": (40_000, -20_000), "WV": (-15_000, -20_000), "ID": (0, -80_000)}
    for row in conus.itertuples():
        p = row.geometry.representative_point()
        dx, dy = nudges.get(row.key, (0, 0))
        if row.key in offsets:
            c = row.geometry.centroid
            ax.annotate(row.key, (c.x, c.y), xytext=offsets[row.key], textcoords="offset points",
                        fontsize=8.5, ha="center", va="center", color=INK, zorder=7,
                        arrowprops=dict(arrowstyle="-", color="#6b767b", linewidth=0.6, shrinkA=0, shrinkB=0))
        else:
            ax.text(p.x + dx, p.y + dy, row.key, fontsize=8.5, ha="center", va="center",
                    color=label_colour(row.key), zorder=7)
    dc = conus[conus.key == "DC"].geometry.centroid.iloc[0]
    ax.scatter([dc.x], [dc.y], s=22, marker="s", color=colors[int(classes["DC"])], edgecolor=INK,
               linewidth=0.6, zorder=8)

    # Insets: Alaska at 0.35x the main scale, Hawaii at 1x, each in its own
    # equal-area projection, placed over the Pacific / Mexico corner.
    scale = cv.map_w / (extent[1] - extent[0])  # inches per metre in the main map
    ak = shapes[shapes.key == "AK"].to_crs("EPSG:3338")
    hi = shapes[shapes.key == "HI"].to_crs("+proj=aea +lat_1=19 +lat_2=21.5 +lat_0=20 +lon_0=-157 +datum=WGS84 +units=m")
    placements = []
    x_cursor = cv.left + 0.15
    for key, frame, factor in (("AK", ak, 0.36), ("HI", hi, 1.0)):
        b = frame.total_bounds
        pad = 60_000 if key == "AK" else 30_000
        w_m, h_m = b[2] - b[0] + 2 * pad, b[3] - b[1] + 2 * pad
        w_in, h_in = w_m * scale * factor, h_m * scale * factor
        inset = cv.add_axes(x_cursor, cv.footer + 0.12, w_in, h_in, frame=True)
        inset.set_facecolor(WATER)
        inset.set_xlim(b[0] - pad, b[2] + pad)
        inset.set_ylim(b[1] - pad, b[3] + pad)
        inset.set_aspect("equal")
        plot_classes(inset, frame, "per_capita_barrels", edges, colors, linewidth=0.6)
        p = frame.geometry.representative_point().iloc[0]
        inset.text(p.x, p.y, key, fontsize=8.5, ha="center", va="center", color=label_colour(key))
        inset.text(0.03, 0.96, {"AK": "Alaska (scale ×0.36)", "HI": "Hawaii (same scale)"}[key],
                   transform=inset.transAxes, fontsize=8, va="top", color=MUTED)
        placements.append((key, frame.crs, (b[0] - pad, b[2] + pad, b[1] - pad, b[3] + pad)))
        x_cursor += w_in + 0.2
    for (key, frame_crs, ext), point in zip(placements, spec("us_diesel")["contains"][2:]):
        check_window("us_diesel", frame_crs, ext, contains=[point], excludes=[])

    lg = cv.add_axes(cv.left, 0.78, 8.5, 0.78)
    class_legend(lg, colors, [f"{e:g}" for e in edges],
                 "Barrels of distillate fuel per resident per year (1 barrel = 42 US gal)")
    cv.footnote([
        f"Source: U.S. EIA State Energy Data System {year} (DFTCP total distillate consumption ÷ TPOPP resident population), public domain. Boundaries: U.S. Census Bureau cartographic boundary file 2024, 1:20M.",
        "Projection: conterminous US Albers equal-area conic (EPSG:5070); Alaska (EPSG:3338, 0.36× scale) and Hawaii (Albers, same scale) inset. Classification: 7 manual classes. "
        f"All 50 states and DC shown (DC as the small square); none excluded. Largest totals: {top['name']} {top.total_thousand_barrels / 1000:,.0f} M bbl, {second['name']} {second.total_thousand_barrels / 1000:,.0f} M bbl.",
    ])
    cv.save("us_diesel")
    return truth


def build_ca_county_income() -> pd.DataFrame:
    raw = pd.read_csv(fetch(ACS_URL, "acsdt5y2023-b19013.dat"), sep="|", dtype=str)
    ca = raw[raw.GEO_ID.str.startswith("0500000US06")].copy()
    counties = gpd.read_file(fetch(COUNTIES_URL, "cb_2023_us_county_500k.zip"))
    counties = counties[counties.STATEFP == "06"].copy()
    counties["key"] = counties.STATEFP + counties.COUNTYFP
    names = counties.set_index("key").NAMELSAD.to_dict()
    ca["key"] = ca.GEO_ID.str[-5:]
    truth = pd.DataFrame({
        "key": ca.key, "name": ca.key.map(names),
        "value": pd.to_numeric(ca.B19013_E001, errors="coerce"),
        "unit": "2023 inflation-adjusted US dollars", "year": "2019-2023",
        "moe": pd.to_numeric(ca.B19013_M001, errors="coerce"),
    }).sort_values("key").reset_index(drop=True)
    if len(truth) != 58 or truth.value.isna().any() or set(truth.key) != set(counties.key):
        raise ValueError(f"ACS: expected 58 valued California counties, got {len(truth)}")
    write_csv("ca_county_income", truth)
    write_md("ca_county_income", [
        f"- URL/API: {ACS_URL} (ACS table-based summary file, rows GEO_ID 0500000US06xxx). The keyless api.census.gov call now returns a 'Missing Key' page, so the flat-file release is used instead.",
        "- Vintage: American Community Survey 2019–2023 5-year estimates, table B19013 (median household income in the past 12 months, 2023 inflation-adjusted dollars).",
        "- Licence: U.S. Census Bureau, public domain. Boundaries: Census cartographic boundary file 2023, 1:500k.",
        f"- Rows: {len(truth)} California counties; key is the 5-digit FIPS code; `moe` is the published 90% margin of error.",
        "- Caveat: the spec's `excludes` points (Las Vegas -115.1,36.2 and Reno -119.8,39.5) both lie inside California's own lon/lat bounding box, so no rectangular frame showing all of California can exclude them.",
    ])
    crs = "EPSG:3310"
    merged = counties.merge(truth[["key", "value"]], on="key", how="left").to_crs(crs)
    xmin, ymin, xmax, ymax = merged.total_bounds
    extent = (xmin - 45_000, xmax + 70_000, ymin - 30_000, ymax + 55_000)
    notes = check_window("ca_county_income", crs, extent)
    cv = Canvas(extent, "Median household income by county, California",
                "Median household income in the past 12 months · American Community Survey 2019–2023 five-year estimates, in 2023 dollars",
                map_w=8.7, left=0.4, footer=0.3)
    ax = cv.ax
    states = gpd.read_file(fetch(STATES_URL, "cb_2024_us_state_20m.zip"))
    states[states.STUSPS.isin(["NV", "OR", "AZ", "UT", "ID"])].to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="white", linewidth=0.8, zorder=1)
    load_world().query("key == 'MEX'").to_crs(crs).plot(ax=ax, color=LAND, edgecolor="none", zorder=1)
    for name, lon, lat in (("NEVADA", -117.4, 38.9), ("OREGON", -121.0, 42.22), ("ARIZONA", -113.85, 33.3),
                           ("MEXICO", -115.6, 32.38), ("Pacific Ocean", -123.0, 34.6)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=9.5 if name.isupper() else 11, color="#9a958a" if name.isupper() else "#5f8796",
                style="normal" if name.isupper() else "italic", ha="center", va="center", zorder=2)
    edges = [60_000, 75_000, 90_000, 110_000, 130_000, 150_000]
    colors = ramp("PuBu", len(edges) + 1, 0.12, 0.95)
    plot_classes(ax, merged, "value", edges, colors, linewidth=0.7)
    classes = dict(zip(truth.key, classify(truth.value, edges)))
    labelled = {"06085": "Santa Clara", "06025": "Imperial", "06019": "Fresno", "06037": "Los Angeles",
                "06073": "San Diego", "06023": "Humboldt", "06067": "Sacramento", "06041": "Marin",
                "06075": "San Francisco", "06081": "San Mateo", "06107": "Tulare", "06093": "Siskiyou",
                "06071": "San Bernardino", "06029": "Kern", "06065": "Riverside", "06049": "Modoc",
                "06027": "Inyo", "06059": "Orange", "06001": "Alameda"}
    callouts = {"06075": (-62, 22), "06041": (-55, 40), "06081": (-62, -8), "06085": (-50, -42),
                "06001": (60, 30), "06059": (-30, -40)}
    for row in merged.itertuples():
        if row.key not in labelled:
            continue
        text = f"{labelled[row.key]}\n${row.value / 1000:,.0f}k"
        p = row.geometry.representative_point()
        if row.key in callouts:
            ax.annotate(text, (p.x, p.y), xytext=callouts[row.key], textcoords="offset points",
                        fontsize=8.3, ha="center", va="center", zorder=7, linespacing=1.05, path_effects=HALO,
                        arrowprops=dict(arrowstyle="-", color="#4d585d", linewidth=0.6, shrinkA=0, shrinkB=0))
        else:
            dark = classes[row.key] >= 4
            ax.text(p.x, p.y, text, fontsize=8.3, ha="center", va="center", zorder=7, linespacing=1.05,
                    color="white" if dark else INK, path_effects=None if dark else HALO)

    col_x = cv.left + cv.map_w + 0.5
    col_w = WIDTH_IN - col_x - 0.4
    top = cv.H - cv.header - 0.1
    lg = cv.panel(col_x, top, col_w, 3.4)
    vertical_legend(lg, colors, range_labels(edges, lambda v: f"${v / 1000:,.0f}k"),
                    "Median household income\n(2023 dollars)")
    ranked = truth.sort_values("value", ascending=False)
    tb = cv.panel(col_x, top - 3.7, col_w, 3.6)
    tb.text(0, 3.58, "Highest", va="top", fontsize=10.5, fontweight="bold")
    tb.text(3.0, 3.58, "Lowest", va="top", fontsize=10.5, fontweight="bold")
    for i, row in enumerate(ranked.head(5).itertuples()):
        tb.text(0, 3.2 - i * 0.27, f"{row.name.replace(' County', '')}", va="top", fontsize=9.5)
        tb.text(2.55, 3.2 - i * 0.27, f"${row.value:,.0f}", va="top", ha="right", fontsize=9.5)
    for i, row in enumerate(ranked.tail(5).iloc[::-1].itertuples()):
        tb.text(3.0, 3.2 - i * 0.27, f"{row.name.replace(' County', '')}", va="top", fontsize=9.5)
        tb.text(5.4, 3.2 - i * 0.27, f"${row.value:,.0f}", va="top", ha="right", fontsize=9.5)
    lo_row, hi_row = ranked.iloc[-1], ranked.iloc[0]
    summary = (f"California's 58 counties range from ${lo_row.value:,.0f} ({lo_row['name'].replace(' County', '')}) "
               f"to ${hi_row.value:,.0f} ({hi_row['name'].replace(' County', '')}), a {hi_row.value / lo_row.value:.1f}x spread. "
               "Coastal metro counties, above all the Bay Area, are highest; the rural north and the Central Valley lowest.")
    tb.text(0, 1.75, textwrap.fill(summary, 72), va="top", fontsize=9.5, color=MUTED, linespacing=1.4)
    cv.footnote([
        "Source: U.S. Census Bureau, American Community Survey 2019–2023 5-year estimates, table B19013 (summary file), public domain. "
        "Boundaries: Census cartographic boundary file 2023, 1:500k.",
        "Projection: California Albers (EPSG:3310, equal-area). Classification: 7 manual classes at round dollar breaks. "
        "All 58 counties valued; none excluded. Neighbouring states and Mexico drawn as unshaded context. Margins of error not mapped.",
    ], width=95, x_in=col_x, y_in=0.4)
    cv.save("ca_county_income")
    if notes:
        print("  (window notes above are the spec's unsatisfiable excludes; see truth.md)")
    return truth


# --------------------------------------------------------------------------
# European Union


def jsonstat_rows(payload: dict[str, Any]) -> pd.DataFrame:
    ids = payload["id"]
    sizes = payload["size"]
    labels: list[list[str]] = []
    for dimension in ids:
        index = payload["dimension"][dimension]["category"]["index"]
        if isinstance(index, dict):
            labels.append([key for key, _ in sorted(index.items(), key=lambda item: item[1])])
        else:
            labels.append(list(index))
    rows = []
    for flat_text, value in payload.get("value", {}).items():
        remainder = int(flat_text)
        positions = []
        for size in reversed(sizes):
            positions.append(remainder % size)
            remainder //= size
        positions.reverse()
        row = {dimension: labels[i][positions[i]] for i, dimension in enumerate(ids)}
        row["value"] = value
        rows.append(row)
    return pd.DataFrame(rows)


def build_eu_nuts2_gdp() -> pd.DataFrame:
    shapes = gpd.read_file(fetch(NUTS_URL, "nuts2_2024_3035.geojson"))
    eu = shapes[shapes.CNTR_CODE.isin(EU27)].copy()
    payload = json.loads(fetch(EUROSTAT_URL, "eurostat_nama_10r_2gdp.json").read_text())
    data = jsonstat_rows(payload)
    data["time"] = pd.to_numeric(data.time)
    selected = data[data.geo.isin(set(eu.NUTS_ID) | {"EU27_2020"})].copy()
    selected = selected.sort_values(["geo", "time"]).groupby("geo", as_index=False).tail(1)
    names = eu.set_index("NUTS_ID").NAME_LATN.to_dict()
    names["EU27_2020"] = "European Union (27 countries, from 2020)"
    truth = pd.DataFrame({
        "key": selected.geo, "name": selected.geo.map(names), "value": selected.value,
        "unit": "PPS (EU27 2020) per inhabitant", "year": selected.time,
    }).sort_values("key").reset_index(drop=True)
    write_csv("eu_nuts2_gdp", truth)
    eu_avg = float(truth.loc[truth.key == "EU27_2020", "value"].iloc[0])
    regions_n = int((truth.key != "EU27_2020").sum())
    years = truth.year
    missing = sorted(set(eu.NUTS_ID) - set(truth.key))
    write_md("eu_nuts2_gdp", [
        f"- URL/API: {EUROSTAT_URL} (JSON-stat); geometry {NUTS_URL}.",
        f"- Vintage: Eurostat nama_10r_2gdp updated {payload.get('updated', '?')}; latest year per region {int(years.min())}–{int(years.max())}. Geometry GISCO NUTS 2024, 1:20M, EPSG:3035.",
        "- Licence: Eurostat data, free reuse with attribution; boundaries © EuroGeographics for the administrative boundaries (GISCO).",
        f"- Rows: {len(truth)} — {regions_n} EU NUTS 2 regions (every NUTS 2024 level-2 region of the 27 member states) plus EU27_2020 = {eu_avg:,.0f} PPS, used for the `LU00>200%_EU` check. Unmatched geometries: {missing or 'none'}.",
        "- Caveat: Greece is 'EL' in NUTS; an earlier draft of this builder used 'GR' and silently dropped all 13 Greek regions. Extra-regio codes (xxZZ) have no geometry and are omitted.",
    ])

    regions = eu.merge(truth[["key", "value"]], left_on="NUTS_ID", right_on="key", how="left")
    regions["index"] = regions.value / eu_avg * 100
    mainland = regions[~regions.NUTS_ID.isin(OUTERMOST)]
    outer = regions[regions.NUTS_ID.isin(OUTERMOST)]
    xmin, ymin, xmax, ymax = mainland.total_bounds
    extent = (xmin - 60_000, xmax + 60_000, ymin - 60_000, ymax + 40_000)
    check_window("eu_nuts2_gdp", "EPSG:3035", extent)

    edges = [50, 75, 90, 100, 125, 150, 200]
    colors = ["#8c510a", "#d8b365", "#f0dfb5", "#f6f0e2", "#c7eae5", "#5ab4ac", "#01665e", "#003c30"]
    colors[3] = "#f5ead0"
    colors[4] = "#d4ede9"
    cv = Canvas(extent, "GDP per inhabitant in the EU’s regions",
                f"NUTS 2 regions, purchasing power standards per inhabitant, {int(years.max())}, as a share of the EU average ({eu_avg:,.0f} PPS = 100)",
                map_w=10.9, footer=0.85)
    ax = cv.ax
    world = load_world().to_crs(3035)
    world.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    plot_classes(ax, mainland.dropna(subset=["value"]), "index", edges, colors, linewidth=0.25)
    plot_missing(ax, mainland[mainland.value.isna()])
    eu_outline = mainland.dissolve("CNTR_CODE").boundary
    eu_outline.plot(ax=ax, color="#5d6a70", linewidth=0.55, zorder=3)
    for code, (lon, lat, dx, dy) in {"LU00": (6.1, 49.8, 60, 30), "IE06": (-6.3, 53.35, -55, 25),
                                       "BG31": (23.3, 43.6, -30, 45), "FRK2": (5.6, 45.5, -75, -30)}.items():
        row = regions[regions.NUTS_ID == code].iloc[0]
        (x, y), = project_points(3035, [(lon, lat)])
        ax.annotate(f"{row.NAME_LATN.split(' (')[0]} {row['index']:.0f}", (x, y), xytext=(dx, dy),
                    textcoords="offset points", fontsize=8.2, ha="center", zorder=8, path_effects=HALO,
                    arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6))

    # Right-hand column: legend, then the outermost regions, each in its own
    # LAEA projection at the main map's scale (Guyane at 1/3 scale).
    col_x = cv.left + cv.map_w + 0.35
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header
    lg = cv.add_axes(col_x, top - 2.95, col_w, 2.95)
    pos = lg.get_position()
    w_in, h_in = pos.width * WIDTH_IN, pos.height * cv.H
    lg.set_xlim(0, w_in)
    lg.set_ylim(0, h_in)
    lg.set_axis_off()
    lg.text(0, h_in - 0.02, "GDP per inhabitant, PPS\n(EU27 average = 100)", va="top", fontsize=10.5, fontweight="bold")
    labels = ["< 50", "50–75", "75–90", "90–100", "100–125", "125–150", "150–200", "≥ 200"]
    for i, (colour, label) in enumerate(zip(colors[::-1], labels[::-1])):
        y = h_in - 0.62 - (i + 1) * 0.24
        lg.add_patch(Rectangle((0, y), 0.42, 0.2, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        lo_hi = label.replace("< ", "<").replace("≥ ", "≥")
        lg.text(0.52, y + 0.1, f"{lo_hi}", va="center", fontsize=9)
    y_out = h_in - 0.62 - 9 * 0.24 - 0.08
    lg.add_patch(Rectangle((0, y_out), 0.42, 0.2, facecolor=LAND, edgecolor="#c9c4b8", linewidth=0.4))
    lg.text(0.52, y_out + 0.1, "Outside the EU (not mapped)", va="center", fontsize=9)
    lg.text(2.3, h_in - 0.62 - 0.24 + 0.1, f"100 = {eu_avg:,.0f} PPS\nper inhabitant", va="top", fontsize=9, color=MUTED)

    scale = cv.map_w / (extent[1] - extent[0])
    order = ["PT20", "PT30", "ES70", "FRY1", "FRY2", "FRY3", "FRY4", "FRY5"]
    # Islands need the 1:3M geometry: at 1:20M Martinique is a quadrilateral.
    detailed = gpd.read_file(fetch(NUTS_03M_URL, "nuts2_2024_03m_4326.geojson"))
    detailed = detailed[detailed.NUTS_ID.isin(OUTERMOST)][["NUTS_ID", "geometry"]].merge(
        outer[["NUTS_ID", "value", "index"]], on="NUTS_ID")
    cell_w, cell_h = (col_w - 0.15) / 2, 1.5
    for i, code in enumerate(order):
        part = detailed[detailed.NUTS_ID == code].copy()
        if code in INSET_CLIP:  # frame the main islands; outliers named in the footnote
            part["geometry"] = part.geometry.intersection(box(*INSET_CLIP[code]))
        c = part.geometry.iloc[0].centroid
        local = f"+proj=laea +lat_0={c.y:.2f} +lon_0={c.x:.2f} +datum=WGS84 +units=m"
        part = part.to_crs(local)
        b = part.total_bounds
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        # Each inset is fitted to its cell (scales differ; a common scale
        # leaves Madeira or Mayotte a few pixels wide).
        usable_h = cell_h - 0.3
        m_per_in = max((b[2] - b[0]) / (cell_w * 0.86), (b[3] - b[1]) / (usable_h * 0.86))
        span_w, span_h = cell_w * m_per_in, cell_h * m_per_in
        r, k = divmod(i, 2)
        inset = cv.add_axes(col_x + k * (cell_w + 0.15), top - 3.05 - (r + 1) * (cell_h + 0.12), cell_w, cell_h, frame=True)
        inset.set_facecolor(WATER)
        inset.set_xlim(cx - span_w / 2, cx + span_w / 2)
        inset.set_ylim(cy - span_h / 2 - 0.1 * m_per_in, cy + span_h / 2 - 0.1 * m_per_in)
        plot_classes(inset, part.dropna(subset=["value"]), "index", edges, colors, linewidth=0.3)
        plot_missing(inset, part[part.value.isna()])
        val = part["index"].iloc[0]
        km = 50 if m_per_in < 150_000 else 100
        bar_in = km * 1000 / m_per_in
        x0 = cx + span_w / 2 - 0.12 * m_per_in - km * 1000
        y0 = cy - span_h / 2 + 0.03 * m_per_in
        inset.plot([x0, x0 + km * 1000], [y0, y0], color=INK, linewidth=1.2, solid_capstyle="butt")
        inset.text(x0 + km * 500, y0 + 0.05 * m_per_in, f"{km} km", ha="center", va="bottom", fontsize=7, color=MUTED)
        del bar_in
        scale_note = ""
        inset.text(0.04, 0.95, f"{OUTERMOST[code]}{scale_note}", transform=inset.transAxes, fontsize=8, va="top")
        inset.text(0.04, 0.80, f"index {val:.0f}", transform=inset.transAxes, fontsize=8, va="top", color=MUTED)

    cv.footnote([
        f"Source: Eurostat, nama_10r_2gdp (PPS_EU27_2020_HAB), {int(years.max())}; © EuroGeographics for the administrative boundaries (GISCO NUTS 2024, 1:20M). Context land: Natural Earth.",
        "Projection: ETRS89 Lambert azimuthal equal-area (EPSG:3035); the eight outermost regions inset (GISCO 1:3M) in local LAEA, each at its own scale (see bars); the Madeira inset omits the Selvagens islets and the Guadeloupe inset the Saint-Martin polygon GISCO files under FRY1. Classification: 8 classes of the EU index, diverging at 100. "
        f"All {regions_n} EU NUTS 2 regions valued; none excluded.",
    ], width=200)
    cv.save("eu_nuts2_gdp")
    return truth


def build_eu_renewables() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_RENEW_URL, "owid_share_electricity_renewables.csv"))
    col = variable_column(raw)
    data = latest_rows(iso_rows(raw))
    data = data[data.Code.isin(EUROPE)]
    truth = standard_truth(data, col, "percent of electricity generation")
    write_csv("eu_renewables", truth)
    absent = sorted(EUROPE - set(truth.key))
    write_md("eu_renewables", [
        f"- URL/API: {OWID_RENEW_URL} (Grapher CSV; latest non-missing year per country).",
        f"- Vintage: OWID 'Renewables - % electricity', metadata updated 2026-06-30 (Ember 2026 and Energy Institute); latest year {int(truth.year.min())}–{int(truth.year.max())} (UKR is 2022, ALB/ISL 2024, the rest 2025).",
        "- Licence: OWID data CC BY 4.0 (Ember CC BY 4.0). Boundaries: Natural Earth 1:50m (public domain).",
        f"- Rows: {len(truth)} of {len(EUROPE)} countries on an explicit European ISO3 list (selection by name, never by box). No source value: {', '.join(absent)}. Kosovo is OWID_KOS in the source, keyed KOS.",
        "- Caveat: Russia is a listed European country and is shaded, but the frame stops near 45°E. Turkey, the South Caucasus and Kazakhstan are not on the list (the spec excludes Yerevan).",
    ])
    crs = "EPSG:3035"
    world = load_world().to_crs(crs)
    sel = world[world.key.isin(EUROPE)][["key", "geometry"]].dissolve("key", as_index=False)
    sel = sel.merge(truth[["key", "value"]], on="key", how="left")
    no_value = sel[sel.value.isna()]
    names = load_world().drop_duplicates("key").set_index("key").NAME.to_dict()
    missing_names = ", ".join(sorted(names.get(k, k) for k in no_value.key))
    extent = (2_480_000, 6_560_000, 1_380_000, 5_480_000)
    check_window("eu_renewables", crs, extent)
    cv = Canvas(extent, "Share of electricity from renewable sources, Europe",
                f"Hydro, wind, solar, bioenergy and other renewables as a share of electricity generation · {int(truth.year.max())} (Ukraine 2022; Albania, Iceland 2024)",
                map_w=11.6, footer=0.85)
    ax = cv.ax
    world[~world.key.isin(EUROPE)].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    edges = [20, 40, 60, 80]
    colors = ramp("YlGn", 5, 0.12, 0.92)
    plot_classes(ax, sel.dropna(subset=["value"]), "value", edges, colors, linewidth=0.6)
    plot_missing(ax, sel[sel.value.isna()], linewidth=0.6)
    xmin, xmax, ymin, ymax = extent
    label_at = {"RUS": (38.0, 57.0), "NOR": (9.0, 61.6), "SWE": (15.2, 62.6), "FIN": (26.0, 63.2),
                "GBR": (-1.8, 52.9), "ITA": (12.6, 42.8), "GRC": (22.0, 39.5), "HRV": (16.3, 45.65),
                "MNE": (19.25, 42.8), "DNK": (9.2, 56.1), "BEL": (4.5, 50.7), "FRA": (2.4, 46.8),
                "ESP": (-3.6, 40.0), "PRT": (-8.1, 39.7), "CYP": (33.1, 35.05), "KOS": (20.9, 42.6),
                "ALB": (20.05, 41.1), "MKD": (21.7, 41.6), "SRB": (20.8, 44.0), "BIH": (17.8, 44.2),
                "SVN": (14.8, 46.1), "IRL": (-8.0, 53.2), "ISL": (-18.6, 64.9)}
    for row in sel.dropna(subset=["value"]).itertuples():
        if row.key in {"LUX", "MLT", "LIE", "AND", "SMR", "VAT", "MCO"}:
            continue
        if row.key in label_at:
            (x, y), = project_points(crs, [label_at[row.key]])
        else:
            p = row.geometry.intersection(box(xmin, ymin, xmax, ymax)).representative_point()
            x, y = p.x, p.y
        dark = classify(pd.Series([row.value]), edges).iloc[0] >= 3
        ax.text(x, y, f"{row.value:.0f}", fontsize=8.8, ha="center", va="center",
                zorder=7, color="white" if dark else INK, path_effects=None if dark else HALO)
    for key, lon, lat, dx, dy in (("LUX", 6.13, 49.8, -66, 30), ("MLT", 14.4, 35.9, 28, 14)):
        v = truth.loc[truth.key == key, "value"].iloc[0]
        (x, y), = project_points(crs, [(lon, lat)])
        ax.annotate(f"{v:.0f}", (x, y), xytext=(dx, dy), textcoords="offset points", fontsize=8.6,
                    ha="center", zorder=8, path_effects=HALO,
                    arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6))
    col_x = cv.left + cv.map_w + 0.35
    lg = cv.add_axes(col_x, cv.H - cv.header - 3.0, WIDTH_IN - col_x - 0.3, 3.0)
    pos = lg.get_position()
    w_in, h_in = pos.width * WIDTH_IN, pos.height * cv.H
    lg.set_xlim(0, w_in)
    lg.set_ylim(0, h_in)
    lg.set_axis_off()
    lg.text(0, h_in - 0.02, "Renewable share of\nelectricity generation\n(%, scale 0–100)", va="top",
            fontsize=10.5, fontweight="bold")
    labels = ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"]
    for i, (colour, label) in enumerate(zip(colors[::-1], labels[::-1])):
        y = h_in - 0.95 - (i + 1) * 0.26
        lg.add_patch(Rectangle((0, y), 0.45, 0.22, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        lg.text(0.56, y + 0.11, label, va="center", fontsize=9.5)
    y = h_in - 0.95 - 6 * 0.26 - 0.1
    lg.add_patch(Rectangle((0, y), 0.45, 0.22, facecolor=NODATA, edgecolor=HATCH, hatch="/////", linewidth=0.5))
    lg.text(0.56, y + 0.11, "No data in the source", va="center", fontsize=9.5)
    y -= 0.3
    lg.add_patch(Rectangle((0, y), 0.45, 0.22, facecolor=LAND, edgecolor="#c9c4b8", linewidth=0.4))
    lg.text(0.56, y + 0.11, "Not in the European set", va="center", fontsize=9.5)
    lg.text(0, y - 0.35, "Numbers on the map: share in %", fontsize=9, color=MUTED, va="top")
    ranked = truth.sort_values("value", ascending=False)
    tb = cv.panel(col_x, cv.H - cv.header - 3.6, WIDTH_IN - col_x - 0.3, 4.2)
    tb.text(0, 4.15, "Highest", va="top", fontsize=10.5, fontweight="bold")
    for i, row in enumerate(ranked.head(6).itertuples()):
        tb.text(0, 3.82 - i * 0.25, row.name, va="top", fontsize=9.5)
        tb.text(2.9, 3.82 - i * 0.25, f"{row.value:.0f}%", va="top", ha="right", fontsize=9.5)
    tb.text(0, 2.12, "Lowest", va="top", fontsize=10.5, fontweight="bold")
    for i, row in enumerate(ranked.tail(6).iloc[::-1].itertuples()):
        tb.text(0, 1.79 - i * 0.25, row.name, va="top", fontsize=9.5)
        tb.text(2.9, 1.79 - i * 0.25, f"{row.value:.0f}%", va="top", ha="right", fontsize=9.5)
    cv.footnote([
        "Source: Our World in Data, 'Share of electricity from renewables' (Ember; Energy Institute Statistical Review of World Energy), CC BY 4.0; latest year per country. Boundaries: Natural Earth 1:50m.",
        f"Projection: ETRS89 Lambert azimuthal equal-area (EPSG:3035). Classification: 5 equal classes on a fixed 0–100% scale. {len(truth)} countries chosen by name from an explicit list "
        f"of {len(EUROPE)}; no value in the source for {missing_names} (hatched). Russia is shaded but its territory east of ~45°E is outside the frame.",
    ], width=200)
    cv.save("eu_renewables")
    return truth


# --------------------------------------------------------------------------
# France high-speed rail


def route_km(lines_l93: gpd.GeoSeries, *, buffer_m: float = 30.0, spacing_m: float = 4.5) -> float:
    """Dissolve parallel tracks into route length: OSM maps each track of a
    double-track line as its own way, so summing ways counts most route twice.
    The union of flat-capped buffers has area ~ length x (2*buffer + spacing)."""
    union = unary_union(lines_l93.buffer(buffer_m, cap_style="flat").values)
    return union.area / (2 * buffer_m + spacing_m) / 1000


def overpass_rows() -> tuple[pd.DataFrame, gpd.GeoDataFrame, str]:
    payload = json.loads(fetch_overpass().read_text())
    geod = Geod(ellps="WGS84")
    rows, geoms = [], []
    for element in payload["elements"]:
        coordinates = [(p["lon"], p["lat"]) for p in element.get("geometry", [])]
        if len(coordinates) < 2:
            continue
        line = LineString(coordinates)
        tags = element.get("tags", {})
        rows.append({
            "osm_id": element["id"],
            "name": tags.get("name") or tags.get("ref") or "",
            "maxspeed": tags.get("maxspeed", ""),
            "usage": tags.get("usage", ""),
            "service": tags.get("service", ""),
            "length_km": geod.geometry_length(line) / 1000,
            # ~20 m simplification, 1e-4 deg rounding: keeps line-near checks exact enough and the file small
            "geometry_wkt": shapely.to_wkt(line.simplify(0.0002), rounding_precision=4),
        })
        geoms.append(line)
    frame = pd.DataFrame(rows)
    gdf = gpd.GeoDataFrame(frame.drop(columns="geometry_wkt"), geometry=geoms, crs=4326)
    order = frame.osm_id.argsort().to_numpy()
    base = payload.get("fetched_osm_base") or payload.get("osm3s", {}).get("timestamp_osm_base", "unknown")
    return frame.iloc[order].reset_index(drop=True), gdf.iloc[order].reset_index(drop=True), base


def build_france_lgv() -> pd.DataFrame:
    truth, lines, osm_base = overpass_rows()
    write_csv("france_lgv", truth)
    lines = lines.to_crs(2154)
    track_km = truth.length_km.sum()
    main = lines[lines.service == ""]
    route = route_km(main.geometry)
    write_md("france_lgv", [
        "- URL/API: Overpass API (overpass-api.de, mirror fallback), `area[\"ISO3166-1\"=\"FR\"][admin_level=2]; way[railway=rail][highspeed=yes](area); out tags geom;`.",
        f"- Vintage: OpenStreetMap as of {osm_base}; delete `.cache/france_lgv_overpass.json` to refresh.",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Basemap: geoBoundaries FRA ADM1 from IGN (Etalab Open Licence 2.0) and Natural Earth (public domain).",
        f"- Rows: {len(truth)} OSM ways (osm_id, name, maxspeed, usage, service, length_km = WGS84 geodesic length of the full-resolution way, geometry_wkt simplified ~20 m and rounded to 1e-4°). Sum of ways = {track_km:,.0f} track-km; dissolved route length of non-service ways = {route:,.0f} km at full resolution (the spot check, recomputed from the stored WKT, gets within a few km).",
        "- Caveat: OSM maps each track of a double-track LGV as its own way, so the way sum is ~2× the route length. `total_km` is checked against the dissolved route length (union of 30 m flat buffers ÷ 64.5 m); "
        f"{truth[truth.service != ''].length_km.sum():.0f} km of crossovers/sidings are excluded from it.",
    ])
    crs = "EPSG:2154"
    extent = (60_000, 1_270_000, 6_030_000, 7_140_000)
    check_window("france_lgv", crs, extent)
    cv = Canvas(extent, "France’s high-speed railway network",
                f"Lines tagged high-speed in OpenStreetMap, by signalled maximum speed · about {round(route, -1):,.0f} km of route",
                map_w=10.6, footer=0.95)
    ax = cv.ax
    world = load_world().to_crs(crs)
    world[world.key != "FRA"].plot(ax=ax, color="#ebe8e1", edgecolor="#cfcac0", linewidth=0.6, zorder=1)
    regions = gpd.read_file(fetch(FRANCE_GEO_URL, "france_adm1.geojson")).to_crs(crs)
    regions.plot(ax=ax, color="#dedad0", edgecolor="white", linewidth=1.0, zorder=2)
    speed = pd.to_numeric(main.maxspeed, errors="coerce")
    fast = main[speed >= 300]
    slow = main[~(speed >= 300)]
    slow.plot(ax=ax, color="#f08a4b", linewidth=1.8, zorder=4)
    fast.plot(ax=ax, color="#b3001b", linewidth=2.2, zorder=5)
    cities = {
        "Paris": (2.3522, 48.8566, (8, 6)), "Lyon": (4.8357, 45.7640, (8, -2)),
        "Marseille": (5.3698, 43.2965, (6, -12)), "Bordeaux": (-0.5792, 44.8378, (-8, 0)),
        "Lille": (3.0573, 50.6292, (8, 4)), "Strasbourg": (7.7521, 48.5734, (-8, 6)),
        "Rennes": (-1.6778, 48.1173, (-8, 6)), "Nantes": (-1.5536, 47.2184, (-8, -2)),
        "Montpellier": (3.8767, 43.6108, (-8, -10)), "Toulouse": (1.4442, 43.6047, (8, -2)),
        "Le Mans": (0.1996, 48.0061, (8, -2)), "Tours": (0.6848, 47.3941, (8, -4)),
        "Dijon": (5.0415, 47.3220, (8, 0)), "Metz": (6.1757, 49.1193, (8, 0)),
        "Nîmes": (4.3601, 43.8367, (6, 4)), "Perpignan": (2.8954, 42.6887, (8, -2)),
        "Mulhouse": (7.3389, 47.7508, (-8, -6)), "Valence": (4.8920, 44.9334, (8, 0)),
        "Avignon": (4.8055, 43.9493, (-8, 2)), "Poitiers": (0.3404, 46.5802, (8, 0)),
        "Angoulême": (0.1561, 45.6484, (8, 0)),
    }
    major = {"Paris", "Lyon", "Marseille", "Bordeaux", "Lille", "Strasbourg", "Rennes"}
    for name, (lon, lat, (dx, dy)) in cities.items():
        (x, y), = project_points(crs, [(lon, lat)])
        big = name in major
        ax.scatter([x], [y], s=34 if big else 14, color="white", edgecolor=INK, linewidth=1.2 if big else 0.8, zorder=7)
        ax.annotate(name, (x, y), xytext=(dx, dy), textcoords="offset points",
                    ha="left" if dx > 0 else "right", va="center",
                    fontsize=10.5 if big else 8.2, fontweight="bold" if big else "normal",
                    color=INK if big else "#3c474c", zorder=8, path_effects=HALO)
    for name, lon, lat in (("UNITED KINGDOM", 0.3, 51.0), ("BELGIUM", 4.6, 50.55), ("GERMANY", 8.2, 50.0),
                           ("SWITZERLAND", 7.9, 46.75), ("ITALY", 8.6, 45.25), ("SPAIN", -1.2, 42.35),
                           ("Atlantic\nOcean", -4.0, 46.0), ("Mediterranean Sea", 5.4, 42.4),
                           ("English Channel", -2.6, 49.85)):
        (x, y), = project_points(crs, [(lon, lat)])
        sea = not name.isupper()
        ax.text(x, y, name, fontsize=10 if sea else 9, color="#5f8796" if sea else "#9a958a",
                style="italic" if sea else "normal", ha="center", va="center", zorder=3)
    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 5.0)
    lg.text(0, 4.98, "High-speed railway\n(OSM railway=rail + highspeed=yes)", va="top", fontsize=10.5,
            fontweight="bold", linespacing=1.3)
    for i, (colour, lw, label) in enumerate((("#b3001b", 2.6, "Signalled 300–320 km/h"),
                                              ("#f08a4b", 2.2, "Below 300 km/h: connections,\n220–270 km/h sections"))):
        y = 4.2 - i * 0.48
        lg.plot([0, 0.55], [y, y], color=colour, linewidth=lw, solid_capstyle="butt")
        lg.text(0.7, y, label, va="center", fontsize=9.5, linespacing=1.2)
    lg.scatter([0.27], [3.2], s=34, color="white", edgecolor=INK, linewidth=1.2)
    lg.text(0.7, 3.2, "City (bold: the seven largest served)", va="center", fontsize=9.5)
    fast_km = route_km(fast.geometry)
    stats = [
        ("Route length", f"≈ {route:,.0f} km"),
        ("  of which 300–320 km/h", f"≈ {fast_km:,.0f} km"),
        ("Track length (all ways)", f"{track_km:,.0f} km"),
        ("OSM ways", f"{len(truth):,}"),
    ]
    lg.text(0, 2.55, "Network in OpenStreetMap", va="top", fontsize=10.5, fontweight="bold")
    for i, (k, v) in enumerate(stats):
        lg.text(0, 2.18 - i * 0.27, k, va="top", fontsize=9.5)
        lg.text(col_w - 0.1, 2.18 - i * 0.27, v, va="top", ha="right", fontsize=9.5)
    note = ("OSM draws each track of a double-track line as its own way, so summing ways counts the "
            "network twice; route length dissolves parallel tracks.")
    lg.text(0, 0.95, textwrap.fill(note, 52), va="top", fontsize=8.8, color=MUTED, linespacing=1.4)
    cv.footnote([
        f"Source: © OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {osm_base[:10]}: every way tagged railway=rail + highspeed=yes in France. "
        "Regions: geoBoundaries FRA ADM1 from IGN (Etalab 2.0); neighbours: Natural Earth 1:50m.",
        f"Projection: RGF93 / Lambert-93 (EPSG:2154). Colour = OSM maxspeed tag. {len(truth):,} OSM ways = {track_km:,.0f} track-km; ≈ {route:,.0f} km of route once parallel tracks are dissolved. "
        "Metropolitan France only; overseas departments have no high-speed rail and are outside the frame.",
    ], width=160)
    cv.save("france_lgv")
    return truth


# --------------------------------------------------------------------------
# Minard


def load_minard(name: str) -> pd.DataFrame:
    return pd.read_csv(fetch(f"{MINARD_BASE}/Minard.{name}.csv", f"minard_{name}.csv"))


def build_napoleon_1812() -> pd.DataFrame:
    troops = load_minard("troops").rename(columns={"long": "lon"})
    cities = load_minard("cities").rename(columns={"long": "lon"})
    temps = load_minard("temp").rename(columns={"long": "lon", "temp": "temperature_r"})
    records = []
    for row in troops.itertuples():
        records.append({"record_type": "troop", "seq": row.rownames, "lon": row.lon, "lat": row.lat,
                        "survivors": row.survivors, "direction": row.direction, "group": row.group})
    for row in cities.itertuples():
        records.append({"record_type": "city", "seq": row.rownames, "lon": row.lon, "lat": row.lat, "city": row.city})
    for row in temps.itertuples():
        records.append({"record_type": "temperature", "seq": row.rownames, "lon": row.lon,
                        "temperature_r": row.temperature_r, "days": row.days,
                        "date": row.date if isinstance(row.date, str) else ""})
    truth = pd.DataFrame(records, columns=["record_type", "seq", "lon", "lat", "survivors", "direction",
                                           "group", "city", "temperature_r", "days", "date"])
    truth["survivors"] = truth.survivors.astype("Int64")
    truth["group"] = truth.group.astype("Int64")
    truth["days"] = truth.days.astype("Int64")
    write_csv("napoleon_1812", truth)
    urls = ", ".join(f"{MINARD_BASE}/Minard.{n}.csv" for n in ("troops", "cities", "temp"))
    write_md("napoleon_1812", [
        f"- URL/API: {urls}",
        "- Vintage: Charles Joseph Minard, 'Carte figurative des pertes successives en hommes de l’Armée Française dans la campagne de Russie 1812–1813' (1869), as transcribed by Wilkinson (*The Grammar of Graphics*) and shipped in R HistData (Friendly).",
        "- Licence: the 1869 figures are public domain; the transcription is redistributed in HistData (GPL) via Rdatasets — attribution to Minard, Wilkinson and Friendly.",
        f"- Rows: {len(truth)} — `record_type` troop ({len(troops)}: lon, lat, survivors, direction A/R, group 1–3), city ({len(cities)}) and temperature ({len(temps)}: degrees Réaumur as on Minard's chart, days, date).",
        "- Caveat: start_survivors = 422,000 is the sum of the three groups' first rows (340,000 + 60,000 + 22,000); temperatures are Réaumur (°C = °R × 1.25), which HistData leaves unconverted.",
    ])

    lat0 = 55.0
    k = np.cos(np.radians(lat0))
    extent_geo = (23.4, 38.4, 53.55, 56.15)
    extent = (extent_geo[0] * k, extent_geo[1] * k, extent_geo[2], extent_geo[3])
    style()
    map_w = 15.2
    map_h = map_w * (extent[3] - extent[2]) / (extent[1] - extent[0])
    header, footer, gap, temp_h = 1.05, 0.95, 0.25, 1.9
    H = header + map_h + gap + temp_h + footer
    fig = plt.figure(figsize=(WIDTH_IN, H), dpi=DPI)
    fig.patch.set_facecolor(PAPER)
    ax = fig.add_axes([0.4 / WIDTH_IN, (footer + temp_h + gap) / H, map_w / WIDTH_IN, map_h / H])
    tax = fig.add_axes([0.4 / WIDTH_IN, footer / H, map_w / WIDTH_IN, temp_h / H])
    fig.text(0.4 / WIDTH_IN, 1 - 0.30 / H, "Napoleon’s march on Moscow and the retreat, 1812", ha="left", va="top",
             fontsize=23, fontweight="bold")
    fig.text(0.4 / WIDTH_IN, 1 - 0.72 / H,
             "After Minard (1869): band width is proportional to the number of soldiers still with the army · 422,000 crossed the Niemen, about 10,000 came back",
             ha="left", va="top", fontsize=12.5, color=MUTED)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal")
    ax.set_facecolor("#f1eee6")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    view = box(extent_geo[0] - 1, extent_geo[2] - 1, extent_geo[1] + 1, extent_geo[3] + 1)
    world = load_world()
    world = world[world.intersects(view)].copy()
    world["geometry"] = world.geometry.intersection(view)
    world = world.set_geometry(world.geometry.scale(xfact=k, yfact=1, origin=(0, 0)))
    world.boundary.plot(ax=ax, color="#c8c1b1", linewidth=0.8, linestyle=(0, (4, 3)), zorder=1)
    rivers = gpd.read_file(fetch(NE_RIVERS_URL, "ne_10m_rivers_lake_centerlines.zip"))
    rivers = rivers[rivers.intersects(view)].copy()
    rivers["geometry"] = rivers.geometry.intersection(view)
    rivers = rivers.set_geometry(rivers.geometry.scale(xfact=k, yfact=1, origin=(0, 0)))
    rivers.plot(ax=ax, color="#9cc3d1", linewidth=1.0, zorder=1)
    for name, lon, lat in (("Niemen", 23.75, 54.2), ("Dnieper", 30.95, 54.15), ("Daugava\n(W. Dvina)", 27.0, 55.95),
                           ("Berezina", 28.75, 53.75)):
        ax.text(lon * k, lat, name, fontsize=8.5, color="#5f8796", style="italic", ha="center", zorder=2)
    for name, lon, lat in (("LITHUANIA", 24.6, 55.95), ("BELARUS", 29.2, 53.62), ("RUSSIA", 32.6, 55.98),
                           ("LATVIA", 25.6, 56.1)):
        ax.text(lon * k, lat, name, fontsize=8.5, color="#aaa292", ha="center", va="center", zorder=2)

    # Bands are polygons in data units, so width is exact and joints are clean:
    # each segment is a flat-capped buffer, joints are filled with a disc of
    # the narrower neighbour's width, and the whole path is unioned.
    deg_per_pt = (extent[1] - extent[0]) / (map_w * 72)
    pt_per_man = 40.0 / 422_000          # 422,000 men would be 40 pt wide

    def half_width(survivors: float) -> float:
        return max(0.6, survivors * pt_per_man) * deg_per_pt / 2

    def ribbon(group: pd.DataFrame) -> shapely.Geometry:
        pts = [(lon * k, lat) for lon, lat in group[["lon", "lat"]].to_numpy()]
        surv = group.survivors.to_numpy(dtype=float)
        parts = []
        for i, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
            if a == b:
                continue
            parts.append(LineString([a, b]).buffer(half_width(surv[i]), cap_style="flat"))
            if 0 < i:
                parts.append(Point(a).buffer(min(half_width(surv[i - 1]), half_width(surv[i])), 32))
        return unary_union(parts)

    advance, retreat = "#d8b47e", "#23292c"
    shapes = {"A": [], "R": []}
    for (grp, direction), part in troops.groupby(["group", "direction"], sort=True):
        part = part.sort_values("rownames")
        if direction == "R":  # start the retreat where that group's advance ended
            prev = troops[(troops.group == grp) & (troops.direction == "A")].sort_values("rownames").tail(1)
            part = pd.concat([prev.assign(survivors=part.survivors.iloc[0]), part])
        shapes[direction].append(ribbon(part))
    gpd.GeoSeries(shapes["A"]).plot(ax=ax, color=advance, edgecolor="#b8955f", linewidth=0.4, zorder=3)
    gpd.GeoSeries(shapes["R"]).plot(ax=ax, color=retreat, edgecolor="none", zorder=4)
    ax.set_aspect("equal")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_xlabel("")
    ax.set_ylabel("")

    city_offsets = {"Moscou": (0, 12), "Kowno": (-8, 12), "Wilna": (0, 12), "Smolensk": (0, 13), "Polotzk": (0, 12),
                    "Witebsk": (0, 11), "Gloubokoe": (0, 10), "Minsk": (0, -12), "Mohilow": (0, -12),
                    "Malo-Jarosewii": (0, -12), "Tarantino": (14, -10)}
    for row in cities.itertuples():
        dx, dy = city_offsets.get(row.city, (0, 9))
        ax.scatter([row.lon * k], [row.lat], s=12, color=INK, zorder=6)
        ax.annotate(row.city, (row.lon * k, row.lat), xytext=(dx, dy), textcoords="offset points",
                    ha="center", va="center", fontsize=9.5 if row.city in {"Kowno", "Wilna", "Smolensk", "Moscou"} else 8.3,
                    fontweight="bold" if row.city in {"Kowno", "Wilna", "Smolensk", "Moscou"} else "normal",
                    style="italic", zorder=7, path_effects=HALO)
    for text, lon, lat, colour, ha in (("422,000 men cross the Niemen, 24 June", 23.55, 55.55, "#8d6a37", "left"),
                                       ("100,000 reach Moscow, 14 Sept.", 37.4, 56.03, "#8d6a37", "right"),
                                       ("24,000 left at Smolensk", 31.6, 54.33, INK, "left"),
                                       ("≈ 10,000 recross the Niemen", 23.55, 54.12, INK, "left")):
        ax.text(lon * k, lat, text, fontsize=10, fontweight="bold", color=colour, ha=ha, zorder=8, path_effects=HALO)
    # Width key, drawn with the same geometry as the bands.
    kx, ky = 33.85 * k, 53.95
    ax.text(kx, ky + 0.36, "Band width = men", fontsize=9.5, fontweight="bold", zorder=8)
    for i, v in enumerate((300_000, 100_000, 20_000)):
        y = ky + 0.16 - i * 0.17
        geom = LineString([(kx, y), (kx + 0.55, y)]).buffer(half_width(v), cap_style="flat")
        gpd.GeoSeries([geom]).plot(ax=ax, color="#a39d90", edgecolor="none", zorder=8)
        ax.text(kx + 0.65, y, f"{v:,}", va="center", fontsize=9, zorder=8)
    for i, (colour, label) in enumerate(((advance, "Advance, June–Sept."), (retreat, "Retreat, Oct.–Dec."))):
        y = ky + 0.16 - i * 0.17
        ax.add_patch(Rectangle((kx + 1.5, y - 0.04), 0.3, 0.08, facecolor=colour, edgecolor="none", zorder=8))
        ax.text(kx + 1.88, y, label, va="center", fontsize=9, zorder=8)
    ax.set_aspect("equal")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])

    # Temperature on the retreat, aligned to the same longitudes, Réaumur -> °C.
    tax.set_xlim(extent[0], extent[1])
    temps = temps.assign(c=temps.temperature_r * 1.25)
    tax.plot(temps.lon * k, temps.c, color="#2e6f8e", linewidth=2.0, marker="o", markersize=5, zorder=3)
    for row in temps.itertuples():
        date = row.date if isinstance(row.date, str) else ""
        date = re.sub(r"([A-Za-z]+)(\d+)", lambda m: f"{int(m.group(2))} {m.group(1)}", date)
        tax.annotate(f"{row.c:.0f} °C\n{date}".replace("-", "−").strip(), (row.lon * k, row.c), xytext=(0, -10), textcoords="offset points",
                     ha="center", va="top", fontsize=8.5, linespacing=1.1)
        tax.axvline(row.lon * k, color="#b6b0a3", linewidth=0.6, linestyle=(0, (2, 2)), zorder=1)
    tax.set_ylim(-45, 6)
    tax.set_yticks([0, -10, -20, -30, -40])
    tax.set_yticklabels(["0 °C", "−10", "−20", "−30", "−40"], fontsize=8.5, color=MUTED)
    tax.grid(axis="y", linewidth=0.5, color="#d8d3c8")
    tax.set_xticks([lon * k for lon in range(24, 39, 2)])
    tax.set_xticklabels([f"{lon}°E" for lon in range(24, 39, 2)], fontsize=8.5, color=MUTED)
    tax.set_facecolor(PAPER)
    for side in ("top", "right", "left"):
        tax.spines[side].set_visible(False)
    tax.spines["bottom"].set_color("#b6b0a3")
    tax.tick_params(length=0)
    tax.text(0, 1.02, "Temperature during the retreat (Minard’s Réaumur readings converted to °C)", transform=tax.transAxes,
             fontsize=10, fontweight="bold", va="bottom")
    text = "\n".join(textwrap.fill(p, 200) for p in [
        "Source: Charles Joseph Minard, 'Carte figurative des pertes successives en hommes de l’Armée Française dans la campagne de Russie 1812–1813' (1869); data as transcribed by L. Wilkinson, "
        "The Grammar of Graphics, distributed in R HistData (M. Friendly) via Rdatasets. Rivers and modern borders (dashed): Natural Earth 1:10m/1:50m.",
        "Projection: equirectangular with standard parallel 55°N. Encoding: band width ∝ survivors (Minard’s values; three army groups). Historical French place-name spellings kept as on Minard’s map.",
    ])
    fig.text(0.4 / WIDTH_IN, 0.16 / H, text, ha="left", va="bottom", fontsize=8.4, color=MUTED, linespacing=1.5)
    ax.set_xlabel("")
    ax.set_ylabel("")
    check_window("napoleon_1812", "EPSG:4326", extent_geo)
    output = MAPS_DIR / "napoleon_1812" / "reference.png"
    fig.savefig(output, dpi=DPI, facecolor=PAPER, metadata={"Software": "maps etalon builder"})
    plt.close(fig)
    print(f"  wrote {output}")
    return truth


# --------------------------------------------------------------------------
# Earthquakes


def build_quakes_2025() -> pd.DataFrame:
    payload = json.loads(fetch(USGS_URL, "usgs_quakes_2025_m6.geojson").read_text())
    rows = []
    for feature in payload["features"]:
        lon, lat, depth = feature["geometry"]["coordinates"]
        props = feature["properties"]
        rows.append({
            "id": feature["id"],
            "time": pd.to_datetime(props["time"], unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lon": lon, "lat": lat, "depth_km": depth, "mag": props["mag"], "magType": props.get("magType", ""),
            "place": props["place"],
        })
    truth = pd.DataFrame(rows).sort_values(["time", "id"]).reset_index(drop=True)
    write_csv("quakes_2025", truth)
    generated = pd.to_datetime(payload["metadata"]["generated"], unit="ms", utc=True).strftime("%Y-%m-%d")
    write_md("quakes_2025", [
        f"- URL/API: {USGS_URL}",
        f"- Vintage: USGS ComCat as of {generated}; events 2025-01-01T00:00Z ≤ t < 2026-01-01T00:00Z with magnitude ≥ 6.0.",
        "- Licence: U.S. Geological Survey Earthquake Hazards Program, U.S. government work (public domain). Plate boundaries for context: Bird (2003) PB2002 via github.com/fraxen/tectonicplates (ODC-BY 1.0).",
        f"- Rows: {len(truth)} events — id, UTC time, lon, lat, depth_km, mag, magType, place. M7.7 Mandalay (Myanmar, 28 Mar) and M8.8 Kamchatka (29 Jul) are present.",
        "- Caveat: ComCat revises magnitudes and adds/removes events; the count can drift by a few. The spec's `count=120±35%` passes at 145.",
    ])
    lon0 = 150.0
    cv, crs = world_canvas(f"Every magnitude 6+ earthquake in 2025",
                           f"{len(truth)} events recorded by the USGS, centred on the Pacific · circle area grows with magnitude, colour shows depth",
                           lon0=lon0, footer=1.75)
    ax = cv.ax
    cut = lon0 - 180
    world = split_at_meridian(load_world(), cut).to_crs(crs)
    world.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.25, zorder=1)
    plates = gpd.read_file(fetch(PLATES_URL, "pb2002_boundaries.json"))
    plates = split_at_meridian(plates, cut).to_crs(crs)
    plates = plates[plates.geometry.length < 5e6 * 10]
    plates.plot(ax=ax, color="#c28f6b", linewidth=0.7, alpha=0.8, zorder=2)
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(crs)
    pts = pts.sort_values("mag", kind="stable")  # smallest first, largest drawn last

    def size(m):
        return 24 * np.power(3.2, np.asarray(m) - 6)

    depth_classes = [(0, 70, "#d7301f", "Shallow, < 70 km"), (70, 300, "#f6a23c", "Intermediate, 70–300 km"),
                     (300, 1000, "#2b6ca3", "Deep, > 300 km")]

    def depth_colour(d):
        return next(c for lo, hi, c, _ in depth_classes if lo <= d < hi)

    ax.scatter(pts.geometry.x, pts.geometry.y, s=size(pts.mag), c=[depth_colour(d) for d in pts.depth_km],
               alpha=0.72, edgecolor="white", linewidth=0.6, zorder=4)
    labels = {"us6000pv1n": None}
    big = pts[pts.mag >= 7.6].sort_values("mag", ascending=False)
    offsets = {}
    for row in big.itertuples():
        place = str(row.place)
        place = re.sub(r"^2025 ", "", place).replace(" Earthquake", "")
        place = re.sub(r"^\d+ km [NSEW]+ of ", "off ", place)
        place = place.replace("Burma (Myanmar)", "Myanmar").replace(", Russia", "").replace("Peninsula", "")
        date = pd.Timestamp(row.time).strftime("%-d %b")
        offsets.setdefault(row.Index, None)
        text = f"M{row.mag:g} {place.strip()}, {date}"
        dx, dy = 12, 10
        if "Kamchatka" in place and row.mag == 8.8:
            dx, dy = -22, 26
        elif "Petropavlovsk" in str(row.place):
            dx, dy = 22, -14
        elif "Myanmar" in place:
            dx, dy = -14, -20
        elif "Cayman" in str(row.place):
            dx, dy = -14, 16
        elif "Drake" in place:
            dx, dy = -14, -14
        elif "Aomori" in place:
            dx, dy = -14, 20
        ax.annotate(text, (row.geometry.x, row.geometry.y), xytext=(dx, dy), textcoords="offset points",
                    ha="left" if dx > 0 else "right", fontsize=8.6,
                    fontweight="bold" if row.mag >= 7.7 else "normal", zorder=8, path_effects=HALO,
                    arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6, shrinkA=0, shrinkB=3))
    del labels
    lg = cv.add_axes(cv.left, 0.86, 14.0, 0.8)
    pos = lg.get_position()
    w_in, h_in = pos.width * WIDTH_IN, pos.height * cv.H
    lg.set_xlim(0, w_in)
    lg.set_ylim(0, h_in)
    lg.set_axis_off()
    lg.text(0, h_in - 0.02, "Magnitude (circle area)", va="top", fontsize=10.5, fontweight="bold")
    x = 0.15
    for m in (6, 7, 8, 8.8):
        d = size(m) ** 0.5 / 72
        lg.add_patch(Circle((x + d / 2, 0.30), d / 2, facecolor="#8d979c", alpha=0.72, edgecolor="white", linewidth=0.6))
        lg.text(x + d + 0.07, 0.30, f"M{m:g}", va="center", fontsize=9)
        x += d + 0.6
    x0 = 5.2
    lg.text(x0, h_in - 0.02, "Depth of focus (colour)", va="top", fontsize=10.5, fontweight="bold")
    for i, (_lo, _hi, colour, label) in enumerate(depth_classes):
        lg.add_patch(Circle((x0 + 0.1 + i * 2.35, 0.30), 0.09, facecolor=colour, alpha=0.8, edgecolor="white"))
        lg.text(x0 + 0.26 + i * 2.35, 0.30, label, va="center", fontsize=9)
    lg.plot([x0 + 7.3, x0 + 7.8], [0.30, 0.30], color="#c28f6b", linewidth=1.2)
    lg.text(x0 + 7.9, 0.30, "Plate boundary (Bird 2003)", va="center", fontsize=9)
    cv.footnote([
        f"Source: U.S. Geological Survey, ComCat via the FDSN event service: all events of magnitude ≥ 6.0 from 1 Jan 2025 to 31 Dec 2025 (UTC), retrieved {generated}; public domain. "
        "Plate boundaries: P. Bird (2003), PB2002 (ODC-BY). Land: Natural Earth 1:50m.",
        f"Projection: Equal Earth centred on 150°E so the Pacific Rim is unbroken. Encoding: circle area ∝ 3.2^(M−6), translucent, smallest drawn first. All {len(truth)} events shown; none excluded. "
        "Labelled: every event of M ≥ 7.6.",
    ])
    cv.save("quakes_2025")
    return truth


BUILDERS: dict[str, Callable[[], pd.DataFrame]] = {
    "gdp_world": build_gdp_world,
    "ukraine_oblast_population": build_ukraine_oblast_population,
    "us_diesel": build_us_diesel,
    "eu_nuts2_gdp": build_eu_nuts2_gdp,
    "ca_county_income": build_ca_county_income,
    "population_change": build_population_change,
    "france_lgv": build_france_lgv,
    "napoleon_1812": build_napoleon_1812,
    "quakes_2025": build_quakes_2025,
    "eu_renewables": build_eu_renewables,
}


# --------------------------------------------------------------------------
# Spot checks: evaluated only from the written truth.csv files


def num(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.1f}"


def truth_value(frame: pd.DataFrame, key: str) -> float:
    row = frame[frame.key.astype(str) == key]
    if row.empty or pd.isna(row.value.iloc[0]):
        raise ValueError(f"no numeric truth for {key}")
    return float(row.value.iloc[0])


def evaluate_spot(map_id: str, spot: str, frame: pd.DataFrame) -> tuple[bool, str]:
    try:
        if spot.startswith("count="):
            expected, tolerance = re.fullmatch(r"count=([\d.]+)±([\d.]+)%", spot).groups()
            actual = len(frame)
            ok = abs(actual - float(expected)) <= float(expected) * float(tolerance) / 100
            return ok, f"{actual} events"
        if spot.startswith("total_km="):
            expected, tolerance = re.fullmatch(r"total_km=([\d.]+)±([\d.]+)%", spot).groups()
            main = frame[frame.service.fillna("") == ""]
            lines = gpd.GeoSeries([wkt.loads(t) for t in main.geometry_wkt], crs=4326).to_crs(2154)
            actual = route_km(lines)
            ok = abs(actual - float(expected)) <= float(expected) * float(tolerance) / 100
            return ok, f"{actual:,.0f} route-km dissolved; {frame.length_km.sum():,.0f} km summing ways"
        if spot.startswith("line_near="):
            lon, lat = map(float, spot.split("=", 1)[1].split(","))
            geod = Geod(ellps="WGS84")
            point = Point(lon, lat)
            nearest = min((wkt.loads(t) for t in frame.geometry_wkt), key=lambda g: g.distance(point))
            q = nearest.interpolate(nearest.project(point))
            km = geod.inv(lon, lat, q.x, q.y)[2] / 1000
            return km <= 20, f"{km:.1f} km; tolerance 20 km"
        if spot.startswith("event_near="):
            lon, lat = map(float, spot.split("=", 1)[1].split(","))
            geod = Geod(ellps="WGS84")
            km = min(geod.inv(lon, lat, r.lon, r.lat)[2] / 1000 for r in frame.itertuples())
            return km <= 400, f"{km:.0f} km; tolerance 400 km"
        if map_id == "napoleon_1812":
            troops = frame[frame.record_type == "troop"].copy()
            troops["survivors"] = pd.to_numeric(troops.survivors)
            troops = troops.sort_values("seq")
            if spot.startswith("start_survivors="):
                actual = troops[troops.direction == "A"].groupby("group").first().survivors.sum()
            elif spot.startswith("moscow_survivors="):
                actual = troops.loc[(troops.lon - 37.6).abs() < .05, "survivors"].max()
            elif spot == "end_survivors<15000":
                actual = troops.loc[troops.direction == "R", "survivors"].min()
                return actual < 15_000, f"{actual:,.0f}"
            else:
                raise ValueError("unknown Minard check")
            expected, tolerance = re.search(r"=([\d.]+)±([\d.]+)%", spot).groups()
            ok = abs(actual - float(expected)) <= float(expected) * float(tolerance) / 100
            return ok, f"{actual:,.0f}"
        if "_per_capita" in spot:
            left, right = (s.removesuffix("_per_capita") for s in spot.split(">"))
            a = float(frame.loc[frame.key == left, "per_capita_barrels"].iloc[0])
            b = float(frame.loc[frame.key == right, "per_capita_barrels"].iloc[0])
            return a > b, f"{a:.2f} > {b:.2f} bbl/person"
        if spot == "LU00>200%_EU":
            actual = truth_value(frame, "LU00") / truth_value(frame, "EU27_2020") * 100
            return actual > 200, f"{actual:.0f}% of EU27"
        comparison = re.fullmatch(r"([A-Z0-9-]+)([<>])(-?[A-Z0-9.-]+)", spot)
        if comparison:
            left, operator, right = comparison.groups()
            a = truth_value(frame, left)
            # "06085>06019" compares two FIPS keys; "BDI<1500" a key with a number.
            b = truth_value(frame, right) if right in set(frame.key.astype(str)) else float(right)
            ok = a > b if operator == ">" else a < b
            return ok, f"{num(a)} {operator} {num(b)}"
        equality = re.fullmatch(r"([A-Z0-9-]+)=([\d.]+)±([\d.]+)(%)?", spot)
        if equality:
            key, expected, tolerance, percent = equality.groups()
            actual = truth_value(frame, key)
            expected_f = float(expected)
            tolerance_f = expected_f * float(tolerance) / 100 if percent else float(tolerance)
            return abs(actual - expected_f) <= tolerance_f, f"{num(actual)}, {(actual / expected_f - 1) * 100:+.1f}%"
        raise ValueError("unsupported expression")
    except Exception as exc:
        return False, f"unverifiable: {exc}"


def evaluate(ids: Iterable[str]):
    specs = tomllib.loads(SPECS_PATH.read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in specs["map"]}
    results, counts = {}, {}
    for map_id in ids:
        path = MAPS_DIR / map_id / "truth.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype={"key": str})
        counts[map_id] = len(frame)
        results[map_id] = [(s, *evaluate_spot(map_id, s, frame)) for s in by_id[map_id]["spot"]]
    return results, counts


REPORT_NOTES = """
## What was measured, and what the spec should know

All values above are recomputed from the committed `truth.csv` files alone
(`uv run evals/maps/etalons/build.py --check-only`), never from the
renderer.

### Spec defects (the reference cannot fix these)

- **`ca_county_income` excludes are unsatisfiable.** Las Vegas (-115.1, 36.2)
  and Reno (-119.8, 39.5) both lie inside California's own lon/lat bounding
  box (-124.41…-114.13, 32.53…42.01, Census 1:500k counties). Any rectangular frame that contains all
  58 counties contains both cities, so `run.py`'s bbox-based `excludes` check
  will fail for every correct map. The reference frames them but leaves
  Nevada unshaded. Suggest replacing them with points outside the box, e.g.
  Phoenix (-112.1, 33.45) and Salt Lake City (-111.9, 40.76).
- **`france_lgv` `total_km` is route length, not way length.** OpenStreetMap
  draws each track of a double-track LGV as its own way: summing the 7,248
  ways gives 5,282 km and FAILS `2800±20%`; dissolving parallel tracks (union
  of 30 m flat-capped buffers ÷ 64.5 m, service sidings excluded) gives
  2,627 km and passes. A service that sums OSM way lengths will fail this
  check while drawing the right network.
- **`france_lgv` `line_near` has no stated tolerance.** Measured distances
  from the spec points to the nearest high-speed way: Lyon 7.7 km, Bordeaux
  11.4 km, Strasbourg 12.1 km — LGVs stop at the city edge and trains run on
  classic track into the centre. The reference uses 20 km. A 5 km tolerance
  would fail all three for correct data.
- **`ukraine_oblast_population` truth wording.** Ukrstat's table is the
  *present* (наявне, de facto) population, not the resident population the
  spec's `truth` line names. The values match the spec's spot checks exactly
  (Kyiv 2,952,301; Kharkiv 2,598,961), and the 25 published regions sum
  exactly to the printed national total, 41,167,335.
- **`gdp_world` source drift.** The OWID slug `gdp-per-capita-worldbank` is
  now a 2026 multi-source release (Eurostat, OECD, IMF, World Bank), latest
  years 2022–2025. India sits at +11.5% of the spec's 9,000 (limit 15%), so
  `IND=9000±15%` is one release away from failing for a correct map.
- **`population_change` row count.** OWID has 238 ISO3 entities with both
  years (territories such as Gibraltar and Vatican included); the spec's
  `units = [180, 220]` counts drawn countries, and Natural Earth 1:50m draws
  fewer. Not a value problem.

### Primary-source changes found while building

- The Census API now refuses keyless requests (it returns an HTML "Missing
  Key" page with HTTP 200, which the original builder cached as JSON). The
  California values come from the ACS table-based summary file for the same
  release (B19013, 2019–2023); the downloader now refuses HTML where data was
  expected.
- OWID Grapher CSVs gained a second column ("World region according to
  OWID"); the original builder crashed on it.
- Eurostat/NUTS spell Greece `EL`. The original builder's EU list used `GR`,
  which silently dropped all 13 Greek NUTS 2 regions (232 rows instead of 245).
- Natural Earth `ADM0_A3` gives South Sudan as `SDS` and Kosovo as `KOS`;
  keys here are `ISO_A3_EH` with `ADM0_A3` as fallback, and OWID's `OWID_KOS`
  is keyed `KOS` to match mapsvc's spelling.
- EIA SEDS has no diesel-only state series. `DFTCP` is all distillate fuel
  oil (on- and off-road diesel plus heating oil), latest year 2024; that is
  why Vermont and New Hampshire rank high per resident.
- HistData's Minard temperatures are in degrees Réaumur, not Celsius; the
  reference converts (°C = °R × 1.25), and truth keeps the source unit.

### Reference images

Each `reference.png` is 1600 px wide, built with geopandas/matplotlib
outside mapsvc. Projections: Equal Earth (world maps; Pacific-centred for
earthquakes), LAEA centred on Ukraine, USA Albers (EPSG:5070) with Alaska and
Hawaii insets, ETRS89-LAEA (EPSG:3035) for both EU maps with the eight
outermost regions inset, California Albers (EPSG:3310), Lambert-93 for
France, and an equirectangular projection at 55°N for Minard. Missing values
are hatched, never coloured as a class; every footnote names source, licence,
vintage, projection, classification and exclusions.
"""


def render_report(results, counts) -> str:
    lines = [
        "# Etalon build report",
        "",
        "Generated by `uv run evals/maps/etalons/build.py`. Spot checks are evaluated "
        "from the written `truth.csv` files only.",
        "",
        "| Map | Rows | Spot checks |",
        "|---|---:|---|",
    ]
    total = passed = 0
    for map_id in ALL_IDS:
        if map_id not in results:
            lines.append(f"| `{map_id}` | — | not built |")
            continue
        cells = []
        for spot, ok, measured in results[map_id]:
            total += 1
            passed += ok
            cells.append(f"{'PASS' if ok else '**FAIL**'} `{spot}` ({measured})")
        lines.append(f"| `{map_id}` | {counts[map_id]} | {'<br>'.join(cells)} |")
    lines += ["", f"**{passed} of {total} spot checks pass.**", REPORT_NOTES.rstrip(), ""]
    return "\n".join(lines)


def print_results(results, counts) -> None:
    print("\nSpot-check results")
    for map_id, checks in results.items():
        print(f"{map_id} ({counts[map_id]} rows)")
        for spot, ok, measured in checks:
            print(f"   {'PASS' if ok else 'FAIL'}  {spot:32} {measured}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=ALL_IDS, help="map ids; default: all ten")
    parser.add_argument("--check-only", action="store_true", help="only evaluate existing truth.csv files")
    args = parser.parse_args()
    ids = tuple(args.ids) or ALL_IDS
    if not args.check_only:
        for map_id in ids:
            print(f"\n[{map_id}]", flush=True)
            BUILDERS[map_id]()
    results, counts = evaluate(ALL_IDS)
    print_results({k: v for k, v in results.items() if k in ids}, counts)
    (Path(__file__).resolve().parent / "REPORT.md").write_text(render_report(results, counts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
