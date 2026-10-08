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


# --------------------------------------------------------------------------
# Tokyo subway (OpenStreetMap)


TOKYO_LINES = {  # ref -> (English name, operator); the 13 lines of Tokyo Metro and Toei
    "G": ("Ginza", "Tokyo Metro"), "M": ("Marunouchi", "Tokyo Metro"), "H": ("Hibiya", "Tokyo Metro"),
    "T": ("Tōzai", "Tokyo Metro"), "C": ("Chiyoda", "Tokyo Metro"), "Y": ("Yūrakuchō", "Tokyo Metro"),
    "Z": ("Hanzōmon", "Tokyo Metro"), "N": ("Namboku", "Tokyo Metro"), "F": ("Fukutoshin", "Tokyo Metro"),
    "A": ("Asakusa", "Toei"), "I": ("Mita", "Toei"), "S": ("Shinjuku", "Toei"), "E": ("Ōedo", "Toei"),
}
TOKYO_STATIONS = {  # label -> (OSM name, dx, dy) in points
    "Tokyo": ("東京", 12, -6), "Shinjuku": ("新宿", -12, 4), "Shibuya": ("渋谷", -12, -4),
    "Ikebukuro": ("池袋", -10, 8), "Ueno": ("上野", 12, 6),
}


def tokyo_lines() -> tuple[pd.DataFrame, list, str]:
    payload = overpass(*OVERPASS_FILES["tokyo_subway"])
    rels = [e for e in payload["elements"] if e["type"] == "relation"]
    stations = [e for e in payload["elements"] if e["type"] == "node"]
    rows = []
    for ref, (name_en, operator) in TOKYO_LINES.items():
        mine = [r for r in rels if r["tags"].get("ref") == ref]
        if not mine:
            raise ValueError(f"no OSM route relation for line {ref}")
        ways = {}
        for r in mine:
            for m in r["members"]:
                if m["type"] == "way" and "platform" not in m.get("role", "") and m.get("geometry"):
                    line = way_line(m)
                    if line is not None:
                        ways[m["ref"]] = line
        geom = unary_union(list(ways.values()))
        colours = [r["tags"]["colour"].upper() for r in mine if r["tags"].get("colour", "").startswith("#")]
        name_ja = sorted({r["tags"].get("name", "").split(" ")[0].split(":")[0].strip() for r in mine})[0]
        rows.append({
            "ref": ref, "name_en": f"{name_en} Line", "name_ja": name_ja, "operator": operator,
            "network": mine[0]["tags"]["network"], "colour": max(set(colours), key=colours.count),
            "osm_relations": ";".join(str(r["id"]) for r in sorted(mine, key=lambda r: r["id"])),
            "osm_ways": len(ways),
            # Tokyo's two directions often run in separate shield tunnels 20-80 m apart, so the
            # dissolve buffer is 100 m here (30 m leaves e.g. the Oedo line at 43.6 km, not 40.7)
            "route_km": round(dissolved_route_km(list(ways.values()), buffer_m=100, spacing_m=10), 2),
            "half_track_km": round(sum(geodesic_km(w) for w in ways.values()) / 2, 2),
            "geometry_wkt": wkt_lines(linemerge(geom) if geom.geom_type == "MultiLineString" else geom, 0.00003),
        })
    return pd.DataFrame(rows), stations, osm_base(payload)


def build_tokyo_subway() -> pd.DataFrame:
    truth, stations, base = tokyo_lines()
    write_csv("tokyo_subway", truth)
    narrow_km = sum(dissolved_route_km([LineString(c) for c in lines_of(wkt.loads(t))]) for t in truth.geometry_wkt)
    metro = truth[truth.operator == "Tokyo Metro"].route_km.sum()
    toei = truth[truth.operator == "Toei"].route_km.sum()
    write_md("tokyo_subway", [
        "- URL/API: Overpass API (overpass-api.de, mirror fallback): `" + compact(TOKYO_QUERY) + "`; relations with ref 'Al' (Keikyu–Toei–Keisei airport through-service) dropped.",
        f"- Vintage: OpenStreetMap as of {base}; delete `.cache/s2o_tokyo_subway_overpass.json` to refresh.",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Wards: geoBoundaries gbOpen JPN ADM2 (from OpenStreetMap, ODbL).",
        f"- Rows: {len(truth)} lines (9 Tokyo Metro, 4 Toei) — ref, name_en, name_ja, operator, network, colour (most common OSM `colour` tag of the line's relations), osm_relations, osm_ways, route_km (both directions dissolved with 100 m buffers), half_track_km (sum of member ways ÷ 2), geometry_wkt (member track ways, merged, simplified ~3 m). Sum of route_km: Tokyo Metro {metro:,.1f} km, Toei {toei:,.1f} km (operators' line lengths sum to 203.3 and 109.0).",
        f"- Caveat: one OSM route relation per direction and service pattern (39 kept); lines are their union. Directions often run in separate tunnels 20–80 m apart: a 30 m dissolve sums to {narrow_km:,.0f} km (Ōedo 43.6 vs 40.7 official), hence 100 m; half_track_km is closest to the operators' figures. Track shared by Yūrakuchō and Fukutoshin (Wakōshi–Kotake-mukaihara) counts in both. Through-running onto JR and private railways is not part of the subway and not drawn.",
    ])
    crs = "EPSG:6677"   # JGD2011 / Japan Plane Rectangular CS IX (Tokyo)
    lines = gpd.GeoDataFrame(truth, geometry=[wkt.loads(t) for t in truth.geometry_wkt], crs=4326).to_crs(crs)
    xmin, ymin, xmax, ymax = lines.total_bounds
    extent = (xmin - 1_500, xmax + 4_200, ymin - 1_300, ymax + 1_300)
    check_window("tokyo_subway", crs, extent)
    cv = Canvas(extent, "Tokyo subway network",
                f"The 13 lines of Tokyo Metro and Toei, drawn in their official colours · {truth.route_km.sum():,.0f} km of line",
                map_w=12.0, footer=0.95)
    ax = cv.ax
    wards = gpd.read_file(fetch(JPN_ADM2_URL, "s2o_geoboundaries_JPN_ADM2.geojson"))
    wards = wards.cx[139.3:140.3, 35.3:36.1].to_crs(crs)
    special = {"Chiyoda", "Chuo", "Minato", "Shinjuku", "Bunkyo", "Taito", "Sumida", "Koto", "Shinagawa", "Meguro",
               "Ota", "Setagaya", "Shibuya", "Nakano", "Suginami", "Toshima", "Kita", "Arakawa", "Itabashi",
               "Nerima", "Adachi", "Katsushika", "Edogawa"}
    in23 = wards[wards.shapeName.isin(special)]
    if len(in23) != 23:
        raise ValueError(f"expected 23 special wards, found {len(in23)}")
    wards[~wards.shapeName.isin(special)].plot(ax=ax, color="#eeebe4", edgecolor="white", linewidth=0.8, zorder=1)
    in23.plot(ax=ax, color=LAND, edgecolor="white", linewidth=1.0, zorder=1)
    gpd.GeoSeries([in23.union_all().boundary], crs=crs).plot(ax=ax, color="#b9b2a3", linewidth=1.0, zorder=1.2)
    central = {"Chiyoda", "Chuo", "Minato", "Shinjuku", "Bunkyo", "Taito", "Toshima", "Shibuya", "Arakawa", "Kita"}
    for row in in23[~in23.shapeName.isin(central)].itertuples():
        p = row.geometry.representative_point()
        ax.text(p.x, p.y, row.shapeName.upper(), fontsize=7.2, color="#a39d90", ha="center", va="center", zorder=1.5,
                clip_on=True)
    order = truth.sort_values("route_km", ascending=False).index
    for i in order:
        geom = lines.geometry.iloc[i]
        gpd.GeoSeries([geom], crs=crs).plot(ax=ax, color="white", linewidth=4.6, zorder=3 + i * 0.01)
        gpd.GeoSeries([geom], crs=crs).plot(ax=ax, color=truth.colour.iloc[i], linewidth=3.0, zorder=3.005 + i * 0.01)
    st = pd.DataFrame([{"name": n["tags"].get("name", ""), "lon": n["lon"], "lat": n["lat"]} for n in stations])
    sx, sy = Transformer.from_crs(4326, crs, always_xy=True).transform(st.lon.to_numpy(), st.lat.to_numpy())
    network = lines.union_all()
    on_line = np.array([network.distance(Point(x, y)) < 150 for x, y in zip(sx, sy)])
    sx, sy = sx[on_line], sy[on_line]   # drop Saitama Rapid, Toyo Rapid and other non-member stations
    ax.scatter(sx, sy, s=9, color="white", edgecolor="#3c474c", linewidth=0.6, zorder=5)
    for label, (osm_name, dx, dy) in TOKYO_STATIONS.items():
        hit = st[st.name == osm_name]
        if hit.empty:
            raise ValueError(f"station {osm_name} not in OSM result")
        x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(hit.lon.mean(), hit.lat.mean())
        ax.scatter([x], [y], s=46, color="white", edgecolor=INK, linewidth=1.5, zorder=6)
        ax.annotate(label, (x, y), xytext=(dx, dy), textcoords="offset points", ha="left" if dx > 0 else "right",
                    va="center", fontsize=11, fontweight="bold", zorder=8, path_effects=HALO, color=INK)
    for name, lon, lat in (("Tokyo Bay", 139.86, 35.60),):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", zorder=2)
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 6.6)
    lg.text(0, 6.55, "Line (OSM colour tag)", va="top", fontsize=10.5, fontweight="bold")
    y = 6.15
    for operator in ("Tokyo Metro", "Toei"):
        lg.text(0, y, operator, va="top", fontsize=9.5, color=MUTED, fontweight="bold")
        y -= 0.33
        for row in truth[truth.operator == operator].itertuples():
            lg.plot([0, 0.45], [y, y], color=row.colour, linewidth=4, solid_capstyle="butt")
            lg.scatter([0.6], [y], s=150, color="white", edgecolor=row.colour, linewidth=2.2, zorder=3)
            lg.text(0.6, y, row.ref, va="center", ha="center", fontsize=7.5, color=INK, fontweight="bold", zorder=4)
            lg.text(0.82, y, row.name_en, va="center", fontsize=9.5)
            lg.text(col_w - 0.05, y, f"{row.route_km:.1f} km", va="center", ha="right", fontsize=9, color=MUTED)
            y -= 0.27
        y -= 0.12
    lg.scatter([0.22], [y], s=9, color="white", edgecolor="#3c474c", linewidth=0.6)
    lg.text(0.58, y, "Subway station", va="center", fontsize=9.5)
    y -= 0.27
    lg.scatter([0.22], [y], s=46, color="white", edgecolor=INK, linewidth=1.5)
    lg.text(0.58, y, "Major interchange", va="center", fontsize=9.5)
    y -= 0.27
    lg.add_patch(Rectangle((0.02, y - 0.1), 0.4, 0.2, facecolor=LAND, edgecolor="#b9b2a3", linewidth=0.6))
    lg.text(0.58, y, "The 23 special wards", va="center", fontsize=9.5)
    cv.footnote([
        f"Source: © OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {base[:10]}: route=subway relations of the Tokyo Metro and Toei networks, "
        "colours from their colour tags; letters are the operators' line codes. Wards: geoBoundaries JPN ADM2 (OSM-derived, ODbL).",
        "Projection: JGD2011 / Japan Plane Rectangular CS IX (EPSG:6677). Route km merge the two directions (often separate tunnels) with 100 m buffers. "
        "Excluded: the airport through-service relation and all JR/private through-running beyond subway termini.",
    ], width=160)
    cv.save("tokyo_subway")
    return truth


# --------------------------------------------------------------------------
# Trans-Siberian Railway (OpenStreetMap)


TSR_STOPS = [  # (label, lon, lat, major, dx, dy) -- city centres; the dot is snapped to the line
    ("Moscow", 37.62, 55.75, True, -8, -12), ("Yaroslavl", 39.87, 57.63, False, -8, 8),
    ("Kirov", 49.67, 58.60, False, 0, 13), ("Perm", 56.25, 58.01, False, 9, 4),
    ("Yekaterinburg", 60.61, 56.84, True, 6, 14), ("Tyumen", 65.53, 57.15, False, -6, -14),
    ("Omsk", 73.37, 54.99, False, 0, -16), ("Novosibirsk", 82.92, 55.03, True, 0, -18),
    ("Krasnoyarsk", 92.87, 56.01, False, 0, 13), ("Irkutsk", 104.28, 52.29, True, -10, -12),
    ("Ulan-Ude", 107.58, 51.83, False, -4, 14), ("Chita", 113.50, 52.03, False, 10, -12),
    ("Skovorodino", 123.94, 53.99, False, 0, 13), ("Khabarovsk", 135.07, 48.48, True, 10, 4),
    ("Vladivostok", 131.88, 43.12, True, 10, -4),
]


def tsr_ways() -> tuple[pd.DataFrame, dict, str]:
    payload = overpass(*OVERPASS_FILES["trans_siberian"])
    ways: dict[int, dict] = {}
    paths = {}
    for rel in payload["elements"]:
        direction = TSR_RELATIONS[rel["id"]]
        members = [m for m in rel["members"] if m["type"] == "way" and m.get("geometry")]
        paths[direction] = linemerge(unary_union([way_line(m) for m in members]))
        for m in members:
            entry = ways.setdefault(m["ref"], {"osm_id": m["ref"], "directions": set(), "line": way_line(m)})
            entry["directions"].add(direction)
    rows = []
    for entry in ways.values():
        d = entry["directions"]
        rows.append({
            "osm_id": entry["osm_id"], "direction": "both" if len(d) == 2 else next(iter(d)),
            "length_km": geodesic_km(entry["line"]),
            "geometry_wkt": wkt_lines(entry["line"], 0.0002, 4),
        })
    frame = pd.DataFrame(rows).sort_values("osm_id").reset_index(drop=True)
    return frame, paths, osm_base(payload)


def tsr_route_km(frame: pd.DataFrame) -> dict[str, float]:
    """Route length = each direction's continuous path (its own ways plus shared ones).
    The two directions are each one unbroken Moscow-Vladivostok chain in OSM."""
    out = {d: frame[frame.direction.isin([d, "both"])].length_km.sum() for d in ("even", "odd")}
    out["route"] = (out["even"] + out["odd"]) / 2
    return out


def chainage(path: LineString, lon: float, lat: float) -> tuple[float, float, float]:
    coords = np.asarray(path.coords)
    seg = np.array([GEOD.inv(a[0], a[1], b[0], b[1])[2] for a, b in zip(coords[:-1], coords[1:])]) / 1000
    cum = np.concatenate([[0], np.cumsum(seg)])
    i = int(np.argmin((coords[:, 0] - lon) ** 2 * math.cos(math.radians(lat)) ** 2 + (coords[:, 1] - lat) ** 2))
    return cum[i], coords[i, 0], coords[i, 1]


def build_trans_siberian() -> pd.DataFrame:
    truth, paths, base = tsr_ways()
    write_csv("trans_siberian", truth)
    km = tsr_route_km(truth)
    track_km = truth.length_km.sum()
    lines = [wkt.loads(t) for t in truth.geometry_wkt]
    dissolved30 = dissolved_route_km(lines)
    even = paths["even"]
    if even.geom_type != "LineString":
        raise ValueError(f"even-direction relation is not one chain: {even.geom_type}")
    if haversine_km(even.coords[0], (37.66, 55.78)) > haversine_km(even.coords[-1], (37.66, 55.78)):
        even = LineString(list(even.coords)[::-1])
    stops = []
    for label, lon, lat, major, dx, dy in TSR_STOPS:
        ch, slon, slat = chainage(even, lon, lat)
        off = haversine_km((lon, lat), (slon, slat))
        stops.append((label, slon, slat, major, dx, dy, ch, off))
        print(f"  {label:13} km {ch:7,.0f}  (line passes {off:4.1f} km from the centre point)")
    write_md("trans_siberian", [
        "- URL/API: Overpass API (overpass-api.de, mirror fallback): `" + compact(TSR_QUERY) + "` — relation 2128634 'Транссибирская магистраль (чётное направление)' (Moscow → Vladivostok) and 4224465 (нечётное, Vladivostok → Moscow); their child relations (the Nizhny Novgorod variant, 13092161/13096470) are not followed.",
        f"- Vintage: OpenStreetMap as of {base}; both relations are tagged comment='in work'. Delete `.cache/s2o_trans_siberian_overpass.json` to refresh.",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Basemap, lakes and rivers: Natural Earth (public domain).",
        f"- Rows: {len(truth):,} OSM ways — osm_id, direction (even / odd / both = shared by both relations), length_km (WGS84 geodesic, full resolution), geometry_wkt (simplified ~20 m, 1e-4°). Track sum {track_km:,.0f} km; each direction is one unbroken chain: even {km['even']:,.0f} km, odd {km['odd']:,.0f} km, route = mean {km['route']:,.0f} km (official 9,289 km).",
        f"- Caveat: route length with parallel tracks merged = one direction's chain, not a buffer dissolve: a 30 m dissolve gives {dissolved30:,.0f} km because the directions split onto separate alignments for long stretches (bridges, bypasses, yards) and single-track shared ways shrink to 0.93×. Route is via Yaroslavl–Kirov–Perm (the classic line), not via Nizhny Novgorod.",
    ])
    crs = "+proj=eqdc +lat_1=47 +lat_2=57 +lat_0=52 +lon_0=86 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    gl = gpd.GeoSeries(lines, crs=4326).to_crs(crs)
    xmin, ymin, xmax, ymax = gl.total_bounds
    extent = (xmin - 260_000, xmax + 480_000, ymin - 230_000, ymax + 260_000)
    check_window("trans_siberian", crs, extent)
    cv = Canvas(extent, "The Trans-Siberian Railway, Moscow to Vladivostok",
                f"The classic route from Moscow Yaroslavsky station via Yekaterinburg, Novosibirsk and Irkutsk · {km['route']:,.0f} km by rail in OpenStreetMap (official 9,289 km)",
                footer=1.0)
    ax = cv.ax
    region = box(10, 25, 160, 80)
    world = B.load_world().clip(region).to_crs(crs)
    world[world.key != "RUS"].plot(ax=ax, color="#eeebe4", edgecolor="white", linewidth=0.6, zorder=1)
    world[world.key == "RUS"].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.6, zorder=1)
    lakes = gpd.read_file(fetch(NE_LAKES_URL, "s2o_ne_50m_lakes.zip")).clip(region).to_crs(crs)
    lakes.plot(ax=ax, color=WATER, edgecolor="none", zorder=1.2)
    rivers = gpd.read_file(fetch(B.NE_RIVERS_URL, "ne_10m_rivers_lake_centerlines.zip"))
    rivers = rivers[rivers.scalerank <= 3].clip(region).to_crs(crs)
    rivers.plot(ax=ax, color="#a9c8d4", linewidth=0.6, zorder=1.3)
    for lon in range(40, 141, 20):
        lats = np.linspace(30, 75, 91)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.7, zorder=1.1)
    for lat in (40, 50, 60, 70):
        lons = np.linspace(20, 150, 261)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.7, zorder=1.1)
    gl.plot(ax=ax, color="white", linewidth=4.2, zorder=3)
    gl.plot(ax=ax, color="#b3001b", linewidth=2.2, zorder=4)
    for label, lon, lat, major, dx, dy, ch, _off in stops:
        x, y = t.transform(lon, lat)
        ax.scatter([x], [y], s=46 if major else 18, color="white", edgecolor=INK, linewidth=1.4 if major else 0.9, zorder=6)
        text = f"{label}\n{ch:,.0f} km" if label != "Moscow" else "Moscow\n0 km"
        ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points",
                    ha="center" if dx == 0 else ("left" if dx > 0 else "right"), va="center",
                    fontsize=10.5 if major else 8.6, fontweight="bold" if major else "normal",
                    linespacing=1.05, zorder=8, path_effects=HALO, color=INK if major else "#3c474c")
    for name, lon, lat, size in (("R U S S I A", 96, 60.0, 12), ("KAZAKHSTAN", 68, 48.5, 9), ("MONGOLIA", 103, 46.8, 9),
                                 ("CHINA", 120, 44.5, 9), ("Lake Baikal", 106.3, 53.9, 8.5), ("Sea of Japan", 134.5, 41.5, 8.5)):
        x, y = t.transform(lon, lat)
        water = name in {"Lake Baikal", "Sea of Japan"}
        ax.text(x, y, name, fontsize=size, color="#5f8796" if water else "#9a958a", style="italic" if water else "normal",
                ha="center", va="center", zorder=2, clip_on=True)
    lg = legend_axes(cv, cv.left + 0.15, cv.footer + 0.15, 3.6, 1.0)
    lg.add_patch(Rectangle((-0.1, -0.05), 3.6, 1.0, facecolor=PAPER, edgecolor="#c9c4b8", linewidth=0.6, alpha=0.95))
    lg.plot([0, 0.55], [0.7, 0.7], color="#b3001b", linewidth=2.6)
    lg.text(0.7, 0.7, "Trans-Siberian main line (OSM)", va="center", fontsize=9.5)
    lg.scatter([0.27], [0.4], s=46, color="white", edgecolor=INK, linewidth=1.4)
    lg.text(0.7, 0.4, "Major stop, km by rail from Moscow", va="center", fontsize=9.5)
    lg.scatter([0.27], [0.12], s=18, color="white", edgecolor=INK, linewidth=0.9)
    lg.text(0.7, 0.12, "Other stop", va="center", fontsize=9.5)
    cv.footnote([
        f"Source: © OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {base[:10]}: route relations 2128634 and 4224465 (the two running directions of the Trans-Siberian main line). "
        "Land, lakes and rivers: Natural Earth, public domain.",
        f"Projection: equidistant conic, standard parallels 47°N and 57°N, centred on 86°E. Route length = each direction's continuous chain of ways ({km['even']:,.0f} / {km['odd']:,.0f} km), so parallel tracks are not counted twice; "
        f"all {len(truth):,} ways drawn. Distances are measured along the Moscow→Vladivostok chain.",
    ], width=205)
    cv.save("trans_siberian")
    return truth


# --------------------------------------------------------------------------
# Volyn settlements (KATOTTH + OpenStreetMap)


KATOTTH_CATEGORY = {"M": "місто (city)", "T": "селище міського типу (urban-type settlement)",
                    "X": "селище (settlement; former urban-type settlements since 2024)", "C": "село (village)"}
OSM_SETTLEMENT = {"city", "town", "village", "hamlet"}
OSM_UNMATCHED_NOTES = {  # measured with Nominatim on 2026-10-08
    "UA07020050160029877": ": in OSM only as way 30718441 tagged place=locality, no settlement node",
}


def norm_name(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text)).strip().lower()
    text = re.sub(r"[’ʼ'`´‘]", "'", text)
    return re.sub(r"\s+", " ", text)


def read_katotth(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None, dtype=str)
    header = raw.index[raw[0].astype(str).str.contains("Перший рівень", na=False)][0]
    k = raw.iloc[header + 1:, :7].copy()
    k.columns = ["l1", "l2", "l3", "l4", "lx", "cat", "name"]
    k = k[k.l1.notna()]
    for col in ("l1", "l2", "l3", "l4", "lx", "cat", "name"):
        k[col] = k[col].str.strip()
    return k


def volyn_katotth() -> tuple[pd.DataFrame, pd.DataFrame]:
    k = read_katotth(fetch(KATOTTH_URL, "s2o_katotth_2026-07-07.xlsx"))
    v = k[k.l1.str.startswith("UA07")]
    raions = v[v.cat == "P"].set_index("l2").name.to_dict()
    hromadas = v[v.cat == "H"].set_index("l3").name.to_dict()
    sett = v[v.cat.isin(["M", "T", "X", "C"])].copy()
    sett = pd.DataFrame({
        "katotth": sett.l4, "category": sett.cat, "name": sett.name,
        "hromada_katotth": sett.l3, "hromada": sett.l3.map(hromadas),
        "raion_katotth": sett.l2, "raion": sett.l2.map(raions),
    })
    return sett.sort_values("katotth").reset_index(drop=True), v


def volyn_osm():
    payload = overpass(*OVERPASS_FILES["volyn_settlements"])
    nodes = [e for e in payload["elements"] if e["type"] == "node"]
    rels = [e for e in payload["elements"] if e["type"] == "relation"]
    polys = {}
    for r in rels:
        tags = r["tags"]
        lvl, code = tags.get("admin_level"), tags.get("katotth", "")
        if lvl == "4":
            polys[("oblast", tags.get("ISO3166-2"))] = (tags.get("name:en", tags.get("name")), relation_polygon(r))
        elif lvl in ("6", "7") and code.startswith("UA07"):
            polys[("raion" if lvl == "6" else "hromada", code)] = (tags.get("name"), relation_polygon(r))
    places = pd.DataFrame([{
        "osm_node_id": n["id"], "lon": n["lon"], "lat": n["lat"], "osm_place": n["tags"].get("place"),
        "osm_name": n["tags"].get("name:uk") or n["tags"].get("name", ""), "osm_katotth": n["tags"].get("katotth", ""),
        "osm_prefix": n["tags"].get("name:prefix", ""),
    } for n in nodes])
    return places, polys, osm_base(payload)


def match_volyn(sett: pd.DataFrame, places: pd.DataFrame, polys: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    hromada_polys = {code: g for (kind, code), (_n, g) in polys.items() if kind == "hromada"}
    raion_polys = {code: g for (kind, code), (_n, g) in polys.items() if kind == "raion"}
    pts = [Point(x, y) for x, y in zip(places.lon, places.lat)]
    tree = shapely.STRtree(pts)

    def contained(polys_by_code):
        out = [""] * len(pts)
        for code, geom in polys_by_code.items():
            for i in tree.query(geom, predicate="contains"):
                out[i] = code
        return out

    places = places.copy()
    places["hromada_katotth"] = contained(hromada_polys)
    places["raion_katotth"] = contained(raion_polys)
    places["key"] = places.osm_name.map(norm_name)
    settl = places[places.osm_place.isin(OSM_SETTLEMENT)]
    used: set[int] = set()
    rows = []
    for s in sett.itertuples():
        key = norm_name(s.name)
        how, hit = "unmatched", None
        for level, column, code in (("name in hromada", "hromada_katotth", s.hromada_katotth),
                                    ("name in raion", "raion_katotth", s.raion_katotth)):
            cand = settl[(settl[column] == code) & (settl.key == key) & ~settl.osm_node_id.isin(used)]
            if len(cand) > 1:   # namesakes: the node's own katotth tag decides
                tagged = cand[cand.osm_katotth == s.katotth]
                cand = tagged if len(tagged) == 1 else cand.iloc[0:0]
                level += " (namesake resolved by katotth tag)" if len(cand) else ""
            if len(cand) == 1:
                how, hit = level, cand.iloc[0]
                break
        if hit is None:
            cand = settl[(settl.osm_katotth == s.katotth) & ~settl.osm_node_id.isin(used)]
            if len(cand) == 1:
                how, hit = "katotth tag only (name differs)", cand.iloc[0]
        if hit is not None:
            used.add(int(hit.osm_node_id))
        rows.append({
            **s._asdict(), "match": how,
            "osm_node_id": int(hit.osm_node_id) if hit is not None else None,
            "osm_place": hit.osm_place if hit is not None else "", "osm_name": hit.osm_name if hit is not None else "",
            "lon": round(float(hit.lon), 6) if hit is not None else None,
            "lat": round(float(hit.lat), 6) if hit is not None else None,
        })
    out = pd.DataFrame(rows).drop(columns="Index")
    out["osm_node_id"] = out.osm_node_id.astype("Int64")
    leftovers = settl[~settl.osm_node_id.isin(used)]
    return out, leftovers


UK_LATIN = {  # Ukrainian national transliteration (Cabinet of Ministers resolution 55, 2010)
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
    "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "",
}
UK_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def translit(text: str) -> str:
    out = []
    for word in re.split(r"(\W+)", norm_name(text)):
        word = word.replace("зг", "zgh")
        latin = "".join(UK_INITIAL.get(ch, UK_LATIN.get(ch, ch)) if i == 0 else UK_LATIN.get(ch, ch)
                        for i, ch in enumerate(word))
        out.append(latin)
    return "".join(out).title().replace("'", "")


RAION_EN = {"Луцький": "LUTSK RAION", "Ковельський": "KOVEL RAION", "Володимирський": "VOLODYMYR RAION",
            "Камінь-Каширський": "KAMIN-KASHYRSKYI\nRAION"}


def render_volyn(truth, polys, base, how, unmatched) -> None:
    crs = "+proj=tmerc +lat_0=51.1 +lon_0=24.85 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)

    def proj(geom):
        return gpd.GeoSeries([geom], crs=4326).to_crs(crs)

    volyn = polys[("oblast", "UA-07")][1]
    rivne = polys[("oblast", "UA-56")][1]
    lviv = polys[("oblast", "UA-46")][1]
    vx0, vy0, vx1, vy1 = proj(volyn).total_bounds
    (rx, _ry), = project_points(crs, [(26.25, 50.62)])
    extent = (vx0 - 9_000, min(vx1 + 7_000, rx - 1_500), vy0 - 7_000, vy1 + 7_000)
    check_window("volyn_settlements", crs, extent)
    n_loc = int(truth.lon.notna().sum())
    cv = Canvas(extent, "Every town and village of Volyn oblast",
                f"All {len(truth):,} settlements in Ukraine's official codifier (KATOTTH), by legal status · {n_loc:,} located on OpenStreetMap",
                map_w=10.4, footer=1.2)
    ax = cv.ax
    ax.set_facecolor("#f1efe9")
    for geom, colour in ((rivne, "#e9e6de"), (lviv, "#e9e6de")):
        proj(geom).plot(ax=ax, color=colour, edgecolor="none", zorder=1)
    hromadas = [g for (kind, _c), (_n, g) in polys.items() if kind == "hromada"]
    proj(volyn).plot(ax=ax, color="#e3ddcf", edgecolor="none", zorder=2)
    # Horokhiv hromada has 43 sub-km2 exclaves inside Mar'ianivka: real, but noise at this scale
    outlines = [unary_union([p for p in getattr(g, "geoms", [g]) if p.area > 1.5e-4]) for g in hromadas]
    outlines = [shapely.Polygon(g.exterior) if g.geom_type == "Polygon" else g for g in outlines]
    gpd.GeoSeries([g.boundary for g in outlines], crs=4326).to_crs(crs).plot(ax=ax, color="white", linewidth=0.7, zorder=2.1)
    raions = {code: (name, g) for (kind, code), (name, g) in polys.items() if kind == "raion"}
    for _code, (_name, g) in raions.items():
        proj(g.boundary.difference(volyn.boundary.buffer(0.001))).plot(ax=ax, color="#7d7668", linewidth=1.5, zorder=3)
    # the state border: Volyn's and its neighbours' edges that touch no Ukrainian oblast in the set
    others = unary_union([rivne, lviv]).buffer(0.002)
    proj(volyn.boundary.intersection(others)).plot(ax=ax, color="#5d564a", linewidth=2.0, zorder=3.1)
    border = [volyn.boundary.difference(others),
              lviv.boundary.difference(unary_union([volyn, rivne]).buffer(0.002)).intersection(box(22, 49, 24.3, 52)),
              rivne.boundary.difference(unary_union([volyn, lviv]).buffer(0.002)).intersection(box(25, 51.45, 28, 52.5))]
    for g in border:
        proj(g).plot(ax=ax, color="#5d564a", linewidth=2.0, linestyle=(0, (5, 2.5)), zorder=3.2)
    for name, lon, lat, rot in (("POLAND", 23.72, 51.0, 0), ("BELARUS", 25.3, 51.98, 0), ("RIVNE OBLAST", 25.85, 50.42, 0),
                                ("LVIV OBLAST", 24.2, 50.33, 0)):
        x, y = t.transform(lon, lat)
        ax.text(x, y, name, fontsize=9, color="#8e897d", ha="center", va="center", rotation=rot, zorder=2.5, clip_on=True)

    loc = truth.dropna(subset=["lon", "lat"]).copy()
    loc["x"], loc["y"] = t.transform(loc.lon.to_numpy(), loc.lat.to_numpy())
    vil, sel, city = (loc[loc.category == c] for c in ("C", "X", "M"))
    ax.scatter(vil.x, vil.y, s=6, color="#4a5559", edgecolor="none", alpha=0.85, zorder=5)
    ax.scatter(sel.x, sel.y, s=30, color="white", edgecolor=INK, linewidth=1.1, zorder=6)
    ax.scatter(city.x, city.y, s=[110 if n == "Луцьк" else 58 for n in city.name], marker="s", color=INK,
               edgecolor="white", linewidth=1.0, zorder=7)

    # greedy label placement: try eight positions, keep the first that overlaps nothing placed
    cv.fig.canvas.draw()
    renderer = cv.fig.canvas.get_renderer()
    taken = []
    for row in pd.concat([city.assign(rank=0), sel.assign(rank=1)]).sort_values(["rank", "name"]).itertuples():
        big = row.category == "M"
        label = translit(row.name)
        candidates = [(8, 0, "left", "center"), (-8, 0, "right", "center"), (0, 9, "center", "bottom"),
                      (0, -9, "center", "top"), (7, 7, "left", "bottom"), (-7, 7, "right", "bottom"),
                      (7, -7, "left", "top"), (-7, -7, "right", "top")]
        for dx, dy, ha, va in candidates:
            txt = ax.annotate(label, (row.x, row.y), xytext=(dx * (1.3 if big else 1), dy * (1.3 if big else 1)),
                              textcoords="offset points", ha=ha, va=va,
                              fontsize=(12 if row.name == "Луцьк" else 10) if big else 8.2,
                              fontweight="bold" if big else "normal", style="normal" if big else "italic",
                              color=INK if big else "#2b3539", zorder=9, path_effects=HALO)
            bb = txt.get_window_extent(renderer).expanded(1.04, 1.1)
            if not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                break
            txt.remove()
        else:
            print(f"  WARNING: no free label position for {label}")
    # symbols of labelled places also block raion names
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
            txt = ax.text(x, y, RAION_EN[short], fontsize=11, color="#7a7365", ha="center", va="center", zorder=2.6,
                          fontweight="bold", alpha=0.8, linespacing=1.1)
            bb = txt.get_window_extent(renderer).expanded(1.05, 1.15)
            if not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                break
            txt.remove()
    cats = truth.category.value_counts()
    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 7.6)
    lg.text(0, 7.55, "Settlement status (KATOTTH)", va="top", fontsize=10.5, fontweight="bold")
    items = [("s", 110, INK, "white", f"Місто — city ({cats.get('M', 0)}),\nLutsk the oblast centre", True),
             ("o", 30, "white", INK, f"Селище — settlement ({cats.get('X', 0)}),\nformer urban-type settlement", True),
             ("o", 6, "#4a5559", "none", f"Село — village ({cats.get('C', 0)})", False)]
    y = 7.0
    for marker, size, face, edge, label, _lab in items:
        lg.scatter([0.2], [y], s=size, marker=marker, color=face, edgecolor=edge, linewidth=1.0)
        lg.text(0.5, y, label, va="center", fontsize=9.5, linespacing=1.25)
        y -= 0.55
    y -= 0.1
    lg.text(0, y, "Boundaries", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    for colour, lw, style, label in (("#5d564a", 2.0, (0, (5, 2.5)), "State border"),
                                     ("#5d564a", 2.0, "-", "Oblast border"),
                                     ("#7d7668", 1.5, "-", "Raion border (4 raions)"),
                                     ("white", 2.2, "-", f"Hromada border ({sum(1 for k in polys if k[0] == 'hromada')} hromadas)")):
        lg.plot([0, 0.4], [y, y], color=colour, linewidth=lw, linestyle=style, solid_capstyle="butt")
        if colour == "white":
            lg.add_patch(Rectangle((0, y - 0.09), 0.4, 0.18, facecolor="#e3ddcf", edgecolor="none", zorder=0))
        lg.text(0.5, y, label, va="center", fontsize=9.5)
        y -= 0.3
    y -= 0.25
    lg.text(0, y, "Located on OpenStreetMap", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    lines = [("KATOTTH settlements", f"{len(truth):,}"),
             ("matched by name within hromada", f"{int(how.get('name in hromada', 0)):,}")]
    for k, v in how.items():
        if k not in ("name in hromada", "unmatched"):
            lines.append((k, f"{v:,}"))
    lines.append(("not found as a settlement node", f"{int(how.get('unmatched', 0)):,}"))
    for k, v in lines:
        lg.text(0, y, k, va="center", fontsize=9.3)
        lg.text(col_w - 0.05, y, v, va="center", ha="right", fontsize=9.3)
        y -= 0.27
    if len(unmatched):
        note = ("Not drawn: " + "; ".join(f"{translit(r.name)} ({r.name}), {translit(r.hromada)} hromada" for r in unmatched.itertuples())
                + " — mapped in OSM only as a place=locality area, not as a settlement node.")
        lg.text(0, y - 0.05, textwrap.fill(note, 44), va="top", fontsize=8.6, color=MUTED, linespacing=1.4)
    cv.footnote([
        f"Source: Кодифікатор адміністративно-територіальних одиниць та територій територіальних громад (KATOTTH), Ministry for Development of Communities and Territories of Ukraine, {KATOTTH_ORDER}. "
        f"Positions, boundaries: © OpenStreetMap contributors (ODbL 1.0), place nodes and admin relations as of {base[:10]}.",
        "Projection: transverse Mercator centred on 24.85°E. Names transliterated with Ukraine's 2010 national system. Every city and settlement labelled; villages unlabelled. "
        "Since 2024 KATOTTH has no urban-type settlements (T): all were renamed селище (X). Hromada exclaves under 1 km² not outlined.",
    ], width=165)
    cv.save("volyn_settlements")


def build_volyn_settlements() -> pd.DataFrame:
    sett, volyn_rows = volyn_katotth()
    places, polys, base = volyn_osm()
    truth, leftovers = match_volyn(sett, places, polys)
    write_csv("volyn_settlements", truth)
    cats = truth.category.value_counts()
    how = truth.match.value_counts()
    matched = int(truth.lon.notna().sum())
    unmatched = truth[truth.lon.isna()]
    n_h = int((volyn_rows.cat == "H").sum())
    osm_settl = places[places.osm_place.isin(OSM_SETTLEMENT)]
    old_prefix = osm_settl.osm_prefix.value_counts()
    print(f"  KATOTTH: {len(truth)} settlements {dict(cats)}; OSM {len(osm_settl)} settlement nodes")
    print(f"  match: {dict(how)}")
    print("  unmatched:", "; ".join(f"{r.name} ({r.category}, {r.hromada})" for r in unmatched.itertuples()))
    print("  OSM leftovers:", "; ".join(f"{r.osm_name} [{r.osm_place}]" for r in leftovers.itertuples()))
    cat_osm = pd.crosstab(truth.category, truth.osm_place.replace("", "—"))
    print(cat_osm)
    mismatch_rows = truth[(truth.lon.notna()) & (truth.osm_katotth if "osm_katotth" in truth else False)] if False else None
    del mismatch_rows
    write_md("volyn_settlements", [
        f"- URL/API: {KATOTTH_URL} (KATOTTH xlsx, linked from {KATOTTH_PAGE}; rows under UA07, categories M/T/X/C). Coordinates: Overpass API, `" + compact(VOLYN_QUERY) + "`.",
        f"- Vintage: KATOTTH as amended by the Ministry for Development of Communities and Territories {KATOTTH_ORDER} (the newest of 26 editions listed on 2026-10-08); OpenStreetMap as of {base}.",
        "- Licence: KATOTTH is an official act of a Ukrainian ministry (not subject to copyright, Law of Ukraine on Copyright Art. 10); coordinates and boundaries © OpenStreetMap contributors, ODbL 1.0.",
        f"- Rows: {len(truth)} KATOTTH settlements of Volyn oblast — {cats.get('M', 0)} cities (M), {cats.get('T', 0)} urban-type settlements (T), {cats.get('X', 0)} селища (X), {cats.get('C', 0)} villages (C); 4 raions, {n_h} hromadas. Columns: katotth, category, name, hromada/raion codes and names, match, osm_node_id, osm_place, osm_name, lon, lat. "
        f"Located via an OSM place=city/town/village/hamlet node: {matched} ({', '.join(f'{v} {k}' for k, v in how.items() if k != 'unmatched')}); unmatched {int(how.get('unmatched', 0))}" + (f" ({'; '.join(f'{r.name}, {r.hromada} hromada' + OSM_UNMATCHED_NOTES.get(r.katotth, '') for r in unmatched.itertuples())})" if len(unmatched) else "")
        + f". OSM category vs KATOTTH: {int(((truth.category == 'C') & (truth.osm_place == 'hamlet')).sum())} villages are tagged place=hamlet; all 12 cities and 21 селища are city/town nodes.",
        f"- Caveat: KATOTTH has no 'T' rows any more: the 2024 reform renamed every urban-type settlement to селище (X); OSM still tags {old_prefix.get('селище міського типу', 0)} nodes name:prefix='селище міського типу' and {old_prefix.get('місто', 0)} 'місто' (KATOTTH: {cats.get('M', 0)} cities). "
        f"{len(leftovers)} OSM settlement nodes in Volyn match no KATOTTH row" + (f" ({'; '.join(f'{r.osm_name} [{r.osm_place}]' for r in leftovers.head(12).itertuples())}{' …' if len(leftovers) > 12 else ''})" if len(leftovers) else "") + ".",
    ])
    render_volyn(truth, polys, base, how, unmatched)
    return truth


BUILDERS: dict[str, Callable[[], pd.DataFrame]] = {
    "atlantic_hurricanes_2024": build_atlantic_hurricanes_2024,
    "tokyo_subway": build_tokyo_subway,
    "trans_siberian": build_trans_siberian,
    "volyn_settlements": build_volyn_settlements,
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
        if m and "direction" in frame:
            expected, tol = map(float, m.groups())
            km = tsr_route_km(frame)
            ok = abs(km["route"] - expected) <= expected * tol / 100
            return ok, (f"{km['route']:,.0f} route-km ({(km['route'] / expected - 1) * 100:+.1f}%; directions {km['even']:,.0f} / "
                        f"{km['odd']:,.0f}); {frame.length_km.sum():,.0f} km summing all ways")
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


REPORT_NOTES = """
## Proposed spec corrections

| Map | Spot | Problem | Proposed |
|---|---|---|---|
| `atlantic_hurricanes_2024` | `line_near=-77.3,18.1` (Beryl, the original spec) | **FAILED**: -77.3,18.1 is inland Jamaica, and Beryl's centre never crossed the island. Its nearest best-track fix is 2024-07-03 21:00 UTC at -77.5,17.5 (Cat 4, 118 kt), 69.8 km away. | Already replaced in specs.toml (commit 9d6111f) by `line_near=-95.97,28.63`, Beryl's Texas landfall, which passes at 2.9 km. Alternatives at 0.0 km: `line_near=-77.5,17.5` (closest pass to Jamaica) or `line_near=-61.5,12.5` (Carriacou landfall). |
| `atlantic_hurricanes_2024` | `line_near=-82.6,27.2` (Milton), `line_near=-83.9,29.9` (Helene) | PASS but at 14.9 and 17.7 km of a 20 km tolerance: neither is the NHC landfall point. | `line_near=-82.6,27.4` and `line_near=-83.7,30.0` (NHC landfall records, `usa_record = L`); both 0.0 km |

Every other spot passes with a wide margin. The `(verify)` count for Volyn
(1,080±12%) is right: KATOTTH lists 1,087.

## What was measured, and what the spec should know

- **`trans_siberian` units = [1, 5000] vs 8,181 OSM ways.** The two
  direction relations (2128634 even, 4224465 odd) have 8,181 distinct ways
  between them. A map that draws them way by way has more features than the
  range allows. A map that draws the merged route has 1 or 2. The range should
  say which one it counts.
- **`trans_siberian` total_km: a buffer dissolve is the wrong tool here.**
  Each direction relation is one unbroken Moscow→Vladivostok chain (9,295 and
  9,298 km). Their mean, 9,297 km, is 0.1% off the official 9,289. The France
  method (30 m buffers) gives 9,565 km, and 9,428 km at 100 m. It overcounts
  because the two directions run on separate alignments for long stretches,
  and undercounts shared single-track ways at 0.93×. Distances along the chain
  match the official kilometre posts: Yekaterinburg 1,817 (official 1,816),
  Novosibirsk 3,336 (3,343), Irkutsk 5,186 (5,185), Khabarovsk 8,528 (8,521).
  Both relations carry `comment=in work`.
- **`tokyo_subway`: separate tunnels defeat a 30 m dissolve.** Each line's
  two directions often run in separate shield tunnels 20–80 m apart. With
  30 m buffers the 13 lines sum to 327 km; with 100 m buffers, 308 km. The
  operators' line lengths sum to 312 km (Metro 203.3, which counts the shared
  Wakōshi–Kotake-mukaihara section twice, plus Toei 109.0). Half the sum of
  member ways matches best: Ōedo 40.70 against 40.7 official, Mita 26.50
  against 26.5. 39 route relations: one per direction and service pattern.
  The relation with ref `Al` (the Keikyu–Toei–Keisei airport through-service)
  is dropped.
- **`volyn_settlements`: KATOTTH no longer has urban-type settlements.**
  Codifier edition: order No. 1321 of 7 July 2026, the newest of 26 on
  mindev.gov.ua (the Ministry's site; minregion.gov.ua answers 403). It lists
  12 M, 0 T, 21 X and 1,054 C for UA07: 1,087 settlements, 4 raions and 54
  hromadas. Since the 2024 reform every former смт has been an X (селище).
  OSM still tags 22 nodes `name:prefix=селище міського типу`, and 11 `місто`
  against KATOTTH's 12 cities. OSM nodes carry a `katotth` tag, but matching
  used names, not that tag. 1,086 of 1,087 KATOTTH names match exactly one
  place=city/town/village/hamlet node inside the OSM hromada polygon with the
  same KATOTTH code. No namesakes needed resolving, and no match needed the
  raion fallback. The one miss is Октавин (Zymne hromada): OSM has it only as
  way 30718441 tagged place=locality (checked with Nominatim). Two OSM hamlets
  match no KATOTTH row: Хлібове, and Ягодин (tagged `is_in:village=Римачі`).
  38 KATOTTH villages are tagged place=hamlet in OSM. The excludes hold:
  Volyn's lon/lat bbox ends at 26.107°E, 0.14° short of Rivne (26.25°E).
- **`europe_largest_cities`: the definition is GeoNames' `population`
  field.** Places of class PPLX (city sections, e.g. Pest at 1.00 M) are
  dropped. Europe means 45 states, plus Russia west of 58°E (Ufa and Perm in,
  Yekaterinburg out), plus Istanbul. Turkey otherwise, Transcaucasia and
  Kazakhstan are out (Baku, 2.35 M, would otherwise rank 8th). GeoNames is
  not consistent: Stockholm (1.52 M) and Copenhagen (1.15 M) are urban-area
  figures, and Moscow is the 2010 census figure, 10.38 M. Ranks 26–30 are
  separated by 1.16–1.12 M, so a different source reorders them. Without
  Istanbul, Oslo (1.08 M) becomes 30th.
- **`volcanoes_holocene`: 1,214 volcanoes, served live.** volcano.si.edu
  answers scripted requests with HTTP 403. The GVP GeoServer WFS serves the
  live list: 1,214 rows, numberMatched 1,214. 366 of them have no dated
  eruption: "Holocene" is GVP's judgement of activity, not a dated eruption.
  The newest published citation found is v. 5.3.4 (30 Dec 2025), but
  last-eruption years run to 2026, so the WFS is newer.
- **`atlantic_hurricanes_2024`: 18 named storms, 919 fixes, IBTrACS
  v04r01.** One UNNAMED 2024 system, Potential Tropical Cyclone Eight, is
  excluded. Beryl's two NHC landfalls are Carriacou (Cat 4, 1 Jul) and
  Matagorda, Texas (Cat 1, 8 Jul).

## Reference images

Every reference.png is 1,600 px wide and built with geopandas/matplotlib
outside mapsvc:

- Hurricanes: Lambert conformal conic (20°/45°N). Tracks coloured by the
  Saffir–Simpson category of each segment, pre-genesis and post-tropical
  stages dashed grey. Every landfall ringed; Beryl, Helene and Milton
  annotated.
- Tokyo: Japan Plane Rectangular CS IX. The 13 lines in their OSM colour
  tags, with stations and a legend of line codes. The 23 wards are muted
  context.
- Volcanoes: Equal Earth centred on 150°E, six type classes, plate
  boundaries.
- Trans-Siberian: equidistant conic (47°/57°N). 15 stops labelled with
  kilometres from Moscow.
- European cities: LAEA centred on 52°N 22°E, circle area ∝ population, all
  30 labelled. The 58°E limit is drawn.
- Volyn: transverse Mercator. Cities, селища and villages by symbol; the 4
  raions labelled; 54 hromadas outlined; state and oblast borders
  distinguished.
"""


def render_report(results) -> str:
    lines = [
        "# Set-2 overlay references: spot-check report", "",
        "Generated by `uv run evals/maps/etalons/set2_overlays.py`. Spot checks are evaluated from the written "
        "`truth.csv` files only, with run.py's tolerances (event_near 100 km, line_near 20 km).", "",
        "| Map | Rows | Count | Spot checks |", "|---|---:|---:|---|",
    ]
    total = passed = 0
    for map_id in IDS:
        if map_id not in results:
            lines.append(f"| `{map_id}` | — | — | not built |")
            continue
        count, rows, checks = results[map_id]
        cells = []
        for spot, ok, measured in checks:
            total += 1
            passed += ok
            cells.append(f"{'PASS' if ok else '**FAIL**'} `{spot}` ({measured})")
        lines.append(f"| `{map_id}` | {rows:,} | {count:,} | {'<br>'.join(cells)} |")
    lines += ["", f"**{passed} of {total} spot checks pass.**", REPORT_NOTES.rstrip(), ""]
    return "\n".join(lines)


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
    REPORT_PATH.write_text(render_report(evaluate(IDS)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
