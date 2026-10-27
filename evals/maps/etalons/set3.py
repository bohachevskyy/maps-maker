#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "geopandas>=1.0,<2",
#   "matplotlib>=3.9,<4",
#   "numpy>=2,<3",
#   "openpyxl>=3.1,<4",
#   "pandas>=2.2,<3",
#   "pillow>=10",
#   "pypdf>=5,<7",
#   "pyproj>=3.7,<4",
#   "requests>=2.32,<3",
#   "shapely>=2.0,<3",
#   "xlrd>=2.0,<3",
# ]
# ///
"""Reference ("etalon") maps for the ten Set-3 specs.

    uv run evals/maps/etalons/set3.py [ids...] [--check-only] [--fetch-only]

Ids: southeast_asia_life_expectancy, africa_gdp_per_capita,
france_regions_population, eu_elderly_share_nuts2 (choropleths and symbols);
indonesia_quakes_2025, north_indian_cyclones_2023, paris_metro,
lviv_settlements, germany_wind_turbines, norway_lighthouses (overlays).

Shares the downloader, canvas and palette of ``build.py`` and the helpers of
``set2_choropleth.py`` / ``set2_overlays.py`` (imported, not copied) and, like
them, never imports mapsvc: this is an independent oracle.  Cached downloads
live in ``.cache/`` with an ``s3_`` prefix; delete a file to refresh it.
Spot checks (and ``must_include``) are evaluated from the written truth.csv
files only and written to REPORT_set3.md; specs.toml is never edited.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import textwrap
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as B  # noqa: E402  (sets the Agg backend)
import set2_choropleth as C  # noqa: E402
import set2_overlays as O  # noqa: E402

import geopandas as gpd  # noqa: E402
import matplotlib as mpl  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
import shapely  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402
from pyproj import Geod, Transformer  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import LineString, MultiLineString, Point, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from build import (  # noqa: E402
    CACHE_DIR, HALO, HTTP, INK, LAND, MAPS_DIR, MUTED, NODATA, PAPER, WATER, WIDTH_IN, HATCH,
    Canvas, check_window, classify, fetch, iso_rows, latest_rows, load_world, plot_classes,
    plot_missing, project_points, ramp, variable_column, write_csv, write_md,
)
from set2_choropleth import callout, column_legend, label  # noqa: E402

REPORT_PATH = HERE / "REPORT_set3.md"
WINDOW_NOTES_PATH = CACHE_DIR / "s3_window_notes.json"
_window_notes: dict[str, list[str]] = {}


def check_window(map_id, crs, extent, **kw):   # noqa: F811  (records what the spec's frame checks say)
    notes = B.check_window(map_id, crs, extent, **kw)
    _window_notes[map_id] = notes
    return notes


def save_window_notes() -> None:
    old = json.loads(WINDOW_NOTES_PATH.read_text()) if WINDOW_NOTES_PATH.exists() else {}
    old.update(_window_notes)
    WINDOW_NOTES_PATH.write_text(json.dumps(old, indent=1, ensure_ascii=False))
SPECS_PATH = MAPS_DIR / "specs.toml"
IDS = (
    "southeast_asia_life_expectancy",
    "france_regions_population",
    "indonesia_quakes_2025",
    "paris_metro",
    "lviv_settlements",
    "germany_wind_turbines",
    "north_indian_cyclones_2023",
    "africa_gdp_per_capita",
    "eu_elderly_share_nuts2",
    "norway_lighthouses",
)
GEOD = Geod(ellps="WGS84")
NEAR_KM = {"event_near": 100.0, "line_near": 20.0}   # as evals/maps/run.py
UA = "maps-maker-etalon/1.0 (independent evaluation builder; dmytro.bohachevskyy@gmail.com)"
HTTP.headers.update({"User-Agent": UA})

OWID_LIFE_URL = "https://ourworldindata.org/grapher/life-expectancy.csv"
OWID_GDP_URL = "https://ourworldindata.org/grapher/gdp-per-capita-worldbank.csv"
NE_LAKES_URL = f"{B.NE}/50m_physical/ne_50m_lakes.zip"
NE10_COUNTRIES_URL = f"{B.NE}/10m_cultural/ne_10m_admin_0_countries.zip"
NE10_LAND_URL = f"{B.NE}/10m_physical/ne_10m_land.zip"
FRANCE_GEO_URL = B.FRANCE_GEO_URL

# --------------------------------------------------------------------------
# Overpass queries (all cached by file name; the payload keeps its query)

PARIS_QUERY = """
[out:json][timeout:600];
rel["route"="subway"]["network"="Métro de Paris"](48.50,1.90,49.10,2.80)->.m;
.m out body geom;
node(r.m)["railway"~"^(station|stop)$"];
out body;
"""
LVIV_QUERY = """
[out:json][timeout:900];
area["ISO3166-2"="UA-46"]["admin_level"="4"]->.v;
node["place"](area.v);
out body;
rel(area.v)["boundary"="administrative"]["admin_level"~"^(6|7)$"];
out body geom;
rel["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"="UA-46"];
out body geom;
"""
GERMANY_QUERY = """
[out:json][timeout:900];
area["ISO3166-1"="DE"]["admin_level"="2"]->.de;
nwr["power"="generator"]["generator:source"="wind"](area.de);
out center tags qt;
"""
NORWAY_QUERY = """
[out:json][timeout:900];
area["ISO3166-1"="NO"]["admin_level"="2"]->.no;
nwr["man_made"="lighthouse"](area.no);
out center tags qt;
area["ISO3166-1"="SJ"]->.sj;
nwr["man_made"="lighthouse"](area.sj);
out center tags qt;
"""
PARIS_CONTEXT_QUERY = """
[out:json][timeout:300];
way["waterway"="river"]["name"~"^(La )?Seine$"](48.60,1.95,49.10,2.80);
out geom;
rel["boundary"="administrative"]["admin_level"="8"]["ref:INSEE"="75056"];
out geom;
"""
OVERPASS_FILES = {
    "paris_context": (PARIS_CONTEXT_QUERY, "s3_paris_context_overpass.json"),
    "paris_metro": (PARIS_QUERY, "s3_paris_metro_overpass.json"),
    "lviv_settlements": (LVIV_QUERY, "s3_lviv_overpass.json"),
    "germany_wind_turbines": (GERMANY_QUERY, "s3_germany_wind_overpass.json"),
    "norway_lighthouses": (NORWAY_QUERY, "s3_norway_lighthouses_overpass.json"),
}
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def overpass(map_id: str) -> dict:
    """POST the map's Overpass query once, cache the JSON, fall back across mirrors."""
    query, filename = OVERPASS_FILES[map_id]
    O.OVERPASS_ENDPOINTS = ENDPOINTS
    return O.overpass(query, filename)


def spec_entry(map_id: str) -> dict:
    return B.spec(map_id)


def thin_axes(ax) -> None:
    O.thin_frame(ax)


def main_fetch() -> None:
    for map_id in OVERPASS_FILES:
        payload = overpass(map_id)
        print(f"  {map_id}: {len(payload['elements'])} elements, OSM base {O.osm_base(payload)}", flush=True)


# --------------------------------------------------------------------------
# Shared drawing helpers


def sea_label(ax, crs, text, lon, lat, *, size=10, rotation=0):
    (x, y), = project_points(crs, [(lon, lat)])
    ax.text(x, y, text, fontsize=size, color="#5f8796", style="italic", ha="center", va="center",
            zorder=2, rotation=rotation)


def annotate(ax, x, y, text, dxdy, *, size=8.4, weight="normal", color=INK, line=True, z=9):
    dx, dy = dxdy
    ax.annotate(text, (x, y), xytext=dxdy, textcoords="offset points", fontsize=size,
                ha="center" if abs(dx) < 4 else ("left" if dx > 0 else "right"), va="center",
                zorder=z, linespacing=1.1, fontweight=weight, color=color, path_effects=HALO,
                arrowprops=dict(arrowstyle="-", color="#4d585d", linewidth=0.6, shrinkA=0, shrinkB=2)
                if line and (abs(dx) > 8 or abs(dy) > 8) else None)


def project_geoms(gdf, crs):
    return gdf.to_crs(crs)


def owid_latest(url: str, filename: str, codes: set[str]) -> tuple[pd.DataFrame, str, pd.DataFrame]:
    raw = pd.read_csv(fetch(url, filename))
    col = variable_column(raw)
    data = latest_rows(iso_rows(raw))
    return data[data.Code.isin(codes)].copy(), col, raw


def scale_bar(ax, x, y, km, *, label=None, size=8):
    ax.plot([x, x + km * 1000], [y, y], color=INK, linewidth=1.6, solid_capstyle="butt", zorder=9)
    ax.text(x + km * 500, y + km * 1000 * 0.04, label or f"{km} km", ha="center", va="bottom",
            fontsize=size, color=MUTED, zorder=9, path_effects=HALO)


# --------------------------------------------------------------------------
# 1. Southeast Asia: life expectancy (OWID / UN WPP 2024)

SEA = {"BRN": "Brunei", "KHM": "Cambodia", "IDN": "Indonesia", "LAO": "Laos", "MYS": "Malaysia",
       "MMR": "Myanmar", "PHL": "Philippines", "SGP": "Singapore", "THA": "Thailand",
       "TLS": "Timor-Leste", "VNM": "Vietnam"}


def build_southeast_asia_life_expectancy() -> pd.DataFrame:
    data, col, _raw = owid_latest(OWID_LIFE_URL, "s3_owid_life_expectancy.csv", set(SEA))
    # OWID spells Timor-Leste "East Timor"; display names are the UN M49 short names.
    truth = B.standard_truth(data, col, "years (period life expectancy at birth, both sexes)")
    truth["name"] = truth.key.map(SEA)
    write_csv("southeast_asia_life_expectancy", truth)
    year = int(truth.year.max())
    write_md("southeast_asia_life_expectancy", [
        f"- URL/API: {OWID_LIFE_URL} (Grapher CSV; latest non-missing year per ISO3 code; metadata at the same path with `.metadata.json`). Region = the 11 states of UN M49 sub-region 'South-eastern Asia' (BRN KHM IDN LAO MYS MMR PHL SGP THA TLS VNM).",
        f"- Vintage: OWID 'Life expectancy' = UN World Population Prospects 2024 (from 1950; earlier years HMD/Riley/Zijdeman); latest year {year} for all {len(truth)} states. WPP 2024 itself ends at 2023 for observed values (2024+ are projections and are not in this series).",
        "- Licence: OWID data CC BY 4.0 (UN WPP: CC BY 3.0 IGO). Boundaries: Natural Earth 1:50m and 1:10m (public domain).",
        f"- Rows: {len(truth)} — key (ISO3), name (UN M49 short name), value (years, period life expectancy at birth, both sexes), unit, year. Timor-Leste is 'East Timor' in the OWID file.",
        "- Caveat: period life expectancy is a synthetic cohort measure of that year's mortality, not a forecast of how long people born in 2023 will live; Myanmar's 2023 value reflects conflict and weak registration.",
    ])
    crs = "+proj=laea +lat_0=3 +lon_0=114 +datum=WGS84 +units=m +no_defs"
    world = load_world()
    sea = world[world.key.isin(SEA)][["key", "geometry"]].dissolve("key", as_index=False)
    sea = sea.merge(truth[["key", "value", "name"]], on="key")
    # frame: the eleven states, trimmed to the Indonesian archipelago proper
    framed = sea.geometry.intersection(box(90, -11.5, 142, 29))
    p = gpd.GeoSeries(framed, crs=4326).to_crs(crs)
    xmin, ymin, xmax, ymax = p.total_bounds
    extent = (xmin - 330_000, xmax + 1_650_000, ymin - 150_000, ymax + 130_000)
    check_window("southeast_asia_life_expectancy", crs, extent)
    edges = [68, 70, 72, 75, 78]
    colors = ramp("YlGnBu", len(edges) + 1, 0.10, 0.98)
    cv = Canvas(extent, "Life expectancy across Southeast Asia",
                f"Years at birth, both sexes, {year} · the 11 states of the UN's South-eastern Asia sub-region",
                map_w=15.2, footer=0.95)
    ax = cv.ax
    others = world[~world.key.isin(SEA) & (world.key != "ATA")]
    others.clip(box(75, -30, 165, 45)).to_crs(crs).plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    proj = sea.to_crs(crs)
    plot_classes(ax, proj, "value", edges, colors, edgecolor="white", linewidth=0.45)
    classes = dict(zip(proj.key, classify(proj.value, edges)))
    # graticule
    t = Transformer.from_crs(4326, crs, always_xy=True)
    for lon in range(95, 141, 10):
        lats = np.linspace(-12, 30, 100)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        (lx, ly), = project_points(crs, [(lon, -11.3)])
        ax.text(lx, ly, f"{lon}°E", fontsize=7.5, color="#7c97a1", ha="center", va="bottom", zorder=0.6)
    for lat in range(-10, 29, 10):
        lons = np.linspace(90, 143, 100)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        (lx, ly), = project_points(crs, [(91.0, lat)])
        ax.text(lx, ly + 15_000, f"{abs(lat)}°{'N' if lat >= 0 else 'S'}" if lat else "Equator", fontsize=7.5,
                color="#7c97a1", ha="left", va="bottom", zorder=0.6)
    pos = {  # key -> (lon, lat, dx, dy) value labels placed on the unit; None offset = in place
        "MMR": (96.3, 21.3, 0, 0), "THA": (101.0, 15.6, 0, 0), "LAO": (102.9, 19.0, 0, 0),
        "VNM": (105.6, 21.2, 0, 0), "KHM": (104.9, 12.7, 0, 0), "MYS": (102.2, 4.2, 0, 0),
        "IDN": (114.3, -1.2, 0, 0), "PHL": (121.9, 16.5, 0, 0),
    }
    for key, (lon, lat, _dx, _dy) in pos.items():
        v = float(truth.loc[truth.key == key, "value"].iloc[0])
        (x, y), = project_points(crs, [(lon, lat)])
        dark = classes[key] >= 4
        size = 11 if key in ("IDN", "MMR", "THA", "VNM", "PHL") else 9.5
        ax.text(x, y, f"{SEA[key]}\n{v:.1f}", fontsize=size, ha="center", va="center", zorder=8,
                linespacing=1.15, fontweight="bold" if key == "IDN" else "normal",
                color="white" if dark else INK, path_effects=None if dark else HALO)
    # Malaysia's Borneo half, Brunei, Timor-Leste, Singapore: callouts with a disc in the class colour
    small = {
        "MYS": (116.2, 4.3, (46, 30), None),
        "BRN": (114.65, 4.55, (-60, 34), "Brunei"),
        "TLS": (125.85, -8.8, (46, -22), "Timor-Leste"),
        "SGP": (103.82, 1.35, (-70, -42), "Singapore"),
    }
    for key, (lon, lat, off, text) in small.items():
        v = float(truth.loc[truth.key == key, "value"].iloc[0])
        (x, y), = project_points(crs, [(lon, lat)])
        if key == "MYS":
            annotate(ax, x, y, f"Malaysia (Borneo)\n{v:.1f}", off, size=9.5)
            continue
        ax.scatter([x], [y], s=150, color=colors[int(classes[key])], edgecolor=INK, linewidth=0.9, zorder=7)
        annotate(ax, x, y, f"{text}\n{v:.1f}", off, size=9.5)
    # sea names
    for text, lon, lat, rot in (("South China\nSea", 112.0, 11.5, 0), ("Andaman\nSea", 95.6, 10.0, 0),
                                ("Java Sea", 111.5, -5.1, 0), ("Celebes\nSea", 121.5, 3.0, 0),
                                ("Philippine\nSea", 132.5, 14.0, 0), ("Indian Ocean", 100.0, -9.4, 0),
                                ("Banda Sea", 128.5, -5.5, 0), ("Gulf of\nThailand", 101.8, 9.3, 0)):
        sea_label(ax, crs, text, lon, lat, rotation=rot)
    for text, lon, lat in (("CHINA", 100.5, 26.8), ("INDIA", 92.6, 24.5), ("BANGLADESH", 91.6, 23.4),
                           ("PAPUA\nNEW GUINEA", 142.0, -6.6)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, text, fontsize=8, color="#9a958a", ha="center", va="center", zorder=2)
    # Singapore inset (10m geometry)
    c10 = gpd.read_file(fetch(NE10_COUNTRIES_URL, "s3_ne_10m_admin_0_countries.zip"))
    c10["key"] = np.where(c10.ISO_A3_EH.str.fullmatch(r"[A-Z]{3}", na=False), c10.ISO_A3_EH, c10.ADM0_A3)
    win = box(103.45, 1.12, 104.25, 1.52)
    loc_crs = "+proj=laea +lat_0=1.33 +lon_0=103.85 +datum=WGS84 +units=m"
    near = c10[c10.key.isin(["SGP", "MYS", "IDN"])].copy()
    near["geometry"] = near.geometry.intersection(win)
    near = near[~near.geometry.is_empty].to_crs(loc_crs)
    ins_w, ins_h = 2.5, 1.7
    ix = cv.left + cv.map_w - ins_w - 0.15
    iy = 0.95 + 0.15
    # place the inset in the Indian Ocean, bottom-left
    ix, iy = cv.left + 0.2, 0.95 + 0.25
    inset = cv.add_axes(ix, iy, ins_w, ins_h, frame=True)
    inset.set_facecolor(WATER)
    b = gpd.GeoSeries([win], crs=4326).to_crs(loc_crs).total_bounds
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    half_w = (b[2] - b[0]) / 2
    inset.set_xlim(cx - half_w, cx + half_w)
    inset.set_ylim(cy - half_w * ins_h / ins_w, cy + half_w * ins_h / ins_w)
    near[near.key != "SGP"].plot(ax=inset, color=LAND, edgecolor="white", linewidth=0.5)
    near[near.key == "SGP"].plot(ax=inset, color=colors[int(classes["SGP"])], edgecolor="white", linewidth=0.5)
    inset.text(0.04, 0.95, "Singapore, enlarged", transform=inset.transAxes, fontsize=8.6, va="top", fontweight="bold")
    inset.text(0.96, 0.05, "Malaysia (Johor)", transform=inset.transAxes, fontsize=7.5, va="bottom", ha="right", color=MUTED)
    inset.text(0.96, 0.95, "Indonesia\n(Riau Is.)", transform=inset.transAxes, fontsize=7.5, va="top", ha="right", color=MUTED)
    sg = float(truth.loc[truth.key == "SGP", "value"].iloc[0])
    inset.text(0.5, 0.42, f"Singapore {sg:.1f}", transform=inset.transAxes, fontsize=8.6, ha="center", va="center",
               path_effects=HALO, zorder=9)
    bar = 20_000
    inset.plot([cx - half_w + 0.1 * half_w, cx - half_w + 0.1 * half_w + bar],
               [cy - half_w * ins_h / ins_w * 0.86] * 2, color=INK, linewidth=1.5, solid_capstyle="butt")
    inset.text(cx - half_w + 0.1 * half_w + bar / 2, cy - half_w * ins_h / ins_w * 0.80, "20 km", ha="center",
               fontsize=7, color=MUTED, va="bottom")
    # connector from the island to the inset
    (sx, sy), = project_points(crs, [(103.82, 1.35)])
    fig_x0 = (ix + ins_w) / WIDTH_IN
    # legend + ranking, in the empty Pacific at the right
    col_x = cv.left + cv.map_w - 3.6
    top = cv.H - cv.header - 0.2
    lg = cv.panel(col_x, top, 3.5, 3.3)
    counts = np.bincount(classify(truth.value, edges).astype(int), minlength=len(colors))
    labels = range_labels = [f"< {edges[0]}"] + [f"{a} – {b}" for a, b in zip(edges[:-1], edges[1:])] + [f"≥ {edges[-1]}"]
    labels = [f"{lab} years   ({n})" for lab, n in zip(labels, counts)]
    column_legend(lg, colors, labels, "Life expectancy at birth, years\n(states per class)", extra=[(LAND, "Outside Southeast Asia")])
    rk = cv.panel(col_x, top - 3.5, 3.5, 3.1)
    rk.text(0, 3.08, f"Ranked, {year}", va="top", fontsize=10.5, fontweight="bold")
    ranked = truth.sort_values("value", ascending=False)
    for i, r in enumerate(ranked.itertuples()):
        y = 2.7 - i * 0.245
        rk.add_patch(Rectangle((0, y - 0.07), 0.16, 0.16, facecolor=colors[int(classes[r.key])], edgecolor="#c9c4b8", linewidth=0.4))
        rk.text(0.28, y, SEA[r.key], va="center", fontsize=9.3)
        rk.text(3.35, y, f"{r.value:.1f}", va="center", ha="right", fontsize=9.3)
    cv.footnote([
        f"Source: Our World in Data, 'Life expectancy', from the UN World Population Prospects 2024 (period life expectancy at birth, both sexes), {year}; CC BY 4.0. Boundaries: Natural Earth 1:50m (Singapore inset 1:10m), public domain.",
        "Projection: Lambert azimuthal equal-area centred on 3°N 114°E. Classification: six hand-set classes (68, 70, 72, 75, 78 years) so the cluster of low-70s values stays distinguishable from Singapore. "
        "All 11 UN M49 South-eastern Asia states shown with the latest year (2023 for every one); none excluded. Brunei, Timor-Leste and Singapore are marked with discs.",
    ], width=205, y_in=0.12)
    cv.save("southeast_asia_life_expectancy")
    return truth


# --------------------------------------------------------------------------
# 2. Africa: GDP per capita (OWID / World Bank + IMF), latest year


AFRICA_NAMES = {  # one naming style: the UN short English names
    "DZA": "Algeria", "AGO": "Angola", "BEN": "Benin", "BWA": "Botswana", "BFA": "Burkina Faso",
    "BDI": "Burundi", "CPV": "Cabo Verde", "CMR": "Cameroon", "CAF": "Central African Rep.", "TCD": "Chad",
    "COM": "Comoros", "COG": "Congo", "COD": "DR Congo", "CIV": "Côte d’Ivoire", "DJI": "Djibouti",
    "EGY": "Egypt", "GNQ": "Equatorial Guinea", "ERI": "Eritrea", "SWZ": "Eswatini", "ETH": "Ethiopia",
    "GAB": "Gabon", "GMB": "Gambia", "GHA": "Ghana", "GIN": "Guinea", "GNB": "Guinea-Bissau",
    "KEN": "Kenya", "LSO": "Lesotho", "LBR": "Liberia", "LBY": "Libya", "MDG": "Madagascar",
    "MWI": "Malawi", "MLI": "Mali", "MRT": "Mauritania", "MUS": "Mauritius", "MAR": "Morocco",
    "MOZ": "Mozambique", "NAM": "Namibia", "NER": "Niger", "NGA": "Nigeria", "RWA": "Rwanda",
    "STP": "São Tomé and Príncipe", "SEN": "Senegal", "SYC": "Seychelles", "SLE": "Sierra Leone",
    "SOM": "Somalia", "ZAF": "South Africa", "SSD": "South Sudan", "SDN": "Sudan", "TZA": "Tanzania",
    "TGO": "Togo", "TUN": "Tunisia", "UGA": "Uganda", "ZMB": "Zambia", "ZWE": "Zimbabwe",
    "ESH": "Western Sahara",
}


def africa_geometries(world: gpd.GeoDataFrame, crs) -> gpd.GeoDataFrame:
    """The 54 states plus Western Sahara.  Natural Earth draws the de facto
    line: its Morocco polygon holds the 178,000 km2 of Western Sahara west of
    the berm; Western Sahara is everything south of 27°40'N."""
    africa = world[world.key.isin(C.AFRICA)][["key", "NAME", "geometry"]].dissolve("key", as_index=False, aggfunc="first")
    if set(africa.key) != C.AFRICA:
        raise ValueError(f"Natural Earth Africa mismatch: {set(africa.key) ^ C.AFRICA}")
    ws_box = box(-20.0, 20.0, -8.0, 27 + 40 / 60)
    mar, esh = africa.key == "MAR", africa.key == "ESH"
    moved = africa.loc[mar, "geometry"].iloc[0].intersection(ws_box)
    africa.loc[esh, "geometry"] = [africa.loc[esh, "geometry"].iloc[0].union(moved)]
    africa.loc[mar, "geometry"] = [africa.loc[mar, "geometry"].iloc[0].difference(ws_box)]
    return africa


def build_africa_gdp_per_capita() -> pd.DataFrame:
    data, col, _raw = owid_latest(OWID_GDP_URL, "s3_owid_gdp.csv", set(C.AFRICA))
    truth = B.standard_truth(data, col, "international-$ per person (PPP, constant 2021 prices)")
    write_csv("africa_gdp_per_capita", truth)
    absent = sorted(C.AFRICA - set(truth.key))
    years = truth.year
    write_md("africa_gdp_per_capita", [
        f"- URL/API: {OWID_GDP_URL} (Grapher CSV; latest non-missing year per ISO3 code; metadata at the same path with `.metadata.json`). Africa = the 54 UN member states of UN M49 'Africa' plus Western Sahara (55 units).",
        f"- Vintage: OWID 'GDP per capita' (World Bank / IMF / OECD / Eurostat, PPP, constant 2021 international-$), retrieved 2026-10-27; latest year {int(years.max())} for {int((years == years.max()).sum())} of {len(truth)} countries (all years {int(years.min())}–{int(years.max())}); recent years are IMF estimates and projections, not outturns.",
        "- Licence: OWID data CC BY 4.0 (World Bank WDI CC BY 4.0; IMF WEO terms). Boundaries: Natural Earth 1:50m (public domain).",
        f"- Rows: {len(truth)} of {len(C.AFRICA)} units. No value in the source: {', '.join(f'{k} ({AFRICA_NAMES[k]})' for k in absent)} — drawn hatched. Keys are ISO3; OWID spells Cabo Verde 'Cape Verde' and Côte d'Ivoire 'Cote d'Ivoire' in the name column.",
        "- Caveat: Natural Earth 1:50m puts all of Western Sahara's occupied area inside its Morocco polygon; the reference cuts Morocco at 27°40′N so Western Sahara is hatched whole rather than shown in Morocco's class. The OWID value for Morocco therefore describes the internationally recognised Morocco, drawn here without Western Sahara.",
    ])
    crs = "+proj=laea +lat_0=3 +lon_0=17 +datum=WGS84 +units=m +no_defs"
    world = load_world()
    africa = africa_geometries(world, crs).merge(truth[["key", "value", "year"]], on="key", how="left").to_crs(crs)
    framed = africa.geometry.intersection(
        gpd.GeoSeries([box(-30, -36, 65, 40).segmentize(0.5)], crs=4326).to_crs(crs).iloc[0])
    xmin, ymin, xmax, ymax = framed.total_bounds
    extent = (xmin - 260_000, xmax + 330_000, ymin - 200_000, ymax + 220_000)
    check_window("africa_gdp_per_capita", crs, extent)
    edges = [1500, 2500, 4000, 7000, 10000, 15000]
    colors = ramp("YlGnBu", len(edges) + 1, 0.04, 1.0)
    classes = dict(zip(truth.key, classify(truth.value, edges)))
    cv = Canvas(extent, "GDP per capita across Africa",
                f"International dollars per person, PPP at constant 2021 prices · latest year, {int(years.max())} for all countries with data",
                map_w=10.4, footer=1.15)
    ax = cv.ax
    world[~world.key.isin(C.AFRICA) & (world.key != "ATA")].clip(box(-35, -45, 75, 60)).to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    valued = africa.dropna(subset=["value"])
    for row in valued[valued.key.isin(C.ISLANDS)].itertuples():
        c = row.geometry.envelope.centroid
        ax.scatter([c.x], [c.y], s=330, color=colors[int(classes[row.key])], edgecolor="#4d585d",
                   linewidth=0.7, zorder=2, alpha=0.9)
    plot_classes(ax, valued, "value", edges, colors, edgecolor="white", linewidth=0.45)
    plot_missing(ax, africa[africa.value.isna()], linewidth=0.45)
    islands = {"CPV": ("Cabo Verde", (0, 34)), "STP": ("São Tomé and Príncipe", (-70, -34)),
               "COM": ("Comoros", (-52, 16)), "MUS": ("Mauritius", (0, -30)), "SYC": ("Seychelles", (0, 28))}
    for row in valued[valued.key.isin(islands)].itertuples():
        c = row.geometry.envelope.centroid
        name, offset = islands[row.key]
        callout(ax, c.x, c.y, f"{name} {row.value / 1000:.1f}k", offset, size=8.4)
    for row in valued[valued.key.isin(C.SMALL_MAINLAND)].itertuples():
        p = row.geometry.representative_point()
        callout(ax, p.x, p.y, f"{AFRICA_NAMES[row.key]} {row.value / 1000:.1f}k", C.SMALL_MAINLAND[row.key], size=8.0)
    nudges = {"SOM": (60_000, -150_000), "MOZ": (110_000, 120_000), "ZAF": (0, 80_000), "GHA": (0, -40_000),
              "COD": (-120_000, 180_000), "TZA": (0, 30_000), "MAR": (60_000, 0)}
    big = valued[(valued.geometry.area > 9e10) & ~valued.key.isin(islands) & ~valued.key.isin(C.SMALL_MAINLAND)]
    for row in big.itertuples():
        p = row.geometry.representative_point()
        dx, dy = nudges.get(row.key, (0, 0))
        dark = classes[row.key] >= 5
        size = 8.6 if row.geometry.area > 4e11 else 7.6
        text = f"{AFRICA_NAMES[row.key]}\n{row.value / 1000:.1f}k" if row.geometry.area > 2e11 else f"{row.value / 1000:.1f}k"
        label(ax, p.x + dx, p.y + dy, text, dark=dark, size=size)
    nolabel = africa[africa.value.isna()]
    for row in nolabel.itertuples():
        p = row.geometry.representative_point()
        if row.key == "ESH":
            callout(ax, p.x, p.y, "Western Sahara\nno data", (-62, 22), size=8.0)
        else:
            label(ax, p.x, p.y, f"{AFRICA_NAMES[row.key]}\nno data", size=7.6)
    for name, lon, lat in (("Atlantic\nOcean", -16.0, -12.0), ("Indian\nOcean", 52.0, -29.0),
                           ("Mediterranean Sea", 18.0, 34.4), ("Red Sea", 38.3, 20.2)):
        sea_label(ax, crs, name, lon, lat, rotation=-52 if name == "Red Sea" else 0)

    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 4.4)
    lab = ["< 1,500", "1,500 – 2,500", "2,500 – 4,000", "4,000 – 7,000", "7,000 – 10,000", "10,000 – 15,000", "≥ 15,000"]
    counts = np.bincount(classify(truth.value, edges).astype(int), minlength=len(colors))
    labels = [f"{a}   ({n})" for a, n in zip(lab, counts)]
    column_legend(lg, colors, labels, "GDP per capita, int.-$ (PPP)\n(countries per class)",
                  missing="No data (" + ", ".join(AFRICA_NAMES[k] for k in absent) + ")", extra=[(LAND, "Outside Africa")])
    tb = cv.panel(col_x, top - 4.75, col_w, 1.9)
    short = truth.assign(short=truth.key.map(AFRICA_NAMES))
    C.ranked_lists(tb, short, lambda v: f"{v / 1000:.1f}k", name_col="short", col2=2.45, width=col_w - 0.05, size=9.2)
    ratio = truth.value.max() / truth.value.min()
    summary = (f"The median African country sits at {truth.value.median():,.0f} int.-$. Island states and the oil-rich or mining-rich "
               f"economies lead; the richest ({AFRICA_NAMES[truth.loc[truth.value.idxmax(), 'key']]}) is {ratio:.0f} times the poorest "
               f"({AFRICA_NAMES[truth.loc[truth.value.idxmin(), 'key']]}). Values in thousands (k).")
    cv.fig.text(col_x / WIDTH_IN, (top - 6.85) / cv.H, textwrap.fill(summary, 56), va="top", fontsize=9.5,
                color=MUTED, linespacing=1.45)
    cv.footnote([
        "Source: Our World in Data, 'GDP per capita' (World Bank, IMF, OECD and Eurostat; PPP, constant 2021 international-$), retrieved 27 Oct 2026; CC BY 4.0. "
        f"Latest year per country: {int(years.max())} for all {len(truth)}; recent values are IMF estimates. Boundaries: Natural Earth 1:50m, public domain.",
        "Projection: Lambert azimuthal equal-area centred on 3°N 17°E. Classification: seven hand-set classes with wider steps at the top (1.5, 2.5, 4, 7, 10, 15 thousand) so the many low-income countries stay distinguishable from the two island outliers. "
        f"{len(truth)} of 55 units valued; {', '.join(AFRICA_NAMES[k] for k in absent)} hatched (no value). Western Sahara is drawn whole (south of 27°40′N), never in Morocco's class. Small island states drawn as discs in their class colour.",
    ], width=205)
    cv.save("africa_gdp_per_capita")
    return truth


# --------------------------------------------------------------------------
# 3. France: population of the 13 metropolitan regions (INSEE, 1 Jan 2026)

INSEE_PAGE = "https://www.insee.fr/fr/statistiques/8721456"
INSEE_URL = "https://www.insee.fr/fr/statistiques/fichier/8721456/estim-pop-nreg-sexe-gca-1975-2026.xlsx"
FR_REGIONS = {  # INSEE name -> (ISO 3166-2, English name, label offset in points, label side)
    "Auvergne-Rhône-Alpes": ("FR-ARA", "Auvergne-Rhône-Alpes"),
    "Bourgogne-Franche-Comté": ("FR-BFC", "Bourgogne-Franche-Comté"),
    "Bretagne": ("FR-BRE", "Brittany"),
    "Centre-Val-de-Loire": ("FR-CVL", "Centre-Val de Loire"),
    "Corse": ("FR-COR", "Corsica"),
    "Grand Est": ("FR-GES", "Grand Est"),
    "Hauts-de-France": ("FR-HDF", "Hauts-de-France"),
    "Île-de-France": ("FR-IDF", "Île-de-France"),
    "Normandie": ("FR-NOR", "Normandy"),
    "Nouvelle-Aquitaine": ("FR-NAQ", "Nouvelle-Aquitaine"),
    "Occitanie": ("FR-OCC", "Occitanie"),
    "Pays de la Loire": ("FR-PDL", "Pays de la Loire"),
    "Provence-Alpes-Côte d'Azur": ("FR-PAC", "Provence-Alpes-Côte d’Azur"),
}
GB_ISO_FIX = {"FR-20R": "FR-COR"}


def build_france_regions_population() -> pd.DataFrame:
    path = fetch_browser_xlsx(INSEE_URL, "s3_insee_estim_pop_nreg_1975_2026.xlsx")
    sheet = pd.read_excel(path, sheet_name="2026", header=None)
    rows = sheet.iloc[5:25, [0, 6]].dropna()
    rows.columns = ["region", "total"]
    rows["region"] = rows.region.astype(str).str.strip()
    metro = rows[rows.region.isin(FR_REGIONS)].copy()
    overseas = rows[rows.region.isin(["Guadeloupe", "Martinique", "Guyane", "La Réunion", "Mayotte"])]
    note = pd.read_excel(path, sheet_name="À savoir", header=None)[0].dropna().astype(str)
    updated = next(v for v in note if v.startswith("Données actualisées"))
    sum_row = rows[rows.region == "France métropolitaine"].total.iloc[0]
    truth = pd.DataFrame({
        "key": metro.region.map(lambda r: FR_REGIONS[r][0]),
        "name": metro.region.map(lambda r: FR_REGIONS[r][1]),
        "value": metro.total.astype(int),
        "unit": "inhabitants (estimated resident population at 1 January)",
        "year": 2026,
    }).sort_values("key").reset_index(drop=True)
    if truth.value.sum() != int(sum_row):
        raise AssertionError(f"regions sum {truth.value.sum()} != INSEE metropolitan total {sum_row}")
    write_csv("france_regions_population", truth)
    write_md("france_regions_population", [
        f"- URL/API: {INSEE_URL} (sheet '2026', column 'Ensemble / Total'; page {INSEE_PAGE}). INSEE 'Estimation de population au 1er janvier, par région, sexe et grande classe d'âge'; nouvelles régions (2016 boundaries). Keys are ISO 3166-2:FR region codes.",
        f"- Vintage: population at 1 January 2026, INSEE estimate published 13 January 2026 ({updated.strip().lower()}); the 2026 figures are provisional and are revised each January. Metropolitan total {int(sum_row):,}; overseas regions ({int(overseas.total.sum()):,} in Guadeloupe, Martinique, Guyane, La Réunion, Mayotte) are out of scope.",
        "- Licence: INSEE data under Licence Ouverte / Open Licence (Etalab 2.0). Boundaries: geoBoundaries gbOpen FRA ADM1 simplified (CC BY 4.0); neighbouring land Natural Earth 1:50m (public domain).",
        f"- Rows: {len(truth)} metropolitan regions, sum {int(truth.value.sum()):,} = INSEE's own 'France métropolitaine' row. Île-de-France {int(truth.loc[truth.key == 'FR-IDF', 'value'].iloc[0]):,}; Corsica {int(truth.loc[truth.key == 'FR-COR', 'value'].iloc[0]):,}.",
        "- Caveat: INSEE spells the region 'Centre-Val-de-Loire' (hyphens); the ISO name is 'Centre-Val de Loire'. Corsica is a 'collectivité territoriale unique' but counts as a region (ISO FR-COR; geoBoundaries labels it FR-20R). Figures exclude Saint-Martin and Saint-Barthélemy.",
    ])
    crs = "EPSG:2154"
    geo = gpd.read_file(fetch(FRANCE_GEO_URL, "france_adm1.geojson")).rename(columns={"shapeISO": "iso"})
    geo["key"] = geo.iso.replace(GB_ISO_FIX)
    geo = geo[geo.key.isin(truth.key)].merge(truth[["key", "name", "value"]], on="key")
    if len(geo) != 13:
        raise AssertionError(f"expected 13 geometries, got {len(geo)}")
    geo = geo.to_crs(crs)
    xmin, ymin, xmax, ymax = geo.total_bounds
    extent = (xmin - 260_000, xmax + 260_000, ymin - 90_000, ymax + 110_000)
    check_window("france_regions_population", crs, extent)
    cv = Canvas(extent, "Population of metropolitan France, by region",
                "Estimated residents at 1 January 2026 · circle area is proportional to population; region shading shows density",
                map_w=9.4, footer=1.0)
    ax = cv.ax
    world = load_world()
    world[(world.key != "FRA") & (world.key != "ATA")].clip(box(-15, 30, 30, 60)).to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    geo["density"] = geo.value / (geo.geometry.area / 1e6)
    edges = [60, 80, 110, 200]
    colors = ["#e5dfd0", "#d6cfbd", "#c5bca6", "#b2a78e", "#9c9078"]
    plot_classes(ax, geo, "density", edges, colors, edgecolor="white", linewidth=0.8)
    ax.set_facecolor(WATER)

    def radius(v):
        return 95_000 * math.sqrt(v / 12.4e6)

    anchors = {  # key -> (lon, lat) where the circle sits (a populated part of the region)
        "FR-IDF": (2.45, 48.75), "FR-ARA": (4.9, 45.55), "FR-NAQ": (-0.55, 44.85), "FR-OCC": (1.45, 43.6),
        "FR-HDF": (2.9, 50.15), "FR-GES": (6.1, 48.8), "FR-PAC": (5.9, 43.75), "FR-PDL": (-0.9, 47.4),
        "FR-BRE": (-3.0, 48.2), "FR-NOR": (0.2, 49.15), "FR-BFC": (4.9, 47.1), "FR-CVL": (1.6, 47.5),
        "FR-COR": (9.1, 42.15),
    }
    offsets = {  # label offset in points (dx, dy); None = centred on the circle
        "FR-IDF": (46, 74), "FR-ARA": (0, 0), "FR-NAQ": (0, 0), "FR-OCC": (0, 0), "FR-HDF": (10, 38),
        "FR-GES": (0, 0), "FR-PAC": (0, 0), "FR-PDL": (-80, -8), "FR-BRE": (-50, 30), "FR-NOR": (-80, 30),
        "FR-BFC": (0, 0), "FR-CVL": (-70, -42), "FR-COR": (66, 0),
    }
    pts = []
    for row in geo.sort_values("value", ascending=False).itertuples():
        (x, y), = project_points(crs, [anchors[row.key]])
        r = radius(row.value)
        ax.add_patch(Circle((x, y), r, facecolor="#c0392b", alpha=0.58, edgecolor="white", linewidth=1.0, zorder=4))
        pts.append((row, x, y, r))
    for row, x, y, r in pts:
        dx, dy = offsets[row.key]
        text = f"{row.name}\n{row.value / 1e6:.2f} M" if row.value >= 1e6 else f"{row.name}\n{row.value / 1e3:,.0f} k"
        if (dx, dy) == (0, 0):
            ax.text(x, y, text, fontsize=8.8 if r > 40_000 else 8, ha="center", va="center", zorder=8,
                    linespacing=1.1, path_effects=HALO, fontweight="bold" if row.value > 6e6 else "normal")
        else:
            annotate(ax, x, y, text, (dx, dy), size=8.6)
    for name, lon, lat in (("Atlantic Ocean", -4.4, 45.6), ("English Channel", -1.9, 50.0), ("Mediterranean Sea", 5.5, 42.35)):
        sea_label(ax, crs, name, lon, lat)
    for name, lon, lat in (("SPAIN", -2.0, 42.2), ("ITALY", 8.4, 44.9), ("SWITZERLAND", 8.1, 46.75), ("GERMANY", 8.9, 49.7),
                           ("BELGIUM", 4.5, 50.55), ("UNITED\nKINGDOM", -1.5, 51.6), ("LUXEMBOURG", 6.13, 49.78)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=7.5, color="#9a958a", ha="center", va="center", zorder=2)
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 3.0)
    lg.text(0, 2.98, "Population (circle area)", va="top", fontsize=10.5, fontweight="bold")
    scale_in = cv.map_w / (extent[1] - extent[0])
    y0 = 2.6
    for v, lab in ((12e6, "12 million"), (4e6, "4 million"), (1e6, "1 million")):
        rr = radius(v) * scale_in
        lg.add_patch(Circle((0.8, y0 - rr), rr, facecolor="#c0392b", alpha=0.58, edgecolor="white", linewidth=1.0))
        lg.text(1.8, y0 - rr, lab, va="center", fontsize=9.3)
        y0 -= 2 * rr + 0.1
    dl = cv.panel(col_x, top - 3.1, col_w, 2.5)
    counts = np.bincount(classify(geo.density, edges).astype(int), minlength=len(colors))
    labs = ["< 60", "60 – 80", "80 – 110", "110 – 200", "≥ 200"]
    column_legend(dl, colors, [f"{a}   ({n})" for a, n in zip(labs, counts)], "Density, inhabitants per km²\n(regions per class)",
                  extra=[(LAND, "Neighbouring countries")], step=0.26)
    cv.fig.text(col_x / WIDTH_IN, (top - 5.8) / cv.H, textwrap.fill(
        f"Metropolitan France counts {truth.value.sum() / 1e6:.1f} million residents; Île-de-France alone holds "
        f"{truth.loc[truth.key == 'FR-IDF', 'value'].iloc[0] / truth.value.sum() * 100:.0f}% of them. "
        f"The five overseas regions (together {overseas.total.sum() / 1e6:.2f} million) are not mapped.", 56),
        va="top", fontsize=9.5, color=MUTED, linespacing=1.45)
    cv.footnote([
        "Source: INSEE, 'Estimation de population au 1er janvier, par région, sexe et grande classe d'âge' (nouvelles régions), population at 1 January 2026 (provisional; published 13 Jan 2026), Licence Ouverte 2.0. "
        "Boundaries: geoBoundaries gbOpen (CC BY 4.0); neighbouring land: Natural Earth 1:50m.",
        "Projection: Lambert-93 (EPSG:2154), the French legal projection. Counts as circle area (largest drawn first); density (inhabitants per km², region area from the simplified boundaries) as the brown shading in five hand-set classes. "
        "All 13 metropolitan regions shown, Corsica included; the five overseas regions are outside the frame and excluded (not part of the metropolitan scope).",
    ], width=205)
    cv.save("france_regions_population")
    return truth


def fetch_browser_xlsx(url: str, filename: str) -> Path:
    return C.fetch_browser(url, filename, magic=b"PK")


# --------------------------------------------------------------------------
# 4. Indonesia: earthquakes M >= 5 in 2025 (USGS ComCat)

USGS_ID_URL = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=2025-01-01"
               "&endtime=2026-01-01&minmagnitude=5&minlatitude=-13&maxlatitude=9&minlongitude=93"
               "&maxlongitude=143&orderby=time-asc")
INDONESIAN_SEAS = {"Banda Sea", "Molucca Sea", "Ceram Sea", "Flores Sea", "Java Sea", "Sulawesi Sea", "Halmahera Sea"}
DEPTH_CLASSES = [(0, 70, "#d7301f", "Shallow, < 70 km"), (70, 300, "#f6a23c", "Intermediate, 70–300 km"),
                 (300, 1000, "#2b6ca3", "Deep, ≥ 300 km")]


def depth_colour(d: float) -> str:
    return next(c for lo, hi, c, _ in DEPTH_CLASSES if lo <= d < hi)


def build_indonesia_quakes_2025() -> pd.DataFrame:
    payload = json.loads(fetch(USGS_ID_URL, "s3_usgs_indonesia_2025_m5.geojson").read_text())
    rows = []
    for f in payload["features"]:
        lon, lat, depth = f["geometry"]["coordinates"]
        p = f["properties"]
        place = p["place"] or ""
        tail = place.split(", ")[-1]
        rows.append({"id": f["id"], "time": pd.to_datetime(p["time"], unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "lon": lon, "lat": lat, "depth_km": depth, "mag": p["mag"], "magType": p.get("magType", ""),
                     "place": place, "country": "Indonesia" if tail == "Indonesia" or place in INDONESIAN_SEAS else tail})
    allrows = pd.DataFrame(rows).sort_values(["time", "id"]).reset_index(drop=True)
    truth = allrows[allrows.country == "Indonesia"].reset_index(drop=True)
    others = allrows[allrows.country != "Indonesia"].reset_index(drop=True)
    write_csv("indonesia_quakes_2025", truth)
    generated = pd.to_datetime(payload["metadata"]["generated"], unit="ms", utc=True).strftime("%Y-%m-%d")
    big = truth[truth.mag >= 6.3].sort_values("mag", ascending=False)
    write_md("indonesia_quakes_2025", [
        f"- URL/API: {USGS_ID_URL} (FDSN event service, GeoJSON). The query rectangle is 93–143°E, 13°S–9°N ({len(allrows)} events); the truth keeps the events USGS places in Indonesia — place string ends ', Indonesia' or is an Indonesian sea ('Banda Sea') — {len(truth)} events.",
        f"- Vintage: USGS ComCat as of {generated}; events 2025-01-01T00:00Z ≤ t < 2026-01-01T00:00Z, magnitude ≥ 5.0 (preferred magnitude, mostly mww/mb).",
        "- Licence: U.S. Geological Survey Earthquake Hazards Program, U.S. government work (public domain). Plate boundaries: Bird (2003) PB2002 via github.com/fraxen/tectonicplas (ODC-BY 1.0). Land: Natural Earth 1:50m.",
        f"- Rows: {len(truth)} events — id, UTC time, lon, lat, depth_km, mag, magType, place, country. Largest: M{big.mag.iloc[0]:g} {big.place.iloc[0]} ({big.time.iloc[0][:10]}). The {len(others)} other events in the rectangle (Philippines {int((others.country == 'Philippines').sum())}, Timor Leste {int((others.country == 'Timor Leste').sum())}, Papua New Guinea {int((others.country == 'Papua New Guinea').sum())}, other {int((~others.country.isin(['Philippines', 'Timor Leste', 'Papua New Guinea'])).sum())}) are not in truth.csv; the map draws them as hollow grey circles for context.",
        f"- Caveat: 2025 had no M7+ event in Indonesia (maximum M{truth.mag.max():g}). ComCat revises magnitudes and adds/removes events, so the count can drift by a few. 'Indonesia' follows the USGS place string, i.e. the nearest named place; an offshore event nearer a Philippine or Timorese town is counted there.",
    ])
    crs = "+proj=merc +lon_0=118 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    (xa, ya), (xb, yb) = project_points(crs, [(91.9, -11.8), (141.8, 7.6)])
    extent = (xa, xb, ya, yb)
    check_window("indonesia_quakes_2025", crs, extent)
    cv = Canvas(extent, "Earthquakes of magnitude 5 or more around Indonesia in 2025",
                f"{len(truth)} events recorded by the USGS · circle area grows with magnitude, colour shows depth of focus",
                map_w=15.2, footer=1.85)
    ax = cv.ax
    world = load_world()
    world.clip(box(80, -25, 165, 35)).to_crs(crs).plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    # Indonesia slightly darker than its neighbours
    world[world.key == "IDN"].to_crs(crs).plot(ax=ax, color="#ddd8cb", edgecolor="white", linewidth=0.5, zorder=1.2)
    plates = gpd.read_file(fetch(C.base.PLATES_URL, "pb2002_boundaries.json"))
    plates = plates.clip(box(80, -25, 165, 35)).to_crs(crs)
    plates.plot(ax=ax, color="#c28f6b", linewidth=0.8, alpha=0.8, zorder=2)
    for lon in range(95, 141, 10):
        lats = np.linspace(-12, 8, 60)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        ax.text(gx[0], gy[0] + 40_000, f"{lon}°E", fontsize=7.5, color="#7c97a1", ha="center", va="bottom", zorder=0.6)
    for lat in (-10, -5, 0, 5):
        lons = np.linspace(93, 143, 60)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        ax.text(extent[0] + 25_000, gy[0] + 12_000, f"{abs(lat)}°{'N' if lat > 0 else 'S' if lat < 0 else ''}" if lat else "Equator",
                fontsize=7.5, color="#7c97a1", ha="left", va="bottom", zorder=0.6)

    def size(m):
        return 26 * np.power(3.0, np.asarray(m) - 5)

    px = gpd.GeoDataFrame(others, geometry=gpd.points_from_xy(others.lon, others.lat), crs=4326).to_crs(crs)
    ax.scatter(px.geometry.x, px.geometry.y, s=size(px.mag), facecolor="none", edgecolor="#8d979c", linewidth=0.8, zorder=3)
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(crs)
    pts = pts.sort_values("mag", kind="stable")
    ax.scatter(pts.geometry.x, pts.geometry.y, s=size(pts.mag), c=[depth_colour(d) for d in pts.depth_km],
               alpha=0.72, edgecolor="white", linewidth=0.6, zorder=4)
    offs = {"us7000qcik": (60, -40), "us6000qvpp": (64, -34), "us6000rhg5": (-30, 30), "us7000re2g": (66, -26),
            "us6000rjx3": (30, 44), "us6000qugg": (-34, 30)}
    for row in big.itertuples():
        if row.id not in offs:
            continue
        p = pts[pts.id == row.id].iloc[0]
        place = re.sub(r"^\d+ km [NSEW]+ of ", "near ", row.place).replace(", Indonesia", "")
        annotate(ax, p.geometry.x, p.geometry.y, f"M{row.mag:g} {place}\n{pd.Timestamp(row.time).strftime('%-d %b')} · {row.depth_km:.0f} km deep",
                 offs[row.id], size=8.4, weight="bold" if row.mag >= 6.5 else "normal")
    for name, lon, lat in (("INDONESIA", 114.0, -0.4), ("PHILIPPINES", 123.5, 11.0)):
        pass
    for name, lon, lat in (("Indian Ocean", 100.0, -9.2), ("Java Sea", 111.5, -4.6), ("Banda Sea", 127.0, -6.0),
                           ("Celebes Sea", 121.5, 3.4), ("South\nChina Sea", 108.5, 5.0), ("Pacific Ocean", 135.5, 5.5),
                           ("Arafura Sea", 136.5, -8.3), ("Timor Sea", 126.5, -11.0)):
        sea_label(ax, crs, name, lon, lat, size=9.5)
    for name, lon, lat in (("INDONESIA", 114.0, 0.2), ("PHILIPPINES", 123.2, 8.5), ("MALAYSIA", 102.2, 3.6),
                           ("PAPUA\nNEW GUINEA", 143.0, -6.4), ("AUSTRALIA", 131.0, -12.5)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=8.5, color="#9a958a", ha="center", va="center", zorder=2)
    lg = cv.add_axes(cv.left, 0.98, 14.8, 0.66)
    pos = lg.get_position()
    w_in, h_in = pos.width * WIDTH_IN, pos.height * cv.H
    lg.set_xlim(0, w_in)
    lg.set_ylim(0, h_in)
    lg.set_axis_off()
    lg.text(0, h_in - 0.02, "Magnitude (circle area)", va="top", fontsize=10.5, fontweight="bold")
    x = 0.12
    for m in (5, 5.5, 6, 6.5):
        d = size(m) ** 0.5 / 72
        lg.add_patch(Circle((x + d / 2, 0.22), d / 2, facecolor="#8d979c", alpha=0.72, edgecolor="white", linewidth=0.6))
        lg.text(x + d + 0.07, 0.22, f"M{m:g}", va="center", fontsize=9)
        x += d + 0.62
    x0_ = 4.5
    lg.text(x0_, h_in - 0.02, "Depth of focus (colour)", va="top", fontsize=10.5, fontweight="bold")
    for i, (_lo, _hi, colour, lab) in enumerate(DEPTH_CLASSES):
        n = int(((truth.depth_km >= _lo) & (truth.depth_km < _hi)).sum())
        lg.add_patch(Circle((x0_ + 0.1 + i * 2.35, 0.22), 0.09, facecolor=colour, alpha=0.8, edgecolor="white"))
        lg.text(x0_ + 0.26 + i * 2.35, 0.22, f"{lab} ({n})", va="center", fontsize=9)
    xx = x0_ + 7.4
    lg.add_patch(Circle((xx + 0.1, 0.22), 0.09, facecolor="none", edgecolor="#8d979c", linewidth=0.9))
    lg.text(xx + 0.26, 0.22, f"Event in a neighbouring country ({len(others)})", va="center", fontsize=9)
    lg.plot([xx + 4.2, xx + 4.6], [0.22, 0.22], color="#c28f6b", linewidth=1.2)
    lg.text(xx + 4.7, 0.22, "Plate boundary", va="center", fontsize=9)
    cv.footnote([
        f"Source: U.S. Geological Survey, ComCat via the FDSN event service: all events of magnitude ≥ 5.0 from 1 Jan 2025 to 31 Dec 2025 (UTC) in 93–143°E, 13°S–9°N, retrieved {generated}; public domain. "
        "Plate boundaries: P. Bird (2003), PB2002 (ODC-BY). Land: Natural Earth 1:50m.",
        f"Projection: Mercator centred on 118°E (the region straddles the equator). Encoding: circle area ∝ 3^M, translucent, smallest drawn first. All {len(truth)} events that USGS places in Indonesia are shown as filled circles; "
        f"{len(others)} events in neighbouring countries inside the window (Philippines, Timor-Leste, Papua New Guinea, Micronesia) as hollow grey circles. Labelled: every event of M ≥ 6.3.",
    ], width=205)
    cv.save("indonesia_quakes_2025")
    return truth


# --------------------------------------------------------------------------
# 5. Paris: the Métro (OpenStreetMap route=subway relations)

LIGHT_LINES = {"1", "3bis", "6", "7", "7bis", "8", "9", "13"}   # colours light enough for dark ink
HUBS = ("Châtelet", "Gare du Nord", "Gare de Lyon", "Montparnasse - Bienvenüe", "Charles de Gaulle - Étoile",
        "Nation", "République", "La Défense (Grande Arche)", "Saint-Lazare", "Bastille", "Concorde", "Opéra",
        "Gare de l'Est", "Place d'Italie", "Les Halles", "Trocadéro", "Denfert-Rochereau", "Gare d'Austerlitz",
        "Porte de Clignancourt", "Place de Clichy", "Bercy", "Madeleine", "Invalides", "Oberkampf", "Bir-Hakeim")


def line_sort_key(ref: str):
    m = re.fullmatch(r"(\d+)(bis)?", ref)
    return (int(m.group(1)), m.group(2) or "") if m else (99, ref)


def paris_lines() -> tuple[pd.DataFrame, dict, str]:
    payload = overpass("paris_metro")
    rels = [e for e in payload["elements"] if e["type"] == "relation"
            and e["tags"].get("network") == "Métro de Paris" and e["tags"].get("ref") != "Orlyval"]
    nodes = {e["id"]: e for e in payload["elements"] if e["type"] == "node"}
    by_ref: dict[str, list] = {}
    for r in rels:
        by_ref.setdefault(r["tags"]["ref"], []).append(r)
    rows, stops_of = [], {}
    for ref in sorted(by_ref, key=line_sort_key):
        ways, seen, stop_ids = [], set(), set()
        colour = by_ref[ref][0]["tags"].get("colour", "#888888")
        for r in by_ref[ref]:
            for m in r["members"]:
                if m["type"] == "way" and m.get("role", "") == "" and m["ref"] not in seen:
                    seen.add(m["ref"])
                    line = O.way_line(m)
                    if line is not None:
                        ways.append(line)
                elif m["type"] == "node" and m.get("role", "").startswith("stop"):
                    stop_ids.add(m["ref"])
        merged = shapely.line_merge(unary_union(ways))
        stations = {nodes[i]["tags"].get("name") for i in stop_ids if i in nodes and nodes[i].get("tags", {}).get("name")}
        stops_of[ref] = stop_ids
        first = next((r_["tags"] for r_ in by_ref[ref] if r_["tags"].get("from") != r_["tags"].get("to")), by_ref[ref][0]["tags"])
        ends = [(first.get("from"), first.get("to"))]
        rows.append({
            "key": ref, "name": f"Ligne {ref}", "colour": colour.lower(), "operator": "RATP",
            "from": first.get("from", ""), "to": first.get("to", ""), "relations": len(by_ref[ref]),
            "stations": len(stations), "length_km": round(O.geodesic_km(merged), 2),
            "relation_ids": " ".join(str(r["id"]) for r in by_ref[ref]),
            "geometry_wkt": O.wkt_lines(merged, 0.00002),
        })
    del ends
    return pd.DataFrame(rows), {"rels": by_ref, "nodes": nodes, "stops_of": stops_of}, O.osm_base(payload)


def paris_context():
    payload = overpass("paris_context")
    rivers = [O.way_line(e) for e in payload["elements"] if e["type"] == "way" and e["tags"].get("waterway") == "river"]
    peri = []
    rel = next((e for e in payload["elements"] if e["type"] == "relation"), None)
    city = O.relation_polygon(rel) if rel else None
    return [r for r in rivers if r is not None], [r for r in peri if r is not None], city


def build_paris_metro() -> pd.DataFrame:
    truth, aux, base = paris_lines()
    write_csv("paris_metro", truth)
    total_km = truth.length_km.sum()
    write_md("paris_metro", [
        "- URL/API: Overpass `[out:json]; rel[\"route\"=\"subway\"][\"network\"=\"Métro de Paris\"](48.50,1.90,49.10,2.80)->.m; .m out body geom; node(r.m)[\"railway\"~\"^(station|stop)$\"]; out body;` (overpass-api.de). Context: Seine (waterway=river), Paris boundary (admin_level=8, ref:INSEE=75056), by a second query cached beside it.",
        f"- Vintage: OpenStreetMap as of {base}. {int(truth.relations.sum())} route relations (one per direction and branch) of {len(truth)} lines: 1–14, 3bis and 7bis. Colours are the relations' own `colour` tags (RATP's official palette).",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Context from the same source; no other basemap.",
        f"- Rows: {len(truth)} lines — key (line ref), name, colour, from/to termini of the first relation, relations, stations (distinct stop names), length_km (geodesic, track ways de-duplicated across directions; total {total_km:.0f} km), relation_ids, geometry_wkt (merged MULTILINESTRING, 2 m simplification) as used by `line_near`.",
        "- Caveat: OSM also tags the Orlyval automated shuttle as network 'Métro de Paris' (2 relations): it is a light-rail shuttle, not a Métro line, and is excluded; line 15 (Grand Paris Express) carries network 'Île-de-France Mobilités' and is excluded. Lines 7 and 7bis, and 3bis and 3, share no track in the data but run in parallel in places, so the drawn width is thin there.",
    ])
    rivers, peri, city = paris_context()
    crs = "EPSG:2154"
    lines = gpd.GeoDataFrame(truth.assign(geometry=truth.geometry_wkt.map(wkt.loads)), geometry="geometry", crs=4326).to_crs(crs)
    xmin, ymin, xmax, ymax = lines.total_bounds
    extent = (xmin - 3_000, xmax + 3_000, ymin - 3_000, ymax + 3_000)
    check_window("paris_metro", crs, extent)
    main_w = 8.0
    cv = Canvas(extent, "The Paris Métro", f"{len(truth)} lines: 1–14, 3bis and 7bis · {total_km:.0f} km of line, "
                f"{aux['nodes'] and len({n['tags'].get('name') for n in aux['nodes'].values() if n.get('tags', {}).get('name') and n['id'] in set().union(*aux['stops_of'].values())})} stations; each line in its official colour",
                map_w=main_w, footer=1.25, header=1.05)
    ax = cv.ax
    ax.set_facecolor("#f1efe8")
    t = Transformer.from_crs(4326, crs, always_xy=True)

    def proj_line(line):
        return gpd.GeoSeries([line], crs=4326).to_crs(crs).iloc[0]

    seine = unary_union(rivers)
    gpd.GeoSeries([seine], crs=4326).to_crs(crs).plot(ax=ax, color="#b9d3df", linewidth=3.2, zorder=1, capstyle="round")
    gpd.GeoSeries([city.boundary], crs=4326).to_crs(crs).plot(ax=ax, color="#b9b2a0", linewidth=0.8, zorder=1.1, linestyle=(0, (4, 3)))

    def draw_lines(axis, scale_w):
        order = sorted(truth.key, key=line_sort_key)
        for key in order:
            row = lines[lines.key == key].iloc[0]
            w = (1.9 if "bis" not in key else 1.5) * scale_w
            axis.plot(*zip(*[c for g in (row.geometry.geoms if hasattr(row.geometry, "geoms") else [row.geometry]) for c in list(g.coords) + [(np.nan, np.nan)]]),
                      color=row.colour, linewidth=w + 1.6 * scale_w, zorder=3, solid_capstyle="round", alpha=1.0) if False else None
        for key in order:
            row = lines[lines.key == key].iloc[0]
            geoms = row.geometry.geoms if hasattr(row.geometry, "geoms") else [row.geometry]
            for g in geoms:
                xs, ys = g.xy
                axis.plot(xs, ys, color="white", linewidth=(1.6 if "bis" not in key else 1.3) * scale_w + 1.4, zorder=3,
                          solid_capstyle="round", solid_joinstyle="round")
        for key in order:
            row = lines[lines.key == key].iloc[0]
            geoms = row.geometry.geoms if hasattr(row.geometry, "geoms") else [row.geometry]
            for g in geoms:
                xs, ys = g.xy
                axis.plot(xs, ys, color=row.colour, linewidth=(1.6 if "bis" not in key else 1.3) * scale_w, zorder=3.2,
                          solid_capstyle="round", solid_joinstyle="round")

    draw_lines(ax, 1.15)
    # stations: clustered by name; interchanges (>= 2 lines) larger
    nodes = aux["nodes"]
    st = {}
    for ref, ids in aux["stops_of"].items():
        for i in ids:
            n = nodes.get(i)
            if n and n.get("tags", {}).get("name"):
                st.setdefault(n["tags"]["name"], {"pts": [], "lines": set()})
                st[n["tags"]["name"]]["pts"].append((n["lon"], n["lat"]))
                st[n["tags"]["name"]]["lines"].add(ref)
    stations = []
    for name, d in st.items():
        x, y = np.mean(d["pts"], axis=0)
        (px_, py_), = project_points(crs, [(x, y)])
        stations.append((name, px_, py_, len(d["lines"])))
    stations_df = pd.DataFrame(stations, columns=["name", "x", "y", "n"])
    for r in stations_df.itertuples():
        ax.scatter([r.x], [r.y], s=14 if r.n >= 2 else 4, facecolor="white", edgecolor=INK, linewidth=0.6 if r.n >= 2 else 0.35, zorder=4)
    # line bubbles at the termini of the first relation
    def bubble(axis, x, y, ref, size):
        light = ref in LIGHT_LINES
        axis.text(x, y, ref, fontsize=size, fontweight="bold", ha="center", va="center", zorder=7,
                  color=INK if light else "white",
                  bbox=dict(boxstyle="round,pad=0.22" if len(ref) == 1 else "round,pad=0.18", fc=lines[lines.key == ref].colour.iloc[0], ec="white", lw=0.8))

    ends = {}
    for ref, rels in aux["rels"].items():
        pts = []
        for r in rels:
            st_ids = [m for m in r["members"] if m["type"] == "node" and m.get("role", "").startswith("stop")]
            if st_ids:
                pts += [(st_ids[0]["lon"], st_ids[0]["lat"]), (st_ids[-1]["lon"], st_ids[-1]["lat"])]
        uniq = []
        for p_ in pts:
            if all(abs(p_[0] - q[0]) + abs(p_[1] - q[1]) > 0.004 for q in uniq):
                uniq.append(p_)
        ends[ref] = uniq
    for ref, pts in ends.items():
        for lon, lat in pts:
            (x, y), = project_points(crs, [(lon, lat)])
            bubble(ax, x, y, ref, 7.5)
    # inset window on the main map
    iw = (2.283, 48.836, 2.402, 48.885)
    (ix0, iy0), (ix1, iy1) = project_points(crs, [(iw[0], iw[1]), (iw[2], iw[3])])
    ax.add_patch(Rectangle((ix0, iy0), ix1 - ix0, iy1 - iy0, fill=False, edgecolor=INK, linewidth=1.0, zorder=6, linestyle=(0, (3, 2))))
    ax.text(ix1, iy0 - 400, "enlarged at right", fontsize=7.5, color=INK, ha="right", va="top", zorder=8, path_effects=HALO)
    for name, lon, lat in (("La Défense", 2.238, 48.8925), ("Saint-Denis", 2.355, 48.936), ("Orly airport", 2.368, 48.737), ("Bagneux", 2.31, 48.795), ("Rosny", 2.48, 48.885)):
        pass
    for name, lon, lat, dx, dy, ha in (("La Défense", 2.238, 48.892, -6, 10, "right"), ("Saint-Denis", 2.346, 48.934, 6, 8, "left"),
                                       ("Orly airport", 2.364, 48.730, 8, -3, "left"), ("Bagneux", 2.31, 48.8, -8, 0, "right"),
                                       ("Rosny-sous-Bois", 2.482, 48.884, -6, 11, "right"), ("Villejuif", 2.364, 48.787, 9, -6, "left"),
                                       ("Boulogne-\nBillancourt", 2.22, 48.832, -9, -16, "right"), ("Montreuil", 2.44, 48.862, 9, 10, "left")):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.annotate(name, (x, y), xytext=(dx, dy), textcoords="offset points", fontsize=8, color="#6b665a", ha=ha, va="center",
                    zorder=8, path_effects=HALO)
    (sx, sy), = project_points(crs, [(2.215, 48.955)])
    ax.text(sx, sy, "Seine", fontsize=8, color="#5f8796", style="italic", rotation=0)
    scale_bar(ax, extent[0] + 2_000, extent[2] + 2_000, 5, label="5 km")

    # --- enlarged centre
    col_x = cv.left + main_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    (cx0, cy0), (cx1, cy1) = project_points(crs, [(iw[0], iw[1]), (iw[2], iw[3])])
    ins_h = col_w * (cy1 - cy0) / (cx1 - cx0)
    top = cv.H - cv.header
    factor = (extent[1] - extent[0]) / main_w / ((cx1 - cx0) / col_w)
    inset = cv.add_axes(col_x, top - ins_h, col_w, ins_h, frame=True)
    inset.set_facecolor("#f1efe8")
    inset.set_xlim(cx0, cx1)
    inset.set_ylim(cy0, cy1)
    gpd.GeoSeries([seine], crs=4326).to_crs(crs).plot(ax=inset, color="#b9d3df", linewidth=7, zorder=1, capstyle="round")
    gpd.GeoSeries([city.boundary], crs=4326).to_crs(crs).plot(ax=inset, color="#b9b2a0", linewidth=1, zorder=1.1, linestyle=(0, (4, 3)))
    draw_lines(inset, 2.4)
    inside = stations_df[(stations_df.x > cx0) & (stations_df.x < cx1) & (stations_df.y > cy0) & (stations_df.y < cy1)]
    for r in inside.itertuples():
        inset.scatter([r.x], [r.y], s=46 if r.n >= 2 else 14, facecolor="white", edgecolor=INK, linewidth=1.0 if r.n >= 2 else 0.6, zorder=4)
    for ref, pts in ends.items():
        for lon, lat in pts:
            (x, y), = project_points(crs, [(lon, lat)])
            if cx0 < x < cx1 and cy0 < y < cy1:
                bubble(inset, x, y, ref, 9)
    shown = set()
    label_dx = {"Châtelet": (10, -12), "Charles de Gaulle - Étoile": (12, 12), "Gare du Nord": (6, 9), "Nation": (9, -10),
                "République": (8, 8), "Gare de Lyon": (9, -9), "Montparnasse - Bienvenüe": (-8, -10), "Bastille": (9, -3)}
    for r in inside[inside.n >= 3].itertuples():
        name = r.name
        if name in shown:
            continue
        shown.add(name)
        dx, dy = label_dx.get(name, (7, 7))
        inset.annotate(name.replace(" - ", "–"), (r.x, r.y), xytext=(dx, dy), textcoords="offset points", fontsize=7.8, zorder=9,
                       ha="left" if dx > 0 else "right", va="center", path_effects=HALO, color=INK)
    sx, sy = project_points(crs, [(2.368, 48.8255)])[0]
    scale_bar(inset, cx0 + 400, cy0 + 350, 2, label="2 km", size=8)
    inset.text(cx1 - 250, cy0 + 200, f"Central Paris, enlarged ×{factor:.1f}", fontsize=9.3, fontweight="bold", ha="right", va="bottom", zorder=9, path_effects=HALO)
    # legend: the 16 lines in two columns
    ly = top - ins_h - 0.3
    lg = cv.panel(col_x, ly, col_w, 3.55)
    lg.text(0, 3.53, "Lines (termini of the first relation)", va="top", fontsize=10.5, fontweight="bold")
    for i, (_ix, r) in enumerate(truth.iterrows()):
        c, k = divmod(i, 8)
        x = c * (col_w / 2)
        y = 3.05 - k * 0.37
        light = r["key"] in LIGHT_LINES
        lg.text(x + 0.19, y, r["key"], fontsize=9, fontweight="bold", ha="center", va="center", color=INK if light else "white",
                bbox=dict(boxstyle="round,pad=0.25", fc=r["colour"], ec="white", lw=0.8))
        ends_txt = f"{r['from']} – {r['to']}".replace(" (Grande Arche)", "").replace(" - ", "–")
        lg.text(x + 0.48, y, textwrap.fill(ends_txt, 34) + f"\n{r['length_km']:.1f} km · {r['stations']} stations",
                fontsize=7.3, va="center", linespacing=1.15)
    lg.add_patch(Rectangle((0, 0.02), 0.2, 0.2, facecolor="white", edgecolor=INK, linewidth=0.6))
    lg.scatter([0.1], [0.12], s=14, facecolor="white", edgecolor=INK, linewidth=0.6)
    lg.text(0.3, 0.12, "Station  (larger dot: interchange of 2+ lines)", va="center", fontsize=8.6)
    cv.footnote([
        f"Source: OpenStreetMap (route=subway relations of network 'Métro de Paris', stops, Seine, Paris boundary), as of {base}; © OpenStreetMap contributors, ODbL 1.0. Line colours: the official RATP palette from each relation's colour tag.",
        f"Projection: Lambert-93 (EPSG:2154); main map and enlargement share it (enlargement ×{factor:.1f}). Orlyval (airport shuttle, tagged as Métro de Paris in OSM) and line 15 (Grand Paris Express, not a RATP Métro line) are excluded. "
        "Line ends marked with the line number. Stations are clustered by name.",
    ], width=205)
    cv.save("paris_metro")
    return truth


# --------------------------------------------------------------------------
# 6. Lviv oblast: every town and village (KATOTTH + OpenStreetMap)

LVIV_RAION_EN = {"Львівський": "LVIV RAION", "Стрийський": "STRYI RAION", "Самбірський": "SAMBIR RAION",
                 "Шептицький": "SHEPTYTSKYI RAION", "Яворівський": "YAVORIV RAION", "Золочівський": "ZOLOCHIV RAION",
                 "Дрогобицький": "DROHOBYCH RAION"}


def lviv_katotth() -> tuple[pd.DataFrame, pd.DataFrame]:
    k = O.read_katotth(fetch(O.KATOTTH_URL, "s2o_katotth_2026-07-07.xlsx"))
    v = k[k.l1.str.startswith("UA46")]
    raions = v[v.cat == "P"].set_index("l2").name.to_dict()
    hromadas = v[v.cat == "H"].set_index("l3").name.to_dict()
    sett = v[v.cat.isin(["M", "T", "X", "C"])].copy()
    sett = pd.DataFrame({
        "katotth": sett.l4, "category": sett.cat, "name": sett.name,
        "hromada_katotth": sett.l3, "hromada": sett.l3.map(hromadas),
        "raion_katotth": sett.l2, "raion": sett.l2.map(raions),
    })
    return sett.sort_values("katotth").reset_index(drop=True), v


def lviv_osm():
    payload = overpass("lviv_settlements")
    nodes = [e for e in payload["elements"] if e["type"] == "node"]
    rels = [e for e in payload["elements"] if e["type"] == "relation"]
    polys = {}
    for r in rels:
        tags = r["tags"]
        lvl, code = tags.get("admin_level"), tags.get("katotth", "")
        if lvl == "4":
            polys[("oblast", tags.get("ISO3166-2"))] = (tags.get("name:en", tags.get("name")), O.relation_polygon(r))
        elif lvl in ("6", "7") and code.startswith("UA46"):
            polys[("raion" if lvl == "6" else "hromada", code)] = (tags.get("name"), O.relation_polygon(r))
    places = pd.DataFrame([{
        "osm_node_id": n["id"], "lon": n["lon"], "lat": n["lat"], "osm_place": n["tags"].get("place"),
        "osm_name": n["tags"].get("name:uk") or n["tags"].get("name", ""), "osm_katotth": n["tags"].get("katotth", ""),
        "osm_prefix": n["tags"].get("name:prefix", ""),
    } for n in nodes])
    return places, polys, O.osm_base(payload)


def render_lviv(truth, polys, base, how, unmatched) -> None:
    crs = "+proj=tmerc +lat_0=49.8 +lon_0=23.7 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)

    def proj(geom):
        return gpd.GeoSeries([geom], crs=4326).to_crs(crs)

    lviv = polys[("oblast", "UA-46")][1]
    vx0, vy0, vx1, vy1 = proj(lviv).total_bounds
    extent = (vx0 - 12_000, vx1 + 12_000, vy0 - 10_000, vy1 + 10_000)
    check_window("lviv_settlements", crs, extent)
    n_loc = int(truth.lon.notna().sum())
    cv = Canvas(extent, "Every town and village of Lviv oblast",
                f"All {len(truth):,} settlements in Ukraine's official codifier (KATOTTH), by legal status · {n_loc:,} located on OpenStreetMap",
                map_w=10.6, footer=1.2)
    ax = cv.ax
    ax.set_facecolor("#f1efe9")
    c10 = gpd.read_file(fetch(NE10_COUNTRIES_URL, "s3_ne_10m_admin_0_countries.zip"))
    neigh = c10.clip(box(19, 46.5, 28, 52)).to_crs(crs)
    neigh[~neigh.ADM0_A3.isin(["UKR"])].plot(ax=ax, color="#e9e6de", edgecolor="none", zorder=1)
    ukr_adm1 = gpd.read_file(fetch(B.UKRAINE_GEO_URL, "ukraine_adm1.geojson")).to_crs(crs)
    ukr_adm1.plot(ax=ax, color="#e9e6de", edgecolor="#d3cec1", linewidth=0.6, zorder=1.1)
    proj(lviv).plot(ax=ax, color="#e3ddcf", edgecolor="none", zorder=2)
    hromadas = [g for (kind, _c), (_n, g) in polys.items() if kind == "hromada"]
    outlines = [unary_union([p for p in getattr(g, "geoms", [g]) if p.area > 1.5e-4]) for g in hromadas]
    outlines = [shapely.Polygon(g.exterior) if g.geom_type == "Polygon" else g for g in outlines]
    gpd.GeoSeries([g.boundary for g in outlines], crs=4326).to_crs(crs).plot(ax=ax, color="white", linewidth=0.7, zorder=2.1)
    raions = {code: (name, g) for (kind, code), (name, g) in polys.items() if kind == "raion"}
    for _code, (_name, g) in raions.items():
        proj(g.boundary.difference(lviv.boundary.buffer(0.001))).plot(ax=ax, color="#7d7668", linewidth=1.4, zorder=3)
    nat = c10[c10.ADM0_A3.isin(["POL", "SVK"])]
    near_border = unary_union(list(nat.geometry)).buffer(0.02)
    proj(lviv.boundary.difference(near_border)).plot(ax=ax, color="#5d564a", linewidth=2.0, zorder=3.1)
    proj(lviv.boundary.intersection(near_border)).plot(ax=ax, color="#5d564a", linewidth=2.0, linestyle=(0, (5, 2.5)), zorder=3.2)
    for name, lon, lat in (("POLAND", 22.65, 50.05), ("SLOVAKIA", 22.5, 48.9), ("VOLYN OBLAST", 24.6, 51.0),
                           ("TERNOPIL OBLAST", 25.35, 49.6), ("IVANO-FRANKIVSK\nOBLAST", 24.2, 48.85),
                           ("ZAKARPATTIA\nOBLAST", 23.2, 48.75), ("RIVNE\nOBLAST", 25.35, 50.65)):
        x, y = t.transform(lon, lat)
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=8.8, color="#8e897d", ha="center", va="center", zorder=2.5, linespacing=1.1)

    loc = truth.dropna(subset=["lon", "lat"]).copy()
    loc["x"], loc["y"] = t.transform(loc.lon.to_numpy(), loc.lat.to_numpy())
    vil, sel, city = (loc[loc.category == c] for c in ("C", "X", "M"))
    ax.scatter(vil.x, vil.y, s=4.5, color="#4a5559", edgecolor="none", alpha=0.85, zorder=5)
    ax.scatter(sel.x, sel.y, s=22, color="white", edgecolor=INK, linewidth=1.0, zorder=6)
    ax.scatter(city.x, city.y, s=[130 if n == "Львів" else (62 if n in ("Дрогобич", "Стрий", "Червоноград", "Шептицький", "Самбір", "Золочів", "Борислав", "Трускавець", "Яворів") else 40) for n in city.name],
               marker="s", color=INK, edgecolor="white", linewidth=0.9, zorder=7)
    cv.fig.canvas.draw()
    renderer = cv.fig.canvas.get_renderer()
    taken = []
    ordered = pd.concat([city.assign(rank=0, pop=city.name.eq("Львів")), sel.assign(rank=1, pop=False)])
    ordered = ordered.sort_values(["rank", "pop", "name"], ascending=[True, False, True])
    placed = unplaced = 0
    for row in ordered.itertuples():
        big = row.category == "M"
        label_ = O.translit(row.name)
        lviv_city = row.name == "Львів"
        candidates = [(8, 0, "left", "center"), (-8, 0, "right", "center"), (0, 9, "center", "bottom"),
                      (0, -9, "center", "top"), (7, 7, "left", "bottom"), (-7, 7, "right", "bottom"),
                      (7, -7, "left", "top"), (-7, -7, "right", "top"), (14, 0, "left", "center"), (-14, 0, "right", "center"),
                      (0, 17, "center", "bottom"), (0, -17, "center", "top"), (14, 12, "left", "bottom"), (-14, 12, "right", "bottom"), (14, -12, "left", "top"), (-14, -12, "right", "top")]
        ok = False
        for dx, dy, ha, va in candidates:
            txt = ax.annotate(label_, (row.x, row.y), xytext=(dx * (1.4 if lviv_city else 1), dy * (1.4 if lviv_city else 1)),
                              textcoords="offset points", ha=ha, va=va,
                              fontsize=12.5 if lviv_city else (9 if big else 7.6),
                              fontweight="bold" if big else "normal", style="normal" if big else "italic",
                              color=INK if big else "#2b3539", zorder=9, path_effects=HALO)
            bb = txt.get_window_extent(renderer).expanded(1.04, 1.1)
            if not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                ok = True
                break
            txt.remove()
        placed += ok
        unplaced += (not ok)
        if not ok:
            print(f"  note: no free label position for {label_}")
    for row in pd.concat([city, sel]).itertuples():
        px, py = ax.transData.transform((row.x, row.y))
        taken.append(mpl.transforms.Bbox.from_extents(px - 6, py - 6, px + 6, py + 6))
    for _code, (name, g) in raions.items():
        short = name.split()[0]
        gp = proj(g).iloc[0]
        inner = gp.buffer(-7_000)
        centre = gp.representative_point()
        xs_ = np.arange(inner.bounds[0], inner.bounds[2], 3_000)
        ys_ = np.arange(inner.bounds[1], inner.bounds[3], 3_000)
        cands = sorted(((x, y) for x in xs_ for y in ys_ if inner.contains(Point(x, y))),
                       key=lambda c: (c[0] - centre.x) ** 2 + (c[1] - centre.y) ** 2)
        for x, y in cands:
            txt = ax.text(x, y, LVIV_RAION_EN[short].replace(" RAION", "\nRAION") if len(LVIV_RAION_EN[short]) > 14 else LVIV_RAION_EN[short],
                          fontsize=11, color="#7a7365", ha="center", va="center", zorder=2.6, fontweight="bold", alpha=0.85, linespacing=1.1)
            bb = txt.get_window_extent(renderer).expanded(1.05, 1.15)
            if not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                break
            txt.remove()
        else:
            print(f"  WARNING: no room for raion label {name}")
    cats = truth.category.value_counts()
    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 7.6)
    lg.text(0, 7.55, "Settlement status (KATOTTH)", va="top", fontsize=10.5, fontweight="bold")
    items = [("s", 110, INK, "white", f"Місто — city ({cats.get('M', 0)}),\nLviv the oblast centre"),
             ("o", 30, "white", INK, f"Селище — settlement ({cats.get('X', 0)}),\nformer urban-type settlement"),
             ("o", 6, "#4a5559", "none", f"Село — village ({cats.get('C', 0)})")]
    y = 7.0
    for marker, size, face, edge, label_ in items:
        lg.scatter([0.2], [y], s=size, marker=marker, color=face, edgecolor=edge, linewidth=1.0)
        lg.text(0.5, y, label_, va="center", fontsize=9.5, linespacing=1.25)
        y -= 0.55
    y -= 0.1
    lg.text(0, y, "Boundaries", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    for colour, lw, style, label_ in (("#5d564a", 2.0, (0, (5, 2.5)), "State border"), ("#5d564a", 2.0, "-", "Oblast border"),
                                      ("#7d7668", 1.4, "-", f"Raion border ({len(raions)} raions)"),
                                      ("white", 2.2, "-", f"Hromada border ({sum(1 for k in polys if k[0] == 'hromada')} hromadas)")):
        lg.plot([0, 0.4], [y, y], color=colour, linewidth=lw, linestyle=style, solid_capstyle="butt")
        if colour == "white":
            lg.add_patch(Rectangle((0, y - 0.09), 0.4, 0.18, facecolor="#e3ddcf", edgecolor="none", zorder=0))
        lg.text(0.5, y, label_, va="center", fontsize=9.5)
        y -= 0.3
    y -= 0.25
    lg.text(0, y, "Located on OpenStreetMap", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    lines = [("KATOTTH settlements", f"{len(truth):,}"), ("matched by name within hromada", f"{int(how.get('name in hromada', 0)):,}")]
    for k, v in how.items():
        if k not in ("name in hromada", "unmatched"):
            lines.append((k.replace("name in hromada (namesake resolved by katotth tag)", "namesakes, resolved by katotth tag"), f"{v:,}"))
    lines.append(("not found as a settlement node", f"{int(how.get('unmatched', 0)):,}"))
    for k, v in lines:
        lg.text(0, y, k, va="center", fontsize=9.0)
        lg.text(col_w - 0.05, y, v, va="center", ha="right", fontsize=9.0)
        y -= 0.27
    if len(unmatched):
        note = f"Not drawn: {len(unmatched)} settlements with no OSM settlement node (listed in truth.csv with empty lon/lat)."
        lg.text(0, y - 0.05, textwrap.fill(note, 44), va="top", fontsize=8.6, color=MUTED, linespacing=1.4)
    cv.footnote([
        f"Source: Кодифікатор адміністративно-територіальних одиниць та територій територіальних громад (KATOTTH), Ministry for Development of Communities and Territories of Ukraine, {O.KATOTTH_ORDER}. "
        f"Positions, boundaries: © OpenStreetMap contributors (ODbL 1.0), place nodes and admin relations as of {base[:10]}. Neighbouring oblasts: geoBoundaries (CC BY 4.0); foreign land: Natural Earth.",
        "Projection: transverse Mercator centred on 23.7°E. Names transliterated with Ukraine's 2010 national system. Every city labelled; settlements labelled where room allows; villages unlabelled. "
        "KATOTTH has no urban-type settlements (T) since 2024: all were renamed селище (X). Hromada exclaves under 1 km² not outlined.",
    ], width=165)
    cv.save("lviv_settlements")


def build_lviv_settlements() -> pd.DataFrame:
    sett, lviv_rows = lviv_katotth()
    places, polys, base = lviv_osm()
    truth, leftovers = O.match_volyn(sett, places, polys)
    write_csv("lviv_settlements", truth)
    cats = truth.category.value_counts()
    how = truth.match.value_counts()
    matched = int(truth.lon.notna().sum())
    unmatched = truth[truth.lon.isna()]
    n_h = int((lviv_rows.cat == "H").sum())
    n_r = int((lviv_rows.cat == "P").sum())
    osm_settl = places[places.osm_place.isin(O.OSM_SETTLEMENT)]
    old_prefix = osm_settl.osm_prefix.value_counts()
    print(f"  KATOTTH: {len(truth)} settlements {dict(cats)}; OSM {len(osm_settl)} settlement nodes")
    print(f"  match: {dict(how)}")
    print("  unmatched:", "; ".join(f"{r.name} ({r.category}, {r.hromada})" for r in unmatched.itertuples())[:600])
    print("  OSM leftovers:", len(leftovers))
    write_md("lviv_settlements", [
        f"- URL/API: {O.KATOTTH_URL} (KATOTTH xlsx, linked from {O.KATOTTH_PAGE}; rows under UA46, categories M/T/X/C). Coordinates: Overpass API, `" + O.compact(LVIV_QUERY) + "`.",
        f"- Vintage: KATOTTH as amended by the Ministry for Development of Communities and Territories {O.KATOTTH_ORDER} (the same edition as volyn_settlements); OpenStreetMap as of {base}.",
        "- Licence: KATOTTH is an official act of a Ukrainian ministry (not subject to copyright, Law of Ukraine on Copyright Art. 10); coordinates and boundaries © OpenStreetMap contributors, ODbL 1.0.",
        f"- Rows: {len(truth)} KATOTTH settlements of Lviv oblast — {cats.get('M', 0)} cities (M), {cats.get('T', 0)} urban-type settlements (T), {cats.get('X', 0)} селища (X), {cats.get('C', 0)} villages (C); {n_r} raions, {n_h} hromadas. Columns: katotth, category, name, hromada/raion codes and names, match, osm_node_id, osm_place, osm_name, lon, lat. "
        f"Located via an OSM place=city/town/village/hamlet node: {matched} ({', '.join(f'{v} {k}' for k, v in how.items() if k != 'unmatched')}); unmatched {int(how.get('unmatched', 0))}"
        + (f" ({'; '.join(f'{r.name}, {r.hromada} hromada' for r in unmatched.head(10).itertuples())}{' …' if len(unmatched) > 10 else ''})" if len(unmatched) else "") + ".",
        f"- Caveat: the six 'B' rows (districts of Lviv city) are not settlements and are excluded; KATOTTH 'M' (cities) = {cats.get('M', 0)} includes Lviv, Chervonohrad (renamed Sheptytskyi in 2024) and the oblast's other cities. {len(leftovers)} OSM settlement nodes in the oblast match no KATOTTH row (mostly hamlets and neighbourhood-like nodes). "
        f"OSM still tags {old_prefix.get('селище міського типу', 0)} nodes name:prefix='селище міського типу'.",
    ])
    render_lviv(truth, polys, base, how, unmatched)
    return truth


# --------------------------------------------------------------------------
# 7. Germany: wind turbines (OpenStreetMap power=generator + generator:source=wind)


def osm_xy(e: dict) -> tuple[float | None, float | None]:
    if e["type"] == "node":
        return e.get("lon"), e.get("lat")
    c = e.get("center", {})
    return c.get("lon"), c.get("lat")


def mw(value) -> float | None:
    """generator:output:electricity -> megawatts ('2.5 MW', '850 kW', '3000000 W', '2500')."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*([\d.,]+)\s*([kKMG]?)[wW]?\s*", value)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    unit = m.group(2).upper()
    return v * {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}[unit]


def build_germany_wind_turbines() -> pd.DataFrame:
    payload = overpass("germany_wind_turbines")
    base = O.osm_base(payload)
    rows = []
    for e in payload["elements"]:
        lon, lat = osm_xy(e)
        if lon is None:
            continue
        tags = e["tags"]
        rows.append({"id": e["id"], "type": e["type"], "lon": round(lon, 6), "lat": round(lat, 6),
                     "method": tags.get("generator:method", ""), "output_mw": mw(tags.get("generator:output:electricity")),
                     "name": tags.get("name", "")})
    truth = pd.DataFrame(rows).sort_values(["type", "id"]).reset_index(drop=True)
    write_csv("germany_wind_turbines", truth)
    n_tag = int(truth.output_mw.notna().sum())
    write_md("germany_wind_turbines", [
        "- URL/API: Overpass `" + O.compact(GERMANY_QUERY) + "` (overpass-api.de; `out center` gives way centroids).",
        f"- Vintage: OpenStreetMap as of {base}. {len(truth):,} features: {int((truth.type == 'node').sum()):,} nodes and {int((truth.type == 'way').sum())} ways (centroids), all with power=generator + generator:source=wind.",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Länder: Eurostat/GISCO NUTS 2024 level 1 (© EuroGeographics); neighbouring land Natural Earth.",
        f"- Rows: {len(truth):,} — id, OSM type, lon, lat, generator:method, output_mw (parsed from generator:output:electricity; present on {n_tag:,}), name. Features per Land are counted in the map's side table by a point-in-polygon test.",
        "- Caveat: OSM's Germany area is the land territory: offshore wind farms in the North and Baltic Seas are not in the count. OSM is crowd-sourced: the count is what volunteers have mapped, and a few features may be dismantled turbines still tagged. Windparks mapped as one point per park (rather than per turbine) understate dense sites.",
    ])
    crs = "EPSG:3035"
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(crs)
    nuts1 = gpd.read_file(fetch(f"{C.GISCO}/NUTS_RG_03M_2024_4326_LEVL_1.geojson", "s3_nuts1_2024_03m_4326.geojson"))
    de = nuts1[nuts1.CNTR_CODE == "DE"].to_crs(crs)
    xmin, ymin, xmax, ymax = de.total_bounds
    extent = (xmin - 40_000, xmax + 40_000, ymin - 40_000, ymax + 40_000)
    check_window("germany_wind_turbines", crs, extent)
    cv = Canvas(extent, "Wind turbines in Germany",
                f"{len(truth):,} onshore turbines mapped in OpenStreetMap · count per 15 km hexagon", map_w=7.9, footer=0.95)
    ax = cv.ax
    world = load_world()
    world[(world.key != "DEU") & (world.key != "ATA")].clip(box(0, 40, 25, 60)).to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    de.plot(ax=ax, color="#f4f1e8", edgecolor="none", zorder=1.5)
    edges = [1, 2, 5, 10, 20, 40, 80]
    colors = ramp("YlGnBu", len(edges), 0.12, 1.0)
    cmap = mpl.colors.ListedColormap(colors)
    norm = mpl.colors.BoundaryNorm(edges + [10_000], len(colors))
    hex_w = 15_000
    nx = int((extent[1] - extent[0]) / hex_w)
    hb = ax.hexbin(pts.geometry.x, pts.geometry.y, gridsize=nx, extent=(extent[0], extent[1], extent[2], extent[3]),
                   mincnt=1, cmap=cmap, norm=norm, linewidths=0.15, edgecolors="white", zorder=3)
    counts = hb.get_array()
    de.boundary.plot(ax=ax, color="#4d585d", linewidth=0.7, zorder=4)
    land_names = {"DE1": "Baden-Württemberg", "DE2": "Bavaria", "DE3": "Berlin", "DE4": "Brandenburg", "DE5": "Bremen", "DE6": "Hamburg",
                  "DE7": "Hesse", "DE8": "Mecklenburg-\nVorpommern", "DE9": "Lower Saxony", "DEA": "North Rhine-\nWestphalia",
                  "DEB": "Rhineland-\nPalatinate", "DEC": "Saarland", "DED": "Saxony", "DEE": "Saxony-\nAnhalt", "DEF": "Schleswig-\nHolstein", "DEG": "Thuringia"}
    joined = gpd.sjoin(pts[["id", "geometry"]], de[["NUTS_ID", "geometry"]], how="left", predicate="within")
    per_land = joined.groupby("NUTS_ID").size().reindex(de.NUTS_ID).fillna(0).astype(int)
    outside = int(joined.NUTS_ID.isna().sum())
    if outside:   # coast and border points beyond the generalised 1:3M outlines: nearest Land
        miss = pts.loc[joined.NUTS_ID.isna().to_numpy(), ["id", "geometry"]]
        near_ = gpd.sjoin_nearest(miss, de[["NUTS_ID", "geometry"]], how="left")
        joined.loc[joined.NUTS_ID.isna(), "NUTS_ID"] = near_.groupby(level=0).NUTS_ID.first().reindex(miss.index).to_numpy()
    for row in de.itertuples():
        p_ = row.geometry.representative_point()
        small = row.NUTS_ID in ("DE3", "DE5", "DE6", "DEC")
        if small:
            continue
        ax.text(p_.x, p_.y, land_names[row.NUTS_ID], fontsize=7.8, color="#1d2a30", ha="center", va="center", zorder=7,
                linespacing=1.05, path_effects=HALO, alpha=0.9)
    for code, (dx, dy) in {"DE3": (28, 8), "DE5": (-40, 6), "DE6": (-30, 24), "DEC": (-36, -2)}.items():
        row = de[de.NUTS_ID == code].iloc[0]
        p_ = row.geometry.representative_point()
        annotate(ax, p_.x, p_.y, land_names[code], (dx, dy), size=7.6)
    for name, lon, lat in (("North Sea", 6.1, 54.4), ("Baltic Sea", 12.4, 54.75)):
        sea_label(ax, crs, name, lon, lat, size=9)
    for name, lon, lat in (("DENMARK", 9.2, 55.2), ("POLAND", 16.7, 52.0), ("CZECHIA", 14.4, 49.9), ("AUSTRIA", 13.5, 47.55),
                           ("SWITZERLAND", 8.1, 47.35), ("FRANCE", 6.2, 48.5), ("LUXEMBOURG", 6.1, 49.8), ("BELGIUM", 5.2, 50.6), ("NETHERLANDS", 5.9, 52.5)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=7.5, color="#9a958a", ha="center", va="center", zorder=2)
    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 3.6)
    labs = ["1", "2 – 4", "5 – 9", "10 – 19", "20 – 39", "40 – 79", "≥ 80"]
    cls = np.digitize(counts, edges) - 1
    n_hex = np.bincount(cls, minlength=len(colors))
    column_legend(lg, colors, [f"{a}   ({n} hexagons)" for a, n in zip(labs, n_hex)], "Turbines per hexagon\n(hexagons of 15 km width)",
                  extra=[(LAND, "Neighbouring countries")], step=0.27)
    tb = cv.panel(col_x, top - 3.9, col_w, 5.0)
    tb.text(0, 4.98, "Turbines by Land", va="top", fontsize=10.5, fontweight="bold")
    order = per_land.sort_values(ascending=False)
    mx = order.max()
    for i, (code, n) in enumerate(order.items()):
        y = 4.55 - i * 0.275
        name = land_names[code].replace("\n", "")
        tb.text(0, y, name, va="center", fontsize=8.8)
        tb.add_patch(Rectangle((2.25, y - 0.08), (col_w - 3.1) * n / mx, 0.16, facecolor="#2b6ca3", edgecolor="none"))
        tb.text(2.25 + (col_w - 3.1) * n / mx + 0.07, y, f"{n:,}", va="center", fontsize=8.6)
    cv.fig.text(col_x / WIDTH_IN, (top - 9.05) / cv.H, textwrap.fill(
        f"Lower Saxony, Brandenburg and Schleswig-Holstein hold {per_land[['DE9', 'DE4', 'DEF']].sum() / per_land.sum() * 100:.0f}% of the mapped turbines; the Alps and the south-west have few. "
        "Offshore wind farms are outside OpenStreetMap's Germany area and not shown.", 52), va="top", fontsize=9.3, color=MUTED, linespacing=1.4)
    cv.footnote([
        f"Source: OpenStreetMap, nodes and ways tagged power=generator + generator:source=wind within Germany, as of {base[:10]}; © OpenStreetMap contributors, ODbL 1.0. "
        "Länder boundaries: Eurostat/GISCO NUTS 2024 level 1, © EuroGeographics; neighbouring land: Natural Earth 1:50m.",
        f"Projection: ETRS89 Lambert azimuthal equal-area (EPSG:3035), so equal hexagons are equal areas. Encoding: count per 15 km hexagon in seven classes (1, 2, 5, 10, 20, 40, 80). All {len(truth):,} features binned"
        + (f"; {outside} lie just outside the generalised 1:3M Länder outlines (coast, border) and are assigned to the nearest Land in the table." if outside else "; every one lies inside a Land."),
    ], width=165)
    cv.save("germany_wind_turbines")
    return truth


# --------------------------------------------------------------------------
# 8. North Indian Ocean cyclones 2023 (IBTrACS v04r01, NI basin)

IBTRACS_NI_URL = ("https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/"
                  "v04r01/access/csv/ibtracs.NI.list.v04r01.csv")
NI_LANDFALLS = {  # storm -> (landfall place, label offset in points)
    "Mocha": ("near Sittwe, Myanmar", (46, 34)),
    "Biparjoy": ("near Jakhau, Gujarat, India", (-70, 34)),
    "Tej": ("Yemen coast near Al Ghaydah", (20, 44)),
    "Hamoon": ("Chittagong coast, Bangladesh", (-120, -40)),
    "Midhili": ("Khulna coast, Bangladesh", (-130, 44)),
    "Michaung": ("near Bapatla, Andhra Pradesh, India", (-76, 28)),
}


def build_north_indian_cyclones_2023() -> pd.DataFrame:
    path = fetch(IBTRACS_NI_URL, "s3_ibtracs_NI_v04r01.csv")
    raw = pd.read_csv(path, skiprows=[1], dtype=str, keep_default_na=False)
    season = raw[(raw.SEASON == "2023") & (raw.NAME != "UNNAMED")].copy()
    unnamed = raw[(raw.SEASON == "2023") & (raw.NAME == "UNNAMED")].SID.nunique()

    def num(col):
        return pd.to_numeric(season[col].str.strip().replace("", np.nan), errors="coerce")

    truth = pd.DataFrame({
        "sid": season.SID, "name": season.NAME.str.title(), "iso_time": season.ISO_TIME,
        "lon": num("LON"), "lat": num("LAT"), "nature": season.NATURE,
        "imd_wind_kt": num("WMO_WIND").astype("Int64"), "imd_pres_mb": num("WMO_PRES").astype("Int64"),
        "jtwc_wind_kt": num("USA_WIND").astype("Int64"), "jtwc_sshs": num("USA_SSHS").astype("Int64"),
        "over_land": (num("LANDFALL") == 0),
    }).sort_values(["sid", "iso_time"]).reset_index(drop=True)
    first_land = truth[truth.over_land].groupby("sid").head(1).index
    truth["landfall"] = False
    truth.loc[first_land, "landfall"] = True
    truth["over_land"] = truth.over_land.astype(int)
    truth["landfall"] = truth.landfall.astype(int)
    write_csv("north_indian_cyclones_2023", truth)
    storms = truth.groupby("sid", sort=False).agg(
        name=("name", "first"), start=("iso_time", "first"), end=("iso_time", "last"), peak=("jtwc_wind_kt", "max"),
        imd_peak=("imd_wind_kt", "max"), maxcat=("jtwc_sshs", "max"), n=("lon", "size")).reset_index().sort_values("start")
    n_storms = len(storms)
    last_mod = pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d")
    write_md("north_indian_cyclones_2023", [
        f"- URL/API: {IBTRACS_NI_URL} (NOAA NCEI IBTrACS v04r01, North Indian basin file); rows with SEASON = 2023 and NAME ≠ UNNAMED.",
        f"- Vintage: IBTrACS v04r01 as downloaded {last_mod} (server Last-Modified 2026-10-25); positions are IBTrACS LAT/LON (WMO/IMD New Delhi best track), intensity both IMD (3-min, WMO_WIND, missing on some fixes) and JTWC (1-min, USA_WIND / USA_SSHS, complete) — the map colours by JTWC Saffir–Simpson so every fix has a class.",
        "- Licence: IBTrACS is a NOAA product, U.S. government work (public domain); cite Knapp et al. (2010) BAMS 91:363 and Gahtan et al. (2024) IBTrACS v04r01, NOAA NCEI, doi:10.25921/82ty-9e16. Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} track fixes (3-hourly) of {n_storms} named storms — sid, name, iso_time (UTC), lon, lat, nature, imd_wind_kt, imd_pres_mb, jtwc_wind_kt, jtwc_sshs (−1 = depression, 0 = tropical storm, 1–5), over_land (IBTrACS LANDFALL = 0), landfall (first over-land fix). `count` = distinct sid: " + ", ".join(f"{r.name} (peak {r.peak} kt JTWC / {r.imd_peak} kt IMD)" for r in storms.itertuples()) + ".",
        f"- Caveat: {unnamed} further 2023 depressions were never named by IMD and are excluded; named-storm count is {n_storms}, the IMD naming list's own tally for 2023 (Mocha, Biparjoy, Tej, Hamoon, Midhili, Michaung). Mocha's JTWC peak is Cat 5 (145 kt) against IMD's 'extremely severe' 115 kt (3-min). Tej made landfall as a weakening storm in Yemen; Hamoon and Midhili as tropical storms in Bangladesh.",
    ])
    crs = "+proj=lcc +lat_1=8 +lat_2=24 +lat_0=15 +lon_0=72 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    xs, ys = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    truth["x"], truth["y"] = xs, ys
    ring = [(lo, 3.2) for lo in range(48, 100)] + [(lo, 25.6) for lo in range(48, 100)] + [(48, la / 2) for la in range(7, 52)] + [(99, la / 2) for la in range(7, 52)]
    rx, ry = zip(*project_points(crs, ring))
    extent = (min(rx), max(rx), min(ry), max(ry))
    check_window("north_indian_cyclones_2023", crs, extent)
    cv = Canvas(extent, "Tracks of the 2023 North Indian Ocean cyclone season",
                f"All {n_storms} named storms, coloured by Saffir–Simpson category (JTWC) along the track · ring = first fix over land",
                map_w=15.2, footer=1.75)
    ax = cv.ax
    world = load_world()
    world.clip(box(30, -10, 125, 45)).to_crs(crs).plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    for lon in range(50, 100, 10):
        lats = np.linspace(0, 35, 120)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
    for lat in range(0, 31, 10):
        lons = np.linspace(40, 110, 200)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        lx, ly = t.transform(50.6, lat)
        if lat:
            ax.text(lx, ly + 10_000, f"{lat}°N", fontsize=7.5, color="#7c97a1", ha="left", va="bottom", zorder=0.6)
    for lon in range(50, 100, 10):
        lx, ly = t.transform(lon, 3.6)
        ax.text(lx, ly, f"{lon}°E", fontsize=7.5, color="#7c97a1", ha="center", va="bottom", zorder=0.6)
    order = storms.sort_values("maxcat").sid
    for sid in order:
        g = truth[truth.sid == sid]
        pts = g[["x", "y"]].to_numpy()
        cats = g.jtwc_sshs.fillna(-1).astype(int).to_numpy()
        seg_cat = np.maximum(cats[:-1], cats[1:])
        segs = np.stack([pts[:-1], pts[1:]], axis=1)
        lw = np.where(seg_cat >= 3, 3.4, np.where(seg_cat >= 1, 2.8, 2.0))
        ax.add_collection(LineCollection(segs, colors="white", linewidths=lw + 1.8, zorder=3.5, capstyle="round"))
        ax.add_collection(LineCollection(segs, colors=[O.sshs_colour(c) for c in seg_cat], linewidths=lw, zorder=4, capstyle="round"))
    land = truth[truth.landfall == 1]
    ax.scatter(land.x, land.y, s=34, facecolor="white", edgecolor=INK, linewidth=1.1, zorder=7)
    for row in land.itertuples():
        place, off = NI_LANDFALLS[row.name]
        when = pd.Timestamp(row.iso_time).strftime("%-d %b")
        wind_at = row.jtwc_wind_kt
        annotate(ax, row.x, row.y, f"{row.name} landfall, {when}\n{place}\n{wind_at} kt at landfall (JTWC)", off, size=8.2)
    for row in storms.itertuples():
        g = truth[truth.sid == row.sid]
        start = g.iloc[0]
        big = row.maxcat >= 3
        a = pd.Timestamp(row.start).strftime("%-d %b")
        b = pd.Timestamp(row.end).strftime("%-d %b")
        dx, dy = {"Mocha": (30, -22), "Biparjoy": (-14, -22), "Tej": (-16, 12), "Hamoon": (-50, -12), "Midhili": (36, -14), "Michaung": (-40, -14)}[row.name]
        ax.annotate(f"{row.name}\n{a} – {b}", (start.x, start.y), xytext=(dx, dy), textcoords="offset points",
                    ha="right" if dx < 0 else "left", va="center", fontsize=10.5 if big else 9.2,
                    fontweight="bold", color=INK, zorder=9, path_effects=HALO, linespacing=1.1,
                    arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.5, shrinkA=0, shrinkB=2))
    for name, lon, lat in (("Arabian Sea", 63.0, 15.0), ("Bay of Bengal", 83.6, 17.6), ("Indian Ocean", 74.0, 4.8), ("Gulf of Aden", 49.0, 12.4)):
        sea_label(ax, crs, name, lon, lat, size=10.5)
    for name, lon, lat in (("INDIA", 78.0, 22.0), ("PAKISTAN", 69.0, 28.0), ("OMAN", 56.5, 20.5), ("YEMEN", 47.5, 16.2), ("MYANMAR", 96.0, 22.5),
                           ("BANGLADESH", 89.5, 24.4), ("SRI LANKA", 80.7, 7.6), ("THAILAND", 99.6, 17.0)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=8.8, color="#9a958a", ha="center", va="center", zorder=2)
    lg = O.legend_axes(cv, cv.left, 0.98, 15.2, 0.72)
    lg.text(0, 0.70, "Saffir–Simpson category at that point of the track (1-min sustained wind, JTWC)", va="top", fontsize=10.5, fontweight="bold")
    x = 0
    for _lo, colour, label_ in O.SSHS:
        lg.plot([x, x + 0.42], [0.2, 0.2], color=colour, linewidth=3.2, solid_capstyle="butt")
        lg.text(x + 0.5, 0.2, label_.replace(" (", "\n("), va="center", fontsize=8.4, linespacing=1.1)
        x += 1.92
    lg.scatter([x + 0.12], [0.2], s=34, facecolor="white", edgecolor=INK, linewidth=1.1)
    lg.text(x + 0.35, 0.2, "First fix over land\n(landfall)", va="center", fontsize=8.4, linespacing=1.1)
    cv.footnote([
        f"Source: NOAA NCEI, International Best Track Archive for Climate Stewardship (IBTrACS) v04r01, North Indian Ocean file, season 2023, retrieved {last_mod}; public domain (Knapp et al. 2010; Gahtan et al. 2024). "
        "Positions: IMD New Delhi best track; intensity: JTWC (USA_WIND, 1-minute). Land: Natural Earth 1:50m, public domain.",
        f"Projection: Lambert conformal conic, standard parallels 8°N and 24°N, centred on 72°E. All {n_storms} named storms shown, {len(truth)} 3-hourly fixes. Excluded: {unnamed} unnamed 2023 depressions. "
        "Line colour and width = JTWC Saffir–Simpson category of each segment; wind speeds at landfall are the JTWC value at the first over-land fix, which can differ from IMD's.",
    ], width=205)
    cv.save("north_indian_cyclones_2023")
    return truth


# --------------------------------------------------------------------------
# 9. EU: share of the population aged 65 or over, NUTS 2 (Eurostat demo_r_pjanind2)

ELDERLY_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_r_pjanind2"
               "?format=JSON&lang=EN&indic_de=PC_Y65_MAX&sinceTimePeriod=2023")


def build_eu_elderly_share_nuts2() -> pd.DataFrame:
    payload = json.loads(fetch(ELDERLY_URL, "s3_eurostat_demo_r_pjanind2.json").read_text())
    rows = B.jsonstat_rows(payload)
    rows = rows[(rows.unit == "PC") & (rows.geo.str.len() == 4)].copy()
    rows["time"] = pd.to_numeric(rows.time)
    shapes = gpd.read_file(fetch(B.NUTS_03M_URL, "nuts2_2024_03m_4326.geojson"))
    eu = shapes[shapes.CNTR_CODE.isin(B.EU27)].copy()
    latest = rows.sort_values("time").groupby("geo").tail(1)
    sel = latest[latest.geo.isin(set(eu.NUTS_ID))]
    names = eu.set_index("NUTS_ID").NAME_LATN.to_dict()
    truth = pd.DataFrame({"key": sel.geo, "name": sel.geo.map(names), "value": sel.value,
                          "unit": "percent of population aged 65 or over (1 January)", "year": sel.time}).sort_values("key").reset_index(drop=True)
    missing = sorted(set(eu.NUTS_ID) - set(truth.key))
    write_csv("eu_elderly_share_nuts2", truth)
    updated = payload.get("updated", "?")
    years = truth.year
    write_md("eu_elderly_share_nuts2", [
        f"- URL/API: {ELDERLY_URL} (JSON-stat; indicator PC_Y65_MAX 'Proportion of population aged 65 years and more', unit PC, NUTS 2; the dataset also holds unit YR which is ignored). Geometry {B.NUTS_03M_URL}.",
        f"- Vintage: Eurostat demo_r_pjanind2 updated {updated}; latest year per region {int(years.min())}–{int(years.max())} ({int((years == years.max()).sum())} of {len(truth)} regions at {int(years.max())}). Population at 1 January. Geometry GISCO NUTS 2024, 1:3M, EPSG:4326.",
        "- Licence: Eurostat data, free reuse with attribution (CC BY 4.0); boundaries © EuroGeographics for the administrative boundaries (GISCO).",
        f"- Rows: {len(truth)} — every NUTS 2024 level-2 region of the 27 member states (incl. the 8 outermost regions); unmatched geometries: {missing or 'none'}. Eurostat also publishes values under superseded NUTS 2021 codes (NL31, NL33, PT16–18, …); those have no 2024 geometry and are omitted.",
        "- Caveat: some 2025 values are flagged provisional or estimated. Régions d'outre-mer are young: Guyane 7.7% against Liguria 29.2% is a younger-population effect, not a data error. Values are the share of residents aged 65+, not of the working-age population.",
    ])
    crs = "EPSG:3035"
    regions = eu.merge(truth[["key", "value"]], left_on="NUTS_ID", right_on="key", how="left").to_crs(crs)
    mainland = regions[~regions.NUTS_ID.isin(B.OUTERMOST)]
    outer = regions[regions.NUTS_ID.isin(B.OUTERMOST)]
    xmin, ymin, xmax, ymax = mainland.total_bounds
    extent = (xmin - 60_000, xmax + 60_000, ymin - 60_000, ymax + 40_000)
    check_window("eu_elderly_share_nuts2", crs, extent)
    q = np.quantile(truth.value, np.linspace(0, 1, 8)[1:-1])
    edges = [float(np.round(x)) for x in q]
    edges = sorted(set(edges))
    colors = ramp("YlOrBr", len(edges) + 1, 0.10, 0.95)
    cv = Canvas(extent, "Share of the population aged 65 or over in EU regions",
                f"NUTS 2 regions, percent of residents on 1 January {int(years.max())} · classes hold roughly equal numbers of regions",
                map_w=10.9, footer=0.95)
    ax = cv.ax
    world = load_world()
    world[world.key != "ATA"].clip(box(-30, 25, 50, 75)).to_crs(crs).plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    plot_classes(ax, mainland.dropna(subset=["value"]), "value", edges, colors, linewidth=0.25)
    plot_missing(ax, mainland[mainland.value.isna()])
    mainland.dissolve("CNTR_CODE").boundary.plot(ax=ax, color="#5d6a70", linewidth=0.55, zorder=3)
    col_x = cv.left + cv.map_w + 0.35
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header
    lg = cv.panel(col_x, top, col_w, 3.3)
    counts = np.bincount(classify(truth.value, edges).astype(int), minlength=len(colors))
    labs = [f"< {edges[0]:g}"] + [f"{a:g} – {b:g}" for a, b in zip(edges[:-1], edges[1:])] + [f"≥ {edges[-1]:g}"]
    column_legend(lg, colors, [f"{a} %   ({n})" for a, n in zip(labs, counts)], "Aged 65+, % of residents\n(regions per class)",
                  extra=[(LAND, "Outside the EU (not mapped)")], step=0.27)
    detailed = gpd.read_file(fetch(B.NUTS_03M_URL, "nuts2_2024_03m_4326.geojson"))
    detailed = detailed[detailed.NUTS_ID.isin(B.OUTERMOST)][["NUTS_ID", "geometry"]].merge(outer[["NUTS_ID", "value"]], on="NUTS_ID")
    order = ["PT20", "PT30", "ES70", "FRY1", "FRY2", "FRY3", "FRY4", "FRY5"]
    cell_w, cell_h = (col_w - 0.15) / 2, 1.4
    for i, code in enumerate(order):
        part = detailed[detailed.NUTS_ID == code].copy()
        if code in B.INSET_CLIP:
            part["geometry"] = part.geometry.intersection(box(*B.INSET_CLIP[code]))
        c = part.geometry.iloc[0].centroid
        local = f"+proj=laea +lat_0={c.y:.2f} +lon_0={c.x:.2f} +datum=WGS84 +units=m"
        part = part.to_crs(local)
        b = part.total_bounds
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        usable_h = cell_h - 0.3
        m_per_in = max((b[2] - b[0]) / (cell_w * 0.86), (b[3] - b[1]) / (usable_h * 0.86))
        span_w, span_h = cell_w * m_per_in, cell_h * m_per_in
        r_, k_ = divmod(i, 2)
        inset = cv.add_axes(col_x + k_ * (cell_w + 0.15), top - 3.45 - (r_ + 1) * (cell_h + 0.12), cell_w, cell_h, frame=True)
        inset.set_facecolor(WATER)
        inset.set_xlim(cx - span_w / 2, cx + span_w / 2)
        inset.set_ylim(cy - span_h / 2 - 0.1 * m_per_in, cy + span_h / 2 - 0.1 * m_per_in)
        plot_classes(inset, part.dropna(subset=["value"]), "value", edges, colors, linewidth=0.3)
        plot_missing(inset, part[part.value.isna()])
        km = 50 if m_per_in < 150_000 else 100
        x0 = cx + span_w / 2 - 0.12 * m_per_in - km * 1000
        y0 = cy - span_h / 2 + 0.03 * m_per_in
        inset.plot([x0, x0 + km * 1000], [y0, y0], color=INK, linewidth=1.2, solid_capstyle="butt")
        inset.text(x0 + km * 500, y0 + 0.05 * m_per_in, f"{km} km", ha="center", va="bottom", fontsize=7, color=MUTED)
        inset.text(0.04, 0.95, B.OUTERMOST[code], transform=inset.transAxes, fontsize=8, va="top")
        inset.text(0.04, 0.80, f"{part.value.iloc[0]:.1f}%", transform=inset.transAxes, fontsize=8, va="top", color=MUTED)
    tb = cv.panel(col_x, top - 3.45 - 4 * (cell_h + 0.12) - 0.1, col_w, 1.5)
    shortn = truth.assign(short=truth.name.str.split(" / ").str[0].str.replace(r" \(.*\)", "", regex=True).str.replace("Ciudad de ", ""))
    C.ranked_lists(tb, shortn, lambda v: f"{v:.1f}%", name_col="short", n=3, col2=2.55, width=col_w, size=9.0)
    n_reg = len(truth)
    cv.footnote([
        f"Source: Eurostat, demo_r_pjanind2 (PC_Y65_MAX, proportion of population aged 65 and more, NUTS 2), {int(years.max())}; © EuroGeographics for the administrative boundaries (GISCO NUTS 2024, 1:3M). Context land: Natural Earth.",
        f"Projection: ETRS89 Lambert azimuthal equal-area (EPSG:3035); the eight outermost regions inset in local LAEA, each at its own scale (see bars). Classification: quantiles of the {n_reg} regional values (about {n_reg // len(colors)} regions per class), "
        f"breaks rounded to whole percentage points ({', '.join(f'{e:g}' for e in edges)}). All {n_reg} NUTS 2 regions of the 27 member states valued; none excluded.",
    ], width=200)
    cv.save("eu_elderly_share_nuts2")
    return truth


# --------------------------------------------------------------------------
# 10. Norway: lighthouses (OpenStreetMap man_made=lighthouse)

NO_CRS = "+proj=lcc +lat_1=58 +lat_2=70 +lat_0=64 +lon_0=14 +datum=WGS84 +units=m +no_defs"
NO_CITIES = (("Oslo", 10.75, 59.91), ("Bergen", 5.32, 60.39), ("Trondheim", 10.40, 63.43), ("Bodø", 14.40, 67.28),
             ("Tromsø", 18.96, 69.65), ("Kirkenes", 30.05, 69.73), ("Stavanger", 5.73, 58.97), ("Ålesund", 6.15, 62.47))
NO_INSETS = {   # key -> (title, lon0, lat0, lon1, lat1)
    "south": ("Skagerrak coast, Oslofjord and Stavanger", 4.9, 58.0, 11.9, 60.0),
    "west": ("West coast: Bergen to Ålesund", 4.4, 59.9, 8.2, 63.1),
    "lofoten": ("Lofoten and Vesterålen", 12.2, 67.2, 16.9, 69.2),
}


def norway_rows() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    payload = overpass("norway_lighthouses")
    seen, rows = set(), []
    for e in payload["elements"]:
        if (e["type"], e["id"]) in seen:
            continue
        seen.add((e["type"], e["id"]))
        lon, lat = osm_xy(e)
        tags = e["tags"]
        region = "Svalbard" if (lat >= 74 and lon >= 8) else "Jan Mayen" if lon < -7 else "mainland"
        rows.append({"id": e["id"], "type": e["type"], "lon": round(lon, 6), "lat": round(lat, 6),
                     "name": tags.get("name", ""), "seamark_type": tags.get("seamark:type", ""), "region": region})
    allrows = pd.DataFrame(rows).sort_values(["type", "id"]).reset_index(drop=True)
    main = allrows[allrows.region == "mainland"].copy().reset_index(drop=True)
    # one place can be mapped twice (a node for the light and a way for the building): cluster within 150 m
    t = Transformer.from_crs(4326, 25833, always_xy=True)
    x, y = t.transform(main.lon.to_numpy(), main.lat.to_numpy())
    cluster = -np.ones(len(main), dtype=int)
    for i in range(len(main)):
        if cluster[i] >= 0:
            continue
        cluster[i] = i
        near = np.where((np.hypot(x - x[i], y - y[i]) < 150) & (cluster < 0))[0]
        cluster[near] = i
    main["place"] = cluster
    return main, allrows[allrows.region != "mainland"], O.osm_base(payload)


def build_norway_lighthouses() -> pd.DataFrame:
    truth, other, base = norway_rows()
    write_csv("norway_lighthouses", truth)
    n_places = truth.place.nunique()
    write_md("norway_lighthouses", [
        "- URL/API: Overpass `" + O.compact(NORWAY_QUERY) + "` (overpass-api.de; `out center` gives way centroids). Norway = ISO3166-1 area NO; the second block queries SJ for completeness.",
        f"- Vintage: OpenStreetMap as of {base}. {len(truth)} mainland features ({int((truth.type == 'node').sum())} nodes, {int((truth.type == 'way').sum())} building ways) at {n_places} distinct places (elements within 150 m share a `place` id: a lighthouse is often mapped as both a light node and a building way). {len(other)} further features on Svalbard / Jan Mayen are listed here only: " + ", ".join(f"{r.name or r.id} ({r.region})" for r in other.head(6).itertuples()) + ("…" if len(other) > 6 else "") + ".",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Land: Natural Earth 1:10m (public domain).",
        f"- Rows: {len(truth)} — id, OSM type, lon, lat, name, seamark:type (light_major {int((truth.seamark_type == 'light_major').sum())}, light_minor {int((truth.seamark_type == 'light_minor').sum())}, landmark {int((truth.seamark_type == 'landmark').sum())}, pile {int((truth.seamark_type == 'pile').sum())}, untagged {int((truth.seamark_type == '').sum())}), region, place (cluster id). `count` = rows. Named: {int((truth.name != '').sum())}.",
        f"- Caveat: man_made=lighthouse is far rarer in OSM than the 'about 1,500' the spec guessed: OSM holds {n_places} distinct places. Coastal lights mapped only as seamark:type=light_* without man_made=lighthouse are not counted. Svalbard ({int((other.region == 'Svalbard').sum())}) and Jan Mayen ({int((other.region == 'Jan Mayen').sum())}) are outside the frame and the count.",
    ])
    crs = NO_CRS
    c10 = gpd.read_file(fetch(NE10_COUNTRIES_URL, "s3_ne_10m_admin_0_countries.zip"))
    land = c10.clip(box(-5, 54, 36, 73)).to_crs(crs)
    nor = land[land.ADM0_A3 == "NOR"]
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(crs)
    xmin, ymin, xmax, ymax = nor.total_bounds
    extent = (xmin - 140_000, xmax + 100_000, ymin - 100_000, ymax + 90_000)
    check_window("norway_lighthouses", crs, extent)
    main_w = 8.3
    cv = Canvas(extent, "Lighthouses along the coast of Norway",
                f"{len(truth)} features tagged man_made=lighthouse in OpenStreetMap, at {n_places} places · enlargements of the three densest coasts at right",
                map_w=main_w, footer=1.0)
    ax = cv.ax
    ax.set_facecolor(WATER)
    land.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.4, zorder=1)
    nor.plot(ax=ax, color="#ddd8cb", edgecolor="white", linewidth=0.4, zorder=1.1)
    t = Transformer.from_crs(4326, crs, always_xy=True)
    for lon in range(0, 35, 10):
        lats = np.linspace(56, 73, 80)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
    for lat in range(58, 73, 4):
        lons = np.linspace(-5, 36, 120)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)

    major = pts[pts.seamark_type == "light_major"]
    rest = pts[pts.seamark_type != "light_major"]
    ax.scatter(rest.geometry.x, rest.geometry.y, s=15, color="#e07b39", edgecolor="white", linewidth=0.4, zorder=5, alpha=0.95)
    ax.scatter(major.geometry.x, major.geometry.y, s=26, color="#b5172b", edgecolor="white", linewidth=0.5, zorder=6)
    for name, lon, lat in NO_CITIES:
        (x, y), = project_points(crs, [(lon, lat)])
        ax.scatter([x], [y], s=12, marker="s", color=INK, zorder=4)
        ax.text(x + 14_000, y + 4_000, name, fontsize=8.6, color=INK, ha="left", va="bottom", zorder=8, path_effects=HALO)
    for name, lon, lat in (("NORWAY", 9.2, 66.4), ("SWEDEN", 16.2, 65.6), ("FINLAND", 26.0, 67.5), ("RUSSIA", 33.5, 69.4), ("DENMARK", 8.7, 56.4)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=9, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("North Sea", 3.3, 59.3), ("Norwegian Sea", 3.5, 66.3), ("Barents Sea", 30.6, 71.6), ("Skagerrak", 8.0, 57.6)):
        sea_label(ax, crs, name, lon, lat, size=10)
    scale_bar(ax, extent[1] - 330_000, extent[2] + 50_000, 200)
    # boxes of the enlargements
    for key, (title, lon0, lat0, lon1, lat1) in NO_INSETS.items():
        (bx0, by0), (bx1, by1) = project_points(crs, [(lon0, lat0), (lon1, lat1)])
        ax.add_patch(Rectangle((bx0, by0), bx1 - bx0, by1 - by0, fill=False, edgecolor=INK, linewidth=0.9, linestyle=(0, (3, 2)), zorder=7))
    letters = {"south": "A", "west": "B", "lofoten": "C"}
    for key, (title, lon0, lat0, lon1, lat1) in NO_INSETS.items():
        (bx0, by0), (bx1, by1) = project_points(crs, [(lon0, lat0), (lon1, lat1)])
        ax.text(bx0 - 8_000, by1, letters[key], fontsize=11, fontweight="bold", ha="right", va="top", zorder=9, path_effects=HALO)

    col_x = cv.left + main_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header
    wB = 2.75
    xr = col_x + wB + 0.15
    wr = col_w - wB - 0.15
    lg = cv.panel(xr, top, wr, 1.35)
    lg.scatter([0.1], [1.15], s=26, color="#b5172b", edgecolor="white", linewidth=0.5)
    lg.text(0.3, 1.15, f"Major light — {int((truth.seamark_type == 'light_major').sum())}", va="center", fontsize=9.3)
    lg.text(0.3, 0.93, "seamark:type = light_major", va="center", fontsize=7.8, color=MUTED)
    lg.scatter([0.1], [0.68], s=15, color="#e07b39", edgecolor="white", linewidth=0.4)
    lg.text(0.3, 0.68, f"Other lighthouse — {len(truth) - int((truth.seamark_type == 'light_major').sum())}", va="center", fontsize=9.3)
    lg.text(0.3, 0.46, "minor light, landmark or untagged", va="center", fontsize=7.8, color=MUTED)
    lg.scatter([0.1], [0.2], s=12, marker="s", color=INK)
    lg.text(0.3, 0.2, "City, for orientation", va="center", fontsize=9.3)
    heights = {}
    avail_top = top - 1.3
    # inset A on top, B and C below
    def draw_inset(key, x_in, y_top_in, w_in):
        title, lon0, lat0, lon1, lat1 = NO_INSETS[key]
        lc = f"+proj=lcc +lat_1=58 +lat_2=70 +lat_0={(lat0 + lat1) / 2} +lon_0={(lon0 + lon1) / 2} +datum=WGS84 +units=m"
        (ix0, iy0), (ix1, iy1) = [Transformer.from_crs(4326, lc, always_xy=True).transform(lo, la) for lo, la in ((lon0, lat0), (lon1, lat1))]
        # use the box's own corners in the local projection, widened to the bounding box of all four
        corners = [Transformer.from_crs(4326, lc, always_xy=True).transform(lo, la) for lo, la in ((lon0, lat0), (lon1, lat0), (lon0, lat1), (lon1, lat1))]
        ix0, ix1 = min(c[0] for c in corners), max(c[0] for c in corners)
        iy0, iy1 = min(c[1] for c in corners), max(c[1] for c in corners)
        h_in = w_in * (iy1 - iy0) / (ix1 - ix0)
        a = cv.add_axes(x_in, y_top_in - h_in, w_in, h_in, frame=True)
        a.set_facecolor(WATER)
        a.set_xlim(ix0, ix1)
        a.set_ylim(iy0, iy1)
        c10.clip(box(lon0 - 1.5, lat0 - 1, lon1 + 1.5, lat1 + 1)).to_crs(lc).plot(ax=a, color=LAND, edgecolor="white", linewidth=0.4, zorder=1)
        sub = truth[(truth.lon >= lon0 - 0.3) & (truth.lon <= lon1 + 0.3) & (truth.lat >= lat0 - 0.2) & (truth.lat <= lat1 + 0.2)]
        sp = gpd.GeoDataFrame(sub, geometry=gpd.points_from_xy(sub.lon, sub.lat), crs=4326).to_crs(lc)
        sp = sp[(sp.geometry.x > ix0) & (sp.geometry.x < ix1) & (sp.geometry.y > iy0) & (sp.geometry.y < iy1)]
        mj = sp[sp.seamark_type == "light_major"]
        oth = sp[sp.seamark_type != "light_major"]
        a.scatter(oth.geometry.x, oth.geometry.y, s=36, color="#e07b39", edgecolor="white", linewidth=0.6, zorder=5)
        a.scatter(mj.geometry.x, mj.geometry.y, s=58, color="#b5172b", edgecolor="white", linewidth=0.7, zorder=6)
        a.text(0.03, 0.97, f"{letters[key]}  {title}", transform=a.transAxes, fontsize=9.6, fontweight="bold", va="top", ha="left", path_effects=HALO, zorder=9)
        n_in = len(sp)
        a.text(0.97, 0.03, f"{n_in} lighthouses", transform=a.transAxes, fontsize=8.6, va="bottom", ha="right", color=MUTED, path_effects=HALO, zorder=9)
        # labels: named features, greedy placement, majors first
        cv.fig.canvas.draw()
        renderer = cv.fig.canvas.get_renderer()
        taken = [a.title.get_window_extent(renderer)] if False else []
        named = pd.concat([mj, oth])
        named = named[named.name != ""]
        title_bb = a.texts[0].get_window_extent(renderer)
        taken.append(title_bb)
        for r in named.itertuples():
            label_ = re.sub(r"\s+fyr$", "", r.name, flags=re.I).replace(" Fyr", "")
            for dx, dy, ha, va in ((6, 0, "left", "center"), (-6, 0, "right", "center"), (0, 7, "center", "bottom"), (0, -7, "center", "top"),
                                   (6, 6, "left", "bottom"), (-6, 6, "right", "bottom"), (6, -6, "left", "top"), (-6, -6, "right", "top")):
                txt = a.annotate(label_, (r.geometry.x, r.geometry.y), xytext=(dx, dy), textcoords="offset points", ha=ha, va=va,
                                 fontsize=7.4, color=INK, zorder=9, path_effects=HALO)
                bb = txt.get_window_extent(renderer).expanded(1.03, 1.1)
                axbb = a.get_window_extent(renderer)
                if bb.x0 > axbb.x0 and bb.x1 < axbb.x1 and bb.y0 > axbb.y0 and bb.y1 < axbb.y1 and not any(bb.overlaps(o) for o in taken):
                    taken.append(bb)
                    break
                txt.remove()
        km = 20 if (ix1 - ix0) < 400_000 else 50
        scale_bar(a, ix0 + 0.04 * (ix1 - ix0), iy0 + 0.05 * (iy1 - iy0), km, size=7.5)
        return h_in, n_in

    hb_, nb_ = draw_inset("west", col_x, top, wB)
    hc_, nc_ = draw_inset("lofoten", xr, top - 1.45, wr)
    ytop_a = top - max(hb_, 1.45 + hc_) - 0.2
    ha_, na_ = draw_inset("south", col_x, ytop_a, col_w)
    cv.footnote([
        f"Source: OpenStreetMap, nodes and ways tagged man_made=lighthouse within Norway's mainland (ISO 3166-1 NO, Svalbard and Jan Mayen excluded: {len(other)} features), as of {base[:10]}; © OpenStreetMap contributors, ODbL 1.0. Land: Natural Earth 1:10m, public domain.",
        f"Projection: Lambert conformal conic, standard parallels 58°N and 70°N, centred on 14°E; enlargements A–C in local Lambert conformal conic projections at larger scales (scale bars). All {len(truth)} features shown ({n_places} places; a lighthouse mapped as both a node and a building outline is drawn twice). "
        "Major = seamark:type=light_major; labels are OSM names without 'fyr'.",
    ], width=205)
    cv.save("norway_lighthouses")
    return truth


BUILDERS = {map_id: globals()[f"build_{map_id}"] for map_id in IDS}


# --------------------------------------------------------------------------
# Spot checks: evaluated only from the written truth.csv files


def read_truth(map_id: str) -> pd.DataFrame | None:
    path = MAPS_DIR / map_id / "truth.csv"
    if not path.exists():
        return None
    return pd.read_csv(path, dtype={"key": str, "katotth": str, "sid": str, "id": str})


def truth_count(map_id: str, frame: pd.DataFrame) -> int:
    return frame.sid.nunique() if map_id == "north_indian_cyclones_2023" else len(frame)


def truth_lines(map_id: str, frame: pd.DataFrame) -> list[list[tuple[float, float]]]:
    if "geometry_wkt" in frame:
        out = []
        for text in frame.geometry_wkt:
            out.extend(O.lines_of(wkt.loads(text)))
        return out
    return [list(zip(g.lon, g.lat)) for _sid, g in frame.sort_values(["sid", "iso_time"]).groupby("sid")]


def evaluate_spot(map_id: str, spot: str, frame: pd.DataFrame) -> tuple[bool, str]:
    try:
        m = re.fullmatch(r"count=([\d.]+)±([\d.]+)%", spot)
        if m:
            expected, tol = map(float, m.groups())
            actual = truth_count(map_id, frame)
            lo, hi = expected * (1 - tol / 100), expected * (1 + tol / 100)
            return lo <= actual <= hi, f"{actual:,}; window {lo:,.0f}–{hi:,.0f}"
        m = re.fullmatch(r"(event_near|line_near)=(-?[\d.]+),(-?[\d.]+)", spot)
        if m:
            what, lon, lat = m.group(1), float(m.group(2)), float(m.group(3))
            if what == "event_near":
                known = frame.dropna(subset=["lon", "lat"])
                km = min(O.haversine_km((lon, lat), p) for p in zip(known.lon, known.lat))
            else:
                km = min(O.line_segments_km((lon, lat), c) for c in truth_lines(map_id, frame))
            return km <= NEAR_KM[what], f"{km:.1f} km; tolerance {NEAR_KM[what]:.0f} km"
        return B.evaluate_spot(map_id, spot, frame)
    except Exception as exc:  # report, never crash the table
        return False, f"unverifiable: {exc}"


def evaluate_must_include(map_id: str, entry: dict, frame: pd.DataFrame) -> list[tuple[str, bool, str]]:
    out = []
    for item in entry.get("must_include", []):
        if map_id == "lviv_settlements":
            names = {O.translit(n): n for n in frame.name}
            hit = item in names and frame[frame.name == names[item]].category.iloc[0] == "M"
            out.append((item, hit, f"{names.get(item, 'absent')} ({'city' if hit else 'not a city row'})"))
        else:
            hit = item in set(frame.key.astype(str))
            out.append((item, hit, "present in truth.csv" if hit else "absent"))
    return out


def evaluate(ids) -> dict:
    specs = tomllib.loads(SPECS_PATH.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in specs["map"]}
    results = {}
    for map_id in ids:
        frame = read_truth(map_id)
        if frame is None:
            continue
        entry = by_id[map_id]
        count = truth_count(map_id, frame)
        lo, hi = entry["units"]
        checks = [("units", f"{lo}–{hi}", lo <= count <= hi, f"{count:,}")]
        checks += [(s, s, *evaluate_spot(map_id, s, frame)) for s in entry["spot"]]
        checks += [("must_include", f"must_include {m}", ok, why) for m, ok, why in evaluate_must_include(map_id, entry, frame)]
        results[map_id] = (len(frame), count, checks)
    return results


def print_results(results) -> None:
    print("\nSpot-check results")
    for map_id, (rows, count, checks) in results.items():
        print(f"{map_id} ({rows} rows, count {count})")
        for _kind, label_, ok, measured in checks:
            print(f"   {'PASS' if ok else 'FAIL'}  {label_:36} {measured}")


PROPOSALS = {
    "north_indian_cyclones_2023|line_near=92.3,20.8": (
        "**Proposed: `line_near=92.9,20.4`** (Mocha's landfall, 14 May 2023, near Sittwe: 0 km; the IBTrACS first over-land fix is 92.6°E 19.9°N). "
        "The remembered point (92.3°E 20.8°N) is the Maungdaw coast on the Bangladesh–Myanmar border, 76 km from the track; "
        "Mocha passed south-east of it and came ashore near Sittwe."),
    "eu_elderly_share_nuts2|FRY4<15": (
        "**Proposed: `FRY3<15`** (Guyane 7.7%; the spec's own comment says 'Guyane?' but FRY4 is La Réunion, which at 15.7% is the "
        "second-youngest outermost region after Mayotte 2.7%). Alternatively `FRY4<16`."),
    "norway_lighthouses|300–2500": (
        "**Proposed: `units = [150, 300]`.** OSM has 230 mainland man_made=lighthouse features (213 places), not 300+."),
    "norway_lighthouses|count=1500±50%": (
        "**Proposed: `count=230±25%`** (172–288). The 1,500 guess is off by a factor of 6.5. OSM holds only 230 features tagged man_made=lighthouse."
        " If seamark:type=light_* beacons without that tag were meant, the prompt would have to say 'lights', not 'lighthouses'."),
    "africa_gdp_per_capita|frame: excludes [46.7, 24.7] inside the frame": (
        "**Spec defect, not fixable by the map.** Riyadh (46.7°E 24.7°N) lies between Cairo (31.2°E, 30.0°N, a `contains` point) and Mauritius "
        "(57.5°E 20.2°S, a `contains` point) on both axes, so any rectangular window holding both holds Riyadh. **Proposed: replace it with Madrid `[-3.70, 40.42]`** "
        "or Delhi `[77.2, 28.6]`; both are outside the reference frame."),
    "lviv_settlements|frame: excludes [24.71, 48.92] inside the frame": (
        "**Spec defect.** Ivano-Frankivsk (24.71°E 48.92°N) lies inside Lviv oblast's own bounding box (22.1–25.2°E, 48.6–50.7°N: the oblast's "
        "south-east corner reaches 48.9°N) — the same trap as the Californian `excludes` in set 2. **Proposed: Rivne `[26.25, 50.62]`**, outside the box and the frame."),
    "norway_lighthouses|frame: excludes [18.07, 59.33] inside the frame": (
        "**Spec defect.** Stockholm (18.07°E 59.33°N) lies inside mainland Norway's bounding box (4.5–31.2°E, 58.0–71.2°N), so every rectangular frame "
        "of Norway contains it. **Proposed: Copenhagen `[12.57, 55.68]`** (outside the reference frame, which starts at about 57.1°N)."),
}


def render_report(results) -> str:
    notes = json.loads(WINDOW_NOTES_PATH.read_text()) if WINDOW_NOTES_PATH.exists() else {}
    lines = [
        "# Set-3 reference maps: spot-check report", "",
        "Generated by `uv run evals/maps/etalons/set3.py`. Every check is evaluated from the written `truth.csv` "
        "files only (tolerances of `evals/maps/run.py`: event_near 100 km, line_near 20 km). `units` and `must_include` "
        "are evaluated the same way; the `contains` / `excludes` frame checks are recorded when the maps are drawn.", "",
        "| Map | Rows | Check | Result | Measured |", "|---|---:|---|---|---|",
    ]
    total = passed = 0
    fails = []
    for map_id in IDS:
        if map_id not in results:
            lines.append(f"| `{map_id}` | — | — | not built | |")
            continue
        rows, _count, checks = results[map_id]
        for kind, label_, ok, measured in checks:
            total += 1
            passed += ok
            lines.append(f"| `{map_id}` | {rows:,} | `{label_}` | {'PASS' if ok else '**FAIL**'} | {measured} |")
            if not ok:
                fails.append((map_id, label_, measured))
        for note in notes.get(map_id, []):
            total += 1
            lines.append(f"| `{map_id}` | {rows:,} | frame: {note.split(' [')[0]} `[{note.split('[')[1].rstrip(']').split(']')[0]}]` | **FAIL** | {note} |")
            fails.append((map_id, "frame: " + note, note))
        if map_id in notes and not notes[map_id]:
            lines.append(f"| `{map_id}` | {rows:,} | frame: every `contains` inside, every `excludes` outside | PASS | reference frame |")
            total += 1
            passed += 1
    passed_frames = sum(1 for m in IDS if m in notes and not notes[m])
    lines += ["", f"**{passed} of {total} checks pass** (frame checks counted per map when clean, per violated point otherwise).", "",
              "## FAILs: true values and proposed corrections", ""]
    for map_id, label_, measured in fails:
        lines.append(f"- `{map_id}` `{label_}` — measured {measured}. {PROPOSALS.get(map_id + '|' + label_, 'see notes')}")
    lines += ["", REPORT_NOTES.strip(), ""]
    return "\n".join(lines)


REPORT_NOTES = """
## What was measured, and what the specs should know

### Counts that moved against the spec's guesses

- **`norway_lighthouses`: 230 features, not 1,500.** OSM (overpass-api.de, 2026-10-27) holds 248 elements with man_made=lighthouse in the Norway and Svalbard
  areas; after de-duplicating the two overlapping queries (Svalbard sits inside Norway's ISO area) that is 230 mainland features plus 9 on Svalbard. A
  lighthouse is often mapped twice, as a light node and as its building outline: 213 distinct places (elements within 150 m). Both the `units` lower bound
  (300) and `count=1500±50%` fail by a wide margin for a service that maps the tag correctly.
- **`germany_wind_turbines`: 30,735 features** (30,547 nodes, 188 way centroids) — inside `count=30000±30%`. OSM's Germany area is land only, so the
  roughly 1,600 offshore turbines are not in the count; the map says so.
- **`lviv_settlements`: 1,928 KATOTTH settlements** (44 cities, 35 селища, 1,849 villages; six city districts and 73 hromadas excluded as units) — inside
  `count=1900±10%`. All 1,928 are located on an OSM place node (1,920 by name within the hromada, 7 namesakes resolved by their katotth tag, 1 by tag only).
- **`indonesia_quakes_2025`: 147 events** that USGS places in Indonesia (place string ends ', Indonesia' or is 'Banda Sea') from 224 in the query rectangle
  93–143°E, 13°S–9°N; the other 77 are Philippines 56, Timor-Leste 11, Papua New Guinea 7 and Micronesia 3. A service that keeps the whole rectangle
  returns 224, which is still inside `150±50%` (75–225) but only by one event. The largest 2025 event is M6.7 (14 Jul, Banda Sea region).
- **`north_indian_cyclones_2023`: six named storms** (Mocha, Biparjoy, Tej, Hamoon, Midhili, Michaung) and four unnamed depressions; the spec's `count=8±40%`
  window is 5–11, so six passes. Mocha is Cat 5 on JTWC's 1-minute scale (145 kt) but 115 kt on IMD's 3-minute scale; IMD's wind column is missing for many fixes
  (only 17 of Midhili's 39 fixes carry one), so the reference colours by the complete JTWC series and says so.
- **`paris_metro`: 16 lines**, 402 km, 324 named stations. OSM tags the Orlyval airport shuttle with network 'Métro de Paris' (two relations): excluded,
  as is line 15 (network 'Île-de-France Mobilités'). A service that counts relations returns 36 (2 per line and a few branches); `count=16±15%` is 14–18.

### Data vintage and source changes found while building

- **INSEE regional population (France):** the page the 2023 link points to (`/statistiques/1893198`) still serves the 1 January **2023** series; the 1 January **2026**
  series (early results, data updated 23 Dec 2025, published 13 Jan 2026) lives at `/statistiques/8721456`, file `estim-pop-nreg-sexe-gca-1975-2026.xlsx`. The 13
  metropolitan regions sum to INSEE's own 'France métropolitaine' row, 66,792,845. INSEE spells 'Centre-Val-de-Loire' with hyphens and pads 'Guadeloupe ' with a trailing blank.
- **OWID life expectancy** ends at 2023 (UN WPP 2024) for all eleven states; OWID names Timor-Leste 'East Timor'. **OWID GDP per capita** now ends at **2025** for
  every African country that has a value — mostly IMF estimates — and has none for Eritrea, South Sudan and (as a non-country) Western Sahara.
- **Eurostat demo_r_pjanind2** carries two units (`YR`, `PC`) under the same indicator and also publishes values under superseded NUTS 2021 codes (NL31, NL33, PT16–18,
  …): the reference uses `PC` and the 244 NUTS 2024 level-2 codes of the EU27, all of which have a 2025 value.
- **Overpass:** overpass-api.de answered all four queries (Paris, Norway, Germany, Lviv; about eight minutes in all, most of it the Lviv hromada polygons) with the User-Agent set; no fallback to the mirrors was needed. Germany's 30,735 features came back in
  one reply (`out center tags qt`).
- Natural Earth draws Western Sahara as part of Morocco; the Africa reference cuts Morocco at 27°40′N so Western Sahara is hatched whole, never in Morocco's class.

### Reference images

Each `reference.png` is 1600 px wide, built with geopandas/matplotlib outside mapsvc. Projections: Lambert azimuthal equal-area (Southeast Asia, Africa), Lambert-93 (France,
Paris), Mercator on 118°E (Indonesia), transverse Mercator (Lviv), ETRS89-LAEA with the eight outermost regions inset (EU), LAEA again for the German hexagons,
Lambert conformal conic (cyclones; Norway with three enlargements). Missing values are hatched, never a class; each footnote names source, licence, vintage,
projection, classification and exclusions.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=IDS, help="map ids; default: all ten")
    parser.add_argument("--check-only", action="store_true", help="only evaluate existing truth.csv files")
    parser.add_argument("--fetch-only", action="store_true", help="only run the Overpass queries (slow)")
    args = parser.parse_args()
    ids = tuple(args.ids) or IDS
    if args.fetch_only:
        main_fetch()
        return 0
    if not args.check_only:
        for map_id in ids:
            print(f"\n[{map_id}]", flush=True)
            BUILDERS[map_id]()
        save_window_notes()
    results = evaluate(IDS)
    print_results({k: v for k, v in results.items() if k in ids})
    REPORT_PATH.write_text(render_report(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
