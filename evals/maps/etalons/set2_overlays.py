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
# ]
# ///
"""Reference ("etalon") maps for the set-2 overlay specs: points and lines.

    uv run evals/maps/etalons/set2_overlays.py [ids...] [--check-only]

Ids: atlantic_hurricanes_2024, tokyo_subway, volcanoes_holocene,
trans_siberian, europe_largest_cities, volyn_settlements.

Shares the downloader, canvas and palette of ``build.py`` (imported, not
copied) and, like it, never imports mapsvc: this is an independent oracle.
Cached downloads live in ``.cache/`` with an ``s2o_`` prefix; delete a file
to refresh it. Spot checks are evaluated from the written truth.csv files
only, with the tolerances of ``evals/maps/run.py`` (event_near 100 km,
line_near 20 km), and written to REPORT_set2_overlays.md.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import textwrap
import tomllib
import unicodedata
import zipfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build as B  # noqa: E402  (sets the Agg backend)

import geopandas as gpd  # noqa: E402
import matplotlib as mpl  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402,F401
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402
from pyproj import CRS, Geod, Transformer  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import LineString, MultiLineString, Point, box  # noqa: E402
from shapely.ops import linemerge, polygonize, unary_union  # noqa: E402

from build import (  # noqa: E402
    CACHE_DIR, HALO, HTTP, INK, LAND, MAPS_DIR, MUTED, PAPER, WATER, WIDTH_IN,
    Canvas, check_window, fetch, project_points, write_csv, write_md,
)

HERE = Path(__file__).resolve().parent
SPECS_PATH = MAPS_DIR / "specs.toml"
REPORT_PATH = HERE / "REPORT_set2_overlays.md"
IDS = (
    "atlantic_hurricanes_2024",
    "tokyo_subway",
    "volcanoes_holocene",
    "trans_siberian",
    "europe_largest_cities",
    "volyn_settlements",
)
GEOD = Geod(ellps="WGS84")
NEAR_KM = {"event_near": 100.0, "line_near": 20.0}   # as evals/maps/run.py

IBTRACS_URL = (
    "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/"
    "v04r01/access/csv/ibtracs.NA.list.v04r01.csv"
)
GVP_WFS_URL = (
    "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS&version=2.0.0"
    "&request=GetFeature&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes"
    "&outputFormat=application%2Fjson"
)
GVP_CITATION = (
    "Global Volcanism Program, 2025. [Database] Volcanoes of the World (v. 5.3.x). Distributed by "
    "Smithsonian Institution, compiled by Venzke, E. https://doi.org/10.5479/si.GVP.VOTW5-2025.5.3"
)
GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
KATOTTH_PAGE = (
    "https://mindev.gov.ua/diialnist/rozvytok-mistsevoho-samovriaduvannia/"
    "kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytorii-terytorialnykh-hromad"
)
KATOTTH_URL = "https://mindev.gov.ua/storage/app/sites/1/uploaded-files/kodifikator-07-07.xlsx"
KATOTTH_ORDER = "order No. 1321 of 7 July 2026"
KATOTTH_2023_URL = "https://mindev.gov.ua/storage/app/sites/1/uploaded-files/kodifikator.xlsx"
JPN_ADM2_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/"
    "gbOpen/JPN/ADM2/geoBoundaries-JPN-ADM2.geojson"
)
NE_LAKES_URL = f"{B.NE}/50m_physical/ne_50m_lakes.zip"
NE_STATES_URL = f"{B.NE}/50m_cultural/ne_50m_admin_1_states_provinces_lines.zip"
NE10_LAKES_URL = f"{B.NE}/10m_physical/ne_10m_lakes.zip"
NE10_COUNTRIES_URL = f"{B.NE}/10m_cultural/ne_10m_admin_0_countries.zip"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
TSR_RELATIONS = {2128634: "even", 4224465: "odd"}   # Moscow->Vladivostok, Vladivostok->Moscow
VOLYN_ISO = "UA-07"

TOKYO_QUERY = """
[out:json][timeout:600];
relation["route"="subway"]["network"~"^(東京メトロ|都営地下鉄)$"](35.40,139.40,36.00,140.20);
out body geom;
node["railway"="station"]["station"="subway"](35.55,139.55,35.85,140.00);
out body;
"""
TSR_QUERY = """
[out:json][timeout:600];
relation(id:2128634,4224465);
out body geom;
"""
VOLYN_QUERY = """
[out:json][timeout:900];
area["ISO3166-2"="UA-07"]["admin_level"="4"]->.v;
node["place"](area.v);
out body;
rel(area.v)["boundary"="administrative"]["admin_level"~"^(6|7)$"];
out body geom;
rel["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"~"^UA-(07|56|46)$"];
out body geom;
"""
OVERPASS_FILES = {
    "tokyo_subway": (TOKYO_QUERY, "s2o_tokyo_subway_overpass.json"),
    "trans_siberian": (TSR_QUERY, "s2o_trans_siberian_overpass.json"),
    "volyn_settlements": (VOLYN_QUERY, "s2o_volyn_overpass.json"),
}


# --------------------------------------------------------------------------
# Downloads


def overpass(query: str, filename: str) -> dict:
    """POST an Overpass query once, cache the JSON, fall back across mirrors."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    if target.exists() and target.stat().st_size:
        return json.loads(target.read_text(encoding="utf-8"))
    errors = []
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            print(f"  overpass {endpoint} -> {filename}", flush=True)
            response = HTTP.post(endpoint, data={"data": query}, timeout=900)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("elements"):
                raise RuntimeError("no elements: " + str(payload.get("remark", ""))[:200])
            if payload.get("remark"):
                raise RuntimeError("Overpass remark: " + payload["remark"][:200])
            payload["query"] = query
            partial = target.with_suffix(".part")
            partial.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            partial.replace(target)
            return payload
        except Exception as exc:  # independent public mirrors
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("All Overpass endpoints failed: " + "; ".join(errors))


def osm_base(payload: dict) -> str:
    return payload.get("osm3s", {}).get("timestamp_osm_base", "unknown")


def compact(query: str) -> str:
    return " ".join(line.strip() for line in query.strip().splitlines())


def way_line(member_or_way: dict) -> LineString | None:
    pts = [(p["lon"], p["lat"]) for p in member_or_way.get("geometry", []) if p]
    return LineString(pts) if len(pts) >= 2 else None


def relation_polygon(rel: dict):
    """Assemble an OSM multipolygon relation delivered with `out geom`."""
    outer, inner = [], []
    for m in rel.get("members", []):
        if m["type"] != "way":
            continue
        line = way_line(m)
        if line is None:
            continue
        (inner if m.get("role") == "inner" else outer if m.get("role") in ("outer", "") else []).append(line)
    poly = unary_union(list(polygonize(unary_union(outer))))
    if inner:
        poly = poly.difference(unary_union(list(polygonize(unary_union(inner)))))
    return poly


def geodesic_km(geom) -> float:
    return GEOD.geometry_length(geom) / 1000


def dissolved_route_km(lines_4326: list, *, buffer_m: float = 30.0, spacing_m: float = 4.5) -> float:
    """Route length with parallel tracks merged, as build.route_km, but in
    6-degree UTM strips so a 9,000 km line is measured without projection
    distortion: union of flat buffers / (2*buffer + track spacing)."""
    total = 0.0
    merged = unary_union(lines_4326)
    xmin, ymin, xmax, ymax = merged.bounds
    for zone in range(int((xmin + 180) // 6) + 1, int((xmax + 180) // 6) + 2):
        west = -180 + (zone - 1) * 6
        part = merged.intersection(box(west, -90, west + 6, 90))
        if part.is_empty:
            continue
        north = (ymin + ymax) / 2 >= 0
        crs = CRS.from_dict({"proj": "utm", "zone": zone, "south": not north, "ellps": "WGS84"})
        t = Transformer.from_crs(4326, crs, always_xy=True)
        projected = shapely.transform(part, lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))
        union = projected.buffer(buffer_m, cap_style="flat")
        total += union.area / (2 * buffer_m + spacing_m) / 1000
    return total


def line_segments_km(target, coords) -> float:
    """Distance from a point to a polyline, as evals/maps/run.py measures it."""
    best = float("inf")
    for a, b in zip(coords[:-1], coords[1:]):
        k = math.cos(math.radians(target[1]))
        ax, ay = (a[0] - target[0]) * k, a[1] - target[1]
        bx, by = (b[0] - target[0]) * k, b[1] - target[1]
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy) * 111.195)
    return best


def haversine_km(a, b) -> float:
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2)
         * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, h)))


def lines_of(geom) -> list[list[tuple[float, float]]]:
    if geom.is_empty:
        return []
    parts = geom.geoms if hasattr(geom, "geoms") else [geom]
    return [list(p.coords) for p in parts]


def wkt_lines(geom, tolerance_deg: float, precision: int = 5) -> str:
    return shapely.to_wkt(geom.simplify(tolerance_deg), rounding_precision=precision)


def thin_frame(ax) -> None:
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_linewidth(0.6)
        spine.set_color("#9aa3a7")


def legend_axes(cv: Canvas, x_in, y_in, w_in, h_in):
    ax = cv.add_axes(x_in, y_in, w_in, h_in)
    ax.set_xlim(0, w_in)
    ax.set_ylim(0, h_in)
    ax.set_axis_off()
    return ax


# --------------------------------------------------------------------------
# Atlantic hurricanes 2024 (IBTrACS)


SSHS = [  # (min USA_SSHS, colour, label)
    (-1, "#7fb3d5", "Tropical depression (< 34 kt)"),
    (0, "#3b9a8f", "Tropical storm (34–63 kt)"),
    (1, "#f2d24b", "Category 1 (64–82 kt)"),
    (2, "#f5a03a", "Category 2 (83–95 kt)"),
    (3, "#e4572e", "Category 3 (96–112 kt)"),
    (4, "#b5172b", "Category 4 (113–136 kt)"),
    (5, "#5e0d3b", "Category 5 (≥ 137 kt)"),
]


def sshs_colour(cat: int) -> str:
    return next((c for lo, c, _ in reversed(SSHS) if cat >= lo), SSHS[0][1])


def build_atlantic_hurricanes_2024() -> pd.DataFrame:
    path = fetch(IBTRACS_URL, "s2o_ibtracs_NA_v04r01.csv")
    raw = pd.read_csv(path, skiprows=[1], dtype=str, keep_default_na=False)
    season = raw[(raw.SEASON == "2024") & (raw.NAME != "UNNAMED")].copy()
    unnamed = raw[(raw.SEASON == "2024") & (raw.NAME == "UNNAMED")].SID.nunique()

    def num(col):
        return pd.to_numeric(season[col].str.strip().replace("", np.nan), errors="coerce")

    truth = pd.DataFrame({
        "sid": season.SID, "name": season.NAME.str.title(), "iso_time": season.ISO_TIME,
        "lon": num("LON"), "lat": num("LAT"), "nature": season.NATURE,
        "wind_kt": num("USA_WIND").astype("Int64"), "pres_mb": num("USA_PRES").astype("Int64"),
        "sshs": num("USA_SSHS").astype("Int64"), "usa_record": season.USA_RECORD.str.strip(),
    }).sort_values(["sid", "iso_time"]).reset_index(drop=True)
    write_csv("atlantic_hurricanes_2024", truth)
    storms = truth.groupby("sid", sort=False).agg(
        name=("name", "first"), start=("iso_time", "first"), peak=("wind_kt", "max"),
        cat=("sshs", "max"), n=("lon", "size")).reset_index().sort_values("start")
    n_storms = len(storms)
    hurricanes = int((storms.cat >= 1).sum())
    majors = int((storms.cat >= 3).sum())
    last_mod = pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d")
    write_md("atlantic_hurricanes_2024", [
        f"- URL/API: {IBTRACS_URL} (NOAA NCEI IBTrACS v04r01, North Atlantic basin file); rows with SEASON = 2024 and NAME ≠ UNNAMED.",
        f"- Vintage: IBTrACS v04r01 as downloaded {last_mod} (server Last-Modified 2026-10-02); 2024 tracks are NHC final best tracks. Positions are IBTrACS LAT/LON, intensity the USA (NHC) agency columns.",
        "- Licence: IBTrACS is a NOAA product, U.S. government work (public domain); cite Knapp et al. (2010) BAMS 91:363 and Gahtan et al. (2024) IBTrACS v04r01, NOAA NCEI, doi:10.25921/82ty-9e16. Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} track fixes (3-hourly, incl. NHC landfall/intensity special records) of {n_storms} named storms — sid, name, iso_time (UTC), lon, lat, nature (TS tropical, ET extratropical, DS disturbance, SS subtropical), wind_kt, pres_mb, sshs (−5…5; −1 = depression, 0 = tropical storm), usa_record (L = landfall, I = intensity peak, R = rapid change). `count` = distinct sid.",
        f"- Caveat: {hurricanes} hurricanes, {majors} major (Cat 3+). {unnamed} unnamed 2024 system (Potential Tropical Cyclone Eight, never a tropical cyclone) is excluded. Tracks include pre-genesis disturbance and post-tropical stages (drawn dashed grey).",
    ])

    crs = "+proj=lcc +lat_1=20 +lat_2=45 +lat_0=30 +lon_0=-48 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    xs, ys = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    # the frame: every track point plus margins for the labels on land
    xmin, xmax = xs.min() - 420_000, xs.max() + 120_000
    ymin, ymax = ys.min() - 260_000, ys.max() + 160_000
    extent = (xmin, xmax, ymin, ymax)
    check_window("atlantic_hurricanes_2024", crs, extent)
    cv = Canvas(extent, "Atlantic hurricanes and tropical storms of 2024",
                f"Tracks of all {n_storms} named storms of the season, coloured by Saffir–Simpson category along the track · "
                f"{hurricanes} became hurricanes, {majors} major hurricanes",
                footer=1.7)
    ax = cv.ax
    frame = box(xmin, ymin, xmax, ymax)
    world = B.load_world()
    world = world.clip(box(-130, -5, 40, 75)).to_crs(crs)
    world.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    states = gpd.read_file(fetch(NE_STATES_URL, "s2o_ne_50m_admin1_lines.zip"))
    states = states[states.ADM0_A3.isin(["USA", "MEX", "CAN"])].clip(box(-130, 5, -50, 60)).to_crs(crs)
    states.plot(ax=ax, color="white", linewidth=0.45, zorder=1.1)
    # graticule
    for lon in range(-110, 21, 10):
        lats = np.linspace(0, 70, 141)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
    for lat in range(10, 61, 10):
        lons = np.linspace(-120, 20, 281)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        lx, ly = t.transform(-33.5 if lat < 50 else -27, lat)
        if frame.contains(Point(lx, ly)):
            ax.text(lx, ly + 15_000, f"{lat}°N", fontsize=7.5, color="#7c97a1", ha="center", va="bottom", zorder=0.6)

    truth["x"], truth["y"] = xs, ys
    order = storms.sort_values("cat").sid  # strongest drawn last
    for sid in order:
        g = truth[truth.sid == sid]
        pts = g[["x", "y"]].to_numpy()
        tropical = g.nature.isin(["TS", "SS"]).to_numpy()
        cats = g.sshs.fillna(-1).astype(int).to_numpy()
        seg_cat = np.maximum(cats[:-1], cats[1:])
        segs = np.stack([pts[:-1], pts[1:]], axis=1)
        trop = tropical[:-1] & tropical[1:]
        lw = np.where(seg_cat >= 3, 3.0, np.where(seg_cat >= 1, 2.4, 1.6))
        ax.add_collection(LineCollection(segs[~trop], colors="#8a8f93", linewidths=1.0,
                                         linestyles=(0, (2.2, 1.6)), zorder=3, capstyle="round"))
        ax.add_collection(LineCollection(segs[trop], colors="white", linewidths=lw[trop] + 1.6,
                                         zorder=3.5, capstyle="round"))
        ax.add_collection(LineCollection(segs[trop], colors=[sshs_colour(c) for c in seg_cat[trop]],
                                         linewidths=lw[trop], zorder=4, capstyle="round"))

    # every NHC landfall record as a ring; the spec's three storms annotated
    landfalls = truth[truth.usa_record == "L"]
    ax.scatter(landfalls.x, landfalls.y, s=18, facecolor="white", edgecolor=INK, linewidth=0.9, zorder=7)
    notes = {  # (storm, landfall date) -> (place, dx, dy)
        ("Beryl", "2024-07-01"): ("Carriacou, Grenada", -16, -26),
        ("Beryl", "2024-07-08"): ("Matagorda, Texas", 0, 40),
        ("Helene", "2024-09-27"): ("Big Bend, Florida", 60, 22),
        ("Milton", "2024-10-10"): ("Siesta Key, Florida", 34, -34),
    }
    for row in landfalls.itertuples():
        key = (row.name, row.iso_time[:10])
        if key not in notes:
            continue
        place, dx, dy = notes[key]
        when = pd.Timestamp(row.iso_time).strftime("%-d %b")
        cat = f"Cat {row.sshs}" if row.sshs >= 1 else "TS"
        ax.annotate(f"{row.name} landfall, {when}\n{place} · {cat}, {row.wind_kt} kt", (row.x, row.y),
                    xytext=(dx, dy), textcoords="offset points",
                    ha="center" if dx == 0 else ("left" if dx > 0 else "right"), va="center",
                    fontsize=7.8, color="#2b3539", zorder=8, path_effects=HALO, linespacing=1.15,
                    arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.5, shrinkA=0, shrinkB=3))
    label_at = {  # storm-name anchor beside its own track: (lon, lat, dx, dy) in points
        "Alberto": (-97.8, 22.4, -8, 0), "Beryl": (-47.0, 9.8, 0, -12), "Chris": (-97.0, 19.8, -8, 0),
        "Debby": (-79.7, 37.0, 10, 0), "Ernesto": (-68.7, 26.1, 10, 0), "Francine": (-90.5, 32.0, 8, 0),
        "Gordon": (-27.9, 15.7, 0, 10), "Helene": (-84.6, 36.0, 10, 4), "Isaac": (-55.1, 36.9, 0, -11),
        "Joyce": (-45.0, 19.3, 44, 14), "Kirk": (-49.5, 32.6, -8, 0), "Leslie": (-29.6, 10.4, 0, -10),
        "Milton": (-91.0, 22.2, 0, -12), "Nadine": (-86.0, 17.35, 0, 9), "Oscar": (-70.2, 21.3, 0, 10),
        "Patty": (-27.0, 37.7, 0, -12), "Rafael": (-91.9, 26.0, -8, 0), "Sara": (-82.2, 15.8, 0, -10),
    }
    for row in storms.itertuples():
        lon, lat, dx, dy = label_at[row.name]
        (x, y), = project_points(crs, [(lon, lat)])
        big = row.name in {"Beryl", "Helene", "Milton"}
        far = abs(dx) > 20 or abs(dy) > 20
        ax.annotate(row.name, (x, y), xytext=(dx, dy), textcoords="offset points",
                    ha="center" if dx == 0 else ("left" if dx > 0 else "right"), va="center",
                    fontsize=11 if big else 8.6, fontweight="bold" if big else "normal",
                    color=INK, zorder=9, path_effects=HALO,
                    arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.5, shrinkA=0, shrinkB=2) if far else None)
    for name, lon, lat in (("UNITED STATES", -98, 37.5), ("MEXICO", -102.5, 23.5), ("CANADA", -75, 51.5),
                           ("AFRICA", -6, 17), ("EUROPE", 2.5, 46)):
        (x, y), = project_points(crs, [(lon, lat)])
        if frame.contains(Point(x, y)):
            ax.text(x, y, name, fontsize=9, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("North Atlantic Ocean", -36, 27.5), ("Gulf of Mexico", -88.6, 27.3),
                           ("Caribbean Sea", -73.5, 14.5)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", va="center", zorder=2)

    lg = legend_axes(cv, cv.left, 0.9, 15.2, 0.72)
    lg.text(0, 0.70, "Saffir–Simpson category at that point of the track (1-min sustained wind, NHC)", va="top",
            fontsize=10.5, fontweight="bold")
    x = 0
    for _lo, colour, label in SSHS:
        lg.plot([x, x + 0.42], [0.2, 0.2], color=colour, linewidth=3.2, solid_capstyle="butt")
        lg.text(x + 0.5, 0.2, label.replace(" (", "\n("), va="center", fontsize=8.4, linespacing=1.1)
        x += 1.92
    lg.plot([x, x + 0.42], [0.2, 0.2], color="#8a8f93", linewidth=1.0, linestyle=(0, (2.2, 1.6)))
    lg.text(x + 0.5, 0.2, "Before genesis /\npost-tropical", va="center", fontsize=8.4, linespacing=1.1)
    cv.footnote([
        f"Source: NOAA NCEI, International Best Track Archive for Climate Stewardship (IBTrACS) v04r01, North Atlantic file, season 2024 (NHC best tracks), retrieved {last_mod}; "
        "public domain (Knapp et al. 2010; Gahtan et al. 2024). Land: Natural Earth 1:50m, public domain.",
        f"Projection: Lambert conformal conic, standard parallels 20°N and 45°N, centred on 48°W. All {n_storms} named storms shown, {len(truth)} 3-hourly fixes; "
        "line colour and width = NHC Saffir–Simpson category of each segment. Excluded: 1 unnamed potential tropical cyclone. Landfall points from NHC reports.",
    ], width=205)
    cv.save("atlantic_hurricanes_2024")
    return truth


# --------------------------------------------------------------------------
# Holocene volcanoes (Smithsonian GVP)


VOLCANO_CLASSES = [  # (class, colour, GVP primary types)
    ("Stratovolcano", "#c8372d", {"Stratovolcano", "Stratovolcano(es)", "Stratovolcano?"}),
    ("Shield", "#f08c2e", {"Shield", "Shield(s)", "Shield(pyroclastic)"}),
    ("Caldera", "#7b3f98", {"Caldera", "Caldera(s)"}),
    ("Volcanic field", "#2878b5", {"Volcanic field"}),
    ("Complex or compound", "#2a9d6f", {"Complex", "Compound"}),
    ("Cones, domes, maars, fissure vents", "#8a6a3b", {
        "Pyroclastic cone", "Pyroclastic cone(s)", "Lava dome", "Lava dome(s)", "Maar", "Maar(s)",
        "Tuff cone", "Tuff cone(s)", "Tuff ring(s)", "Cone", "Lava cone", "Lava cone(es)",
        "Fissure vent", "Fissure vent(s)", "Crater rows", "Explosion crater(s)"}),
]


def volcano_class(kind) -> str:
    for name, _colour, kinds in VOLCANO_CLASSES:
        if kind in kinds:
            return name
    return "Type not recorded"


def build_volcanoes_holocene() -> pd.DataFrame:
    path = fetch(GVP_WFS_URL, "s2o_gvp_holocene_wfs.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for f in payload["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        rows.append({
            "volcano_number": p["Volcano_Number"], "name": p["Volcano_Name"], "country": p["Country"],
            "lon": round(lon, 4), "lat": round(lat, 4), "elevation_m": p.get("Elevation"),
            "primary_type": p.get("Primary_Volcano_Type") or "", "type_class": volcano_class(p.get("Primary_Volcano_Type")),
            "last_eruption_year": p.get("Last_Eruption_Year"), "evidence": p.get("Evidence_Category") or "",
            "region": p.get("Region") or "",
        })
    truth = pd.DataFrame(rows).sort_values("volcano_number").reset_index(drop=True)
    truth["last_eruption_year"] = truth.last_eruption_year.astype("Int64")
    truth["elevation_m"] = pd.to_numeric(truth.elevation_m, errors="coerce").astype("Int64")
    write_csv("volcanoes_holocene", truth)
    retrieved = payload.get("timeStamp", "")[:10]
    counts = truth.type_class.value_counts()
    since_1900 = int((truth.last_eruption_year >= 1900).sum())
    unknown_erupt = int(truth.last_eruption_year.isna().sum())
    write_md("volcanoes_holocene", [
        f"- URL/API: {GVP_WFS_URL} (GVP GeoServer WFS layer GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes, GeoJSON; numberMatched = {payload.get('numberMatched')}). volcano.si.edu itself answers scripted requests with HTTP 403.",
        f"- Vintage: live VOTW database as served on {retrieved}; most recent published citation found: {GVP_CITATION.replace('v. 5.3.x', 'v. 5.3.4; 30 Dec 2025')}. Last eruption years run to 2026, so the WFS is newer than that citation's date.",
        "- Licence: Smithsonian GVP data, free to use with the citation above (Smithsonian terms of use). Plate boundaries: Bird (2003) PB2002 (ODC-BY). Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} Holocene volcanoes — volcano_number, name, country, lon, lat, elevation_m, primary_type (GVP), type_class (6 classes used for colour), last_eruption_year (negative = BCE; blank = no dated eruption, Holocene activity inferred), evidence, region.",
        f"- Caveat: 'Holocene' means GVP judges the volcano active in the last ~11,700 years, not that an eruption is dated: {unknown_erupt} rows have no last-eruption year; {since_1900} erupted in 1900 or later. Classes: "
        + ", ".join(f"{k} {v}" for k, v in counts.items()) + ".",
    ])
    lon0 = 150.0
    cv, crs = B.world_canvas(
        "Every volcano active in the last 10,000 years",
        f"The {len(truth):,} Holocene volcanoes of the Smithsonian Global Volcanism Program, by volcano type · centred on the Pacific",
        lon0=lon0, footer=1.75)
    ax = cv.ax
    cut = lon0 - 180
    world = B.split_at_meridian(B.load_world(), cut).to_crs(crs)
    world.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.25, zorder=1)
    plates = gpd.read_file(fetch(B.PLATES_URL, "pb2002_boundaries.json"))
    plates = B.split_at_meridian(plates, cut).to_crs(crs)
    plates.plot(ax=ax, color="#c9a68c", linewidth=0.6, alpha=0.8, zorder=2)
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(crs)
    colours = {name: colour for name, colour, _ in VOLCANO_CLASSES} | {"Type not recorded": "#9aa0a4"}
    # common classes first so rarer ones stay visible on top
    for name in counts.index:
        part = pts[pts.type_class == name]
        ax.scatter(part.geometry.x, part.geometry.y, s=26, marker="^", color=colours[name],
                   edgecolor="white", linewidth=0.4, alpha=0.95, zorder=4)
    famous = {  # GVP name: (dx, dy) in points
        "Kilauea": (-9, -9), "Etna": (8, -8), "Fujisan": (10, 4), "St. Helens": (-8, 8),
        "Krakatau": (-8, -8), "Pinatubo": (8, 4), "Popocatepetl": (-8, -8), "Erebus": (9, 2),
        "Hekla": (-8, 6), "Vesuvius": (-8, 7), "Villarrica": (8, 0), "Nyiragongo": (-8, 0),
        "Hunga Tonga-Hunga Ha'apai": (-8, 6), "Klyuchevskoy": (8, 6), "Eyjafjallajokull": (8, -7),
    }
    for vname, (dx, dy) in famous.items():
        row = pts[pts.name == vname]
        if row.empty:
            raise ValueError(f"volcano {vname!r} not in the GVP list")
        r = row.iloc[0]
        ax.annotate(vname.replace("Fujisan", "Fuji").replace("Hunga Tonga-Hunga Ha'apai", "Hunga Tonga")
                    .replace("Eyjafjallajokull", "Eyjafjallajökull").replace("Popocatepetl", "Popocatépetl"),
                    (r.geometry.x, r.geometry.y), xytext=(dx, dy), textcoords="offset points",
                    ha="left" if dx > 0 else "right", va="center", fontsize=8.4, zorder=8,
                    path_effects=HALO, color=INK)
    lg = legend_axes(cv, cv.left, 0.86, 15.2, 0.8)
    lg.text(0, 0.78, "Primary volcano type (GVP), number of volcanoes", va="top", fontsize=10.5, fontweight="bold")
    x = 0.05
    for name, colour, _ in VOLCANO_CLASSES:
        lg.scatter([x + 0.08], [0.3], s=60, marker="^", color=colour, edgecolor="white", linewidth=0.4)
        label = f"{name} ({counts.get(name, 0)})"
        lg.text(x + 0.25, 0.3, label, va="center", fontsize=9)
        x += 0.45 + 0.072 * len(label)
    rest = int(counts.get("Type not recorded", 0))
    lg.plot([x + 0.1, x + 0.6], [0.3, 0.3], color="#c9a68c", linewidth=1.2)
    lg.text(x + 0.7, 0.3, "Plate boundary (Bird 2003)", va="center", fontsize=9)
    cv.footnote([
        f"Source: {GVP_CITATION.replace('v. 5.3.x', 'live database')}; retrieved {retrieved} from the GVP web feature service. "
        "Plate boundaries: P. Bird (2003), PB2002 (ODC-BY). Land: Natural Earth 1:50m.",
        f"Projection: Equal Earth centred on 150°E so the Pacific Ring of Fire is unbroken. All {len(truth):,} Holocene volcanoes shown, none excluded; "
        f"GVP's {truth.primary_type.nunique()} primary types grouped into 6 classes" + (f" ({rest} with no type recorded, grey)" if rest else "") + ". "
        "Holocene = judged active in roughly the last 11,700 years.",
    ])
    cv.save("volcanoes_holocene")
    return truth


# --------------------------------------------------------------------------
# Europe's largest cities (GeoNames)


EUROPE_ISO2 = {
    "AL", "AD", "AT", "BY", "BE", "BA", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IS", "IE", "IT", "XK", "LV", "LI", "LT", "LU", "MT", "MD", "MC", "ME", "NL", "MK", "NO",
    "PL", "PT", "RO", "SM", "RS", "SK", "SI", "ES", "SE", "CH", "UA", "GB", "VA",
}
GEONAMES_COLS = ("geonameid name asciiname alternatenames lat lon fclass fcode cc cc2 admin1 admin2 "
                 "admin3 admin4 population elevation dem tz moddate").split()
URALS_LON = 58.0   # European Russia: west of the Ural watershed (Perm 56.2°E, Ufa 56.0°E in; Yekaterinburg 60.6°E out)
# GeoNames carries an urban-area figure rather than the municipality for these:
GEONAMES_NOT_PROPER = {"Stockholm", "Copenhagen", "Oslo", "Brussels", "Dublin"}


def geonames_europe() -> tuple[pd.DataFrame, pd.DataFrame]:
    path = fetch(GEONAMES_URL, "s2o_geonames_cities15000.zip")
    with zipfile.ZipFile(path) as z:
        d = pd.read_csv(z.open("cities15000.txt"), sep="\t", header=None, names=GEONAMES_COLS,
                        dtype=str, keep_default_na=False, quoting=3)
    d["population"] = pd.to_numeric(d.population)
    d["lon"], d["lat"] = pd.to_numeric(d.lon), pd.to_numeric(d.lat)
    places = d[~d.fcode.isin({"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"})]
    europe = places[
        places.cc.isin(EUROPE_ISO2)
        | ((places.cc == "RU") & (places.lon < URALS_LON))
        | ((places.cc == "TR") & (places.name == "Istanbul"))   # the city proper straddles the Bosphorus
    ]
    return d, europe.sort_values(["population", "geonameid"], ascending=[False, True])


def build_europe_largest_cities() -> pd.DataFrame:
    _all, europe = geonames_europe()
    top = europe.head(30).copy()
    nxt = europe.iloc[30:35]
    truth = pd.DataFrame({
        "rank": range(1, 31), "geonameid": top.geonameid.astype(int), "name": top.name, "country": top.cc,
        "lon": top.lon, "lat": top.lat, "population": top.population.astype(int), "feature_code": top.fcode,
        "geonames_modified": top.moddate,
    })
    write_csv("europe_largest_cities", truth)
    base = pd.Timestamp(fetch(GEONAMES_URL, "s2o_geonames_cities15000.zip").stat().st_mtime, unit="s").strftime("%Y-%m-%d")
    flagged = [n for n in truth.name if n in GEONAMES_NOT_PROPER]
    write_md("europe_largest_cities", [
        f"- URL/API: {GEONAMES_URL} (cities15000.txt, all places with population ≥ 15,000); the 30 largest European rows by the `population` field.",
        f"- Vintage: GeoNames dump of {base} (server Last-Modified 2026-10-08); per-row edit dates in `geonames_modified` ({truth.geonames_modified.min()} – {truth.geonames_modified.max()}).",
        "- Licence: GeoNames, CC BY 4.0 (credit geonames.org). Basemap: Natural Earth 1:50m (public domain).",
        "- Rows: 30 — rank, geonameid, name, country (ISO2), lon, lat, population, feature_code, geonames_modified. Definition: GeoNames' population field = city proper (administrative city) for most rows; sections of cities (PPLX, e.g. Pest) excluded. Europe = 45 European states + Russia west of 58°E (Ufa, Perm in; Yekaterinburg out) + Istanbul (straddles the Bosphorus; included). Transcaucasia and Kazakhstan excluded.",
        f"- Caveat: GeoNames is not internally consistent — {', '.join(flagged)} carry urban-area figures, not the municipality; Moscow is still the 2010-census 10.38 M. Next five: "
        + ", ".join(f"{r.name} {r.population / 1e6:.2f} M" for r in nxt.itertuples()) + ". Without Istanbul, Oslo would be 30th.",
    ])
    render_europe_cities(truth, base, flagged, nxt)
    return truth


CITY_LABEL = {  # name -> (dx, dy, ha) in points from the centre; default: right of the circle
    "London": (0, -32, "center"), "Birmingham": (-14, 8, "right"), "Paris": (0, -21, "center"),
    "Madrid": (0, -24, "center"), "Barcelona": (14, -4, "left"), "Rome": (0, -20, "center"),
    "Milan": (-13, 0, "right"), "Munich": (-12, -6, "right"), "Vienna": (6, 15, "center"),
    "Budapest": (14, -4, "left"), "Prague": (-11, 8, "right"), "Berlin": (0, 22, "center"),
    "Hamburg": (-15, 2, "right"), "Copenhagen": (-10, 10, "right"), "Stockholm": (12, 0, "left"),
    "Warsaw": (14, 2, "left"), "Minsk": (14, 4, "left"), "Kyiv": (0, -24, "center"),
    "Kharkiv": (14, -4, "left"), "Rostov-on-Don": (12, -4, "left"), "Belgrade": (-13, -4, "right"),
    "Bucharest": (16, 0, "left"), "Sofia": (-12, -4, "right"), "Istanbul": (36, 0, "left"),
    "Moscow": (0, -38, "center"), "Saint Petersburg": (0, 30, "center"), "Nizhniy Novgorod": (0, 20, "center"),
    "Kazan": (0, 19, "center"), "Samara": (-12, -6, "right"), "Ufa": (12, 0, "left"),
}


def render_europe_cities(truth, base, flagged, nxt) -> None:
    crs = "+proj=laea +lat_0=52 +lon_0=22 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    xs, ys = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    extent = (xs.min() - 640_000, xs.max() + 560_000, ys.min() - 420_000, ys.max() + 330_000)
    check_window("europe_largest_cities", crs, extent)
    cv = Canvas(extent, "Europe’s 30 largest cities by population",
                "Population of the city proper (the administrative city, not the metropolitan area), as recorded by GeoNames · circle area ∝ population",
                footer=1.05)
    ax = cv.ax
    world = B.load_world()
    world = world.clip(box(-40, 20, 100, 85)).to_crs(crs)
    europe_iso3 = set(B.EUROPE) | {"RUS"}
    world[world.key.isin(europe_iso3)].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.6, zorder=1)
    world[~world.key.isin(europe_iso3)].plot(ax=ax, color="#eeebe4", edgecolor="white", linewidth=0.6, zorder=1)
    lats = np.linspace(44, 68, 50)
    ux, uy = t.transform(np.full_like(lats, URALS_LON), lats)
    ax.plot(ux, uy, color="#8a8f93", linewidth=0.9, linestyle=(0, (4, 3)), zorder=2)
    lx, ly = t.transform(URALS_LON - 0.8, 57.4)
    ax.text(lx, ly, f"{URALS_LON:.0f}°E: eastern limit\nused for European Russia", fontsize=8, color=MUTED,
            ha="right", va="center", zorder=2, linespacing=1.2)
    s_max = 36.0  # radius in points of the largest circle
    p_max = float(truth.population.max())

    def radius(pop):
        return s_max * math.sqrt(pop / p_max)

    for row, x, y in sorted(zip(truth.itertuples(), xs, ys), key=lambda r: -r[0].population):
        r = radius(row.population)
        ax.scatter([x], [y], s=(2 * r) ** 2, color="#b5452b", alpha=0.62, edgecolor="white", linewidth=1.0, zorder=4)
        ax.scatter([x], [y], s=5, color=INK, zorder=5)
    for row, x, y in zip(truth.itertuples(), xs, ys):
        r = radius(row.population)
        dx, dy, ha = CITY_LABEL.get(row.name, (r + 3, 0, "left"))
        name = row.name.replace("Nizhniy Novgorod", "Nizhny Novgorod")
        star = "*" if row.name in flagged else ""
        ax.annotate(f"{name}{star}\n{row.population / 1e6:.2f} M", (x, y), xytext=(dx, dy), textcoords="offset points",
                    ha=ha, va="center", fontsize=8.8 if row.rank > 10 else 9.8,
                    fontweight="bold" if row.rank <= 10 else "normal", linespacing=1.05,
                    zorder=8, path_effects=HALO, color=INK)
    for name, lon, lat in (("Atlantic Ocean", -9.5, 47.0), ("North Sea", 3.5, 56.3), ("Mediterranean Sea", 5.5, 38.9),
                           ("Black Sea", 34.0, 43.3), ("Baltic\nSea", 19.3, 57.2)):
        x, y = t.transform(lon, lat)
        ax.text(x, y, name, fontsize=9.5, color="#5f8796", style="italic", ha="center", va="center", zorder=2)
    # legend card in the North Atlantic corner
    lg = cv.panel(cv.left + 0.15, cv.footer + cv.map_h - 0.15, 3.0, 2.05)
    lg.add_patch(Rectangle((0, 0), 3.0, 2.05, facecolor=PAPER, edgecolor="#c9c4b8", linewidth=0.6, alpha=0.95))
    lg.text(0.15, 1.92, "Population, city proper", va="top", fontsize=10.5, fontweight="bold")
    cx, base_y = 0.68, 0.38
    for pop in (15_000_000, 5_000_000, 1_000_000):
        r_in = radius(pop) / 72
        lg.add_patch(Circle((cx, base_y + r_in), r_in, facecolor="#b5452b" if pop == 15_000_000 else "none",
                            alpha=0.62 if pop == 15_000_000 else 1, edgecolor="white" if pop == 15_000_000 else "#7d8a80",
                            linewidth=0.9))
        lg.plot([cx, cx + 0.95], [base_y + 2 * r_in, base_y + 2 * r_in], color=INK, linewidth=0.5)
        lg.text(cx + 1.0, base_y + 2 * r_in, f"{pop / 1e6:.0f} million", va="center", fontsize=9)
    lg.text(0.15, 0.17, "circle area ∝ population", va="center", fontsize=8.2, color=MUTED)
    cv.footnote([
        f"Source: GeoNames cities15000 (geonames.org), CC BY 4.0, dump of {base}; the 'population' field of each place. Basemap: Natural Earth 1:50m, public domain. "
        "Definition: city proper as GeoNames records it; city sections (e.g. Pest) excluded. Europe = 45 European states, Russia west of 58°E, and Istanbul (straddles the Bosphorus).",
        f"* GeoNames gives an urban-area rather than a municipal figure for {', '.join(flagged)}; Moscow is the 2010 census figure. "
        "Projection: Lambert azimuthal equal-area centred on 52°N 22°E. All 30 cities labelled; next in rank: "
        + ", ".join(f"{r.name} {r.population / 1e6:.2f} M" for r in nxt.head(3).itertuples()) + ".",
    ], width=200)
    cv.save("europe_largest_cities")


BUILDERS: dict[str, Callable[[], pd.DataFrame]] = {
    "atlantic_hurricanes_2024": build_atlantic_hurricanes_2024,
    "volcanoes_holocene": build_volcanoes_holocene,
    "europe_largest_cities": build_europe_largest_cities,
}


# --------------------------------------------------------------------------
# Spot checks: evaluated only from the written truth.csv files


def spec_entry(map_id: str) -> dict:
    specs = tomllib.loads(SPECS_PATH.read_text(encoding="utf-8"))
    return next(entry for entry in specs["map"] if entry["id"] == map_id)


def truth_lines(map_id: str, frame: pd.DataFrame) -> list[list[tuple[float, float]]]:
    if "geometry_wkt" in frame:
        out = []
        for text in frame.geometry_wkt:
            out.extend(lines_of(wkt.loads(text)))
        return out
    lines = []
    for _sid, g in frame.sort_values(["sid", "iso_time"]).groupby("sid"):
        lines.append(list(zip(g.lon, g.lat)))
    return lines


def truth_points(frame: pd.DataFrame) -> list[tuple[float, float]]:
    known = frame.dropna(subset=["lon", "lat"])
    return list(zip(known.lon, known.lat))


def truth_count(map_id: str, frame: pd.DataFrame) -> int:
    if map_id == "atlantic_hurricanes_2024":
        return frame.sid.nunique()
    return len(frame)


def evaluate_spot(map_id: str, spot: str, frame: pd.DataFrame) -> tuple[bool, str]:
    try:
        m = re.fullmatch(r"count=([\d.]+)±([\d.]+)%", spot)
        if m:
            expected, tol = map(float, m.groups())
            actual = truth_count(map_id, frame)
            lo, hi = expected * (1 - tol / 100), expected * (1 + tol / 100)
            return lo <= actual <= hi, f"{actual:,}; window {lo:,.0f}–{hi:,.0f}"
        m = re.fullmatch(r"total_km=([\d.]+)±([\d.]+)%", spot)
        if m:
            expected, tol = map(float, m.groups())
            lines = [LineString(c) for c in truth_lines(map_id, frame)]
            actual = dissolved_route_km(lines)
            way_sum = sum(geodesic_km(line) for line in lines)
            ok = abs(actual - expected) <= expected * tol / 100
            return ok, f"{actual:,.0f} route-km dissolved ({(actual / expected - 1) * 100:+.1f}%); {way_sum:,.0f} km summing ways"
        m = re.fullmatch(r"(event_near|line_near)=(-?[\d.]+),(-?[\d.]+)", spot)
        if m:
            what, lon, lat = m.group(1), float(m.group(2)), float(m.group(3))
            if what == "event_near":
                km = min(haversine_km((lon, lat), p) for p in truth_points(frame))
            else:
                km = min(line_segments_km((lon, lat), c) for c in truth_lines(map_id, frame))
            return km <= NEAR_KM[what], f"{km:.1f} km; tolerance {NEAR_KM[what]:.0f} km"
        raise ValueError("unsupported expression")
    except Exception as exc:  # report, never crash the table
        return False, f"unverifiable: {exc}"


def evaluate(ids) -> dict:
    results = {}
    for map_id in ids:
        path = MAPS_DIR / map_id / "truth.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype={"katotth": str, "sid": str})
        entry = spec_entry(map_id)
        results[map_id] = (truth_count(map_id, frame), len(frame),
                           [(s, *evaluate_spot(map_id, s, frame)) for s in entry["spot"]])
    return results


def print_results(results) -> None:
    print("\nSpot-check results")
    for map_id, (count, rows, checks) in results.items():
        print(f"{map_id} ({rows} rows, count {count})")
        for spot, ok, measured in checks:
            print(f"   {'PASS' if ok else 'FAIL'}  {spot:34} {measured}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=IDS, help="map ids; default: all six")
    parser.add_argument("--check-only", action="store_true", help="only evaluate existing truth.csv files")
    parser.add_argument("--fetch-only", action="store_true", help="only run the Overpass queries (slow)")
    args = parser.parse_args()
    ids = tuple(args.ids) or IDS
    if args.fetch_only:
        for map_id in ids:
            if map_id in OVERPASS_FILES:
                payload = overpass(*OVERPASS_FILES[map_id])
                print(f"  {map_id}: {len(payload['elements'])} elements, OSM base {osm_base(payload)}")
        return 0
    if not args.check_only:
        for map_id in ids:
            print(f"\n[{map_id}]", flush=True)
            BUILDERS[map_id]()
    print_results(evaluate(ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
