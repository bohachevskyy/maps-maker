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
"""Reference ("etalon") maps for HELD-OUT SET 2, evals/heldout2/specs.toml.

    uv run evals/heldout2/etalons/build_heldout2.py [ids...] [--check-only] [--fetch]

Writes evals/heldout2/<id>/{truth.csv,truth.md,reference.png} and
evals/heldout2/REPORT.md (spot-check PASS/FAIL).  Downloads are cached in
evals/heldout2/etalons/.cache (gitignored); delete a file to refresh it.

Scaffolding (Canvas, palette, legends, fetch) is imported from
evals/maps/etalons/build.py through evals/heldout/etalons/hbase.py and
redirected to this directory.  Pure parsing helpers for KATOTTH / OSM
relations come from evals/maps/etalons/set2_overlays.py.  This file never
imports mapsvc: the references are an independent oracle.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELDOUT2 = HERE.parent
EVALS = HELDOUT2.parent
sys.path.insert(0, str(EVALS / "heldout" / "etalons"))
sys.path.insert(0, str(EVALS / "maps" / "etalons"))
import hbase as H  # noqa: E402
from hbase import B  # noqa: E402

# redirect the shared scaffolding to this directory
CACHE_DIR = HERE / ".cache"
for mod in (H, B):
    mod.CACHE_DIR = CACHE_DIR
H.HELDOUT = HELDOUT2
B.MAPS_DIR = HELDOUT2
B.SPECS_PATH = HELDOUT2 / "specs.toml"
H.B.HTTP.headers.update({"User-Agent": "maps-maker-heldout2-etalon/1.0 (dmytro.bohachevskyy@gmail.com)"})

from hbase import (  # noqa: E402
    HALO, HATCH, INK, LAND, MUTED, NODATA, PAPER, WATER, WIDTH_IN,
    Canvas, check_window, class_legend, classify, fetch, haversine_km, line_segments_km,
    plot_classes, plot_missing, project_points, ramp, write_csv, write_md,
)

_check_window = check_window
WINDOW_NOTES: dict[str, list[str]] = {}


def check_window(map_id, crs, extent, *, contains=None, excludes=None):   # noqa: F811 -- records the notes for REPORT.md
    notes = _check_window(map_id, crs, extent, contains=contains, excludes=excludes)
    WINDOW_NOTES[map_id] = notes
    return notes


import geopandas as gpd  # noqa: E402
import matplotlib as mpl  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402,F401
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402
from shapely.ops import linemerge, unary_union  # noqa: E402

IDS = (
    "south_asia_literacy", "german_states_population", "turkey_quakes_2023", "madrid_metro",
    "ivano_frankivsk_settlements", "spain_solar_plants", "atlantic_hurricanes_2017",
    "latin_america_fertility", "spain_unemployment_regions", "iceland_volcanoes",
)
NEAR_KM = {"event_near": 100.0, "line_near": 20.0}   # as evals/maps/run.py
NOW = pd.Timestamp.now().strftime("%Y-%m-%d")

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

MADRID_Q = """
[out:json][timeout:300];
relation["route"="subway"]["network"="Metro de Madrid"];
out body geom;
"""
MADRID_CTX_Q = """
[out:json][timeout:300];
rel["boundary"="administrative"]["admin_level"="8"](40.20,-3.98,40.58,-3.28);
out body geom;
way["waterway"="river"]["name"="Río Manzanares"](40.20,-3.98,40.58,-3.28);
out geom;
"""
IF_Q = """
[out:json][timeout:900];
area["ISO3166-2"="UA-26"]["admin_level"="4"]->.v;
node["place"](area.v);
out body;
rel(area.v)["boundary"="administrative"]["admin_level"~"^(6|7)$"];
out body geom;
rel["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"~"^UA-(26|21|46|61|77)$"];
out body geom;
"""
SOLAR_Q = """
[out:json][timeout:900];
area["ISO3166-1"="ES"]["admin_level"="2"]->.es;
nwr["power"="plant"]["plant:source"="solar"](area.es);
out center tags;
"""
OVERPASS_FILES = {"madrid": (MADRID_Q, "madrid_metro_overpass.json"),
                  "madrid_ctx": (MADRID_CTX_Q, "madrid_municipalities_overpass.json"),
                  "if": (IF_Q, "ivano_frankivsk_overpass.json"),
                  "solar": (SOLAR_Q, "spain_solar_overpass.json")}


def overpass(query: str, filename: str, *, timeout: int = 900) -> dict:
    """POST an Overpass query once, cache the JSON, fall back across mirrors."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    if target.exists() and target.stat().st_size:
        return json.loads(target.read_text(encoding="utf-8"))
    errors = []
    for attempt in range(2):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                print(f"  overpass {endpoint} -> {filename}", flush=True)
                response = B.HTTP.post(endpoint, data={"data": query}, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                if payload.get("remark"):
                    raise RuntimeError("Overpass remark: " + payload["remark"][:200])
                if not payload.get("elements"):
                    raise RuntimeError("no elements")
                payload["query"] = query
                part = target.with_suffix(".part")
                part.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                part.replace(target)
                return payload
            except Exception as exc:  # independent public mirrors
                errors.append(f"{endpoint}: {str(exc)[:150]}")
                print("   failed:", errors[-1], flush=True)
    raise RuntimeError("All Overpass endpoints failed: " + "; ".join(errors))


def osm_base(payload: dict) -> str:
    return payload.get("osm3s", {}).get("timestamp_osm_base", "unknown")


def compact(query: str) -> str:
    return " ".join(line.strip() for line in query.strip().splitlines())


if __name__ == "__main__" and "--fetch" in sys.argv:
    for name, (q, f) in OVERPASS_FILES.items():
        try:
            p = overpass(q, f)
            print(name, len(p["elements"]), osm_base(p), flush=True)
        except Exception as e:
            print(name, "FAILED", e, flush=True)
    sys.exit(0)


# ==========================================================================
# Shared drawing helpers (generic ones come from the held-out set 1 builder)

import build_heldout as BH  # noqa: E402  (evals/heldout/etalons; same patched hbase)
from build_heldout import callout, owid_latest, ranked_table, side_legend  # noqa: E402

SEA = "#5f8796"
GREY_TXT = "#9a958a"


def sea_label(ax, crs, name, lon, lat, size=9.5, rotation=0):
    (x, y), = project_points(crs, [(lon, lat)])
    ax.text(x, y, name, fontsize=size, color=SEA, style="italic", ha="center", va="center", zorder=2, rotation=rotation)


def land_label(ax, crs, extent, name, lon, lat, size=8.5):
    (x, y), = project_points(crs, [(lon, lat)])
    if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
        ax.text(x, y, name, fontsize=size, color=GREY_TXT, ha="center", va="center", zorder=2, linespacing=1.1)


def text_colour(cls: int, n_classes: int, dark_from: int) -> str:
    return "white" if cls >= dark_from else INK


def fmt_si(n: float) -> str:
    return f"{n:,.0f}"


# ==========================================================================
# 1. South Asia: adult literacy (OWID / UNESCO)

OWID_LIT_URL = "https://ourworldindata.org/grapher/literacy-rate-adults.csv"
SOUTH_ASIA = ["AFG", "BGD", "BTN", "IND", "IRN", "MDV", "NPL", "PAK", "LKA"]   # UN M49 "Southern Asia"


def build_south_asia_literacy() -> pd.DataFrame:
    map_id = "south_asia_literacy"
    raw = pd.read_csv(fetch(OWID_LIT_URL, "owid_literacy.csv"))
    truth = owid_latest(raw, SOUTH_ASIA)
    truth.insert(3, "unit", "percent of people aged 15+ who can read and write")
    if sorted(truth.key) != sorted(SOUTH_ASIA):
        raise ValueError("literacy: a South Asian country has no value")
    write_csv(map_id, truth)
    yrs = truth.set_index("key").year.to_dict()
    write_md(map_id, [
        f"- URL/API: {OWID_LIT_URL} (Our World in Data Grapher CSV, 'Literacy rate among adults'; UNESCO Institute for Statistics / World Bank, completed from national sources); latest non-missing year per country; geometry {H.NE10_COUNTRIES}.",
        f"- Vintage: OWID file downloaded {NOW}; the latest literacy year differs by country: " + ", ".join(f"{k} {yrs[k]}" for k in sorted(yrs, key=lambda k: -yrs[k])) + ". Literacy is surveyed or census-based, so years are irregular, not annual.",
        "- Licence: OWID data CC BY 4.0 (UNESCO UIS; World Bank). Boundaries: Natural Earth 1:10m (public domain), Crimea drawn as Ukraine (context only).",
        f"- Rows: {len(truth)} — all 9 members of the UN M49 sub-region 'Southern Asia' (AFG BGD BTN IND IRN MDV NPL PAK LKA), chosen by an explicit ISO3 list. Columns: key (ISO3), name, value (% of adults 15+ literate), unit, year.",
        "- Caveat: value years are not comparable like for like (Afghanistan 2021 vs India 2024); India's J&K, Pakistan's and China's claims are drawn as Natural Earth's de facto lines. Iran is in 'Southern Asia' in the UN geoscheme, though often thought of as Middle East.",
    ])
    crs = "+proj=lcc +lat_1=12 +lat_2=34 +lat_0=22 +lon_0=70 +datum=WGS84 +units=m +no_defs"
    world = H.world10()
    sa = world[world.key.isin(SOUTH_ASIA)][["key", "NAME", "geometry"]].dissolve("key", as_index=False)
    sa = sa.merge(truth[["key", "value"]], on="key", how="left")
    window = box(40, -2, 100, 42)
    saq = sa.clip(window).to_crs(crs)
    extent_ll = (44.0, 93.8, 0.4, 39.8)
    ring = [(lo, la) for lo in np.linspace(extent_ll[0], extent_ll[1], 40) for la in (extent_ll[2], extent_ll[3])] + \
           [(lo, la) for la in np.linspace(extent_ll[2], extent_ll[3], 40) for lo in (extent_ll[0], extent_ll[1])]
    xy = project_points(crs, ring)
    extent = (min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy))
    check_window(map_id, crs, extent)
    edges = [50, 60, 70, 80, 90]
    colors = ramp("YlGnBu", 6, 0.12, 0.95)
    lo, hi = truth.sort_values("value").iloc[0], truth.sort_values("value").iloc[-1]
    cv = Canvas(extent, "Adult literacy rate across South Asia",
                f"Share of people aged 15 and over who can read and write, % · latest year per country (2019–2024) · UNESCO via Our World in Data · "
                f"from {lo['name']} {lo.value:.0f}% to {hi['name']} {hi.value:.0f}%",
                map_w=10.6, footer=1.0)
    ax = cv.ax
    ctx = world[~world.key.isin(SOUTH_ASIA)].clip(box(30, -12, 112, 52)).to_crs(crs)
    ctx.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    H.local_graticule(ax, crs, range(50, 100, 10), range(0, 41, 10), lon_span=(40, 100), lat_span=(-2, 42))
    plot_classes(ax, saq, "value", edges, colors, linewidth=0.7)
    byk = truth.set_index("key")
    pos = {"AFG": (66.2, 33.8), "PAK": (69.6, 29.6), "IND": (79.0, 22.5), "IRN": (54.6, 32.2), "NPL": (84.0, 28.35),
           "BGD": (90.1, 24.0), "LKA": (80.7, 7.6)}
    full = {"AFG": "Afghanistan", "PAK": "Pakistan", "IND": "India", "IRN": "Iran", "NPL": "Nepal", "BGD": "Bangladesh", "LKA": "Sri Lanka"}
    for k, (lon, lat) in pos.items():
        (x, y), = project_points(crs, [(lon, lat)])
        v = byk.loc[k, "value"]
        cls = int(classify(pd.Series([v]), edges).iloc[0])
        col = "white" if cls >= 4 else INK
        if k in {"NPL"}:
            callout(ax, (x, y), f"Nepal {v:.0f}%", (24, 34), size=9)
        elif k == "BGD":
            callout(ax, (x, y), f"Bangladesh {v:.0f}%", (32, -34), size=9)
        elif k == "LKA":
            callout(ax, (x, y), f"Sri Lanka {v:.0f}%", (62, -8), size=9)
        else:
            big = k in {"IND"}
            ax.text(x, y, f"{full[k]}\n{v:.0f}%", fontsize=11 if big else 9.6, fontweight="bold" if big else "normal",
                    ha="center", va="center", color=col, zorder=8, linespacing=1.2)
    # Bhutan and the Maldives: too small for a label
    (bx, by), = project_points(crs, [(90.4, 27.45)])
    callout(ax, (bx, by), f"Bhutan {byk.loc['BTN', 'value']:.0f}%", (30, 58), size=9)
    (mx, my), = project_points(crs, [(73.5, 4.2)])
    ax.scatter([mx], [my], s=210, facecolor="none", edgecolor=INK, linewidth=1.1, zorder=7)
    mcol = colors[int(classify(pd.Series([byk.loc["MDV", "value"]]), edges).iloc[0])]
    ax.scatter([mx], [my], s=90, color=mcol, edgecolor=INK, linewidth=0.8, zorder=8)
    callout(ax, (mx + 25_000, my), f"Maldives {byk.loc['MDV', 'value']:.0f}%", (66, 8), size=9)
    for name, lon, lat in (("CHINA", 88.0, 36.0), ("TURKMENISTAN", 58.5, 38.8), ("UZBEKISTAN", 66.5, 39.9), ("TAJIKISTAN", 71.7, 38.8),
                           ("MYANMAR", 95.5, 22.0), ("IRAQ", 44.9, 33.0), ("OMAN", 56.5, 21.0), ("SAUDI\nARABIA", 46.0, 24.0), ("UAE", 54.6, 23.8)):
        land_label(ax, crs, extent, name, lon, lat, 8)
    for name, lon, lat, sz in (("Arabian Sea", 64.0, 14.0, 11), ("Bay of Bengal", 87.5, 13.5, 11), ("Indian Ocean", 77.0, 1.5, 11),
                               ("Persian\nGulf", 51.5, 27.4, 8), ("Gulf of\nOman", 58.4, 24.9, 7.5)):
        sea_label(ax, crs, name, lon, lat, sz)
    col_x = cv.left + cv.map_w + 0.45
    _lg, y = side_legend(cv, col_x, cv.H - cv.header - 0.1, 3.0, "Adult literacy rate\n(% of people aged 15+)", colors,
                         ["< 50", "50–60", "60–70", "70–80", "80–90", "≥ 90"], extra=[(LAND, "Outside South Asia\n(not mapped)")])
    ranked_table(cv, col_x, cv.H - cv.header - 3.6, truth.assign(name=truth.name + "  (" + truth.year.astype(str) + ")"),
                 "Ranked, % literate (year)", lambda v: f"{v:.1f}", width=WIDTH_IN - col_x - 0.3, row_h=0.27, size=9.6)
    cv.footnote([
        "Source: Our World in Data, 'Literacy rate among adults' (UNESCO Institute for Statistics, World Bank; CC BY 4.0), latest year available per country (years 2019–2024 shown beside each value in the ranking). "
        "Boundaries: Natural Earth 1:10m (public domain).",
        "Projection: Lambert conformal conic (parallels 12°N and 34°N, centred on 70°E). Classification: 6 manual classes (10-point steps, <50 to ≥90). All 9 members of the UN M49 sub-region Southern Asia valued, none excluded; "
        "Maldives and Bhutan are marked by a ring/leader because they are too small to colour legibly.",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# 2. German Länder: population (Eurostat demo_r_d2jan, NUTS 1)

EUROSTAT_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
DE_NUTS1 = {  # NUTS 1 -> (ISO 3166-2:DE, name, English name)
    "DE1": ("DE-BW", "Baden-Württemberg"), "DE2": ("DE-BY", "Bayern"), "DE3": ("DE-BE", "Berlin"),
    "DE4": ("DE-BB", "Brandenburg"), "DE5": ("DE-HB", "Bremen"), "DE6": ("DE-HH", "Hamburg"),
    "DE7": ("DE-HE", "Hessen"), "DE8": ("DE-MV", "Mecklenburg-Vorpommern"), "DE9": ("DE-NI", "Niedersachsen"),
    "DEA": ("DE-NW", "Nordrhein-Westfalen"), "DEB": ("DE-RP", "Rheinland-Pfalz"), "DEC": ("DE-SL", "Saarland"),
    "DED": ("DE-SN", "Sachsen"), "DEE": ("DE-ST", "Sachsen-Anhalt"), "DEF": ("DE-SH", "Schleswig-Holstein"),
    "DEG": ("DE-TH", "Thüringen"),
}
NUTS1_URL = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2024_4326_LEVL_1.geojson"


def eurostat_url(dataset: str, filters: str, geos: list[str]) -> str:
    return f"{EUROSTAT_API}/{dataset}?format=JSON&lang=EN&{filters}" + "".join(f"&geo={g}" for g in geos)


def eurostat_frame(path: Path) -> tuple[pd.DataFrame, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = B.jsonstat_rows(payload)
    data["time"] = pd.to_numeric(data.time)
    return data, payload


def build_german_states_population() -> pd.DataFrame:
    map_id = "german_states_population"
    url = eurostat_url("demo_r_d2jan", "sex=T&age=TOTAL&unit=NR", list(DE_NUTS1) + ["DE"])
    data, payload = eurostat_frame(fetch(url, "eurostat_demo_r_d2jan_de.json"))
    shapes = gpd.read_file(fetch(NUTS1_URL, "nuts1_2024_01m_4326.geojson"))
    de = shapes[shapes.CNTR_CODE == "DE"].copy()
    if sorted(de.NUTS_ID) != sorted(DE_NUTS1):
        raise ValueError("GISCO NUTS 1 Germany differs")
    latest = data[data.geo.isin(DE_NUTS1)].sort_values(["geo", "time"]).groupby("geo", as_index=False).tail(1)
    year = int(latest.time.min())
    if latest.time.nunique() != 1:
        raise ValueError("Länder differ in latest year")
    total = int(data[(data.geo == "DE") & (data.time == year)].value.iloc[0])
    area = de.set_index("NUTS_ID").to_crs(3035).geometry.area / 1e6
    truth = pd.DataFrame({
        "key": latest.geo.map(lambda g: DE_NUTS1[g][0]), "name": latest.geo.map(lambda g: DE_NUTS1[g][1]),
        "value": latest.value.astype(int), "unit": "persons", "year": f"{year}-01-01", "nuts": latest.geo,
    }).sort_values("key").reset_index(drop=True)
    truth["area_km2_gisco"] = truth.nuts.map(area).round(0)
    truth["density_per_km2"] = (truth.value / truth.area_km2_gisco).round(1)
    if len(truth) != 16 or truth.value.sum() != total:
        raise ValueError(f"Länder sum {truth.value.sum()} != DE {total}")
    write_csv(map_id, truth)
    write_md(map_id, [
        f"- URL/API: {url} (Eurostat dissemination API 1.0, JSON-stat, demo_r_d2jan 'Population on 1 January by age, sex and NUTS 2 region' -- NUTS 1 rows are included -- total age, total sex; Eurostat republishes Destatis' population update); geometry {NUTS1_URL}.",
        f"- Vintage: population on 1 January {year} (dataset updated {payload.get('updated', '?')[:10]}); the 16 Länder sum to {int(truth.value.sum()):,}, equal to the Germany row ({total:,}). Destatis GENESIS 12411-0010 needs a login, so the Eurostat copy is used.",
        "- Licence: Eurostat data, free reuse with attribution (Commission Decision 2011/833/EU); boundaries (c) EuroGeographics for the administrative boundaries (GISCO NUTS 2024, 1:1M).",
        f"- Rows: 16 Länder, key ISO 3166-2:DE (DE-NW, DE-HB, DE-BE ...), `nuts` = NUTS 1 code (DE1-DEG). Columns: key, name, value (persons), unit, year, nuts, area_km2_gisco and density_per_km2 (derived from the generalised GISCO polygons, for the shading only). Largest {truth.sort_values('value').iloc[-1]['name']} {truth.value.max():,}, smallest {truth.sort_values('value').iloc[0]['name']} {truth.value.min():,}.",
        "- Caveat: Germany's NUTS 1 regions are the 16 Länder one to one, so there is no correspondence table. The 2022 census revision is in the series from 2022 onwards. GISCO areas include coastal waters inside the 1:1M outline (a few tenths of a percent).",
    ])
    crs = "EPSG:25832"
    g = de.merge(truth[["nuts", "key", "name", "value", "density_per_km2"]], left_on="NUTS_ID", right_on="nuts", suffixes=("_gisco", "")).to_crs(crs)
    xmin, ymin, xmax, ymax = g.total_bounds
    extent = (xmin - 40_000, xmax + 40_000, ymin - 25_000, ymax + 30_000)
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "Population of the German federal states",
                f"Residents on 1 January {year}, circle area proportional to population · {total / 1e6:.1f} million in all · shading shows persons per km²",
                map_w=9.7, footer=1.2)
    ax = cv.ax
    world = H.world10()
    ctx = world[~(world.key == "DEU")].clip(box(-2, 44, 22, 58)).to_crs(crs)
    ctx.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    edges = [100, 150, 250, 500, 1000]
    colors = ramp("Blues", 6, 0.08, 0.72)
    plot_classes(ax, g, "density_per_km2", edges, colors, linewidth=1.0, edgecolor="white")
    for name, lon, lat in (("DENMARK", 9.4, 55.7), ("POLAND", 15.9, 52.2), ("CZECHIA", 14.9, 50.0), ("AUSTRIA", 13.9, 47.6), ("SWITZERLAND", 8.1, 47.5),
                           ("FRANCE", 6.6, 48.3), ("LUXEMBOURG", 6.1, 49.65), ("BELGIUM", 5.0, 50.5), ("NETHERLANDS", 5.9, 52.5)):
        land_label(ax, crs, extent, name, lon, lat, 8.3)
    sea_label(ax, crs, "North Sea", 6.2, 54.6, 10)
    sea_label(ax, crs, "Baltic Sea", 12.6, 54.95, 10)
    pts = g.copy()
    pts["pt"] = pts.geometry.representative_point()
    nudge = {"DE-BE": (-18_000, -6_000), "DE-HH": (0, 0), "DE-HB": (-14_000, 0), "DE-BB": (-22_000, -6_000), "DE-NI": (-10_000, 12_000),
             "DE-NW": (0, 0), "DE-ST": (5_000, 0), "DE-SL": (0, 0), "DE-SH": (-4_000, 6_000), "DE-RP": (-4_000, -4_000), "DE-HE": (0, -8_000),
             "DE-BW": (0, 0), "DE-BY": (0, 0), "DE-SN": (0, 0), "DE-TH": (0, 0), "DE-MV": (0, 0)}
    k = 2.1e-4   # pt^2 of circle area per person
    for row in pts.sort_values("value", ascending=False).itertuples():
        dx, dy = nudge[row.key]
        ax.scatter([row.pt.x + dx], [row.pt.y + dy], s=row.value * k, color="#c2410c", alpha=0.62, edgecolor="white", linewidth=0.9, zorder=5)
    short = {"Nordrhein-Westfalen": "North Rhine-\nWestphalia", "Baden-Württemberg": "Baden-\nWürttemberg", "Mecklenburg-Vorpommern": "Mecklenburg-\nVorpommern",
             "Rheinland-Pfalz": "Rhineland-\nPalatinate", "Schleswig-Holstein": "Schleswig-\nHolstein", "Sachsen-Anhalt": "Saxony-\nAnhalt",
             "Bayern": "Bavaria", "Niedersachsen": "Lower Saxony", "Sachsen": "Saxony", "Thüringen": "Thuringia", "Hessen": "Hesse"}
    callouts = {"DE-BE": (40, 8), "DE-HH": (50, -16), "DE-HB": (-44, 18), "DE-SH": (-62, 6)}
    for row in pts.itertuples():
        dx, dy = nudge[row.key]
        x, y = row.pt.x + dx, row.pt.y + dy
        r_pt = (row.value * k / np.pi) ** 0.5
        name = short.get(row.name, row.name)
        label = f"{name}\n{row.value / 1e6:.2f} M"
        if row.key in callouts:
            ox, oy = callouts[row.key]
            ax.annotate(f"{name} {row.value / 1e6:.2f} M", (x, y), xytext=(ox, oy), textcoords="offset points",
                        ha="left" if ox > 0 else ("right" if ox < 0 else "center"), va="center", fontsize=8.8, fontweight="bold",
                        zorder=9, path_effects=HALO, arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6, shrinkA=0, shrinkB=1))
        else:
            ax.annotate(label, (x, y), xytext=(0, -(r_pt + 10)), textcoords="offset points", ha="center", va="top", fontsize=8.8,
                        color=INK, zorder=9, path_effects=HALO, linespacing=1.12, fontweight="bold" if row.value > 9e6 else "normal")
    col_x = cv.left + cv.map_w + 0.5
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 8.3)
    lg.text(0, 8.25, "Residents (circle area)", va="top", fontsize=10.5, fontweight="bold")
    y = 7.6
    for val in (15_000_000, 5_000_000, 1_000_000):
        d = (val * k / np.pi) ** 0.5 * 2 / 72
        lg.add_patch(Circle((0.6, y), d / 2, facecolor="#c2410c", alpha=0.62, edgecolor="white"))
        lg.text(1.3, y, f"{val / 1e6:g} million", va="center", fontsize=9.5)
        y -= max(d, 0.3) + 0.22
    y -= 0.15
    lg.text(0, y, "Shading: persons per km²", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.35
    for colour, label in zip(colors[::-1], ["≥ 1,000 (city-states)", "500–1,000", "250–500", "150–250", "100–150", "< 100"]):
        y -= 0.27
        lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        lg.text(0.58, y + 0.11, label, va="center", fontsize=9.2)
    y -= 0.35
    lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=LAND, edgecolor="#c9c4b8", linewidth=0.4))
    lg.text(0.58, y + 0.11, "Neighbouring countries", va="center", fontsize=9.2)
    ranked_table(cv, col_x, cv.H - cv.header - 6.3, truth, "Ranked, residents (millions)", lambda v: f"{v / 1e6:.2f}", width=col_w, row_h=0.2, size=8.8)
    cv.footnote([
        f"Source: Eurostat, demo_r_d2jan (population on 1 January {year}, NUTS 1 = the 16 German Länder; compiled from Destatis). Boundaries: (c) EuroGeographics, GISCO NUTS 2024 1:1M. Density derived from the GISCO outline. Context: Natural Earth 1:10m.",
        "Projection: ETRS89 / UTM zone 32N (EPSG:25832). Encoding: circle area proportional to residents (a count is never shaded), placed near each Land's interior point and nudged for the city-states; shading = persons per km² in 6 manual classes. "
        "All 16 Länder shown including the city-states Berlin, Hamburg and Bremen; none excluded.",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# 3. Turkey and Syria: M4.5+ earthquakes of 2023 (USGS ComCat)

USGS_TR = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=2023-01-01&endtime=2024-01-01"
           "&minmagnitude=4.5&minlatitude=32&maxlatitude=42.5&minlongitude=25.5&maxlongitude=45.5&orderby=time-asc")
TR_PHASES = [  # (label, colour)
    ("Before 6 February", "#7f8c93"),
    ("6 February (Mw 7.8 and 7.5 mainshocks)", "#b5172b"),
    ("7 February – 31 March (aftershocks)", "#f08a24"),
    ("1 April – 31 December", "#2b6ca3"),
]


def tr_phase(t: pd.Timestamp) -> int:
    if t < pd.Timestamp("2023-02-06", tz="UTC"):
        return 0
    if t < pd.Timestamp("2023-02-07", tz="UTC"):
        return 1
    if t < pd.Timestamp("2023-04-01", tz="UTC"):
        return 2
    return 3


def quake_area(m):
    return 22 * np.power(2.9, np.asarray(m, dtype=float) - 4.5)   # points^2


def build_turkey_quakes_2023() -> pd.DataFrame:
    map_id = "turkey_quakes_2023"
    payload = json.loads(fetch(USGS_TR, "usgs_turkey_syria_2023_m45.geojson").read_text())
    rows = []
    for f in payload["features"]:
        lon, lat, depth = f["geometry"]["coordinates"]
        pr = f["properties"]
        rows.append({"id": f["id"], "time": pd.to_datetime(pr["time"], unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "lon": lon, "lat": lat, "depth_km": depth, "mag": pr["mag"], "magType": pr.get("magType", ""), "place": pr["place"]})
    allq = pd.DataFrame(rows)
    crs = "+proj=lcc +lat_1=35 +lat_2=41 +lat_0=38.5 +lon_0=35.5 +datum=WGS84 +units=m +no_defs"
    world = H.world10()
    land = world[world.key.isin(["TUR", "SYR"])].to_crs(crs).geometry.union_all().buffer(30_000)
    pts = gpd.GeoSeries(gpd.points_from_xy(allq.lon, allq.lat), crs=4326).to_crs(crs)
    keep = pts.within(land).to_numpy()
    dropped = allq[~keep]
    truth = allq[keep].sort_values(["time", "id"]).reset_index(drop=True)
    write_csv(map_id, truth)
    seq = truth[(truth.time >= "2023-02-06") & (truth.lon.between(35, 40)) & (truth.lat.between(36, 39))]
    top = truth.sort_values("mag", ascending=False).head(2)
    write_md(map_id, [
        f"- URL/API: {USGS_TR} (USGS FDSN event service, ComCat), then kept if the epicentre lies on or within 30 km of Natural Earth 1:10m Turkey or Syria (so Aegean/Mediterranean events near the coast stay; Greek, Cypriot, Iranian and Iraqi events do not).",
        f"- Vintage: USGS ComCat as of {pd.to_datetime(payload['metadata']['generated'], unit='ms', utc=True).strftime('%Y-%m-%d')}; events 2023-01-01T00:00Z <= t < 2024-01-01T00:00Z, magnitude >= 4.5, query box 32-42.5N, 25.5-45.5E ({len(allq)} events), {len(truth)} kept, {len(dropped)} dropped ({', '.join(dropped.place.str.split(', ').str[-1].value_counts().index[:6])}).",
        "- Licence: U.S. Geological Survey Earthquake Hazards Program, U.S. government work (public domain). Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} events — id, UTC time, lon, lat, depth_km, mag, magType, place. Largest: M{top.mag.iloc[0]:g} Pazarcik (6 Feb 01:17 UTC, 37.01E 37.23N) and M{top.mag.iloc[1]:g} Elbistan (6 Feb 10:24 UTC); {len(seq)} events from 6 February inside 35-40E, 36-39N belong to the Kahramanmaras sequence.",
        "- Caveat: the 'region' is a rectangle plus a 30 km coastal buffer, not an official boundary, so the count (the spec's range is 100-600) moves by a handful with the buffer; ComCat revises and adds events. USGS magnitudes (Mww/mb) differ from AFAD's local values.",
    ])
    t = Transformer.from_crs(4326, crs, always_xy=True)
    truth["x"], truth["y"] = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    truth["phase"] = pd.to_datetime(truth.time).map(tr_phase)
    ring = [(lo, la) for lo in np.linspace(25.4, 45.3, 40) for la in (32.0, 42.4)] + [(lo, la) for la in np.linspace(32.0, 42.4, 40) for lo in (25.4, 45.3)]
    xy = project_points(crs, ring)
    extent = (min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy))
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "Earthquakes of magnitude 4.5 or more in Turkey and Syria, 2023",
                f"{len(truth)} events recorded by the USGS · circle area grows with magnitude, colour shows when · "
                f"the 6 February M{top.mag.iloc[0]:g} and M{top.mag.iloc[1]:g} began a sequence of {len(seq)} M4.5+ events",
                map_w=10.6, footer=2.0)
    ax = cv.ax
    w = world.clip(box(15, 25, 55, 50)).to_crs(crs)
    w[~w.key.isin(["TUR", "SYR"])].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    w[w.key.isin(["TUR", "SYR"])].plot(ax=ax, color="#efece3", edgecolor="white", linewidth=0.6, zorder=1.1)
    H.local_graticule(ax, crs, range(26, 46, 2), range(32, 43, 2), lon_span=(24, 47), lat_span=(31, 43.5))
    for name, lon, lat in (("TURKEY", 33.5, 39.1), ("SYRIA", 38.4, 34.9), ("GREECE", 25.9, 39.6), ("BULGARIA", 25.9, 42.2), ("GEORGIA", 43.9, 42.15), ("ARMENIA", 44.9, 40.4),
                           ("IRAN", 45.0, 38.2), ("IRAQ", 43.4, 35.2), ("LEBANON", 35.9, 33.9), ("CYPRUS", 33.2, 35.0), ("ISRAEL", 35.0, 32.4), ("JORDAN", 37.6, 32.2),
                           ("AZERBAIJAN", 45.2, 39.9)):
        land_label(ax, crs, extent, name, lon, lat, 8.5 if name in ("TURKEY", "SYRIA") else 7.5)
    for name, lon, lat in (("Black Sea", 35.0, 42.0), ("Aegean\nSea", 25.9, 38.3), ("Mediterranean Sea", 30.3, 35.5), ("Sea of\nMarmara", 28.3, 40.72)):
        sea_label(ax, crs, name, lon, lat, 9.5 if name != "Sea of\nMarmara" else 6.5)
    cities = {"Ankara": (32.86, 39.93), "Istanbul": (28.98, 41.01), "İzmir": (27.14, 38.42), "Konya": (32.49, 37.87), "Adana": (35.32, 37.0),
              "Gaziantep": (37.38, 37.07), "Aleppo": (37.16, 36.2), "Damascus": (36.29, 33.51), "Diyarbakır": (40.23, 37.91), "Malatya": (38.31, 38.35),
              "Kahramanmaraş": (36.92, 37.58), "Antakya": (36.16, 36.2), "Sivas": (37.02, 39.75), "Trabzon": (39.72, 41.0), "Van": (43.38, 38.5),
              "Erzurum": (41.27, 39.9), "Homs": (36.72, 34.73), "Raqqa": (39.01, 35.95), "Düzce": (31.16, 40.84)}
    show = {"Ankara", "Istanbul", "İzmir", "Konya", "Aleppo", "Damascus", "Diyarbakır", "Sivas", "Trabzon", "Van", "Erzurum", "Homs", "Raqqa", "Adana"}
    for nm, (lon, lat) in cities.items():
        (x, y), = project_points(crs, [(lon, lat)])
        if nm in show:
            ax.scatter([x], [y], s=16, marker="s", color=INK, edgecolor="white", linewidth=0.6, zorder=6)
            ax.annotate(nm, (x, y), xytext=(5, -9), textcoords="offset points", fontsize=8.2, color="#2b3539", zorder=9, path_effects=HALO)
    for ph in range(3, -1, -1):
        g = truth[truth.phase == ph]
        ax.scatter(g.x, g.y, s=quake_area(g.mag), color=TR_PHASES[ph][1], alpha=0.55, edgecolor=INK if ph == 1 else "white", linewidth=0.6 if ph == 1 else 0.5, zorder=5 + (3 - ph) * 0.1)
    # box on the main map, detail inset in the right-hand column
    zb = (35.2, 39.2, 36.0, 38.8)   # lon0, lon1, lat0, lat1
    zx0, zy0 = t.transform(zb[0], zb[2])
    zx1, zy1 = t.transform(zb[1], zb[3])
    col_x = cv.left + cv.map_w + 0.3
    col_w = WIDTH_IN - col_x - 0.25
    iw = col_w
    ih = iw * (zy1 - zy0) / (zx1 - zx0)
    top_in = cv.H - cv.header - 0.05
    inset = cv.add_axes(col_x, top_in - ih, iw, ih, frame=True)
    inset.set_facecolor(WATER)
    w.plot(ax=inset, color="#efece3", edgecolor="white", linewidth=0.6, zorder=1)
    for ph in range(3, -1, -1):
        g = truth[truth.phase == ph]
        inset.scatter(g.x, g.y, s=quake_area(g.mag) * 2.4, color=TR_PHASES[ph][1], alpha=0.55, edgecolor=INK if ph == 1 else "white", linewidth=0.7 if ph == 1 else 0.5, zorder=5 + (3 - ph) * 0.1)
    inset.set_xlim(zx0, zx1)
    inset.set_ylim(zy0, zy1)
    bx = [(zx0, zy0), (zx1, zy0), (zx1, zy1), (zx0, zy1), (zx0, zy0)]
    ax.plot([p[0] for p in bx], [p[1] for p in bx], color=INK, linewidth=1.3, zorder=6)
    ax.annotate("detail →", (zx1, zy1), xytext=(8, 6), textcoords="offset points", fontsize=8.8, fontweight="bold", path_effects=HALO, zorder=9)
    inset.text(0.02, 0.985, "The 6 February sequence, detail of the boxed area (circles 2.4× larger)", transform=inset.transAxes, va="top", fontsize=7.8, fontweight="bold", path_effects=HALO, zorder=10)
    for nm in ("Gaziantep", "Kahramanmaraş", "Antakya", "Malatya", "Aleppo"):
        lon, lat = cities[nm]
        (x, y), = project_points(crs, [(lon, lat)])
        inset.scatter([x], [y], s=14, marker="s", color=INK, edgecolor="white", linewidth=0.6, zorder=7)
        inset.annotate(nm, (x, y), xytext=(5, -10), textcoords="offset points", fontsize=7.8, color="#2b3539", zorder=9, path_effects=HALO)
    mains = truth[truth.mag >= 7.0].sort_values("mag", ascending=False)
    offs = {7.8: (-70, -34, "right"), 7.5: (-60, 46, "right")}
    for r in mains.itertuples():
        txt = {7.8: "Mw 7.8 Pazarcık\n6 Feb, 01:17 UTC", 7.5: "Mw 7.5 Elbistan\n6 Feb, 10:24 UTC"}[round(r.mag, 1)]
        dx, dy, ha = offs[round(r.mag, 1)]
        inset.annotate(txt, (r.x, r.y), xytext=(dx, dy), textcoords="offset points", ha=ha, va="center", fontsize=8.6, fontweight="bold",
                       zorder=10, path_effects=HALO, linespacing=1.15, arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.7, shrinkA=0, shrinkB=2))
    # right column: the largest events
    y = top_in - ih - 0.3
    lg = cv.panel(col_x, y, col_w, 2.2)
    lg.text(0, 2.17, "Largest events", va="top", fontsize=10.5, fontweight="bold")
    fix = {"Nurda??": "Nurdağı", "Uzunba?": "Uzunbağ (Hatay)", "Do?an?ehir": "Doğanşehir", "Çelikhan": "Çelikhan", "Göksun": "Göksun"}

    def where(r):
        if "Pazarcik" in r.place:
            return "Pazarcık"
        if "Elbistan" in r.place:
            return "Elbistan"
        tail = r.place.split(" of ", 1)[-1].split(",")[0]
        return fix.get(tail, tail)
    yy = 1.78
    for r in truth.sort_values("mag", ascending=False).head(8).itertuples():
        lg.text(0, yy, f"M{r.mag:g}", va="center", fontsize=8.8, fontweight="bold")
        lg.text(0.55, yy, f"{pd.Timestamp(r.time).strftime('%-d %b')} · {where(r)}", va="center", fontsize=8.8)
        yy -= 0.215
    # footer legend, horizontal
    fl = cv.panel(cv.left, cv.footer - 0.05, 15.2, 1.15)
    fl.text(0, 1.12, "Date of the event (colour)", va="top", fontsize=10.5, fontweight="bold")
    x = 0
    for lab, col in TR_PHASES:
        fl.scatter([x + 0.12], [0.62], s=110, color=col, alpha=0.7, edgecolor="white")
        fl.text(x + 0.32, 0.62, lab.replace(" (", "\n("), va="center", fontsize=8.8, linespacing=1.2)
        x += 2.55
    fl.text(10.9, 1.12, "Magnitude (circle area)", va="top", fontsize=10.5, fontweight="bold")
    x = 11.05
    for m in (5, 6, 7, 7.8):
        d = (quake_area(m) / np.pi) ** 0.5 * 2 / 72
        fl.add_patch(Circle((x + d / 2, 0.55), d / 2, facecolor="#9aa3a7", alpha=0.5, edgecolor="white"))
        fl.text(x + d / 2, 0.55 - max(d, 0.2) / 2 - 0.13, f"M {m:g}", va="top", ha="center", fontsize=8.8)
        x += max(d, 0.2) + 0.5
    cv.footnote([
        f"Source: U.S. Geological Survey, ComCat via the FDSN event service (public domain), data as of {pd.to_datetime(payload['metadata']['generated'], unit='ms', utc=True).strftime('%Y-%m-%d')}; events of magnitude 4.5 or more, 1 January – 31 December 2023. Land: Natural Earth 1:10m.",
        f"Projection: Lambert conformal conic (parallels 35°N and 41°N, centred on 35.5°E). {len(truth)} events shown, no classification (circle area ∝ 2.9^M, colour = period). Excluded: {len(dropped)} events of the query box that lie in Iran, Greece, Cyprus, Iraq or more than 30 km off the Syrian coast "
        "(more than 30 km from Turkey or Syria). The inset repeats the Kahramanmaraş region at larger scale.",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# 4. Madrid Metro (OpenStreetMap route=subway, network "Metro de Madrid")

MADRID_ORDER = ["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10", "L11", "L12", "R"]
MADRID_NAME = {"R": "Ramal (Ópera – Príncipe Pío)"}
MADRID_LANDMARKS = {  # name -> (lon, lat) of the station (snapped to the network for drawing)
    "Sol": (-3.7038, 40.4168), "Atocha": (-3.6905, 40.4065), "Nuevos Ministerios": (-3.6925, 40.4465),
    "Cuatro Caminos": (-3.7038, 40.4468), "Príncipe Pío": (-3.7203, 40.4212), "Moncloa": (-3.7191, 40.4349),
    "Av. de América": (-3.6770, 40.4380), "Plaza de Castilla": (-3.6894, 40.4659), "Chamartín": (-3.6826, 40.4725),
    "Tribunal": (-3.7012, 40.4268), "Retiro": (-3.6830, 40.4204), "Ópera": (-3.7099, 40.4181),
}


def lum(hexcol: str) -> float:
    r, g, b = (int(hexcol.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def badge(ax, x, y, ref, colour, *, size=8.2, zorder=9, scale=1.0):
    label = ref.lstrip("L")
    face = "#ffffff" if colour.lower() == "white" else colour
    ax.text(x, y, label, fontsize=size, fontweight="bold", ha="center", va="center", zorder=zorder,
            color="white" if lum(face) < 0.5 else INK,
            bbox=dict(boxstyle="round,pad=0.22,rounding_size=0.5", facecolor=face, edgecolor=INK if lum(face) > 0.8 else "white", linewidth=0.9))


def build_madrid_metro() -> pd.DataFrame:
    from set2_overlays import dissolved_route_km, geodesic_km, relation_polygon, way_line
    map_id = "madrid_metro"
    payload = overpass(MADRID_Q, "madrid_metro_overpass.json")
    base = osm_base(payload)
    rels = [e for e in payload["elements"] if e["type"] == "relation"]
    rows, stops_all, ends_all = [], [], {}
    for ref in MADRID_ORDER:
        mine = sorted([r for r in rels if r["tags"].get("ref") == ref], key=lambda r: r["id"])
        if not mine:
            raise ValueError(f"no relation for {ref}")
        ways = {}
        for r in mine:
            for m in r["members"]:
                if m["type"] == "way" and not re.search(r"platform|stop|entry", m.get("role", "")) and m.get("geometry"):
                    ln = way_line(m)
                    if ln is not None:
                        ways[m["ref"]] = ln
        stops = {}
        ends = []
        for r in mine:
            st = [(m["lon"], m["lat"]) for m in r["members"] if m["type"] == "node" and re.match(r"stop", m.get("role", "")) and "lon" in m]
            for m in r["members"]:
                if m["type"] == "node" and re.match(r"stop", m.get("role", "")) and "lon" in m:
                    stops[m["ref"]] = (m["lon"], m["lat"])
            if st:
                ends += [st[0], st[-1]]
        cols = [r["tags"]["colour"] for r in mine if r["tags"].get("colour")]
        geom = unary_union(list(ways.values()))
        merged = linemerge(geom) if geom.geom_type == "MultiLineString" else geom
        pretty = MADRID_NAME.get(ref) or re.sub(r"^Línea (\d+)[.:] ?", "", mine[0]["tags"].get("name", "")).strip()
        rows.append({
            "ref": ref, "name": ("Línea " + ref[1:] if ref != "R" else "Línea Ramal"), "network": "Metro de Madrid",
            "colour": max(set(cols), key=cols.count), "osm_relations": ";".join(str(r["id"]) for r in mine), "osm_ways": len(ways),
            "stops": len({(round(x, 4), round(y, 4)) for x, y in stops.values()}),
            "from_to": f"{mine[0]['tags'].get('from', '')} - {mine[0]['tags'].get('to', '')}",
            "route_km": round(dissolved_route_km(list(ways.values()), buffer_m=40, spacing_m=8), 1),
            "geometry_wkt": shapely_wkt(merged.simplify(0.00002)),
        })
        stops_all += [(ref, x, y) for x, y in stops.values()]
        ends_all[ref] = ends
    truth = pd.DataFrame(rows)
    write_csv(map_id, truth)
    n_stations = len({(round(x, 3), round(y, 3)) for _r, x, y in stops_all})
    write_md(map_id, [
        f"- URL/API: Overpass API (overpass-api.de): `{compact(MADRID_Q)}`; context (municipalities admin_level 8 and the Manzanares) `{compact(MADRID_CTX_Q)}`.",
        f"- Vintage: OpenStreetMap as of {base}; {len(rels)} route=subway relations (one per direction/pattern) grouped by `ref` into {len(truth)} lines. Line colours are the OSM `colour` tags (Ramal is tagged `white`).",
        "- Licence: (c) OpenStreetMap contributors, ODbL 1.0. Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} — L1-L12 plus the Ramal shuttle (Opera - Principe Pio): ref, name, network, colour, osm_relations, osm_ways, stops (distinct stop positions within 11 m), from_to, route_km (member ways of all relations dissolved, 40 m buffers), geometry_wkt (member track ways, merged, simplified ~2 m). {n_stations} distinct stations at ~100 m resolution; total {truth.route_km.sum():,.0f} route-km.",
        "- Caveat: the spec says '12 lines + Ramal' = 13; both counts agree. Line 6 is a loop (two relations, 'Andén 1/2') and Line 12 (MetroSur) is also a loop and shares the Puerta del Sur terminus with Line 10. Metro Ligero (light rail, separate network) and Cercanias are not included. Network total double counts stretches where two lines share track by a few km.",
    ])
    crs = "EPSG:25830"
    lines = gpd.GeoDataFrame(truth, geometry=[wkt.loads(t) for t in truth.geometry_wkt], crs=4326).to_crs(crs)
    xmin, ymin, xmax, ymax = lines.total_bounds
    extent = (xmin - 3_500, xmax + 3_500, ymin - 3_000, ymax + 3_000)
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "The Madrid Metro network",
                f"The 12 lines and the Ramal shuttle in their official colours, from OpenStreetMap · {truth.route_km.sum():,.0f} km of route and about {n_stations} stations",
                map_w=10.2, footer=1.05, min_h=7.4)
    ax = cv.ax
    ax.set_facecolor("#efede6")
    ctxp = overpass(MADRID_CTX_Q, "madrid_municipalities_overpass.json")
    muni = {}
    river = []
    for e in ctxp["elements"]:
        if e["type"] == "relation" and e.get("members"):
            try:
                muni[e["tags"].get("name", "?")] = relation_polygon(e)
            except Exception:
                pass
        elif e["type"] == "way" and e.get("geometry"):
            river.append(LineString([(q["lon"], q["lat"]) for q in e["geometry"]]))
    mg = gpd.GeoSeries(list(muni.values()), index=list(muni), crs=4326).to_crs(crs)
    for nm, g in mg.items():
        gpd.GeoSeries([g], crs=crs).plot(ax=ax, facecolor="#f6f4ee" if nm == "Madrid" else LAND, edgecolor="white", linewidth=1.0, zorder=1)
    if river:
        gpd.GeoSeries(river, crs=4326).to_crs(crs).plot(ax=ax, color="#b9d3df", linewidth=2.2, zorder=1.5)
    inside = box(*extent)
    for nm, g in mg.items():
        c = g.representative_point()
        if nm in ("Madrid",):
            continue
        if extent[0] < c.x < extent[1] and extent[2] < c.y < extent[3]:
            ax.text(c.x, c.y, nm.upper(), fontsize=6.6, color="#aaa496", ha="center", va="center", zorder=2)
    cm = mg["Madrid"].representative_point() if "Madrid" in mg else None
    width = {"L6": 4.4, "L12": 3.8, "R": 3.0}
    order = list(range(len(lines)))
    tr = Transformer.from_crs(4326, crs, always_xy=True)
    sx, sy = zip(*[tr.transform(x, y) for _r, x, y in stops_all])

    def draw(a, wmul, sz):
        for i in order:
            ref = lines.ref[i]
            wd = width.get(ref, 3.0) * wmul
            casing = "#4a5559" if lines.colour[i].lower() == "white" else "white"
            gpd.GeoSeries([lines.geometry[i]], crs=crs).plot(ax=a, color=casing, linewidth=wd + 1.5 * wmul ** 0.5, zorder=3, capstyle="round")
        for i in order:
            ref = lines.ref[i]
            wd = width.get(ref, 3.0) * wmul
            col = "#ffffff" if lines.colour[i].lower() == "white" else lines.colour[i]
            gpd.GeoSeries([lines.geometry[i]], crs=crs).plot(ax=a, color=col, linewidth=wd, zorder=4, capstyle="round")
        a.scatter(sx, sy, s=sz, facecolor="white", edgecolor=INK, linewidth=0.5, zorder=5)
    draw(ax, 1.0, 5)
    # line-number badges at the terminals, nudged apart
    placed = []
    for i in range(len(lines)):
        ref = lines.ref[i]
        ends = []
        for lon, lat in ends_all[ref]:
            x, y = tr.transform(lon, lat)
            if not any(np.hypot(x - ex, y - ey) < 400 for ex, ey in ends):
                ends.append((x, y))
        for (x, y) in ends:
            for dx, dy in [(0, 0), (1200, 0), (-1200, 0), (0, 1200), (0, -1200), (1200, 1200), (-1200, 1200), (1200, -1200), (-1200, -1200)]:
                if not any(np.hypot(x + dx - px, y + dy - py) < 1000 for px, py in placed):
                    placed.append((x + dx, y + dy))
                    badge(ax, x + dx, y + dy, ref, lines.colour[i], size=7.6)
                    break
    net = lines.geometry.union_all()
    snapped = {}
    for nm, (lon, lat) in MADRID_LANDMARKS.items():
        x, y = tr.transform(lon, lat)
        d = net.distance(Point(x, y))
        snapped[nm] = (x, y, d)
        print(f"  landmark {nm}: {d:.0f} m from the network")
    # central inset
    cb = (-3.762, -3.640, 40.396, 40.470)
    cx0, cy0 = tr.transform(cb[0], cb[2])
    cx1, cy1 = tr.transform(cb[1], cb[3])
    col_x = cv.left + cv.map_w + 0.35
    col_w = WIDTH_IN - col_x - 0.25
    ih = col_w * (cy1 - cy0) / (cx1 - cx0)
    top_in = cv.H - cv.header - 0.05
    inset = cv.add_axes(col_x, top_in - ih, col_w, ih, frame=True)
    inset.set_facecolor("#efede6")
    for nm, g in mg.items():
        gpd.GeoSeries([g], crs=crs).plot(ax=inset, facecolor="#f6f4ee" if nm == "Madrid" else LAND, edgecolor="white", linewidth=1.0, zorder=1)
    if river:
        gpd.GeoSeries(river, crs=4326).to_crs(crs).plot(ax=inset, color="#b9d3df", linewidth=4.0, zorder=1.5)
    draw(inset, 1.7, 16)
    inset.set_xlim(cx0, cx1)
    inset.set_ylim(cy0, cy1)
    offs = {"Sol": (10, -12, "left"), "Atocha": (10, -12, "left"), "Nuevos Ministerios": (10, 12, "left"), "Cuatro Caminos": (-10, 12, "right"),
            "Príncipe Pío": (-10, -12, "right"), "Moncloa": (-10, 12, "right"), "Av. de América": (10, 12, "left"),
            "Plaza de Castilla": (10, 12, "left"), "Chamartín": (10, 6, "left"), "Tribunal": (-10, 8, "right"), "Retiro": (10, -4, "left"), "Ópera": (-10, -6, "right")}
    for nm, (dx, dy, ha) in offs.items():
        x, y, d = snapped[nm]
        if d > 400:
            continue
        inset.scatter([x], [y], s=34, color="white", edgecolor=INK, linewidth=1.3, zorder=7)
        inset.annotate(nm, (x, y), xytext=(dx, dy), textcoords="offset points", ha=ha, va="center", fontsize=8, fontweight="bold",
                       color=INK, zorder=9, path_effects=HALO)
    inset.text(0.02, 0.98, "Central Madrid (detail)", transform=inset.transAxes, va="top", fontsize=9, fontweight="bold", path_effects=HALO, zorder=10)
    bx = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1), (cx0, cy0)]
    ax.plot([p[0] for p in bx], [p[1] for p in bx], color=INK, linewidth=1.0, zorder=6)
    ax.annotate("Central Madrid →", (cx1, cy1), xytext=(6, 8), textcoords="offset points", fontsize=8.6, fontweight="bold", path_effects=HALO, zorder=9)
    # legend
    lg_h = 0.5 + 0.255 * 13 + 0.3
    lg = cv.panel(col_x, top_in - ih - 0.25, col_w, lg_h)
    lg.text(0, lg_h - 0.02, "Line (OSM colour tag) · termini · route km", va="top", fontsize=10, fontweight="bold")
    for k, r in enumerate(truth.itertuples()):
        y = lg_h - 0.52 - k * 0.255
        face = "#ffffff" if r.colour.lower() == "white" else r.colour
        lg.plot([0.05, 0.5], [y, y], color="#4a5559" if r.colour.lower() == "white" else face, linewidth=5, solid_capstyle="butt")
        if r.colour.lower() == "white":
            lg.plot([0.05, 0.5], [y, y], color="white", linewidth=3, solid_capstyle="butt")
        lg.text(0.62, y, r.ref.lstrip("L") if r.ref != "R" else "R", va="center", fontsize=8.8, fontweight="bold")
        ft = r.from_to.replace("Hospital Infanta Sofía", "H. Infanta Sofía").replace("Pinar de Chamartín", "Pinar de Chamartín").replace("Aeropuerto T4", "Aerop. T4")
        if r.ref in ("L6", "L12"):
            ft = {"L6": "Circular, via Laguna", "L12": "MetroSur loop, via Puerta del Sur"}[r.ref]
        lg.text(0.95, y, textwrap.shorten(ft, 38, placeholder="…"), va="center", fontsize=7.7)
        lg.text(col_w - 0.02, y, f"{r.route_km:.0f}", va="center", ha="right", fontsize=8.2, color=MUTED)
    yy = lg_h - 0.52 - 13 * 0.255
    lg.scatter([0.27], [yy], s=16, facecolor="white", edgecolor=INK, linewidth=0.6)
    lg.text(0.62, yy, "Station (stop position)", va="center", fontsize=8.4)
    cv.footnote([
        f"Source: (c) OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {base[:10]}: route=subway relations of the network 'Metro de Madrid' with their colour tags (stations from the relations' stop members); municipality boundaries (admin_level 8) and the Manzanares likewise. Route lengths merge both directions (40 m).",
        "Projection: ETRS89 / UTM zone 30N (EPSG:25830). Numbers in rounded boxes mark the termini of each line. Excluded: Metro Ligero (light rail) and Cercanias (separate networks). Line 6 and Line 12 are loops; the Ramal is a one-station-pair shuttle drawn white with a dark casing.",
    ], width=200)
    cv.save(map_id)
    return truth


def shapely_wkt(g) -> str:
    import shapely
    return shapely.to_wkt(g, rounding_precision=5)


# ==========================================================================
# 8. Ivano-Frankivsk oblast: every settlement (KATOTTH + OpenStreetMap)


KATOTTH_URL = "https://mindev.gov.ua/storage/app/sites/1/uploaded-files/kodifikator-07-07.xlsx"
KATOTTH_PAGE = ("https://mindev.gov.ua/diialnist/rozvytok-mistsevoho-samovriaduvannia/"
                "kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytorii-terytorialnykh-hromad")
KATOTTH_ORDER = "order No. 1321 of 7 July 2026"
IVF = "UA26"


def if_data():
    from set2_overlays import match_volyn, read_katotth, relation_polygon
    k = read_katotth(fetch(KATOTTH_URL, "katotth_2026-07-07.xlsx"))
    v = k[k.l1.str.startswith(IVF)]
    raions = v[v.cat == "P"].set_index("l2").name.to_dict()
    hromadas = v[v.cat == "H"].set_index("l3").name.to_dict()
    sett = v[v.cat.isin(["M", "T", "X", "C"])].copy()
    sett = pd.DataFrame({"katotth": sett.l4, "category": sett.cat, "name": sett.name, "hromada_katotth": sett.l3,
                         "hromada": sett.l3.map(hromadas), "raion_katotth": sett.l2, "raion": sett.l2.map(raions)})
    sett = sett.sort_values("katotth").reset_index(drop=True)
    payload = overpass(IF_Q, "ivano_frankivsk_overpass.json")
    nodes = [e for e in payload["elements"] if e["type"] == "node"]
    rels = [e for e in payload["elements"] if e["type"] == "relation"]
    polys = {}
    for r in rels:
        tags = r["tags"]
        lvl, code = tags.get("admin_level"), tags.get("katotth", "")
        if lvl == "4":
            polys[("oblast", tags.get("ISO3166-2"))] = (tags.get("name:en", tags.get("name")), relation_polygon(r))
        elif lvl in ("6", "7") and code.startswith(IVF):
            polys[("raion" if lvl == "6" else "hromada", code)] = (tags.get("name"), relation_polygon(r))
    places = pd.DataFrame([{
        "osm_node_id": n["id"], "lon": n["lon"], "lat": n["lat"], "osm_place": n["tags"].get("place"),
        "osm_name": n["tags"].get("name:uk") or n["tags"].get("name", ""), "osm_katotth": n["tags"].get("katotth", ""),
        "osm_prefix": n["tags"].get("name:prefix", "")} for n in nodes])
    truth, leftovers = match_volyn(sett, places, polys)
    return truth, leftovers, polys, v, osm_base(payload), places


def build_ivano_frankivsk_settlements() -> pd.DataFrame:
    from set2_overlays import OSM_SETTLEMENT, norm_name, translit
    map_id = "ivano_frankivsk_settlements"
    truth, leftovers, polys, vrows, base, places = if_data()
    write_csv(map_id, truth)
    cats = truth.category.value_counts()
    how = truth.match.value_counts()
    matched = int(truth.lon.notna().sum())
    unmatched = truth[truth.lon.isna()]
    n_h = int((vrows.cat == "H").sum())
    n_r = int((vrows.cat == "P").sum())
    osm_settl = places[places.osm_place.isin(OSM_SETTLEMENT)]
    write_md(map_id, [
        f"- URL/API: {KATOTTH_URL} (KATOTTH xlsx, linked from {KATOTTH_PAGE}; rows under {IVF}, categories M/T/X/C). Coordinates and boundaries: Overpass API, `{compact(IF_Q)}`.",
        f"- Vintage: KATOTTH as amended by the Ministry for Development of Communities and Territories {KATOTTH_ORDER} (the newest of the 24 xlsx editions listed on the ministry page on {NOW}); OpenStreetMap as of {base}.",
        "- Licence: KATOTTH is an official act of a Ukrainian ministry (not subject to copyright, Law of Ukraine on Copyright Art. 10); coordinates and boundaries © OpenStreetMap contributors, ODbL 1.0.",
        f"- Rows: {len(truth)} KATOTTH settlements of Ivano-Frankivsk oblast — {cats.get('M', 0)} cities (M), {cats.get('T', 0)} urban-type settlements (T), {cats.get('X', 0)} селища (X), {cats.get('C', 0)} villages (C); {n_r} raions, {n_h} hromadas. Columns: katotth, category, name, hromada/raion codes and names, match, osm_node_id, osm_place, osm_name, lon, lat. Located via an OSM place node: {matched} ({', '.join(f'{v} {k}' for k, v in how.items() if k != 'unmatched')}); unmatched {int(how.get('unmatched', 0))}" + (f" ({'; '.join(f'{r.name}, {r.hromada} hromada' for r in unmatched.itertuples())})" if len(unmatched) else "") + ".",
        f"- Caveat: KATOTTH has no 'T' rows since the 2024 reform renamed every urban-type settlement to селище (X). {len(leftovers)} OSM settlement nodes in the oblast match no KATOTTH row" + (f" ({'; '.join(f'{r.osm_name} [{r.osm_place}]' for r in leftovers.head(10).itertuples())}{' …' if len(leftovers) > 10 else ''})" if len(leftovers) else "") + ". The spec's `count=800±10%` (720-880) is checked against the KATOTTH count (" + str(len(truth)) + ").",
    ])
    crs = "+proj=tmerc +lat_0=48.7 +lon_0=24.9 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)

    def proj(geom):
        return gpd.GeoSeries([geom], crs=4326).to_crs(crs)

    ivf = polys[("oblast", "UA-26")][1]
    others = {k[1]: g for k, (_n, g) in polys.items() if k[0] == "oblast" and k[1] != "UA-26"}
    x0, y0, x1, y1 = proj(ivf).total_bounds
    extent = (x0 - 9_000, x1 + 1_000, y0 - 6_000, y1 + 6_000)
    check_window(map_id, crs, extent)
    n_loc = matched
    cv = Canvas(extent, "Every town and village of Ivano-Frankivsk oblast",
                f"All {len(truth):,} settlements in Ukraine’s official codifier (KATOTTH), by legal status · {n_loc:,} located on OpenStreetMap",
                map_w=11.6, footer=1.15)
    ax = cv.ax
    ax.set_facecolor("#f1efe9")
    for code, g in others.items():
        proj(g).plot(ax=ax, color="#e9e6de", edgecolor="none", zorder=1)
    proj(ivf).plot(ax=ax, color="#e3ddcf", edgecolor="none", zorder=2)
    hroms = [g for (kind, _c), (_n, g) in polys.items() if kind == "hromada"]
    outlines = [unary_union([p for p in getattr(g, "geoms", [g]) if p.area > 1.5e-4]) for g in hroms]
    gpd.GeoSeries([g.boundary for g in outlines], crs=4326).to_crs(crs).plot(ax=ax, color="white", linewidth=0.7, zorder=2.1)
    raions = {code: (name, g) for (kind, code), (name, g) in polys.items() if kind == "raion"}
    for _c, (_n, g) in raions.items():
        proj(g.boundary.difference(ivf.boundary.buffer(0.001))).plot(ax=ax, color="#7d7668", linewidth=1.5, zorder=3)
    other_u = unary_union(list(others.values())).buffer(0.002)
    proj(ivf.boundary.intersection(other_u)).plot(ax=ax, color="#5d564a", linewidth=2.0, zorder=3.1)
    proj(ivf.boundary.difference(other_u)).plot(ax=ax, color="#5d564a", linewidth=2.0, linestyle=(0, (5, 2.5)), zorder=3.2)
    for name, lon, lat in (("LVIV OBLAST", 24.15, 49.55), ("TERNOPIL OBLAST", 25.6, 49.35), ("CHERNIVTSI OBLAST", 25.62, 48.15), ("ZAKARPATTIA\nOBLAST", 23.05, 48.4)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=9, color="#8e897d", ha="center", va="center", zorder=2.5, clip_on=True)
    loc = truth.dropna(subset=["lon", "lat"]).copy()
    loc["x"], loc["y"] = t.transform(loc.lon.to_numpy(), loc.lat.to_numpy())
    vil, sel, city = (loc[loc.category == c] for c in ("C", "X", "M"))
    ax.scatter(vil.x, vil.y, s=7, color="#4a5559", edgecolor="none", alpha=0.85, zorder=5)
    ax.scatter(sel.x, sel.y, s=34, color="white", edgecolor=INK, linewidth=1.1, zorder=6)
    ax.scatter(city.x, city.y, s=[120 if n == "Ужгород" else 62 for n in city.name], marker="s", color=INK, edgecolor="white", linewidth=1.0, zorder=7)
    cv.fig.canvas.draw()
    rend = cv.fig.canvas.get_renderer()
    taken = []
    cands = [(8, 0, "left", "center"), (-8, 0, "right", "center"), (0, 9, "center", "bottom"), (0, -9, "center", "top"),
             (7, 7, "left", "bottom"), (-7, 7, "right", "bottom"), (7, -7, "left", "top"), (-7, -7, "right", "top"),
             (16, 12, "left", "bottom"), (-16, 12, "right", "bottom"), (16, -12, "left", "top"), (-16, -12, "right", "top")]
    for row in pd.concat([city.assign(rank=0), sel.assign(rank=1)]).sort_values(["rank", "name"]).itertuples():
        big = row.category == "M"
        label = translit(row.name)
        for dx, dy, ha, va in cands:
            txt = ax.annotate(label, (row.x, row.y), xytext=(dx * (1.3 if big else 1), dy * (1.3 if big else 1)), textcoords="offset points",
                              ha=ha, va=va, fontsize=(11.5 if row.name == "Ужгород" else 9.5) if big else 8.0,
                              fontweight="bold" if big else "normal", style="normal" if big else "italic", color=INK if big else "#2b3539",
                              zorder=9, path_effects=HALO)
            txt.update_positions(rend)
            bb = mpl.text.Text.get_window_extent(txt, rend).expanded(1.04, 1.1)
            if not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                break
            txt.remove()
        else:
            print(f"  WARNING: no free label position for {label}")
    for row in pd.concat([city, sel]).itertuples():
        px, py = ax.transData.transform((row.x, row.y))
        taken.append(mpl.transforms.Bbox.from_extents(px - 6, py - 6, px + 6, py + 6))
    for _c, (name, g) in raions.items():
        label_txt = translit(name.split()[0]).upper() + "\nRAION"
        gp = proj(g).iloc[0]
        inner = gp.buffer(-6_000)
        if inner.is_empty:
            inner = gp
        centre = gp.representative_point()
        xs_ = np.arange(inner.bounds[0], inner.bounds[2], 2_500)
        ys_ = np.arange(inner.bounds[1], inner.bounds[3], 2_500)
        cc = sorted(((x, y) for x in xs_ for y in ys_ if inner.contains(Point(x, y))), key=lambda c: (c[0] - centre.x) ** 2 + (c[1] - centre.y) ** 2)
        for x, y in cc:
            txt = ax.text(x, y, label_txt, fontsize=10.5, color="#7a7365", ha="center", va="center", zorder=2.6, fontweight="bold", alpha=0.85, linespacing=1.1)
            bb = txt.get_window_extent(rend).expanded(1.05, 1.15)
            if not any(bb.overlaps(o) for o in taken):
                taken.append(bb)
                break
            txt.remove()
        else:
            print("  WARNING: no raion label position for", label_txt)
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 6.3)
    lg.text(0, 6.25, "Settlement status (KATOTTH)", va="top", fontsize=10.5, fontweight="bold")
    items = [("s", 120, INK, "white", f"Місто — city ({cats.get('M', 0)})", ), ("o", 34, "white", INK, f"Селище — settlement ({cats.get('X', 0)})"),
             ("o", 7, "#4a5559", "none", f"Село — village ({cats.get('C', 0)})")]
    y = 5.7
    for marker, size, face, edge, label in items:
        lg.scatter([0.2], [y], s=size, marker=marker, color=face, edgecolor=edge, linewidth=1.0)
        lg.text(0.5, y, label, va="center", fontsize=9.3)
        y -= 0.42
    y -= 0.12
    lg.text(0, y, "Boundaries", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    for colour, lw, style, label in (("#5d564a", 2.0, (0, (5, 2.5)), "State border"), ("#5d564a", 2.0, "-", "Oblast border"),
                                     ("#7d7668", 1.5, "-", f"Raion border ({n_r} raions)"), ("white", 2.2, "-", f"Hromada border ({n_h} hromadas)")):
        lg.plot([0, 0.4], [y, y], color=colour, linewidth=lw, linestyle=style, solid_capstyle="butt")
        if colour == "white":
            lg.add_patch(Rectangle((0, y - 0.09), 0.4, 0.18, facecolor="#e3ddcf", edgecolor="none", zorder=0))
        lg.text(0.5, y, label, va="center", fontsize=9.3)
        y -= 0.3
    y -= 0.25
    lg.text(0, y, "Located on OpenStreetMap", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    lines = [("KATOTTH settlements", f"{len(truth):,}")] + [(k, f"{v:,}") for k, v in how.items() if k != "unmatched"] + [("not found as a place node", f"{int(how.get('unmatched', 0)):,}")]
    for k, v in lines:
        lg.text(0, y, k[:34], va="center", fontsize=8.8)
        lg.text(col_w - 0.05, y, v, va="center", ha="right", fontsize=8.8)
        y -= 0.26
    if len(unmatched):
        note = "Not drawn: " + "; ".join(f"{translit(r.name)} ({r.name}), {translit(r.hromada)} hromada" for r in unmatched.itertuples()) + "."
        lg.text(0, y - 0.05, textwrap.fill(note, 40), va="top", fontsize=8.4, color=MUTED, linespacing=1.4)
    cv.footnote([
        f"Source: Кодифікатор адміністративно-територіальних одиниць та територій територіальних громад (KATOTTH), Ministry for Development of Communities and Territories of Ukraine, {KATOTTH_ORDER}. "
        f"Positions, boundaries: © OpenStreetMap contributors (ODbL 1.0), place nodes and admin relations as of {base[:10]}.",
        "Projection: transverse Mercator centred on 24.9°E. Names transliterated with Ukraine’s 2010 national system. Every city and settlement labelled; villages unlabelled. "
        "Since 2024 KATOTTH has no urban-type settlements (T): all were renamed селище (X). Hromada exclaves under 1 km² not outlined.",
    ], width=200)
    cv.save(map_id)
    return truth



# ==========================================================================
# 6. Solar power plants in Spain (OpenStreetMap power=plant, plant:source=solar)


def parse_mw(v):
    """MW from an OSM output tag. Unitless numbers are watts in OSM (5000 = 5 kW), which no
    plant is, so they are treated as untagged rather than guessed."""
    if v is None:
        return np.nan
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*(gw|mw|kw|w)[a-z]*\s*(;.*)?$", str(v), re.I)
    if not m:
        return np.nan
    x = float(m.group(1).replace(",", "."))
    return x * {"gw": 1000, "mw": 1, "kw": 0.001, "w": 1e-6}[m.group(2).lower()]


def build_spain_solar_plants() -> pd.DataFrame:
    map_id = "spain_solar_plants"
    payload = overpass(SOLAR_Q, "spain_solar_overpass.json")
    base = osm_base(payload)
    rows, dropped = [], 0
    for e in payload["elements"]:
        t = e.get("tags", {})
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if t.get("disused") == "yes" or t.get("construction") or t.get("proposed"):
            dropped += 1
            continue
        mw = parse_mw(t.get("plant:output:electricity"))
        rows.append({"osm": f"{e['type'][0]}{e['id']}", "name": t.get("name", ""), "lon": round(c["lon"], 5), "lat": round(c["lat"], 5),
                     "capacity_mw": mw, "capacity_tag": t.get("plant:output:electricity", ""),
                     "method": t.get("plant:method", ""), "operator": t.get("operator", "")})
    truth = pd.DataFrame(rows).sort_values(["osm"]).reset_index(drop=True)
    truth["method"] = truth.method.replace({"": "unspecified"})
    write_csv(map_id, truth)
    tagged = truth.capacity_mw.notna()
    pv = (truth.method == "photovoltaic")
    csp = (truth.method == "thermal")
    write_md(map_id, [
        f"- URL/API: Overpass API (overpass-api.de): `{compact(SOLAR_Q)}` (centre point of each way/relation; `power=plant` objects tagged `plant:source=solar` inside Spain's national boundary, so Canaries, Balearics, Ceuta and Melilla included).",
        f"- Vintage: OpenStreetMap as of {base}. Dropped {dropped} objects tagged disused/construction/proposed.",
        "- Licence: (c) OpenStreetMap contributors, ODbL 1.0. Land and provinces: Natural Earth (public domain).",
        f"- Rows: {len(truth):,} solar plants — osm (n/w/r + id), name, lon, lat (centre), capacity_mw (parsed from `plant:output:electricity`; blank when absent or not numeric, e.g. 'yes'), capacity_tag (raw), method (plant:method: photovoltaic {int(pv.sum()):,}, thermal = concentrated solar {int(csp.sum())}, unspecified {int((~pv & ~csp).sum())}), operator. {int(tagged.sum()):,} carry a numeric capacity, totalling {truth.capacity_mw.sum() / 1000:,.1f} GW (OSM, not an official register).",
        "- Caveat: OSM is incomplete and unevenly tagged; the sum of tagged capacity is well below official Spanish solar capacity (REE: tens of GW), because many plants are mapped as `power=generator` arrays instead, and rooftop systems are not plants. The spec's `count=900±60%` (360-1,440) is far below the OSM count of " + f"{len(truth):,}" + " (see REPORT.md).",
    ])
    crs = "EPSG:25830"
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326)
    can = pts[(pts.lon < -12)]
    main = pts[pts.lon >= -12]
    main_xy = main.to_crs(crs)
    ring = [(lo, la) for lo in np.linspace(-9.6, 4.6, 40) for la in (35.15, 44.0)] + [(lo, la) for la in np.linspace(35.15, 44.0, 40) for lo in (-9.6, 4.6)]
    xy = project_points(crs, ring)
    extent = (min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy))
    check_window(map_id, crs, extent, excludes=[[2.35, 48.85]])
    cv = Canvas(extent, "Solar power plants in Spain",
                f"{len(truth):,} plants mapped in OpenStreetMap · circle area proportional to capacity where tagged ({int(tagged.sum()):,} plants, {truth.capacity_mw.sum() / 1000:,.1f} GW), hollow rings where it is not",
                map_w=11.3, footer=1.2)
    ax = cv.ax
    world = H.world10()
    w = world.clip(box(-12, 33, 8, 47)).to_crs(crs)
    w[w.key != "ESP"].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    esp = w[w.key == "ESP"]
    esp.plot(ax=ax, color="#efece3", edgecolor="white", linewidth=0.5, zorder=1.1)
    adm = gpd.read_file(fetch(H.NE10_ADMIN1, "ne_10m_admin_1_states_provinces.zip"))
    prov = adm[adm.adm0_a3 == "ESP"].clip(box(-12, 34, 8, 47)).to_crs(crs)
    prov.boundary.plot(ax=ax, color="white", linewidth=0.55, zorder=1.2)
    H.local_graticule(ax, crs, range(-10, 6, 2), range(36, 45, 2), lon_span=(-12, 6), lat_span=(34, 46))
    k = 0.55   # points^2 per MW
    min_s = 5.0

    def draw(a, frame_xy, kk=k, lw=0.5):
        t_ = frame_xy[frame_xy.capacity_mw.notna()].sort_values("capacity_mw", ascending=False)
        u_ = frame_xy[frame_xy.capacity_mw.isna()]
        a.scatter(u_.geometry.x, u_.geometry.y, s=6, facecolor="none", edgecolor="#7d6a3a", linewidth=0.45, alpha=0.7, zorder=4)
        for meth, col in (("photovoltaic", "#f08a0c"), ("thermal", "#b5172b")):
            part = t_[t_.method == meth] if meth != "photovoltaic" else t_[t_.method != "thermal"]
            a.scatter(part.geometry.x, part.geometry.y, s=np.maximum(part.capacity_mw * kk, min_s), color=col, alpha=0.55,
                      edgecolor="white", linewidth=lw, zorder=5 if meth == "photovoltaic" else 6)
    draw(ax, main_xy)
    big = main_xy.sort_values("capacity_mw", ascending=False).head(60)
    sea_label(ax, crs, "Atlantic Ocean", -8.6, 36.4, 10)
    sea_label(ax, crs, "Mediterranean Sea", 1.6, 37.6, 10)
    sea_label(ax, crs, "Bay of Biscay", -4.6, 44.0, 8.5)
    for name, lon, lat in (("SPAIN", -3.4, 40.3), ("PORTUGAL", -8.0, 39.3), ("FRANCE", 2.2, 43.4), ("MOROCCO", -4.8, 35.45), ("ALGERIA", 2.4, 35.5), ("ANDORRA", 1.55, 42.55)):
        land_label(ax, crs, extent, name, lon, lat, 8.5 if name != "SPAIN" else 10)
    for nm, (lon, lat) in {"Madrid": (-3.70, 40.42), "Seville": (-5.98, 37.39), "Barcelona": (2.17, 41.39), "Valencia": (-0.38, 39.47), "Zaragoza": (-0.88, 41.65),
                           "Murcia": (-1.13, 37.98), "Badajoz": (-6.97, 38.88)}.items():
        (x, y), = project_points(crs, [(lon, lat)])
        ax.scatter([x], [y], s=14, marker="s", color=INK, edgecolor="white", linewidth=0.6, zorder=7)
        ax.annotate(nm, (x, y), xytext=(5, -9), textcoords="offset points", fontsize=8.3, color="#2b3539", zorder=9, path_effects=HALO)
    # label the very largest plants (names where OSM has them)
    top = truth.sort_values("capacity_mw", ascending=False).head(12)
    print(top[["osm", "name", "lon", "lat", "capacity_mw", "method"]].to_string())
    # Canary Islands inset
    ccrs = "EPSG:4083"
    can_xy = can.to_crs(ccrs)
    cb = (-18.5, -13.0, 27.4, 29.6)
    t2 = Transformer.from_crs(4326, ccrs, always_xy=True)
    c0 = t2.transform(cb[0], cb[2])
    c1 = t2.transform(cb[1], cb[3])
    col_x = cv.left + cv.map_w + 0.35
    col_w = WIDTH_IN - col_x - 0.25
    ih = col_w * (c1[1] - c0[1]) / (c1[0] - c0[0])
    top_in = cv.H - cv.header - 0.05
    inset = cv.add_axes(col_x, top_in - ih, col_w, ih, frame=True)
    inset.set_facecolor(WATER)
    cw = world.clip(box(-20, 26, -11, 31)).to_crs(ccrs)
    cw.plot(ax=inset, color="#efece3", edgecolor="white", linewidth=0.5, zorder=1)
    draw(inset, can_xy, kk=k * 2.2, lw=0.6)
    inset.set_xlim(c0[0], c1[0])
    inset.set_ylim(c0[1], c1[1])
    inset.text(0.02, 0.97, f"Canary Islands (inset, circles ×2.2)\n{len(can)} plants", transform=inset.transAxes, va="top", fontsize=8.8, fontweight="bold", path_effects=HALO, zorder=10, linespacing=1.2)
    bal = pts[(pts.lon > 1.0) & (pts.lat > 38.4) & (pts.lat < 40.2)]
    # right column legend
    lg_h = 6.0
    y0 = top_in - ih - 0.25
    lg = cv.panel(col_x, y0, col_w, lg_h)
    lg.text(0, lg_h - 0.02, "Capacity (circle area)", va="top", fontsize=10.5, fontweight="bold")
    y = lg_h - 0.55
    for mw in (10, 50, 200, 500):
        d = (max(mw * k, min_s) / np.pi) ** 0.5 * 2 / 72
        lg.add_patch(Circle((0.45, y), d / 2, facecolor="#f08a0c", alpha=0.6, edgecolor="white"))
        lg.text(1.0, y, f"{mw} MW", va="center", fontsize=9.2)
        y -= max(d, 0.2) + 0.14
    y -= 0.05
    lg.scatter([0.45], [y], s=18, facecolor="none", edgecolor="#7d6a3a", linewidth=0.7)
    lg.text(1.0, y, "capacity not tagged", va="center", fontsize=9.2)
    y -= 0.4
    lg.text(0, y, "Technology", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.42
    for col, lab in (("#f08a0c", f"Photovoltaic and unspecified ({int((~csp).sum()):,})"), ("#b5172b", f"Thermal, concentrated solar ({int(csp.sum())})")):
        lg.scatter([0.45], [y], s=70, color=col, alpha=0.6, edgecolor="white")
        lg.text(1.0, y, lab, va="center", fontsize=8.9)
        y -= 0.32
    y -= 0.15
    lg.text(0, y, "Largest tagged plants (MW)", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.36
    for r in truth.sort_values("capacity_mw", ascending=False).head(8).itertuples():
        nm = (r.name or f"unnamed ({r.lat:.2f}N {abs(r.lon):.2f}W)")[:24]
        lg.text(0, y, nm, va="top", fontsize=8.5)
        lg.text(col_w - 0.03, y, f"{r.capacity_mw:,.0f}", va="top", ha="right", fontsize=8.5)
        y -= 0.21
    cv.footnote([
        f"Source: (c) OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {base[:10]}: power=plant with plant:source=solar inside Spain; capacity from plant:output:electricity where numeric. Land and provinces: Natural Earth 1:10m. OSM is not an official register: capacity is tagged for {tagged.mean() * 100:.0f}% of plants.",
        f"Projection: ETRS89 / UTM zone 30N (EPSG:25830); Canary Islands inset in REGCAN95 / UTM 28N (EPSG:4083). All {len(truth):,} plants shown ({len(bal)} on the Balearic Islands, drawn in place; {len(can)} in the Canaries inset); circle area proportional to MW, minimum size for plants under about 9 MW. "
        f"Excluded: {dropped} objects tagged disused, under construction or proposed. Rooftop systems and arrays mapped as power=generator are not plants and are not shown.",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# 7. Atlantic hurricane season 2017 (NOAA IBTrACS v04r01, NA basin)

IBTRACS_NA = ("https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/"
              "v04r01/access/csv/ibtracs.NA.list.v04r01.csv")
SSHS = [  # (min USA_SSHS, colour, label)
    (-1, "#7fb3d5", "Tropical depression (< 34 kt)"),
    (0, "#3b9a8f", "Tropical storm (34–63 kt)"),
    (1, "#f2d24b", "Category 1 (64–82 kt)"),
    (2, "#f5a03a", "Category 2 (83–95 kt)"),
    (3, "#e4572e", "Category 3 (96–112 kt)"),
    (4, "#b5172b", "Category 4 (113–136 kt)"),
    (5, "#5e0d3b", "Category 5 (≥ 137 kt)"),
]
NE_STATES_URL = f"{B.NE}/50m_cultural/ne_50m_admin_1_states_provinces_lines.zip"


def sshs_colour(cat: int) -> str:
    return next((c for lo, c, _ in reversed(SSHS) if cat >= lo), SSHS[0][1])


def build_atlantic_hurricanes_2017() -> pd.DataFrame:
    map_id = "atlantic_hurricanes_2017"
    path = fetch(IBTRACS_NA, "ibtracs_NA_v04r01.csv")
    raw = pd.read_csv(path, skiprows=[1], dtype=str, keep_default_na=False)
    season = raw[(raw.SEASON == "2017") & (raw.NAME != "UNNAMED")].copy()
    unnamed = raw[(raw.SEASON == "2017") & (raw.NAME == "UNNAMED")].SID.nunique()

    def num(col):
        return pd.to_numeric(season[col].str.strip().replace("", np.nan), errors="coerce")

    truth = pd.DataFrame({
        "sid": season.SID, "name": season.NAME.str.title(), "iso_time": season.ISO_TIME,
        "lon": num("LON"), "lat": num("LAT"), "nature": season.NATURE,
        "wind_kt": num("USA_WIND").astype("Int64"), "pres_mb": num("USA_PRES").astype("Int64"),
        "sshs": num("USA_SSHS").astype("Int64"), "usa_record": season.USA_RECORD.str.strip(),
    }).sort_values(["sid", "iso_time"]).reset_index(drop=True)
    write_csv(map_id, truth)
    storms = truth.groupby("sid", sort=False).agg(name=("name", "first"), start=("iso_time", "first"), peak=("wind_kt", "max"),
                                                  cat=("sshs", "max"), n=("lon", "size")).reset_index().sort_values("start")
    n_storms = len(storms)
    hurricanes = int((storms.cat >= 1).sum())
    majors = int((storms.cat >= 3).sum())
    last_mod = pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d")
    write_md(map_id, [
        f"- URL/API: {IBTRACS_NA} (NOAA NCEI IBTrACS v04r01, North Atlantic basin file); rows with SEASON = 2017 and NAME != UNNAMED.",
        f"- Vintage: IBTrACS v04r01 as downloaded {last_mod}; 2017 tracks are NHC final best tracks. Positions are IBTrACS LAT/LON, intensity the USA (NHC) agency columns.",
        "- Licence: IBTrACS is a NOAA product, U.S. government work (public domain); cite Knapp et al. (2010) BAMS 91:363 and Gahtan et al. (2024) IBTrACS v04r01, NOAA NCEI, doi:10.25921/82ty-9e16. Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} track fixes (3-hourly, incl. NHC landfall/intensity special records) of {n_storms} named storms (Arlene to Rina) — sid, name, iso_time (UTC), lon, lat, nature (TS tropical, ET extratropical, DS disturbance, SS subtropical), wind_kt, pres_mb, sshs (-5..5; -1 = depression, 0 = tropical storm), usa_record (L = landfall, I = intensity peak, R = rapid change). `count` = distinct sid.",
        f"- Caveat: {hurricanes} hurricanes, {majors} major (Cat 3+: Harvey, Irma, Jose, Lee, Maria, Ophelia). {unnamed} unnamed 2017 systems (never named by NHC) are excluded. Harvey's Rockport landfall is the IBTrACS record at 28.2N 97.1W, 26 Aug 06:00 UTC (25 Aug local). Tracks include pre-genesis disturbance and post-tropical stages (drawn dashed grey).",
    ])
    crs = "+proj=lcc +lat_1=20 +lat_2=45 +lat_0=30 +lon_0=-55 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    xs, ys = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    tro = truth.nature.isin(["TS", "SS"]).to_numpy()   # frame: the tropical and subtropical fixes; post-tropical tails run off the edge
    xmin, xmax = xs[tro].min() - 380_000, xs[tro].max() + 150_000
    ymin, ymax = ys[tro].min() - 260_000, ys[tro].max() + 120_000
    extent = (xmin, xmax, ymin, ymax)
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "Tracks of the 2017 Atlantic hurricane season",
                f"All {n_storms} named storms, coloured by Saffir–Simpson category along the track · {hurricanes} became hurricanes, {majors} major hurricanes · "
                "Harvey, Irma and Maria named",
                footer=1.7, map_w=15.2)
    ax = cv.ax
    frame = box(xmin, ymin, xmax, ymax)
    world = H.world10().clip(box(-130, -8, 40, 76)).to_crs(crs)
    world.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    states = gpd.read_file(fetch(NE_STATES_URL, "ne_50m_admin1_lines.zip"))
    states = states[states.ADM0_A3.isin(["USA", "MEX", "CAN"])].clip(box(-130, 5, -50, 60)).to_crs(crs)
    states.plot(ax=ax, color="white", linewidth=0.45, zorder=1.1)
    for lon in range(-110, 21, 10):
        lats = np.linspace(0, 70, 141)
        gx, gy = t.transform(np.full_like(lats, lon), lats)
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
    for lat in range(10, 61, 10):
        lons = np.linspace(-120, 20, 281)
        gx, gy = t.transform(lons, np.full_like(lons, lat))
        ax.plot(gx, gy, color="white", linewidth=0.5, alpha=0.8, zorder=0.5)
        lx, ly = t.transform(-17.5, lat)
        if frame.contains(Point(lx, ly)):
            ax.text(lx, ly + 15_000, f"{lat}°N", fontsize=7.5, color="#7c97a1", ha="center", va="bottom", zorder=0.6)
    truth["x"], truth["y"] = xs, ys
    for sid in storms.sort_values("cat").sid:   # strongest drawn last
        g = truth[truth.sid == sid]
        pts = g[["x", "y"]].to_numpy()
        tropical = g.nature.isin(["TS", "SS"]).to_numpy()
        cats = g.sshs.fillna(-1).astype(int).to_numpy()
        seg_cat = np.maximum(cats[:-1], cats[1:])
        segs = np.stack([pts[:-1], pts[1:]], axis=1)
        trop = tropical[:-1] & tropical[1:]
        lw = np.where(seg_cat >= 3, 3.0, np.where(seg_cat >= 1, 2.4, 1.6))
        ax.add_collection(LineCollection(segs[~trop], colors="#8a8f93", linewidths=1.0, linestyles=(0, (2.2, 1.6)), zorder=3, capstyle="round"))
        ax.add_collection(LineCollection(segs[trop], colors="white", linewidths=lw[trop] + 1.6, zorder=3.5, capstyle="round"))
        ax.add_collection(LineCollection(segs[trop], colors=[sshs_colour(c) for c in seg_cat[trop]], linewidths=lw[trop], zorder=4, capstyle="round"))
    landfalls = truth[truth.usa_record == "L"]
    ax.scatter(landfalls.x, landfalls.y, s=18, facecolor="white", edgecolor=INK, linewidth=0.9, zorder=7)
    notes = {  # (storm, landfall UTC date, hour) -> (place, dx, dy)
        ("Harvey", "2017-08-26", "06"): ("Rockport, Texas", 28, -52),
        ("Harvey", "2017-08-30", "08"): ("Cameron, Louisiana", 12, 52),
        ("Irma", "2017-09-06", "05"): ("Barbuda", 130, 52),
        ("Irma", "2017-09-10", "13"): ("Cudjoe Key, Florida", 90, -32),
        ("Maria", "2017-09-19", "01"): ("Dominica", -90, -22),
        ("Maria", "2017-09-20", "10"): ("Yabucoa, Puerto Rico", -150, 18),
    }
    for row in landfalls.itertuples():
        key = (row.name, row.iso_time[:10], row.iso_time[11:13])
        if key not in notes:
            continue
        place, dx, dy = notes[key]
        when = pd.Timestamp(row.iso_time).strftime("%-d %b")
        cat = f"Cat {row.sshs}" if row.sshs >= 1 else "TS"
        ax.annotate(f"{row.name} landfall, {when}\n{place} · {cat}, {row.wind_kt} kt", (row.x, row.y), xytext=(dx, dy), textcoords="offset points",
                    ha="center" if dx == 0 else ("left" if dx > 0 else "right"), va="center", fontsize=7.9, color="#2b3539", zorder=8, path_effects=HALO,
                    linespacing=1.15, arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.5, shrinkA=0, shrinkB=3))
    for name, lon, lat in (("UNITED STATES", -98, 38.0), ("MEXICO", -102.5, 23.5), ("CANADA", -75, 51.5), ("CUBA", -79.0, 21.9),
                           ("AFRICA", -6, 17), ("EUROPE", 8, 46), ("IRELAND", -8.0, 53.2), ("COLOMBIA", -73.5, 4.5), ("VENEZUELA", -66, 7.5)):
        (x, y), = project_points(crs, [(lon, lat)])
        if frame.contains(Point(x, y)):
            ax.text(x, y, name, fontsize=9, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("North Atlantic Ocean", -36, 27.5), ("Gulf of Mexico", -88.6, 26.0), ("Caribbean Sea", -73.5, 14.5)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", va="center", zorder=2)
    # storm names: greedy placement beside each track, three named storms larger
    cv.fig.canvas.draw()
    rend = cv.fig.canvas.get_renderer()
    taken = []
    for lab in ax.texts:
        taken.append(mpl.text.Text.get_window_extent(lab, rend).expanded(1.05, 1.2))
    big = {"Harvey", "Irma", "Maria"}
    order_names = list(storms.sort_values("peak", ascending=False).name)
    for nm in order_names:
        g = truth[(truth.name == nm) & truth.nature.isin(["TS", "SS"])]
        if len(g) < 3:
            g = truth[truth.name == nm]
        fr = [0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9, 0.05, 0.95]
        placed = False
        for f in fr:
            i = min(int(f * (len(g) - 1)), len(g) - 1)
            px, py = g.x.iloc[i], g.y.iloc[i]
            if not (frame.contains(Point(px, py))):
                continue
            for dx, dy in [(0, 11), (0, -11), (12, 0), (-12, 0), (10, 10), (-10, 10), (10, -10), (-10, -10), (0, 18), (0, -18), (20, 0), (-20, 0)]:
                a = ax.annotate(nm, (px, py), xytext=(dx, dy), textcoords="offset points", ha="left" if dx > 4 else ("right" if dx < -4 else "center"),
                                va="bottom" if dy > 4 else ("top" if dy < -4 else "center"), fontsize=12.5 if nm in big else 8.6,
                                fontweight="bold" if nm in big else "normal", color=INK, zorder=9, path_effects=HALO)
                a.update_positions(rend)
                bb = mpl.text.Text.get_window_extent(a, rend).expanded(1.04, 1.15)
                if not any(bb.overlaps(o) for o in taken) and ax.bbox.padded(-3).contains(bb.x0, bb.y0) and ax.bbox.padded(-3).contains(bb.x1, bb.y1):
                    taken.append(bb)
                    placed = True
                    break
                a.remove()
            if placed:
                break
        if not placed:
            print("  no label position for", nm)
    lg = cv.panel(cv.left, 1.55, 15.2, 0.72)
    lg.text(0, 0.70, "Saffir–Simpson category at that point of the track (1-min sustained wind, NHC)", va="top", fontsize=10.5, fontweight="bold")
    x = 0
    for _lo, colour, label in SSHS:
        lg.plot([x, x + 0.42], [0.2, 0.2], color=colour, linewidth=3.2, solid_capstyle="butt")
        lg.text(x + 0.5, 0.2, label.replace(" (", "\n("), va="center", fontsize=8.4, linespacing=1.1)
        x += 1.92
    lg.plot([x, x + 0.42], [0.2, 0.2], color="#8a8f93", linewidth=1.0, linestyle=(0, (2.2, 1.6)))
    lg.text(x + 0.5, 0.2, "Before genesis /\npost-tropical", va="center", fontsize=8.4, linespacing=1.1)
    lg.scatter([x + 2.0], [0.2], s=18, facecolor="white", edgecolor=INK, linewidth=0.9)
    lg.text(x + 2.15, 0.2, "NHC landfall", va="center", fontsize=8.4)
    cv.footnote([
        f"Source: NOAA NCEI, International Best Track Archive for Climate Stewardship (IBTrACS) v04r01, North Atlantic file, season 2017 (NHC best tracks), retrieved {last_mod}; public domain (Knapp et al. 2010; Gahtan et al. 2024). Land: Natural Earth 1:10m, public domain.",
        f"Projection: Lambert conformal conic, standard parallels 20°N and 45°N, centred on 55°W. All {n_storms} named storms shown, {len(truth)} 3-hourly fixes; line colour and width = NHC Saffir–Simpson category of each segment. "
        f"Excluded: {unnamed} unnamed disturbances that never received a name. Landfall points from NHC reports (UTC dates).",
    ], width=205, y_in=0.16)
    cv.save(map_id)
    return truth


# ==========================================================================
# 8. Latin America and the Caribbean: fertility rate (OWID / UN WPP)

OWID_FERT_URL = "https://ourworldindata.org/grapher/children-born-per-woman.csv"   # the 'fertility-rate' slug of the spec now 404s
LAC = {  # UN M49 region 419 by sub-region
    "Caribbean": "AIA ATG ABW BHS BRB BES VGB CYM CUB CUW DMA DOM GRD GLP HTI JAM MTQ MSR PRI BLM KNA LCA MAF VCT SXM TTO TCA VIR",
    "Central America": "BLZ CRI SLV GTM HND MEX NIC PAN",
    "South America": "ARG BOL BVT BRA CHL COL ECU FLK GUF GUY PRY PER SGS SUR URY VEN",
}
LAC_CODES = [c for v in LAC.values() for c in v.split()]
LAC_SHORT = {"Saint Barthelemy": "St Barthélemy", "British Virgin Islands": "British Virgin Is.", "Bonaire Sint Eustatius and Saba": "Bonaire, St Eustatius, Saba",
             "Saint Vincent and the Grenadines": "St Vincent & Grenadines", "Saint Kitts and Nevis": "St Kitts & Nevis", "Antigua and Barbuda": "Antigua & Barbuda",
             "Sint Maarten (Dutch part)": "Sint Maarten", "Saint Martin (French part)": "St Martin", "Turks and Caicos Islands": "Turks & Caicos", "Trinidad and Tobago": "Trinidad & Tobago",
             "United States Virgin Islands": "US Virgin Is.", "Dominican Republic": "Dominican Rep.", "Saint Lucia": "St Lucia"}


def build_latin_america_fertility() -> pd.DataFrame:
    map_id = "latin_america_fertility"
    raw = pd.read_csv(fetch(OWID_FERT_URL, "owid_fertility.csv"))
    truth = owid_latest(raw, LAC_CODES)
    truth.insert(3, "unit", "children per woman")
    sub = {c: r for r, v in LAC.items() for c in v.split()}
    truth["subregion"] = truth.key.map(sub)
    absent = sorted(set(LAC_CODES) - set(truth.key))
    write_csv(map_id, truth)
    yrs = truth.year.value_counts().to_dict()
    write_md(map_id, [
        f"- URL/API: {OWID_FERT_URL} (Our World in Data Grapher CSV, 'Fertility rate: births per woman' = total fertility rate; Human Fertility Database / UN World Population Prospects 2024; the spec's slug `fertility-rate` now returns 404, `children-born-per-woman` serves the same series); latest non-missing year per country; geometry {H.NE10_COUNTRIES} and the 1:10m map-units file (to colour French Guiana, Guadeloupe, Martinique separately).",
        f"- Vintage: OWID file downloaded {NOW}; every row is {', '.join(f'{y} ({n})' for y, n in yrs.items())} (UN WPP 2024 estimates, no projections in the file).",
        "- Licence: OWID data CC BY 4.0 (UN WPP: CC BY 3.0 IGO). Boundaries: Natural Earth 1:10m (public domain).",
        f"- Rows: {len(truth)} of the {len(LAC_CODES)} members of UN M49 region 419 'Latin America and the Caribbean' (chosen by an explicit ISO3 list: Caribbean, Central America incl. Mexico, South America). No OWID value: {', '.join(absent)} (uninhabited Bouvet Island and South Georgia). Columns: key (ISO3), name, value (children per woman), unit, year, subregion.",
        "- Caveat: the list contains 17 dependencies (e.g. Puerto Rico, Guadeloupe, French Guiana, Aruba) that OWID reports with their own UN WPP estimate. Spec spot 'GTM>KOR': South Korea is not in the region, so it is unevaluable against this truth.csv; OWID's own value (2023) is KOR 0.72 against Guatemala 2.31, so it holds.",
    ])
    crs = "+proj=laea +lat_0=-8 +lon_0=-70 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    units = gpd.read_file(fetch(H.NE50_UNITS.replace("50m_cultural/ne_50m_admin_0_map_units", "10m_cultural/ne_10m_admin_0_map_units"), "ne_10m_admin_0_map_units.zip"))
    eh = units["ISO_A3_EH"].fillna("")
    units["key"] = np.where(eh.str.fullmatch(r"[A-Z]{3}"), eh, units["ADM0_A3"])
    units["key"] = units.key.replace({"SOL": "SOM", "CYN": "CYP"})
    lac = units[units.key.isin(LAC_CODES)].merge(truth[["key", "value", "name"]], on="key", how="left")
    ring = [(lo, la) for lo in np.linspace(-118.8, -33.3, 60) for la in (-56.6, 33.2)] + [(lo, la) for la in np.linspace(-56.6, 33.2, 60) for lo in (-118.8, -33.3)]
    xy = [t.transform(*p) for p in ring]
    extent = (min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy))
    check_window(map_id, crs, extent)
    edges = [1.2, 1.5, 1.8, 2.1, 2.5]
    colors = ramp("PuRd", 6, 0.1, 0.92)
    lo, hi = truth.sort_values("value").iloc[0], truth.sort_values("value").iloc[-1]
    cv = Canvas(extent, "Fertility rate across Latin America and the Caribbean",
                f"Children per woman, {int(truth.year.max())} · UN WPP 2024 via Our World in Data · {len(truth)} countries and territories · "
                f"{lo['name']} {lo.value:.1f} to {hi['name']} {hi.value:.1f}",
                map_w=8.6, footer=1.25)
    ax = cv.ax
    ctx = units[~units.key.isin(LAC_CODES)].clip(box(-125, -60, -25, 45)).to_crs(crs)
    ctx.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.45, zorder=1)
    H.local_graticule(ax, crs, range(-120, -29, 10), range(-50, 41, 10), lon_span=(-125, -25), lat_span=(-60, 45))
    lac_c = lac.clip(box(-125, -60, -25, 45)).to_crs(crs)
    plot_classes(ax, lac_c.dropna(subset=["value"]), "value", edges, colors, linewidth=0.5)
    # island markers (so every small state is a visible, coloured mark)
    small = ["ATG", "ABW", "BRB", "CUW", "DMA", "GRD", "KNA", "LCA", "VCT", "TTO", "MSR", "AIA", "BLM", "MAF", "SXM", "VGB", "VIR", "TCA", "CYM", "BES", "GLP", "MTQ"]
    byk = truth.set_index("key")
    cl = lambda v: int(classify(pd.Series([v]), edges).iloc[0])
    cent = lac_c.copy()
    cent["pt"] = cent.geometry.representative_point()
    markers = cent[cent.key.isin(small)]

    def mark(a, size, lw):
        for r in markers.itertuples():
            x, y = r.pt.x, r.pt.y
            a.scatter([x], [y], s=size, color=colors[cl(r.value)], edgecolor=INK, linewidth=lw, zorder=6)
    mark(ax, 22, 0.6)
    # (lon, lat of the unit's label anchor, text lon, text lat, ha); text position None = the text sits on the unit
    labels_main = {
        "MEX": (-102.0, 23.6, None, None, "center"), "BRA": (-53.0, -11.0, None, None, "center"), "ARG": (-65.0, -35.5, None, None, "center"),
        "COL": (-73.2, 3.6, None, None, "center"), "PER": (-74.5, -9.5, None, None, "center"), "VEN": (-65.8, 7.0, None, None, "center"),
        "BOL": (-64.6, -16.7, None, None, "center"), "PRY": (-58.4, -23.3, None, None, "center"),
        "ECU": (-78.8, -1.4, -86.0, -1.4, "right"), "URY": (-55.9, -32.8, -50.5, -35.3, "left"), "CHL": (-71.0, -33.0, -82.0, -28.0, "right"),
        "GUY": (-58.9, 4.8, -50.0, 9.3, "left"), "SUR": (-55.9, 4.0, -50.0, 7.7, "left"), "GUF": (-53.1, 3.9, -50.0, 6.1, "left"),
        "GTM": (-90.6, 15.2, -96.5, 14.2, "right"), "SLV": (-88.9, 13.7, -96.5, 12.2, "right"), "NIC": (-85.3, 12.8, -96.5, 10.2, "right"),
        "CRI": (-84.2, 10.0, -96.5, 8.2, "right"), "PAN": (-80.1, 8.5, -96.5, 6.2, "right"),
        "HND": (-86.6, 14.9, -84.0, 17.6, "center"), "BLZ": (-88.7, 17.2, -93.5, 18.3, "right"),
        "CUB": (-79.0, 21.9, -79.0, 23.6, "center"), "JAM": (-77.3, 18.15, -78.4, 16.4, "right"), "HTI": (-73.0, 19.0, -73.6, 16.8, "center"),
        "DOM": (-70.5, 19.0, -70.4, 21.6, "right"), "PRI": (-66.5, 18.2, -66.6, 21.6, "left"), "BHS": (-77.5, 24.8, -76.0, 26.3, "center"),
        "FLK": (-59.5, -51.7, -52.5, -49.5, "left"),
    }
    for k, (lon, lat, tlon, tlat, ha) in labels_main.items():
        (x, y), = project_points(crs, [(lon, lat)])
        v = byk.loc[k, "value"]
        nm = LAC_SHORT.get(byk.loc[k, "name"], byk.loc[k, "name"])
        if tlon is None:
            c = cl(v)
            big = k in ("BRA", "MEX", "ARG")
            ax.text(x, y, f"{nm}\n{v:.2f}", fontsize=10 if big else 8.6, fontweight="bold" if big else "normal", ha="center", va="center", zorder=8,
                    color="white" if c >= 4 else INK, linespacing=1.2)
        else:
            (tx, ty), = project_points(crs, [(tlon, tlat)])
            ax.annotate(f"{nm} {v:.2f}", (x, y), xytext=(tx, ty), textcoords="data", ha=ha, va="center", fontsize=8.4, color=INK, zorder=9, path_effects=HALO,
                        arrowprops=dict(arrowstyle="-", color="#5d6a70", linewidth=0.6, shrinkA=1, shrinkB=1))
    for name, lon, lat, sz in (("Pacific Ocean", -102, -28, 11), ("Atlantic Ocean", -40.5, -30, 11), ("Caribbean Sea", -74.7, 14.4, 8.5), ("Gulf of Mexico", -90.5, 25.4, 8.5)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=sz, color=SEA, style="italic", ha="center", va="center", zorder=2)
    for name, lon, lat in (("UNITED STATES", -104, 31.5), ):
        land_label(ax, crs, extent, name, lon, lat, 8.5)
    # Lesser Antilles inset in the empty Pacific
    zb = (-71.6, -54.0, 9.4, 19.6)
    zx0, zy0 = t.transform(zb[0], zb[2])
    zx1, zy1 = t.transform(zb[1], zb[3])
    iw = 3.6
    ih = iw * (zy1 - zy0) / (zx1 - zx0)
    inset = cv.add_axes(cv.left + 0.12, cv.footer + 0.12, iw, ih, frame=True)
    inset.set_facecolor(WATER)
    ctx.plot(ax=inset, color=LAND, edgecolor="white", linewidth=0.4, zorder=1)
    lac_c.dropna(subset=["value"]).pipe(lambda g: plot_classes(inset, g, "value", edges, colors, linewidth=0.5))
    mark(inset, 55, 0.8)
    inset.set_xlim(zx0, zx1)
    inset.set_ylim(zy0, zy1)
    bx = [(zx0, zy0), (zx1, zy0), (zx1, zy1), (zx0, zy1), (zx0, zy0)]
    ax.plot([p[0] for p in bx], [p[1] for p in bx], color=INK, linewidth=1.0, zorder=7)
    inset.text(0.02, 0.985, "Lesser Antilles (detail)", transform=inset.transAxes, va="top", fontsize=8.6, fontweight="bold", path_effects=HALO, zorder=10)
    chain = {"VGB": (-64.6, 18.45), "VIR": (-64.8, 18.3), "AIA": (-63.05, 18.22), "SXM": (-63.06, 18.04), "BLM": (-62.85, 17.9), "KNA": (-62.7, 17.3),
             "ATG": (-61.8, 17.1), "MSR": (-62.2, 16.74), "GLP": (-61.55, 16.2), "DMA": (-61.35, 15.4), "MTQ": (-61.0, 14.65), "LCA": (-60.98, 13.9),
             "VCT": (-61.2, 13.25), "BRB": (-59.55, 13.18), "GRD": (-61.68, 12.12), "TTO": (-61.3, 10.45)}
    n = len(chain)
    y_top, y_bot = 18.9, 10.3
    for i, (k, (lon, lat)) in enumerate(chain.items()):
        (x, y), = project_points(crs, [(lon, lat)])
        (tx, ty), = project_points(crs, [(-57.7, y_top - i * (y_top - y_bot) / (n - 1))])
        v = byk.loc[k, "value"]
        nm = LAC_SHORT.get(byk.loc[k, "name"], byk.loc[k, "name"])
        inset.annotate(f"{nm} {v:.2f}", (x, y), xytext=(tx, ty), textcoords="data", ha="left", va="center", fontsize=7.2, color=INK, zorder=9, path_effects=HALO,
                       arrowprops=dict(arrowstyle="-", color="#5d6a70", linewidth=0.5, shrinkA=0, shrinkB=2))
    for k, (lon, lat, dx, dy, ha) in {"ABW": (-70.0, 12.5, -2, 11, "center"), "CUW": (-69.0, 12.15, 0, -12, "center"), "BES": (-68.25, 12.15, 6, 11, "left")}.items():
        (x, y), = project_points(crs, [(lon, lat)])
        v = byk.loc[k, "value"]
        nm = {"BES": "Bonaire etc."}.get(k, LAC_SHORT.get(byk.loc[k, "name"], byk.loc[k, "name"]))
        inset.annotate(f"{nm} {v:.2f}", (x, y), xytext=(dx, dy), textcoords="offset points", ha=ha, va="center", fontsize=7.2, color=INK, zorder=9, path_effects=HALO)
    for name, lon, lat in (("VENEZUELA", -66.5, 9.9), ):
        pass
    # right column: legend and all-values table
    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    _lg, ybot = side_legend(cv, col_x, cv.H - cv.header - 0.1, 3.2, "Children per woman\n(total fertility rate)", colors,
                            ["< 1.2", "1.2–1.5", "1.5–1.8", "1.8–2.1 (just below replacement)", "2.1–2.5", "≥ 2.5"], extra=[(LAND, "Outside the region (not mapped)")])
    rk = truth.sort_values("value", ascending=False).reset_index(drop=True)
    half = (len(rk) + 1) // 2
    h = 0.42 + 0.192 * half
    tb = cv.panel(col_x, cv.H - cv.header - 3.75, col_w, h)
    tb.text(0, h - 0.02, "All 50 ranked, children per woman", va="top", fontsize=10.5, fontweight="bold")
    cw = col_w / 2
    for i, r in enumerate(rk.itertuples()):
        c, k_ = divmod(i, half)
        y = h - 0.4 - k_ * 0.192
        tb.add_patch(Rectangle((c * cw, y - 0.145), 0.11, 0.13, facecolor=colors[cl(r.value)], edgecolor="none"))
        tb.text(c * cw + 0.16, y, LAC_SHORT.get(r.name, r.name)[:20], va="top", fontsize=7.8)
        tb.text(c * cw + cw - 0.12, y, f"{r.value:.2f}", va="top", ha="right", fontsize=7.8)
    cv.footnote([
        f"Source: Our World in Data, 'Fertility rate' (UN World Population Prospects 2024; Human Fertility Database; CC BY 4.0), {int(truth.year.max())}. Boundaries: Natural Earth 1:10m map units (public domain).",
        "Projection: Lambert azimuthal equal-area centred on 8°S, 70°W. Classification: 6 manual classes (steps of 0.3, finest at the low end, class edge at replacement level 2.1). All 50 UN M49 countries and territories with a value shown; none excluded "
        "(Bouvet Island and South Georgia have no data and are uninhabited). Coloured dots mark islands too small to colour; the inset enlarges the Lesser Antilles.",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# 9. Spain: unemployment rate by NUTS 2 region (Eurostat lfst_r_lfu3rt)

ES_NUTS2 = ["ES11", "ES12", "ES13", "ES21", "ES22", "ES23", "ES24", "ES30", "ES41", "ES42", "ES43", "ES51", "ES52", "ES53", "ES61", "ES62", "ES63", "ES64", "ES70"]
NUTS2_URL = ("https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2024_4326_LEVL_2.geojson")
ES_SHORT = {"ES11": "Galicia", "ES12": "Asturias", "ES13": "Cantabria", "ES21": "País Vasco", "ES22": "Navarra", "ES23": "La Rioja", "ES24": "Aragón",
            "ES30": "Madrid", "ES41": "Castilla y León", "ES42": "Castilla-La Mancha", "ES43": "Extremadura", "ES51": "Cataluña", "ES52": "C. Valenciana",
            "ES53": "Illes Balears", "ES61": "Andalucía", "ES62": "Murcia", "ES63": "Ceuta", "ES64": "Melilla", "ES70": "Canarias"}


def build_spain_unemployment_regions() -> pd.DataFrame:
    map_id = "spain_unemployment_regions"
    url = eurostat_url("lfst_r_lfu3rt", "sex=T&age=Y15-74&unit=PC&isced11=TOTAL", ES_NUTS2 + ["ES"])
    data, payload = eurostat_frame(fetch(url, "eurostat_lfst_r_lfu3rt_spain.json"))
    shapes = gpd.read_file(fetch(NUTS2_URL, "nuts2_2024_01m_4326.geojson"))
    es = shapes[shapes.CNTR_CODE == "ES"].copy()
    if sorted(es.NUTS_ID) != sorted(ES_NUTS2):
        raise ValueError("GISCO Spain NUTS 2 list differs")
    reg = data[data.geo.isin(ES_NUTS2)].sort_values(["geo", "time"])
    latest = reg.groupby("geo", as_index=False).tail(1)
    prev = reg.groupby("geo", as_index=False).nth(-2).set_index("geo").value
    year = int(latest.time.max())
    if latest.time.nunique() != 1 or len(latest) != 19:
        raise ValueError("regions differ in latest year or are missing")
    nat = float(data[(data.geo == "ES") & (data.time == year)].value.iloc[0])
    names = es.set_index("NUTS_ID").NAME_LATN.str.strip().to_dict()
    truth = pd.DataFrame({"key": latest.geo, "name": latest.geo.map(names), "value": latest.value, "unit": "percent of the labour force aged 15-74",
                          "year": latest.time, "value_previous_year": latest.geo.map(prev)}).sort_values("key").reset_index(drop=True)
    write_csv(map_id, truth)
    hi, lo = truth.sort_values("value").iloc[-1], truth.sort_values("value").iloc[0]
    write_md(map_id, [
        f"- URL/API: {url} (Eurostat dissemination API 1.0, JSON-stat, lfst_r_lfu3rt 'Unemployment rates by educational attainment level and NUTS 2 region', all attainment levels (isced11=TOTAL), both sexes, 15-74 years, percentage); geometry {NUTS2_URL}.",
        f"- Vintage: annual average {year} (dataset updated {payload.get('updated', '?')[:10]}); Spain as a whole {nat:.1f}%. All 19 regions share the latest year {year}; `value_previous_year` is {year - 1}. Geometry GISCO NUTS 2024, 1:1M (Spain's NUTS 2 list is the same as in 2021).",
        "- Licence: Eurostat data, free reuse with attribution (Commission Decision 2011/833/EU); boundaries (c) EuroGeographics for the administrative boundaries (GISCO). Land: Natural Earth (public domain).",
        f"- Rows: 19 NUTS 2 regions (the 17 comunidades autonomas, with the two autonomous cities Ceuta ES63 and Melilla ES64 as regions of their own), key = NUTS code. Highest {hi['name']} ({hi.key}) {hi.value:.1f}%, lowest {lo['name']} ({lo.key}) {lo.value:.1f}%. Columns: key, name, value (%), unit, year, value_previous_year.",
        "- Caveat: the spec cites lfst_r_lfu3rt, which in Eurostat is the by-education table; the all-levels total is isced11=TOTAL. Regional labour-force-survey estimates for Ceuta and Melilla rest on very small samples (swings of 2-3 points between years). The Eurostat headline for Spain is the annual average of the Spanish Encuesta de Poblacion Activa.",
    ])
    crs = "EPSG:25830"
    g = es.merge(truth[["key", "value"]], left_on="NUTS_ID", right_on="key")
    main = g[g.NUTS_ID != "ES70"]
    main = main.clip(box(-12, 34, 8, 45)).to_crs(crs)
    ring = [(lo_, la) for lo_ in np.linspace(-10.2, 4.7, 40) for la in (34.9, 44.0)] + [(lo_, la) for la in np.linspace(34.9, 44.0, 40) for lo_ in (-10.2, 4.7)]
    xy = project_points(crs, ring)
    extent = (min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy))
    check_window(map_id, crs, extent, contains=[[-3.70, 40.42], [-5.98, 37.39], [2.17, 41.39]], excludes=[[2.35, 48.85]])   # Las Palmas is in the Canarias inset
    edges = [8, 10, 12, 15, 20]
    colors = ramp("YlOrBr", 6, 0.1, 0.92)
    cl = lambda v: int(classify(pd.Series([v]), edges).iloc[0])
    cv = Canvas(extent, "Unemployment rate in Spain’s regions",
                f"Share of the labour force aged 15–74 without work, {year} annual average, % · Spain {nat:.1f}% · from {lo['name']} {lo.value:.1f}% to {hi['name']} {hi.value:.1f}% · Eurostat, NUTS 2",
                map_w=10.4, footer=2.0)
    ax = cv.ax
    world = H.world10()
    w = world.clip(box(-14, 33, 9, 47)).to_crs(crs)
    w[w.key != "ESP"].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    H.local_graticule(ax, crs, range(-10, 6, 2), range(36, 45, 2), lon_span=(-14, 8), lat_span=(34, 46))
    plot_classes(ax, main, "value", edges, colors, linewidth=0.8)
    val = truth.set_index("key").value
    pos = {"ES11": (-8.0, 42.8), "ES12": (-5.9, 43.3), "ES13": (-4.0, 43.1), "ES21": (-2.7, 43.0), "ES22": (-1.65, 42.7), "ES23": (-2.5, 42.3), "ES24": (-0.7, 41.5),
           "ES30": (-3.75, 40.45), "ES41": (-4.7, 41.4), "ES42": (-2.9, 39.5), "ES43": (-6.2, 39.2), "ES51": (1.5, 41.75), "ES52": (-0.9, 39.2), "ES53": (2.95, 39.6),
           "ES61": (-4.5, 37.5), "ES62": (-1.5, 38.0)}
    callouts = {"ES12": (-24, 22), "ES13": (-6, 36), "ES21": (14, 34), "ES22": (36, 38), "ES23": (-30, 36), "ES30": (-6, 0), "ES62": (28, -30), "ES53": (10, -30), "ES52": (34, -4)}
    for k, (lon, lat) in pos.items():
        (x, y), = project_points(crs, [(lon, lat)])
        v = val[k]
        c = cl(v)
        txt = f"{ES_SHORT[k]}\n{v:.1f}%"
        if k in callouts and k != "ES30":
            callout(ax, (x, y), f"{ES_SHORT[k]} {v:.1f}%", callouts[k], size=8.6)
        else:
            ax.text(x, y, txt, fontsize=9, ha="center", va="center", zorder=8, linespacing=1.2, color="white" if c >= 5 else INK,
                    path_effects=None if c >= 5 else HALO, fontweight="bold" if k in ("ES61", "ES30") else "normal")
    # Ceuta, Melilla: ringed markers with callouts
    for k, lon, lat, off in (("ES63", -5.32, 35.89, (-52, -22)), ("ES64", -2.94, 35.29, (46, -24))):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.scatter([x], [y], s=150, facecolor="none", edgecolor=INK, linewidth=1.1, zorder=7)
        ax.scatter([x], [y], s=55, color=colors[cl(val[k])], edgecolor=INK, linewidth=0.8, zorder=8)
        callout(ax, (x, y), f"{ES_SHORT[k]} {val[k]:.1f}%", off, size=8.8, weight="bold")
    for name, lon, lat in (("PORTUGAL", -8.1, 39.0), ("FRANCE", 2.2, 43.4), ("MOROCCO", -4.8, 35.55), ("ALGERIA", 3.0, 35.6), ("ANDORRA", 1.55, 42.55)):
        land_label(ax, crs, extent, name, lon, lat, 8.5)
    sea_label(ax, crs, "Atlantic Ocean", -8.7, 36.3, 10)
    sea_label(ax, crs, "Mediterranean Sea", 1.5, 37.9, 10)
    sea_label(ax, crs, "Bay of Biscay", -4.0, 44.0, 8.5)
    # Canary Islands inset (right column, same scale as REGCAN95 map at 1:1 relative to the main axes scale)
    ccrs = "EPSG:4083"
    can = g[g.NUTS_ID == "ES70"].to_crs(ccrs)
    cb = can.total_bounds
    pad = 25_000
    col_x = cv.left + cv.map_w + 0.35
    col_w = WIDTH_IN - col_x - 0.25
    scale = cv.map_w / (extent[1] - extent[0])   # inches per metre on the main map
    iw = (cb[2] - cb[0] + 2 * pad) * scale
    ih = (cb[3] - cb[1] + 2 * pad) * scale
    top_in = cv.H - cv.header - 0.05
    inset = cv.add_axes(col_x, top_in - ih, iw, ih, frame=True)
    inset.set_facecolor(WATER)
    can.plot(ax=inset, color=colors[cl(val["ES70"])], edgecolor="white", linewidth=0.6, zorder=2)
    inset.set_xlim(cb[0] - pad, cb[2] + pad)
    inset.set_ylim(cb[1] - pad, cb[3] + pad)
    inset.text(0.03, 0.96, f"Canarias {val['ES70']:.1f}%\n(same scale)", transform=inset.transAxes, va="top", fontsize=8.8, fontweight="bold", path_effects=HALO, linespacing=1.2)
    lg = cv.add_axes(cv.left, 0.8, 8.6, 1.15)
    class_legend(lg, colors, [], "Unemployment rate, % of the labour force aged 15–74", box_w=1.0, class_labels=["< 8", "8–10", "10–12", "12–15", "15–20", "≥ 20"],
                 extra=[(LAND, "Neighbouring countries (not mapped)")])
    rk = truth.assign(short=truth.key.map(ES_SHORT)).sort_values("value", ascending=False)
    h = 0.42 + 0.225 * len(rk)
    tb = cv.panel(col_x, top_in - ih - 0.35, col_w, h)
    tb.text(0, h - 0.02, f"Ranked, % unemployed ({year})", va="top", fontsize=10.5, fontweight="bold")
    for i, r in enumerate(rk.itertuples()):
        yy = h - 0.42 - i * 0.225
        tb.add_patch(Rectangle((0, yy - 0.15), 0.12, 0.14, facecolor=colors[cl(r.value)], edgecolor="#c9c4b8", linewidth=0.3))
        tb.text(0.2, yy, f"{r.key}  {r.short}", va="top", fontsize=8.8)
        tb.text(col_w - 0.03, yy, f"{r.value:.1f}", va="top", ha="right", fontsize=8.8)
    cv.footnote([
        f"Source: Eurostat, lfst_r_lfu3rt (unemployment rate, 15–74 years, all education levels, both sexes), {year} annual average, NUTS 2. Boundaries: (c) EuroGeographics, GISCO NUTS 2024 1:1M. Neighbouring land: Natural Earth 1:10m.",
        "Projection: ETRS89 / UTM zone 30N (EPSG:25830); Canarias inset in REGCAN95 / UTM 28N at the same scale. Classification: 6 manual classes (<8, 8–10, 10–12, 12–15, 15–20, ≥20 %). All 19 NUTS 2 regions valued, including the autonomous cities Ceuta and Melilla (rings); none excluded.",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# 10. Active volcanoes of Iceland (Smithsonian GVP, Holocene)

GVP_WFS_URL = ("https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS&version=2.0.0&request=GetFeature"
               "&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes&outputFormat=application%2Fjson")
NE10_GLACIERS = f"{B.NE}/10m_physical/ne_10m_glaciated_areas.zip"
ICE_NAME = {"Eyjafjallajokull": "Eyjafjallajökull", "Snaefellsjokull": "Snæfellsjökull", "Ljosufjoll": "Ljósufjöll", "Krysuvik-Trolladyngja": "Krýsuvík–Trölladyngja",
            "Brennisteinsfjoll": "Brennisteinsfjöll", "Hromundartindur": "Hrómundartindur", "Grimsnes": "Grímsnes", "Prestahnukur": "Presthnúkur",
            "Tindfjallajokull": "Tindfjallajökull", "Oddnyjarhnjukur-Langjokull": "Langjökull (Oddnýjarhnúkur)", "Torfajokull": "Torfajökull", "Hofsjokull": "Hofsjökull",
            "Tungnafellsjokull": "Tungnafellsjökull", "Thordarhyrna": "Þórðarhyrna", "Bardarbunga": "Bárðarbunga", "Grimsvotn": "Grímsvötn",
            "Tjornes Fracture Zone": "Tjörnes fracture zone", "Theistareykir": "Þeistareykir", "Heidarspordar": "Heiðarspörðar", "Oraefajokull": "Öræfajökull",
            "Kverkfjoll": "Kverkfjöll", "Esjufjoll": "Esjufjöll", "Snaefell": "Snæfell", "Vestmannaeyjar": "Vestmannaeyjar (Surtsey, Eldfell)", "Helgrindur": "Helgrindur",
            "Fagradalsfjall": "Fagradalsfjall", "Reykjanes": "Reykjanes (Sundhnúkur)", "Fremrinamar": "Fremrinámar"}
VOL_AGE = [  # (min year, colour, label)
    (2000, "#c0182b", "Erupted 2000 or later"),
    (1900, "#ef7d22", "1900–1999"),
    (1500, "#f2c230", "1500–1899"),
    (-12000, "#6e8aa6", "Before 1500 (dated)"),
]


def vol_colour(y):
    if pd.isna(y):
        return None
    return next(c for lo, c, _ in VOL_AGE if y >= lo)


def build_iceland_volcanoes() -> pd.DataFrame:
    map_id = "iceland_volcanoes"
    payload = json.loads(fetch(GVP_WFS_URL, "gvp_holocene_wfs.json").read_text(encoding="utf-8"))
    rows = []
    for f in payload["features"]:
        p = f["properties"]
        if p.get("Country") != "Iceland":
            continue
        lon, lat = f["geometry"]["coordinates"]
        rows.append({"volcano_number": p["Volcano_Number"], "name": p["Volcano_Name"], "country": p["Country"], "lon": lon, "lat": lat,
                     "elevation_m": p.get("Elevation"), "primary_type": p.get("Primary_Volcano_Type"),
                     "last_eruption_year": p.get("Last_Eruption_Year"), "evidence": p.get("Evidence_Category"), "region": p.get("Region")})
    truth = pd.DataFrame(rows).sort_values("volcano_number").reset_index(drop=True)
    truth["last_eruption_year"] = pd.to_numeric(truth.last_eruption_year).astype("Int64")
    write_csv(map_id, truth)
    n_modern = int((truth.last_eruption_year >= 1900).sum())
    stamp = payload.get("timeStamp", "")[:10]
    write_md(map_id, [
        f"- URL/API: {GVP_WFS_URL} (GVP GeoServer WFS layer GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes, GeoJSON; numberMatched {payload.get('numberMatched', '?')}), rows with Country = 'Iceland'. volcano.si.edu itself answers scripted requests with HTTP 403.",
        f"- Vintage: live VOTW database as served on {stamp}; most recent citation: Global Volcanism Program, 2025. [Database] Volcanoes of the World (v. 5.3.4; 30 Dec 2025). Distributed by Smithsonian Institution, compiled by Venzke, E. https://doi.org/10.5479/si.GVP.VOTW5-2025.5.3.",
        "- Licence: Smithsonian GVP data, free to use with the citation above (Smithsonian terms of use). Land: Natural Earth 1:10m incl. glaciated areas, plate boundaries Bird (2003) PB2002 (ODC-BY 1.0).",
        f"- Rows: {len(truth)} Holocene volcanoes with Country = Iceland — volcano_number, name (GVP's ASCII spelling), country, lon, lat, elevation_m, primary_type, last_eruption_year (negative = BCE; blank = no dated Holocene eruption), evidence, region. {n_modern} have erupted since 1900; {int(truth.last_eruption_year.isna().sum())} have no dated eruption.",
        "- Caveat: 'Holocene' means GVP judges the volcano active in the last ~11,700 years, not that it is erupting now. Two of the 35 entries are offshore submarine features (Kolbeinsey Ridge 66.67N, Tjornes Fracture Zone 66.31N). GVP lists Vestmannaeyjar (incl. Surtsey and Eldfell) and the Reykjanes system (incl. Sundhnukur, erupting repeatedly 2023-25) as single entries; Jan Mayen (Norway) is not Iceland.",
    ])
    crs = "+proj=lcc +lat_1=64.25 +lat_2=65.75 +lat_0=65 +lon_0=-19 +x_0=500000 +y_0=500000 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    ring = [(lo, la) for lo in np.linspace(-24.9, -12.4, 40) for la in (62.85, 67.0)] + [(lo, la) for la in np.linspace(62.85, 67.0, 40) for lo in (-24.9, -12.4)]
    xy = project_points(crs, ring)
    extent = (min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy))
    check_window(map_id, crs, extent)
    truth["x"], truth["y"] = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    cv = Canvas(extent, "Active volcanoes of Iceland",
                f"The {len(truth)} Holocene volcanoes in the Smithsonian Global Volcanism Program database · colour = year of the last dated eruption · {n_modern} have erupted since 1900",
                map_w=10.6, footer=1.05)
    ax = cv.ax
    world = H.world10()
    ice = world[world.key == "ISL"].clip(box(-26, 62, -11, 68)).to_crs(crs)
    others = world[world.key != "ISL"].clip(box(-26, 62, -11, 68)).to_crs(crs)
    others.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    ice.plot(ax=ax, color="#efece3", edgecolor="#b9b3a4", linewidth=0.6, zorder=1.2)
    gl = gpd.read_file(fetch(NE10_GLACIERS, "ne_10m_glaciated_areas.zip")).clip(box(-25, 62.5, -12, 67)).to_crs(crs)
    gl.plot(ax=ax, color="#ffffff", edgecolor="#c7d6de", linewidth=0.5, zorder=1.3)
    lakes = gpd.read_file(fetch(H.NE10_LAKES, "ne_10m_lakes.zip")).clip(box(-25, 62.5, -12, 67)).to_crs(crs)
    lakes.plot(ax=ax, color=WATER, edgecolor="none", zorder=1.4)
    pb = json.loads(fetch("https://raw.githubusercontent.com/fraxen/tectonicplates/master/GeoJSON/PB2002_boundaries.json", "pb2002_boundaries.json").read_text())
    pbg = gpd.GeoDataFrame.from_features(pb["features"], crs=4326).clip(box(-26, 62, -11, 68)).to_crs(crs)
    pbg.plot(ax=ax, color="#9b3a3a", linewidth=1.0, linestyle=(0, (4, 2)), alpha=0.55, zorder=1.5)
    H.local_graticule(ax, crs, range(-24, -12, 2), range(63, 67), lon_span=(-26, -11), lat_span=(62, 68))
    for lon in range(-24, -12, 2):
        (gx, gy), = project_points(crs, [(lon, 62.95)])
        ax.text(gx, gy, f"{abs(lon)}°W", fontsize=7.5, color="#7c97a1", ha="center", va="bottom", zorder=0.6)
    for lat in (63, 64, 65, 66):
        (gx, gy), = project_points(crs, [(-24.75, lat)])
        ax.text(gx, gy + 3000, f"{lat}°N", fontsize=7.5, color="#7c97a1", ha="left", va="bottom", zorder=0.6)
    for name, lon, lat in (("Vatnajökull", -16.8, 64.4), ("Langjökull", -20.0, 64.65), ("Hofsjökull", -18.8, 64.8), ("Mýrdalsjökull", -19.1, 63.62)):
        pass
    sea_label(ax, crs, "Atlantic Ocean", -23.7, 65.7, 11)
    sea_label(ax, crs, "Greenland Sea", -15.7, 66.75, 8.5)
    sea_label(ax, crs, "Denmark Strait", -24.0, 66.5, 8.5)
    (rx, ry), = project_points(crs, [(-21.0, 63.2)])
    ax.text(rx, ry, "Mid-Atlantic Ridge\n(plate boundary)", fontsize=8, color="#9b3a3a", ha="center", va="center", style="italic", zorder=2, alpha=0.8)
    for nm, lon, lat in (("Akureyri", -18.1, 65.68), ("Egilsstaðir", -14.4, 65.27)):
        (cx, cy), = project_points(crs, [(lon, lat)])
        ax.scatter([cx], [cy], s=16, marker="s", color=INK, edgecolor="white", linewidth=0.6, zorder=6)
        ax.annotate(nm, (cx, cy), xytext=(5, -9), textcoords="offset points", fontsize=8.2, color="#2b3539", zorder=9, path_effects=HALO)
    # volcano symbols
    for r in truth.itertuples():
        col = vol_colour(r.last_eruption_year)
        if col is None:
            ax.scatter([r.x], [r.y], s=62, marker="^", facecolor="white", edgecolor=INK, linewidth=1.0, zorder=7)
        else:
            ax.scatter([r.x], [r.y], s=78, marker="^", color=col, edgecolor=INK, linewidth=0.8, zorder=7)
    # names: greedy placement around the symbol
    cv.fig.canvas.draw()
    rend = cv.fig.canvas.get_renderer()
    taken = []
    for lab in ax.texts:
        taken.append(mpl.text.Text.get_window_extent(lab, rend).expanded(1.05, 1.2))
    for r in truth.itertuples():
        px, py = ax.transData.transform((r.x, r.y))
        taken.append(mpl.transforms.Bbox.from_extents(px - 7, py - 7, px + 7, py + 7))
    cands = [(9, 0, "left", "center"), (-9, 0, "right", "center"), (0, 10, "center", "bottom"), (0, -10, "center", "top"), (8, 8, "left", "bottom"), (-8, 8, "right", "bottom"),
             (8, -8, "left", "top"), (-8, -8, "right", "top"), (22, 14, "left", "bottom"), (-22, 14, "right", "bottom"), (22, -14, "left", "top"), (-22, -14, "right", "top"),
             (34, 0, "left", "center"), (-34, 0, "right", "center"), (0, 26, "center", "bottom"), (0, -26, "center", "top"), (40, 26, "left", "bottom"), (-40, 26, "right", "bottom"),
             (40, -26, "left", "top"), (-40, -26, "right", "top")]
    modern = truth.assign(rank=truth.last_eruption_year.fillna(-99999)).sort_values("rank", ascending=False)
    for r in modern.itertuples():
        nm = ICE_NAME.get(r.name, r.name)
        recent = (r.last_eruption_year >= 1900) if pd.notna(r.last_eruption_year) else False
        for dx, dy, ha, va in cands:
            far = abs(dx) > 12 or abs(dy) > 12
            a = ax.annotate(nm, (r.x, r.y), xytext=(dx, dy), textcoords="offset points", ha=ha, va=va, fontsize=8.8 if recent else 7.9,
                            fontweight="bold" if recent else "normal", color=INK if recent else "#3a4a50", zorder=9, path_effects=HALO,
                            arrowprops=dict(arrowstyle="-", color="#5d6a70", linewidth=0.5, shrinkA=0, shrinkB=4) if far else None)
            a.update_positions(rend)
            bb = mpl.text.Text.get_window_extent(a, rend).expanded(1.04, 1.12)
            if not any(bb.overlaps(o) for o in taken) and ax.bbox.padded(-3).contains(bb.x0, bb.y0) and ax.bbox.padded(-3).contains(bb.x1, bb.y1):
                taken.append(bb)
                break
            a.remove()
        else:
            print("  no label position for", r.name)
    # right column
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    lg_h = 7.8
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, lg_h)
    lg.text(0, lg_h - 0.02, "Year of the last dated eruption", va="top", fontsize=10.5, fontweight="bold")
    y = lg_h - 0.5
    for _lo, col, lab in VOL_AGE:
        lg.scatter([0.15], [y], s=78, marker="^", color=col, edgecolor=INK, linewidth=0.8)
        lg.text(0.4, y, lab, va="center", fontsize=9.2)
        y -= 0.29
    lg.scatter([0.15], [y], s=62, marker="^", facecolor="white", edgecolor=INK, linewidth=1.0)
    lg.text(0.4, y, f"No dated eruption ({int(truth.last_eruption_year.isna().sum())})", va="center", fontsize=9.2)
    y -= 0.29
    lg.plot([0, 0.3], [y, y], color="#9b3a3a", linewidth=1.0, linestyle=(0, (4, 2)), alpha=0.7)
    lg.text(0.4, y, "Plate boundary (PB2002)", va="center", fontsize=9.2)
    y -= 0.29
    lg.add_patch(Rectangle((0, y - 0.07), 0.3, 0.16, facecolor="white", edgecolor="#c7d6de"))
    lg.text(0.4, y, "Glacier / ice cap", va="center", fontsize=9.2)
    y -= 0.5
    lg.text(0, y, "Erupted since 1900", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.34
    for r in truth[truth.last_eruption_year >= 1900].sort_values("last_eruption_year", ascending=False).itertuples():
        lg.text(0, y, ICE_NAME.get(r.name, r.name).split(" (")[0][:24], va="top", fontsize=8.7)
        lg.text(col_w - 0.03, y, f"{int(r.last_eruption_year)}", va="top", ha="right", fontsize=8.7)
        y -= 0.215
    y -= 0.2
    lg.text(0, y, textwrap.fill("Every name is shown; those in bold have erupted since 1900. 'Holocene' means active within about the last 11,700 years, not currently erupting.", 38),
            va="top", fontsize=8.4, color=MUTED, linespacing=1.4)
    cv.footnote([
        f"Source: Global Volcanism Program, 2025. Volcanoes of the World (v. 5.3.4), Smithsonian Institution (GVP GeoServer WFS, Holocene volcanoes with Country = Iceland, served {stamp}); https://doi.org/10.5479/si.GVP.VOTW5-2025.5.3. Land and glaciers: Natural Earth 1:10m; plate boundary: Bird (2003) PB2002 (ODC-BY).",
        f"Projection: Lambert conformal conic (parallels 64.25°N and 65.75°N, centred on 19°W). All {len(truth)} Icelandic Holocene volcanoes shown, none excluded; two of them (Kolbeinsey Ridge, Tjörnes fracture zone) are offshore. Names use Icelandic spelling (GVP's database spelling is ASCII).",
    ], width=200)
    cv.save(map_id)
    return truth


# ==========================================================================
# Registry, spot checks, report

REPORT_PATH = HELDOUT2 / "REPORT.md"


def builders():
    return {i: globals()[f"build_{i}"] for i in IDS}


def truth_lines(frame: pd.DataFrame):
    if "geometry_wkt" in frame:
        out = []
        for text in frame.geometry_wkt:
            g = wkt.loads(text)
            parts = g.geoms if hasattr(g, "geoms") else [g]
            out.extend(list(p.coords) for p in parts)
        return out
    return [list(zip(g.lon, g.lat)) for _s, g in frame.sort_values(["sid", "iso_time"]).groupby("sid")]


def truth_count(frame: pd.DataFrame) -> int:
    return frame.sid.nunique() if "sid" in frame else len(frame)


def evaluate_spot(spot: str, frame: pd.DataFrame) -> tuple[str, str]:
    """-> (PASS | FAIL | UNEVALUABLE, measured)"""
    try:
        m = re.fullmatch(r"count=([\d.]+)±([\d.]+)%", spot)
        if m:
            expected, tol = map(float, m.groups())
            actual = truth_count(frame)
            lo, hi = expected * (1 - tol / 100), expected * (1 + tol / 100)
            return ("PASS" if lo <= actual <= hi else "FAIL"), f"{actual:,}; window {lo:,.0f}-{hi:,.0f}"
        m = re.fullmatch(r"(event_near|line_near)=(-?[\d.]+),(-?[\d.]+)", spot)
        if m:
            what, lon, lat = m.group(1), float(m.group(2)), float(m.group(3))
            if what == "event_near":
                known = frame.dropna(subset=["lon", "lat"])
                km = min(haversine_km((lon, lat), p) for p in zip(known.lon, known.lat))
            else:
                km = min(line_segments_km((lon, lat), c) for c in truth_lines(frame))
            return ("PASS" if km <= NEAR_KM[what] else "FAIL"), f"{km:.1f} km; tolerance {NEAR_KM[what]:.0f} km"
        m = re.fullmatch(r"([A-Z0-9-]+)([<>])(-?[A-Z0-9.-]+)", spot)
        if m:
            left, op, right = m.groups()
            vals = frame.set_index(frame.key.astype(str)).value
            if left not in vals.index:
                return "UNEVALUABLE", f"{left} not in truth.csv"
            a = float(vals[left])
            if right in vals.index:
                b = float(vals[right])
            else:
                try:
                    b = float(right)
                except ValueError:
                    return "UNEVALUABLE", f"{right} not in truth.csv (outside the region)"
            ok = a > b if op == ">" else a < b
            return ("PASS" if ok else "FAIL"), f"{a:,.2f} {op} {b:,.2f}"
        raise ValueError("unsupported expression")
    except Exception as exc:
        return "UNEVALUABLE", f"{exc}"


def must_include_check(map_id: str, ids: list[str], frame: pd.DataFrame) -> list[tuple[str, bool]]:
    if map_id == "ivano_frankivsk_settlements":
        from set2_overlays import translit
        names = {translit(n) for n in frame.name}
        return [(i, i in names) for i in ids]
    return [(i, i in set(frame.key.astype(str))) for i in ids]


def evaluate(ids) -> dict:
    specs = tomllib.loads((HELDOUT2 / "specs.toml").read_text(encoding="utf-8"))
    out = {}
    for e in specs["map"]:
        if e["id"] not in ids:
            continue
        path = HELDOUT2 / e["id"] / "truth.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, dtype={"key": str, "sid": str})
        rows = [(sp, *evaluate_spot(sp, frame)) for sp in e["spot"]]
        lo, hi = e["units"]
        n = truth_count(frame)
        mi = must_include_check(e["id"], e.get("must_include", []), frame) if e.get("must_include") else []
        out[e["id"]] = {"rows": len(frame), "units": n, "units_ok": lo <= n <= hi, "units_range": (lo, hi), "spots": rows, "must_include": mi,
                        "window": WINDOW_NOTES.get(e["id"])}
    return out


def render_report(results: dict) -> str:
    lines = ["# Held-out set 2 reference build report", "",
             "Generated by `uv run evals/heldout2/etalons/build_heldout2.py`. Spot checks and `must_include` ids are evaluated from the written `truth.csv` files only "
             "(tolerances of `evals/maps/run.py`: `event_near` 100 km, `line_near` 20 km). A FAIL means the spec is wrong, not the data; proposed corrections are below, `specs.toml` is untouched.", "",
             "| Map | Rows | Units (spec range) | Spot checks | must_include |", "|---|---:|---|---|---|"]
    total = passed = 0
    for map_id in IDS:
        r = results.get(map_id)
        if not r:
            lines.append(f"| `{map_id}` | - | - | not built | |")
            continue
        cells = []
        for spot, verdict, measured in r["spots"]:
            total += 1
            passed += verdict == "PASS"
            cells.append(f"{'**FAIL**' if verdict == 'FAIL' else verdict} `{spot}` ({measured})")
        u = f"{'ok' if r['units_ok'] else '**outside**'}: {r['units']} in {r['units_range'][0]}-{r['units_range'][1]}"
        mi = ", ".join(f"{'PASS' if ok else '**FAIL**'} {i}" for i, ok in r["must_include"]) or "-"
        lines.append(f"| `{map_id}` | {r['rows']} | {u} | {'<br>'.join(cells)} | {mi} |")
    lines += ["", f"**{passed} of {total} spot checks pass** (the rest are FAIL or unevaluable, detailed below).", REPORT_NOTES.rstrip(), ""]
    return "\n".join(lines)


REPORT_NOTES = """
## FAILs and proposed spec corrections (specs.toml is untouched)

- **`spain_solar_plants` `count=900±60%` FAILs: 3,369 plants.** OSM has 3,369 `power=plant` + `plant:source=solar`
  objects in Spain (4 more tagged disused/construction are dropped): 2,866 photovoltaic, 50 concentrated-solar
  (`plant:method=thermal`), 453 with no method tag. Only 729 carry a numeric capacity (627 of them >= 1 MW, 166 >= 50 MW),
  totalling 26.5 GW; 1,963 have `plant:output:electricity=yes`. No reading of the filter gives ~900 (the window
  is 360-1,440), so the spec value was a guess. The map's `units = [200, 3000]` is also outside (3,369).
  Proposed: `count=3400±20%` (2,720-4,080) and `units = [200, 4500]`; OSM keeps being edited, so keep
  the tolerance wide.
- **`latin_america_fertility` `GTM>KOR` is UNEVALUABLE, not wrong.** South Korea is outside the region, so it is not in
  `truth.csv`. OWID's own value (2023) is KOR 0.720 against GTM 2.311, so the inequality holds. Proposed: replace
  with `GTM>CHL` (2.311 > 1.167), which the truth can evaluate. Note the spec's slug `fertility-rate` returns HTTP 404
  on OWID today; `children-born-per-woman` serves the same series (2023 is the last year).

## Spec observations that pass but are worth knowing

- **`atlantic_hurricanes_2017` `line_near=-97.1,28.0` passes at 14.7 km** against a 20 km tolerance only. The IBTrACS
  Harvey Rockport landfall fix is 28.2 N, 97.1 W (26 Aug 06:00 UTC = 25 Aug local), 22 km north of the spec's point;
  the track segment passes 14.7 km away. Proposed `line_near=-97.1,28.2` (0.0 km).
  `count=17±25%` is exactly 17 named storms (Arlene to Rina); two unnamed systems are excluded.
- **`iceland_volcanoes` `count=33±20%` passes at 35** (window 26-40). GVP lists two offshore entries as Iceland
  (Kolbeinsey Ridge 66.67 N, Tjornes Fracture Zone 66.31 N); without them there are 33, which is probably where the spec's
  33 comes from. The reference keeps them (GVP says Iceland). `event_near=-19.62,63.63` Eyjafjallajokull: 0.7 km.
- **`turkey_quakes_2023`**: "Turkey-Syria region" has no boundary. The reference queries the box 32-42.5 N, 25.5-45.5 E
  (233 events) and keeps the 216 within 30 km of Turkey or Syria (drops 8 Iran, 5 Greece, 2 Syrian offshore events more than 30 km out, 1 Cyprus, 1 Iraq), the Mw 7.8
  Pazarcik shock is 6.6 km from the spec point. `units = [100, 600]` is met. The spec has no count spot.
- **`madrid_metro`**: 13 relation groups by `ref` (L1-L12 and the Ramal "R", OSM colour `white`); `count=13±20%` is exact.
  Metro Ligero (light rail) and Cercanias are separate networks and are excluded.
- **`ivano_frankivsk_settlements` `count=800±10%` passes at 805** KATOTTH settlements (15 cities, 44 селища, 746 villages;
  6 raions, 62 hromadas); all 805 are located on an OSM place node, 9 OSM hamlet nodes match no KATOTTH row.
  `must_include` is matched on the 2010 national transliteration (Ivano-Frankivsk, Kolomyia, Kalush).
- **`spain_unemployment_regions`**: `lfst_r_lfu3rt` is Eurostat's by-education table; the all-education total is
  `isced11=TOTAL`, 2025 annual averages, all 19 regions valued (Spain 10.5%). The spec's `contains` Las Palmas
  (-15.43, 28.12) lies in the Canarias inset, not in the mainland window; every other contains/excludes point is in/out of
  the main frame. Ceuta (24.3%) and Melilla (24.9%) are the two highest regions, but their sample is tiny.
- **`south_asia_literacy`**: OWID values have different years per country (Maldives, Nepal 2019 ... India, Sri Lanka 2024);
  all 9 UN Southern Asia members have a value. **`german_states_population`**: Eurostat `demo_r_d2jan` (1 Jan 2025)
  stands in for Destatis GENESIS, which now requires a login; the 16 Länder sum to the Germany row (83,577,140).

## Reference images

Each `reference.png` is 1600 px wide, built with geopandas/matplotlib outside mapsvc; every footnote names source,
licence, vintage, projection, classification and exclusions. Projections: Southern Asia Lambert conformal conic;
Germany ETRS89 / UTM 32N; Turkey-Syria LCC; Madrid ETRS89 / UTM 30N; Ivano-Frankivsk transverse Mercator; Spain
UTM 30N with Canarias in REGCAN95 / UTM 28N; Atlantic LCC; Latin America LAEA; Iceland LCC. Missing values are hatched
(none are missing in the choropleths). Crimea and Sevastopol are drawn as Ukraine wherever Ukraine is shown as context.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=IDS, help="map ids; default: all ten")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    ids = tuple(args.ids) or IDS
    if not args.check_only:
        table = builders()
        for map_id in ids:
            print(f"\n[{map_id}]", flush=True)
            table[map_id]()
    results = evaluate(IDS)
    for map_id, r in results.items():
        print(f"{map_id} ({r['rows']} rows, units {r['units']}, window {r['window']})")
        for spot, verdict, measured in r["spots"]:
            print(f"   {verdict:11} {spot:34} {measured}")
        for i, ok in r["must_include"]:
            print(f"   must_include {i}: {'ok' if ok else 'MISSING'}")
    REPORT_PATH.write_text(render_report(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
