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
"""Reference ("etalon") maps for the HELD-OUT set, evals/heldout/specs.toml.

    uv run evals/heldout/etalons/build_heldout.py [ids...] [--check-only]

Writes evals/heldout/<id>/{truth.csv,truth.md,reference.png} and
evals/heldout/REPORT.md (spot-check PASS/FAIL).  Downloads are cached in
evals/heldout/etalons/.cache (gitignored); delete a file to refresh it.

Scaffolding comes from evals/maps/etalons/build.py through ``hbase`` (imported,
redirected to this directory).  This file never imports mapsvc: the held-out
references are an independent oracle.  The overlay maps live in
``heldout_overlays.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hbase as H  # noqa: E402
from hbase import B  # noqa: E402
from hbase import (  # noqa: E402
    HALO, HATCH, HELDOUT, INK, LAND, MUTED, NODATA, PAPER, WATER, WIDTH_IN,
    Canvas, check_window, class_legend, classify, compact, fetch, haversine_km, line_segments_km,
    plot_classes, plot_missing, project_points, ramp, write_csv, write_md,
)

import geopandas as gpd  # noqa: E402
import matplotlib as mpl  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402,F401
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402
from pyproj import Transformer  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import Point, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

REPORT_PATH = HELDOUT / "REPORT.md"
IDS = (
    "south_america_life_expectancy",
    "poland_voivodeship_population",
    "us_median_age",
    "italy_gdp_regions",
    "japan_quakes_2024",
    "london_underground",
    "pacific_typhoons_2024",
    "zakarpattia_settlements",
    "asia_coal_electricity",
    "europe_nuclear_plants",
)
NEAR_KM = {"event_near": 100.0, "line_near": 20.0}   # as evals/maps/run.py

OWID_LE_URL = "https://ourworldindata.org/grapher/life-expectancy.csv"
OWID_COAL_URL = "https://ourworldindata.org/grapher/share-electricity-coal.csv"
BDL_URL = ("https://bdl.stat.gov.pl/api/v1/data/by-variable/72305?unit-level=2"
           "&year=2023&year=2024&year=2025&format=json&page-size=100")
ACS_YEAR = 2024
ACS_URL = ("https://www2.census.gov/programs-surveys/acs/summary_file/{y}/table-based-SF/"
           "data/5YRData/acsdt5y{y}-b01002.dat")
STATES_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_20m.zip"
NUTS_URL = ("https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
            "NUTS_RG_01M_2024_4326_LEVL_2.geojson")
EUROSTAT_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
                "nama_10r_2gdp?format=JSON&lang=EN&freq=A&unit=PPS_EU27_2020_HAB")


# --------------------------------------------------------------------------
# Drawing helpers shared by the choropleths


def halo_text(ax, x, y, text, *, size=8.5, weight="normal", color=INK, ha="center", va="center",
              z=8, style="normal", linespacing=1.15, halo=True):
    return ax.text(x, y, text, fontsize=size, fontweight=weight, color=color, ha=ha, va=va,
                   zorder=z, style=style, linespacing=linespacing,
                   path_effects=HALO if halo else None)


def callout(ax, xy, text, offset_pt, *, size=8.3, weight="normal", ha=None, z=9):
    dx, dy = offset_pt
    ax.annotate(text, xy, xytext=offset_pt, textcoords="offset points",
                ha=ha or ("center" if dx == 0 else ("left" if dx > 0 else "right")), va="center",
                fontsize=size, fontweight=weight, color=INK, zorder=z, path_effects=HALO,
                arrowprops=dict(arrowstyle="-", color="#5d6a70", linewidth=0.6, shrinkA=0, shrinkB=2))


def side_legend(cv: Canvas, col_x: float, top_in: float, height: float, title: str, colors, labels,
                *, missing: str | None = None, extra=None, note: str | None = None):
    """Vertical class legend in the right-hand column, highest class on top."""
    lg = cv.panel(col_x, top_in, WIDTH_IN - col_x - 0.3, height)
    lg.text(0, height - 0.02, title, va="top", fontsize=10.5, fontweight="bold", linespacing=1.3)
    y = height - 0.32 * (title.count("\n") + 1) - 0.18
    for colour, label in zip(colors[::-1], labels[::-1]):
        y -= 0.27
        lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        lg.text(0.58, y + 0.11, label, va="center", fontsize=9.5)
    if missing:
        y -= 0.35
        lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=NODATA, edgecolor=HATCH, hatch="/////", linewidth=0.5))
        lg.text(0.58, y + 0.11, missing, va="center", fontsize=9.5)
    for colour, label in extra or []:
        y -= 0.35
        lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        lg.text(0.58, y + 0.11, label, va="center", fontsize=9.5)
    if note:
        lg.text(0, y - 0.3, note, va="top", fontsize=9, color=MUTED, linespacing=1.35)
    return lg, y


def ranked_table(cv: Canvas, col_x: float, top_in: float, frame: pd.DataFrame, title: str, fmt,
                 *, name_col="name", n=None, width=None, row_h=0.25, size=9.4):
    """Every row (or the top n) ranked, value right-aligned."""
    rows = frame.sort_values("value", ascending=False)
    rows = rows.head(n) if n else rows
    width = width or (WIDTH_IN - col_x - 0.3)
    h = 0.42 + row_h * len(rows)
    ax = cv.panel(col_x, top_in, width, h)
    ax.text(0, h - 0.02, title, va="top", fontsize=10.5, fontweight="bold")
    for i, row in enumerate(rows.itertuples()):
        y = h - 0.42 - i * row_h
        ax.text(0, y, f"{i + 1}.", va="top", fontsize=size, color=MUTED)
        ax.text(0.3, y, getattr(row, name_col), va="top", fontsize=size)
        ax.text(width - 0.02, y, fmt(row.value), va="top", ha="right", fontsize=size)
    return h


def owid_latest(raw: pd.DataFrame, codes=None) -> pd.DataFrame:
    col = [c for c in raw.columns if c not in {"Entity", "Code", "Year"}][0]
    raw = raw.copy()
    raw["Code"] = raw["Code"].replace({"OWID_KOS": "KOS"})
    d = raw[raw.Code.fillna("").str.fullmatch(r"[A-Z]{3}")].dropna(subset=[col])
    if codes is not None:
        d = d[d.Code.isin(codes)]
    d = d.sort_values(["Code", "Year"]).groupby("Code", as_index=False).tail(1)
    out = d.rename(columns={"Code": "key", "Entity": "name", col: "value", "Year": "year"})
    return out[["key", "name", "value", "year"]].sort_values("key").reset_index(drop=True)


def finish(cv: Canvas, map_id: str) -> None:
    cv.save(map_id)


# --------------------------------------------------------------------------
# 1. South America: life expectancy at birth (OWID / UN WPP)


SOUTH_AMERICA = ["ARG", "BOL", "BRA", "CHL", "COL", "ECU", "GUY", "PRY", "PER", "SUR", "URY", "VEN"]
SA_TERRITORIES = ["GUF", "FLK"]


def build_south_america_life_expectancy() -> pd.DataFrame:
    map_id = "south_america_life_expectancy"
    raw = pd.read_csv(fetch(OWID_LE_URL, "owid_life_expectancy.csv"))
    truth = owid_latest(raw, SOUTH_AMERICA + SA_TERRITORIES)
    truth.insert(3, "unit", "years")
    status = {k: "sovereign state" for k in SOUTH_AMERICA}
    status.update({"GUF": "overseas department of France", "FLK": "UK overseas territory (disputed)"})
    truth["status"] = truth.key.map(status)
    if len(truth) != 14:
        raise ValueError(f"expected 14 rows, got {len(truth)}")
    write_csv(map_id, truth)
    yrs = sorted(truth.year.unique())
    year = int(max(yrs))
    write_md(map_id, [
        f"- URL/API: {OWID_LE_URL} (Our World in Data Grapher CSV, 'Life expectancy at birth'; UN World Population Prospects 2024 estimates); latest non-missing year per country; geometry {H.NE50_UNITS}.",
        f"- Vintage: OWID series ends {year}; every row is {', '.join(map(str, yrs))}. OWID file downloaded {pd.Timestamp.now().strftime('%Y-%m-%d')}.",
        "- Licence: OWID data CC BY 4.0 (UN WPP, CC BY 3.0 IGO). Boundaries: Natural Earth 1:50m map units (public domain).",
        f"- Rows: {len(truth)} — the 12 sovereign states of South America plus French Guiana (GUF) and the Falkland Islands (FLK), which OWID reports separately. Columns: key (ISO3), name, value, unit, year, status.",
        "- Caveat: both values for the territories are UN WPP estimates for the territory, not for France or the UK. Natural Earth draws French Guiana inside France's country polygon; the 'map units' edition is used so it can be coloured on its own. The Galapagos Islands (Ecuador) fall outside the frame.",
    ])
    crs = "+proj=laea +lat_0=-15 +lon_0=-60 +datum=WGS84 +units=m +no_defs"
    units = gpd.read_file(fetch(H.NE50_UNITS, "ne_50m_admin_0_map_units.zip"))
    eh = units["ISO_A3_EH"].fillna("")
    units["key"] = np.where(eh.str.fullmatch(r"[A-Z]{3}"), eh, units["ADM0_A3"])
    sa = units[units.key.isin(SOUTH_AMERICA + SA_TERRITORIES)].copy()
    sa = sa.merge(truth[["key", "value"]], on="key", how="left")
    context = units[~units.key.isin(SOUTH_AMERICA + SA_TERRITORIES + ["SGS"])]
    saq = sa.to_crs(crs)
    saq_main = sa[~sa.key.isin([])].clip(box(-82, -60, -30, 14)).to_crs(crs)   # drops the Galapagos
    xmin, ymin, xmax, ymax = saq_main.total_bounds
    extent = (xmin - 700_000, xmax + 330_000, ymin - 200_000, ymax + 250_000)
    check_window(map_id, crs, extent)
    edges = [70, 72, 74, 76, 78, 80]
    colors = ramp("YlGnBu", 7, 0.10, 0.95)
    cv = Canvas(extent, "Life expectancy at birth across South America",
                f"Years a newborn can expect to live, {year} · UN World Population Prospects 2024 via Our World in Data · "
                f"{truth.value.max() - truth.value.min():.1f} years separate {truth.sort_values('value').iloc[-1]['name']} from {truth.sort_values('value').iloc[0]['name']}",
                map_w=8.9, footer=1.25)
    ax = cv.ax
    ax.set_facecolor(WATER)
    ctx = context.to_crs(crs)
    ctx.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    H.local_graticule(ax, crs, range(-100, -19, 10), range(-50, 21, 10), lon_span=(-100, -20), lat_span=(-60, 20))
    plot_classes(ax, saq, "value", edges, colors, linewidth=0.7)
    # labels: name and value; small or awkward ones get leaders
    pos = {"ARG": (-65.0, -35.5), "BOL": (-64.6, -16.7), "BRA": (-52.0, -10.5), "CHL": (-71.2, -33.0),
           "COL": (-73.0, 3.8), "ECU": (-78.2, -1.45), "GUY": (-58.9, 4.9), "PRY": (-58.4, -23.3),
           "PER": (-75.0, -9.2), "SUR": (-56.0, 4.3), "URY": (-55.9, -32.8), "VEN": (-66.0, 7.2)}
    dark = {k for k, v in zip(truth.key, truth.value) if v >= 76 or v < 70}
    short = {"BOL": "Bolivia", "CHL": "Chile"}
    byk = truth.set_index("key")
    for k, (lon, lat) in pos.items():
        (x, y), = project_points(crs, [(lon, lat)])
        v = byk.loc[k, "value"]
        col = "white" if classify(pd.Series([v]), edges).iloc[0] >= 5 else INK
        if k in {"GUY", "SUR"}:
            continue
        if k == "CHL":   # a narrow strip: label it over the Pacific with a leader
            callout(ax, (project_points(crs, [(-70.9, -33.0)])[0]), f"Chile {v:.1f}", (-62, 0), size=9)
            continue
        if k == "URY":
            callout(ax, (x, y), f"Uruguay {v:.1f}", (26, -42), size=8.6)
            continue
        if k == "ECU":
            callout(ax, (x, y), f"Ecuador {v:.1f}", (-58, 22), size=8.6)
            continue
        ax.text(x, y, f"{byk.loc[k, 'name']}\n{v:.1f}", fontsize=9.5 if k in {'BRA', 'ARG'} else 8.8, ha="center", va="center",
                zorder=8, color=col, linespacing=1.2, fontweight="bold" if k in {"BRA", "ARG"} else "normal")
    for k, (lon, lat), off, nm in (("GUY", (-58.9, 6.0), (120, 52), "Guyana"), ("SUR", (-55.9, 4.3), (120, 30), "Suriname"),
                                   ("GUF", (-53.1, 3.9), (120, 8), "French Guiana"), ("FLK", (-59.5, -51.7), (50, -10), "Falkland Is.")):
        v = byk.loc[k, "value"]
        (x, y), = project_points(crs, [(lon, lat)])
        callout(ax, (x, y), f"{nm} {v:.1f}", off, size=8.6)
    for name, lon, lat, size in (("Pacific Ocean", -86.5, -24, 11), ("Atlantic Ocean", -33.5, -25, 11),
                                 ("Caribbean Sea", -73.5, 14.2, 9)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=size, color="#5f8796", style="italic", ha="center", va="center", zorder=2,
                rotation=0)
    for name, lon, lat in (("PANAMA", -80.0, 8.5), ("TRINIDAD", -61.3, 10.3)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, "" if name == "TRINIDAD" else name, fontsize=7.8, color="#9a958a", ha="center", zorder=2)
    col_x = cv.left + cv.map_w + 0.45
    side_legend(cv, col_x, cv.H - cv.header - 0.1, 3.4, "Life expectancy at birth\n(years)", colors,
                ["< 70", "70–72", "72–74", "74–76", "76–78", "78–80", "≥ 80"],
                extra=[(LAND, "Outside South America\n(not mapped)")])
    ranked_table(cv, col_x, cv.H - cv.header - 3.7, truth, "Ranked, years", lambda v: f"{v:.1f}", width=3.2)
    cv.footnote([
        f"Source: Our World in Data, 'Life expectancy' (UN World Population Prospects 2024; CC BY 4.0), {year}, latest year available for every country. Boundaries: Natural Earth 1:50m map units (public domain).",
        "Projection: Lambert azimuthal equal-area centred on 15°S, 60°W. Classification: 7 manual classes of 2 years. French Guiana (France) and the Falkland Islands (UK, disputed) are valued territories, "
        "coloured on their own. All 14 areas valued; none excluded. Galápagos Islands (Ecuador) lie outside the frame.",
    ], width=200)
    finish(cv, map_id)
    return truth


# --------------------------------------------------------------------------
# 2. Poland: population of the voivodeships (GUS BDL)


PL_ISO = {  # ISO 3166-2:PL numeric subdivision codes (TERYT woj. code = ISO code)
    "02": "Dolnośląskie", "04": "Kujawsko-Pomorskie", "06": "Lubelskie", "08": "Lubuskie", "10": "Łódzkie",
    "12": "Małopolskie", "14": "Mazowieckie", "16": "Opolskie", "18": "Podkarpackie", "20": "Podlaskie",
    "22": "Pomorskie", "24": "Śląskie", "26": "Świętokrzyskie", "28": "Warmińsko-Mazurskie",
    "30": "Wielkopolskie", "32": "Zachodniopomorskie",
}
PL_NE_ALPHA = {  # Natural Earth carries the old alphabetic codes
    "PL-DS": "02", "PL-KP": "04", "PL-LU": "06", "PL-LB": "08", "PL-LD": "10", "PL-MA": "12", "PL-MZ": "14",
    "PL-OP": "16", "PL-PK": "18", "PL-PD": "20", "PL-PM": "22", "PL-SL": "24", "PL-SK": "26", "PL-WN": "28",
    "PL-WP": "30", "PL-ZP": "32",
}


def build_poland_voivodeship_population() -> pd.DataFrame:
    map_id = "poland_voivodeship_population"
    payload = json.loads(fetch(BDL_URL, "gus_bdl_72305_voivodeships.json").read_text(encoding="utf-8"))
    rows = []
    for r in payload["results"]:
        iso = "PL-" + r["id"][2:4]
        vals = {int(v["year"]): v["val"] for v in r["values"]}
        rows.append({"key": iso, "name": PL_ISO[r["id"][2:4]], "teryt": r["id"], "vals": vals})
    year = min(max(r["vals"]) for r in rows)
    if not all(year in r["vals"] for r in rows):
        raise ValueError("BDL: missing year")
    truth = pd.DataFrame([{"key": r["key"], "name": r["name"], "value": r["vals"][year],
                           "unit": "persons", "year": year, "teryt": r["teryt"],
                           "value_previous_year": r["vals"].get(year - 1)} for r in rows]).sort_values("key").reset_index(drop=True)
    if len(truth) != 16 or set(truth.key) != {"PL-" + k for k in PL_ISO}:
        raise ValueError("expected the 16 voivodeships")
    adm = gpd.read_file(fetch(H.NE10_ADMIN1, "ne_10m_admin_1_states_provinces.zip"))
    pl = adm[adm.adm0_a3 == "POL"].copy()
    pl["key"] = "PL-" + pl.iso_3166_2.map(PL_NE_ALPHA)
    pl = pl.drop(columns=["name"]).merge(truth[["key", "name", "value"]], on="key")
    if len(pl) != 16:
        raise ValueError("geometry join failed")
    area = pl.to_crs(2180).geometry.area / 1e6
    truth["area_km2_natural_earth"] = truth.key.map(dict(zip(pl.key, area.round(0))))
    truth["density_per_km2"] = (truth.value / truth.area_km2_natural_earth).round(1)
    write_csv(map_id, truth)
    total = int(truth.value.sum())
    write_md(map_id, [
        f"- URL/API: {BDL_URL} (Statistics Poland, Local Data Bank, variable 72305 'Ludność ogółem' — population, total; unit-level 2 = voivodeships); geometry {H.NE10_ADMIN1}.",
        f"- Vintage: reference date 31 December {year} (BDL also holds {year - 1}); the 16 voivodeships sum to {total:,} persons. GUS data retrieved {pd.Timestamp.now().strftime('%Y-%m-%d')}.",
        "- Licence: Statistics Poland open data (reuse with attribution, CC BY 4.0-compatible). Boundaries: Natural Earth 1:10m admin-1 (public domain).",
        f"- Rows: 16, key ISO 3166-2:PL numeric code (PL-14 Mazowieckie, PL-08 Lubuskie). Columns: key, name, value (persons), unit, year, teryt (BDL unit id), value_previous_year, area_km2_natural_earth, density_per_km2 (derived, Natural Earth area — for the map's context only).",
        "- Caveat: Natural Earth labels Polish voivodeships with the obsolete alphabetic codes (PL-MZ); they are mapped to the official numeric ISO codes by name. Area for density comes from the generalised 1:10m outline, so is a few tenths of a percent off the official figures.",
    ])
    crs = "EPSG:2180"
    g = pl.to_crs(crs)
    xmin, ymin, xmax, ymax = g.total_bounds
    extent = (xmin - 22_000, xmax + 60_000, ymin - 45_000, ymax + 50_000)
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "Population of Poland’s voivodeships",
                f"Residents on 31 December {year}, circle area proportional to population · {total / 1e6:.1f} million in total · shading shows persons per km²",
                map_w=10.4, footer=1.2)
    ax = cv.ax
    world = H.world10().to_crs(crs)
    world[~(world.key == "POL")].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    g = g.merge(truth[["key", "density_per_km2"]], on="key")
    edges = [75, 100, 125, 150]
    colors = ["#eef3f7", "#d5e2ec", "#b7cde0", "#93b5d1", "#6f9bc2"]
    plot_classes(ax, g, "density_per_km2", edges, colors, linewidth=1.0, edgecolor="white")
    for name, lon, lat in (("GERMANY", 14.6, 51.5), ("CZECHIA", 15.9, 49.75), ("SLOVAKIA", 19.9, 49.1), ("UKRAINE", 24.2, 50.3),
                           ("BELARUS", 24.1, 53.0), ("LITHUANIA", 23.3, 54.45), ("RUSSIA", 21.0, 54.55), ("Baltic Sea", 17.4, 54.85)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=8.5 if name != "Baltic Sea" else 10, color="#9a958a" if name != "Baltic Sea" else "#5f8796",
                style="normal" if name != "Baltic Sea" else "italic", ha="center", va="center", zorder=2)
    # proportional circles: area ∝ population
    pts = g.copy()
    pts["pt"] = pts.geometry.representative_point()
    nudge = {"PL-24": (10_000, -8_000), "PL-14": (0, 0), "PL-02": (0, -10_000), "PL-12": (0, -8_000), "PL-22": (-18_000, 4_000),
             "PL-30": (0, 0), "PL-32": (-15_000, 5_000), "PL-04": (0, 0), "PL-10": (0, 0), "PL-06": (0, 0),
             "PL-26": (0, 0), "PL-16": (0, 0), "PL-08": (0, 0), "PL-28": (0, 0), "PL-20": (0, 0), "PL-18": (0, 0)}
    k_pt2 = 2.1e-4   # points^2 of circle area per person
    for row in pts.sort_values("value", ascending=False).itertuples():
        dx, dy = nudge[row.key]
        x, y = row.pt.x + dx, row.pt.y + dy
        s = row.value * k_pt2
        ax.scatter([x], [y], s=s, color="#c2410c", alpha=0.62, edgecolor="white", linewidth=0.9, zorder=5)
    for row in pts.itertuples():
        dx, dy = nudge[row.key]
        x, y = row.pt.x + dx, row.pt.y + dy
        r_pt = (row.value * k_pt2 / np.pi) ** 0.5
        big = row.value >= 3_000_000
        below = row.key in {"PL-14", "PL-24", "PL-30", "PL-12", "PL-02"}
        name = f"{row.name}\n{row.value / 1e6:.2f} M"
        ax.annotate(name, (x, y), xytext=(0, -(r_pt + 11)), textcoords="offset points", ha="center", va="top",
                    fontsize=8.7, color=INK, zorder=9, path_effects=HALO, linespacing=1.15,
                    fontweight="bold" if big else "normal")
    # legend column
    col_x = cv.left + cv.map_w + 0.5
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 6.9)
    lg.text(0, 6.85, "Residents (circle area)", va="top", fontsize=10.5, fontweight="bold")
    y = 6.2
    for val in (5_000_000, 2_000_000, 1_000_000):
        d = (val * k_pt2 / np.pi) ** 0.5 * 2 / 72
        lg.add_patch(Circle((0.6, y), d / 2, facecolor="#c2410c", alpha=0.62, edgecolor="white"))
        lg.text(1.2, y, f"{val / 1e6:g} million", va="center", fontsize=9.5)
        y -= max(d, 0.3) + 0.25
    y -= 0.2
    lg.text(0, y, "Shading: persons per km²", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.35
    labels = ["< 75", "75–100", "100–125", "125–150", "≥ 150"]
    for colour, label in zip(colors[::-1], labels[::-1]):
        y -= 0.27
        lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        lg.text(0.58, y + 0.11, label, va="center", fontsize=9.5)
    y -= 0.35
    lg.add_patch(Rectangle((0, y), 0.46, 0.22, facecolor=LAND, edgecolor="#c9c4b8", linewidth=0.4))
    lg.text(0.58, y + 0.11, "Neighbouring countries", va="center", fontsize=9.5)
    cv.footnote([
        f"Source: Statistics Poland (GUS), Local Data Bank, variable 72305 (population, total), 31 December {year}. Boundaries: Natural Earth 1:10m admin-1 (public domain). Density derived from the Natural Earth outline.",
        "Projection: ETRS89 / Poland CS92 (EPSG:2180, transverse Mercator). Encoding: circle area ∝ residents (a count is never shaded), positioned on each voivodeship’s interior point; "
        "shading = persons per km² in 5 manual classes. All 16 voivodeships shown; none excluded.",
    ], width=200)
    finish(cv, map_id)
    return truth


# --------------------------------------------------------------------------
# 3. US: median age by state (ACS 5-year B01002)


AK_CRS = "EPSG:3338"
HI_CRS = "+proj=aea +lat_1=19 +lat_2=21.5 +lat_0=20 +lon_0=-157 +datum=WGS84 +units=m"


def us_insets(cv: Canvas, shapes: gpd.GeoDataFrame, column: str, edges, colors, extent, spec_points,
              *, label_fn, map_id: str):
    """Alaska at 0.36x and Hawaii at 1x the main scale, each equal-area."""
    scale = cv.map_w / (extent[1] - extent[0])
    x_cursor = cv.left + 0.15
    for key, crs, factor, point in (("AK", AK_CRS, 0.36, spec_points[0]), ("HI", HI_CRS, 1.0, spec_points[1])):
        frame = shapes[shapes.stusps == key].to_crs(crs)
        b = frame.total_bounds
        if key == "AK":   # Aleutians cross 180: clip to the main mass for the frame
            b = np.array([b[0], b[1], b[2], b[3]])
        pad = 60_000 if key == "AK" else 30_000
        w_in = (b[2] - b[0] + 2 * pad) * scale * factor
        h_in = (b[3] - b[1] + 2 * pad) * scale * factor
        inset = cv.add_axes(x_cursor, cv.footer + 0.12, w_in, h_in, frame=True)
        inset.set_facecolor(WATER)
        ext = (b[0] - pad, b[2] + pad, b[1] - pad, b[3] + pad)
        inset.set_xlim(ext[0], ext[1])
        inset.set_ylim(ext[2], ext[3])
        inset.set_aspect("equal")
        plot_classes(inset, frame, column, edges, colors, linewidth=0.6)
        label_fn(inset, key, frame, b)
        inset.text(0.03, 0.96, {"AK": "Alaska (scale ×0.36)", "HI": "Hawaii (same scale)"}[key],
                   transform=inset.transAxes, fontsize=8, va="top", color=MUTED)
        check_window(map_id, frame.crs, ext, contains=[point], excludes=[])
        x_cursor += w_in + 0.2


def build_us_median_age() -> pd.DataFrame:
    map_id = "us_median_age"
    url = ACS_URL.format(y=ACS_YEAR)
    raw = pd.read_csv(fetch(url, f"acsdt5y{ACS_YEAR}-b01002.dat"), sep="|", dtype=str)
    states = gpd.read_file(fetch(STATES_URL, "cb_2024_us_state_20m.zip"))
    states = states[states.STUSPS.isin(B.STATE_CODES)].copy()
    states["stusps"] = states.STUSPS
    fips = states.set_index("STATEFP").STUSPS.to_dict()
    st = raw[raw.GEO_ID.str.startswith("0400000US")].copy()
    st["fips"] = st.GEO_ID.str[-2:]
    st = st[st.fips.isin(fips)]
    names = states.set_index("STUSPS").NAME.to_dict()
    truth = pd.DataFrame({
        "key": "US-" + st.fips.map(fips), "name": st.fips.map(fips).map(names),
        "value": pd.to_numeric(st.B01002_E001), "unit": "years",
        "year": f"{ACS_YEAR - 4}-{ACS_YEAR}", "moe": pd.to_numeric(st.B01002_M001),
        "fips": st.fips,
    }).sort_values("key").reset_index(drop=True)
    if len(truth) != 51 or truth.value.isna().any():
        raise ValueError(f"expected 51 valued states, got {len(truth)}")
    write_csv(map_id, truth)
    write_md(map_id, [
        f"- URL/API: {url} (ACS table-based summary file, table B01002 'Median age by sex', column B01002_E001 = both sexes, rows GEO_ID 0400000US<FIPS>). The keyless api.census.gov call demands a key, so the flat-file release (the same estimates) is used; boundaries {STATES_URL}.",
        f"- Vintage: American Community Survey {ACS_YEAR - 4}–{ACS_YEAR} 5-year estimates (released January 2026). Median age in years; `moe` is the published 90% margin of error.",
        "- Licence: U.S. Census Bureau, public domain. Boundaries: Census cartographic boundary file 2024, 1:20M (public domain).",
        f"- Rows: {len(truth)} — 50 states plus the District of Columbia, key `US-XX` (ISO 3166-2:US), `fips` 2-digit; Puerto Rico (FIPS 72) is in the file but not in the 50+DC frame and is not drawn. Youngest {truth.sort_values('value').iloc[0]['name']} {truth.sort_values('value').iloc[0].value:.1f}, oldest {truth.sort_values('value').iloc[-1]['name']} {truth.sort_values('value').iloc[-1].value:.1f}.",
        "- Caveat: a 5-year estimate pools 60 months of survey responses; the state margins of error are 0.1–0.4 years (DC 0.2), so neighbouring values in one class are not statistically distinguishable.",
    ])
    crs = "EPSG:5070"
    shapes = states.merge(truth[["key", "value"]].assign(stusps=lambda d: d.key.str[3:]), on="stusps", how="left")
    conus = shapes[~shapes.stusps.isin(["AK", "HI"])].to_crs(crs)
    xmin, ymin, xmax, ymax = conus.total_bounds
    extent = (xmin - 120_000, xmax + 260_000, ymin - 420_000, ymax + 80_000)
    spec = H.spec_entry(map_id)
    check_window(map_id, crs, extent, contains=spec["contains"][:1], excludes=[])
    edges = [34, 36, 38, 40, 42, 44]
    colors = ramp("BuPu", 7, 0.08, 0.95)
    classes = dict(zip(truth.key.str[3:], classify(truth.value, edges)))
    ranked = truth.sort_values("value")
    lo, hi = ranked.iloc[0], ranked.iloc[-1]
    cv = Canvas(extent, "Median age by US state",
                f"Median age of residents, years · American Community Survey {ACS_YEAR - 4}–{ACS_YEAR} five-year estimates · "
                f"from {lo['name']} ({lo.value:.1f}) to {hi['name']} ({hi.value:.1f})",
                footer=1.9)
    ax = cv.ax
    world = H.world10()
    world[world.key.isin(["CAN", "MEX", "CUB", "BHS"])].to_crs(crs).plot(ax=ax, color=LAND, edgecolor="none", zorder=1)
    plot_classes(ax, conus, "value", edges, colors, linewidth=0.8)
    lookup = truth.assign(s=truth.key.str[3:]).set_index("s").value.to_dict()

    def tcol(code):
        return "white" if classes[code] >= 4 else INK
    offsets = {"VT": (-46, 34), "NH": (-18, 60), "MA": (52, 12), "RI": (46, -6), "CT": (46, -24),
               "NJ": (42, -10), "DE": (42, -16), "MD": (46, -32), "DC": (40, -50)}
    nudges = {"FL": (60_000, 0), "LA": (-60_000, 0), "MI": (95_000, -175_000), "CA": (-30_000, 0),
              "KY": (40_000, -20_000), "WV": (-15_000, -20_000), "ID": (0, -80_000)}
    for row in conus.itertuples():
        txt = f"{row.stusps}\n{lookup[row.stusps]:.1f}"
        if row.stusps in offsets:
            c = row.geometry.centroid
            callout(ax, (c.x, c.y), txt.replace("\n", " "), offsets[row.stusps], size=8.3)
        else:
            p = row.geometry.representative_point()
            dx, dy = nudges.get(row.stusps, (0, 0))
            ax.text(p.x + dx, p.y + dy, txt, fontsize=8.3, ha="center", va="center", zorder=7,
                    color=tcol(row.stusps), linespacing=1.15)
    dc = conus[conus.stusps == "DC"].geometry.centroid.iloc[0]
    ax.scatter([dc.x], [dc.y], s=22, marker="s", color=colors[int(classes["DC"])], edgecolor=INK, linewidth=0.6, zorder=8)

    def label_fn(inset, key, frame, b):
        if key == "HI":
            inset.text(b[0] + 0.22 * (b[2] - b[0]), b[1] + 0.12 * (b[3] - b[1]), f"HI {lookup['HI']:.1f}",
                       fontsize=8.3, ha="center", va="center", zorder=7)
        else:
            p = frame.geometry.representative_point().iloc[0]
            inset.text(p.x, p.y, f"AK\n{lookup['AK']:.1f}", fontsize=8.3, ha="center", va="center", zorder=7,
                       color=tcol("AK"), linespacing=1.15)

    us_insets(cv, shapes, "value", edges, colors, extent, spec["contains"][1:], label_fn=label_fn, map_id=map_id)
    lg = cv.add_axes(cv.left + 6.6, 0.95, 8.4, 0.9)
    class_legend(lg, colors, ["34", "36", "38", "40", "42", "44"], "Median age (years)", box_w=0.7,
                 extra=[(LAND, "Neighbouring countries")])
    cv.footnote([
        f"Source: U.S. Census Bureau, American Community Survey {ACS_YEAR - 4}–{ACS_YEAR} 5-year estimates, table B01002 (median age, both sexes), public domain. Boundaries: Census cartographic boundary file 2024, 1:20M.",
        "Projection: conterminous US Albers equal-area conic (EPSG:5070); Alaska (EPSG:3338, 0.36× scale) and Hawaii (Albers, same scale) inset. Classification: 7 manual classes of 2 years. "
        "All 50 states and DC shown (DC as the small square); Puerto Rico is outside the 50-states-plus-DC scope. None excluded.",
    ], width=200)
    finish(cv, map_id)
    return truth


# --------------------------------------------------------------------------
# 4. Italy: GDP per inhabitant by NUTS 2 region (Eurostat nama_10r_2gdp)


ITALY_NUTS2 = ["ITC1", "ITC2", "ITC3", "ITC4", "ITF1", "ITF2", "ITF3", "ITF4", "ITF5", "ITF6", "ITG1", "ITG2",
               "ITH1", "ITH2", "ITH3", "ITH4", "ITH5", "ITI1", "ITI2", "ITI3", "ITI4"]


def build_italy_gdp_regions() -> pd.DataFrame:
    map_id = "italy_gdp_regions"
    geo_q = "".join(f"&geo={g}" for g in ITALY_NUTS2 + ["EU27_2020"])
    url = EUROSTAT_URL + geo_q
    payload = json.loads(fetch(url, "eurostat_nama_10r_2gdp_italy.json").read_text())
    data = B.jsonstat_rows(payload)
    data["time"] = pd.to_numeric(data.time)
    shapes = gpd.read_file(fetch(NUTS_URL, "nuts2_2024_01m_4326.geojson"))
    it = shapes[shapes.CNTR_CODE == "IT"].copy()
    if sorted(it.NUTS_ID) != sorted(ITALY_NUTS2):
        raise ValueError("GISCO Italy NUTS 2 list differs: " + str(sorted(set(it.NUTS_ID) ^ set(ITALY_NUTS2))))
    latest = data.sort_values(["geo", "time"]).groupby("geo", as_index=False).tail(1)
    names = it.set_index("NUTS_ID").NAME_LATN.to_dict()
    names["EU27_2020"] = "European Union (27 countries, from 2020)"
    truth = pd.DataFrame({"key": latest.geo, "name": latest.geo.map(names), "value": latest.value,
                          "unit": "PPS (EU27 2020) per inhabitant", "year": latest.time}).sort_values("key").reset_index(drop=True)
    regions = truth[truth.key != "EU27_2020"]
    eu = float(truth.loc[truth.key == "EU27_2020", "value"].iloc[0])
    if len(regions) != 21 or regions.value.isna().any():
        raise ValueError("expected 21 valued regions")
    year = int(regions.year.max())
    if regions.year.nunique() != 1:
        print("  NOTE: regions differ in latest year", regions.year.unique())
    write_csv(map_id, truth)
    clean = lambda n: (n.replace("Provincia Autonoma di ", "").replace("Valle d’Aosta/Vallée d’Aoste", "Valle d’Aosta")
                       .replace("Valle d'Aosta/Vallée d'Aoste", "Valle d’Aosta"))
    truth["short"] = truth.name.map(clean)
    hi, lo = regions.sort_values("value").iloc[-1], regions.sort_values("value").iloc[0]
    hi_n, lo_n = clean(hi["name"]), clean(lo["name"])
    write_md(map_id, [
        f"- URL/API: {EUROSTAT_URL} + geo filter (21 Italian NUTS 2 codes and EU27_2020); Eurostat dissemination API 1.0, JSON-stat; geometry {NUTS_URL}.",
        f"- Vintage: nama_10r_2gdp updated {payload.get('updated', '?')[:10]}; unit PPS_EU27_2020_HAB (purchasing power standards per inhabitant, EU27 2020 basis); latest year {year}. Geometry GISCO NUTS 2024, 1:1M.",
        "- Licence: Eurostat data, free reuse with attribution (Commission Decision 2011/833/EU); boundaries © EuroGeographics for the administrative boundaries (GISCO).",
        f"- Rows: {len(truth)} — 21 NUTS 2 regions (including ITH1 Provincia Autonoma di Bolzano/Bozen and ITH2 Trento) plus EU27_2020 = {eu:,.0f} PPS. Highest {hi['name']} {hi.value:,.0f}, lowest {lo['name']} {lo.value:,.0f}.",
        "- Caveat: Italian NUTS 2 regions are the 19 regions plus the two autonomous provinces; the NUTS 2024 and 2021 Italian level-2 lists are identical, so the data and shapes join without a correspondence table. The spec's `excludes` point Ljubljana (14.51, 46.05) lies inside Italy's own lon/lat extent, so no rectangular frame that shows all of Italy can exclude it.",
    ])
    crs = "+proj=aea +lat_1=37 +lat_2=46 +lat_0=42 +lon_0=12.5 +datum=WGS84 +units=m +no_defs"
    g = it.merge(truth[["key", "value"]], left_on="NUTS_ID", right_on="key").to_crs(crs)
    xmin, ymin, xmax, ymax = g.total_bounds
    extent = (xmin - 22_000, xmax + 70_000, ymin - 70_000, ymax + 60_000)
    check_window(map_id, crs, extent)
    edges = [25_000, 30_000, 35_000, 40_000, 45_000, 50_000]
    colors = ramp("GnBu", 7, 0.10, 0.96)
    cv = Canvas(extent, "GDP per inhabitant in Italy’s regions",
                f"GDP in purchasing power standards per inhabitant, {year} · EU27 average {eu:,.0f} PPS · "
                f"{hi_n} {hi.value / eu * 100:.0f}% of it, {lo_n} {lo.value / eu * 100:.0f}%",
                map_w=9.0, footer=0.95)
    ax = cv.ax
    world = H.world10().to_crs(crs)
    world[world.key != "ITA"].plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    H.local_graticule(ax, crs, range(6, 20, 2), range(36, 48, 2), lon_span=(4, 20), lat_span=(34, 48))
    plot_classes(ax, g, "value", edges, colors, linewidth=0.8)
    nudge = {}
    callouts = {"ITC2": (0, 46), "ITH1": (46, 30), "ITI3": (70, 14), "ITF1": (74, -4), "ITF2": (62, 4),
                "ITI4": (-72, -4), "ITF3": (-72, -24), "ITH4": (66, 26), "ITI1": (-70, 6), "ITC3": (-50, -30)}
    cent = {"ITF5": (16.05, 40.5), "ITF6": (16.6, 39.0), "ITG2": (9.0, 40.1), "ITG1": (14.0, 37.6), "ITF3": (14.7, 40.9),
            "ITI1": (11.1, 43.45), "ITH3": (11.7, 45.55), "ITH4": (12.9, 46.0), "ITC1": (7.9, 45.0), "ITC3": (8.7, 44.4),
            "ITH5": (11.0, 44.6), "ITI2": (12.55, 42.95), "ITI3": (13.2, 43.4), "ITF4": (17.15, 40.85), "ITF1": (13.95, 42.2),
            "ITF2": (14.65, 41.65), "ITI4": (12.8, 41.8), "ITC4": (9.7, 45.55), "ITC2": (7.4, 45.7)}
    vals = truth.set_index("key").value
    for row in g.itertuples():
        k = row.NUTS_ID
        nm = clean(row.NAME_LATN).replace("/Bozen", "").replace("/Bolzano", "")
        text = f"{nm}\n{row.value / 1000:.1f}k"
        if k in cent:
            (x, y), = project_points(crs, [cent[k]])
        else:
            p = row.geometry.representative_point(); x, y = p.x, p.y
        cls = classify(pd.Series([row.value]), edges).iloc[0]
        if k == "ITH2":
            ax.text(x, y, f"Trento\n{row.value / 1000:.1f}k", fontsize=8.0, ha="center", va="center", zorder=8, linespacing=1.1,
                    color="white")
        elif k in callouts:
            callout(ax, (x, y), text.replace("\n", " "), callouts[k], size=8.6)
        else:
            ax.text(x, y, text, fontsize=8.6, ha="center", va="center", zorder=8, linespacing=1.15,
                    color="white" if cls >= 5 else INK, path_effects=None if cls >= 5 else HALO)
    for name, lon, lat in (("SWITZERLAND", 8.9, 46.75), ("AUSTRIA", 13.2, 47.35), ("SLOVENIA", 14.6, 45.95),
                           ("CROATIA", 16.0, 45.3), ("TUNISIA", 10.0, 35.5), ("ALGERIA", 6.2, 35.9), ("MALTA", 14.45, 35.75)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=8, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("Tyrrhenian Sea", 12.0, 39.4), ("Ionian Sea", 18.2, 38.6), ("Adriatic Sea", 15.2, 43.0),
                           ("Ligurian Sea", 8.8, 43.7), ("Mediterranean Sea", 11.5, 36.3)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=9, color="#5f8796", style="italic", ha="center", va="center", zorder=2)
    col_x = cv.left + cv.map_w + 0.5
    _, y = side_legend(cv, col_x, cv.H - cv.header - 0.1, 3.5, "GDP per inhabitant\n(PPS, thousands)", colors,
                       ["< 25", "25–30", "30–35", "35–40", "40–45", "45–50", "≥ 50"],
                       extra=[(LAND, "Outside Italy (not mapped)")])
    ranked_table(cv, col_x, cv.H - cv.header - 3.75, regions.assign(name=regions.name.map(clean).str.replace("/Bozen", "")),
                 "Ranked, PPS per inhabitant", lambda v: f"{v:,.0f}", width=WIDTH_IN - col_x - 0.3, row_h=0.235, size=9)
    cv.footnote([
        f"Source: Eurostat, nama_10r_2gdp (PPS_EU27_2020_HAB), {year}; © EuroGeographics for the administrative boundaries (GISCO NUTS 2024, 1:1M). Context land: Natural Earth.",
        "Projection: Albers equal-area conic (standard parallels 37°N and 46°N, centred on 12.5°E). Classification: 7 manual classes of 5,000 PPS (map labels in thousands of PPS). "
        f"All 21 NUTS 2 regions valued, including the autonomous provinces of Bolzano/Bozen and Trento; none excluded.",
    ], width=200)
    finish(cv, map_id)
    return truth


# --------------------------------------------------------------------------
# 9. Asia: share of electricity from coal (OWID / Ember, Energy Institute)


ASIA = {  # UN M49 "Asia" (plus Taiwan); Russia is Eastern Europe in the UN geoscheme and is drawn as context
    "Central Asia": "KAZ KGZ TJK TKM UZB",
    "Eastern Asia": "CHN HKG MAC PRK JPN MNG KOR TWN",
    "South-eastern Asia": "BRN KHM IDN LAO MYS MMR PHL SGP THA TLS VNM",
    "Southern Asia": "AFG BGD BTN IND IRN MDV NPL PAK LKA",
    "Western Asia": "ARM AZE BHR CYP GEO IRQ ISR JOR KWT LBN OMN QAT SAU PSE SYR TUR ARE YEM",
}
ASIA_CODES = [c for v in ASIA.values() for c in v.split()]


def build_asia_coal_electricity() -> pd.DataFrame:
    map_id = "asia_coal_electricity"
    raw = pd.read_csv(fetch(OWID_COAL_URL, "owid_share_electricity_coal.csv"))
    truth = owid_latest(raw, ASIA_CODES)
    truth.insert(3, "unit", "percent of electricity generation")
    sub = {c: r for r, v in ASIA.items() for c in v.split()}
    truth["subregion"] = truth.key.map(sub)
    absent = sorted(set(ASIA_CODES) - set(truth.key))
    write_csv(map_id, truth)
    yrs = truth.year.value_counts().sort_index().to_dict()
    write_md(map_id, [
        f"- URL/API: {OWID_COAL_URL} (Our World in Data Grapher CSV, 'Share of electricity generated by coal'; Ember Yearly Electricity Data and the Energy Institute Statistical Review); latest non-missing year per country; geometry {H.NE10_COUNTRIES}.",
        f"- Vintage: latest year per country — {', '.join(f'{y}: {n} countries' for y, n in yrs.items())}. OWID file downloaded {pd.Timestamp.now().strftime('%Y-%m-%d')}.",
        "- Licence: OWID data CC BY 4.0 (Ember CC BY 4.0; Energy Institute). Boundaries: Natural Earth 1:10m (public domain), Crimea and Sevastopol drawn as Ukraine.",
        f"- Rows: {len(truth)} of the {len(ASIA_CODES)} countries and territories of the UN M49 'Asia' group (plus Taiwan), chosen by name (explicit ISO3 list), never by bounding box. No OWID value: {', '.join(absent) or 'none'}. Columns: key (ISO3), name, value (%), unit, year, subregion.",
        "- Caveat: Russia is transcontinental and is in the UN's Eastern Europe group, so it is context (and has no Asian share here); Turkey, Cyprus, Armenia, Azerbaijan and Georgia are counted as Asia (UN Western Asia). Shares are of generation, not capacity.",
    ])
    crs = "+proj=aea +lat_1=14 +lat_2=50 +lat_0=28 +lon_0=88 +datum=WGS84 +units=m +no_defs"
    world = H.world10()
    world["key"] = world.key.replace({"TWN": "TWN"})
    asia = world[world.key.isin(ASIA_CODES)][["key", "NAME", "geometry"]].dissolve("key", as_index=False)
    asia = asia.merge(truth[["key", "value"]], on="key", how="left")
    window = box(24, -12, 147.5, 56.5)
    clipped = asia.clip(window).to_crs(crs)
    xmin, ymin, xmax, ymax = clipped.total_bounds
    extent = (xmin - 150_000, xmax + 150_000, ymin - 280_000, ymax + 200_000)
    check_window(map_id, crs, extent)
    edges = [10, 25, 50, 75]
    colors = ["#f4efe6", "#e6c8a0", "#d0925a", "#a85a30", "#5d2a18"]
    colors = ramp("Greys", 5, 0.10, 0.92)
    cv = Canvas(extent, "Share of electricity generated from coal across Asia",
                f"Coal’s share of electricity generation, % · latest year per country (mostly 2024–2025) · {len(truth)} Asian countries and territories · "
                f"from {truth.sort_values('value').iloc[0]['name']} {truth.value.min():.0f}% to {truth.sort_values('value').iloc[-1]['name']} {truth.value.max():.0f}%",
                map_w=15.2, footer=3.5)
    ax = cv.ax
    ctx = world[~world.key.isin(ASIA_CODES)].to_crs(crs)
    ctx.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.4, zorder=1)
    H.local_graticule(ax, crs, range(30, 151, 20), range(0, 61, 10), lon_span=(20, 150), lat_span=(-15, 60))
    plot_classes(ax, clipped.dropna(subset=["value"]), "value", edges, colors, linewidth=0.45)
    plot_missing(ax, clipped[clipped.value.isna()], linewidth=0.45)
    full = asia.to_crs(crs)
    byk = truth.set_index("key").value
    big = {"KOR": (58, -14), "TWN": (46, 8), "BGD": (-8, -64), "NPL": (-46, 40)}
    where = {"IDN": (113.5, -1.8)}
    for row in clipped.itertuples():
        k = row.key
        if pd.isna(row.value):
            continue
        area = gpd.GeoSeries([row.geometry], crs=crs).area.iloc[0] / 1e6
        cls = classify(pd.Series([row.value]), edges).iloc[0]
        text = f"{row.value:.0f}"
        if k in big:
            c = row.geometry.representative_point()
            callout(ax, (c.x, c.y), f"{ {'KOR': 'S. Korea', 'TWN': 'Taiwan', 'BGD': 'Bangladesh', 'NPL': 'Nepal'}[k]} {text}", big[k], size=8.2)
        elif area > 90_000:
            p = row.geometry.representative_point()
            if k in where:
                (px, py), = project_points(crs, [where[k]])
                p = Point(px, py)
            ax.text(p.x, p.y, text, fontsize=9.2, ha="center", va="center", zorder=8, fontweight="bold",
                    color="white" if cls >= 3 else INK, path_effects=None if cls >= 3 else HALO)
    for name, lon, lat in (("RUSSIA", 70.0, 55.9),):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=8.5, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("Arabian Sea", 65.0, 15.0), ("Bay of Bengal", 88.5, 13.5), ("South China Sea", 113.5, 12.0),
                           ("Pacific Ocean", 139.0, 28.0), ("Indian Ocean", 84, -4.5), ("Mediterranean Sea", 33.0, 34.4),
                           ("Black Sea", 34.5, 43.0), ("Caspian Sea", 51.0, 41.6)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=8.8, color="#5f8796", style="italic", ha="center", va="center", zorder=2)
    lg = cv.add_axes(cv.left, cv.footer - 1.75, 6.3, 1.65)
    class_legend(lg, colors, ["10", "25", "50", "75"], "Coal share of electricity generation (%, scale 0–100)",
                 box_w=0.6, class_labels=["0–10", "10–25", "25–50", "50–75", "75–100"], missing="No data",
                 extra=[(LAND, "Not in the Asian set")])
    ranked = truth.sort_values("value", ascending=False)
    ncol = 5
    per = (len(ranked) + ncol - 1) // ncol
    colw = 1.74
    h = 0.36 + 0.19 * per
    tb = cv.panel(cv.left + 6.55, cv.footer - 0.08, colw * ncol, h)
    tb.text(0, h - 0.02, "All countries ranked, coal share of generation (%)", va="top", fontsize=10.5, fontweight="bold")
    for i, row in enumerate(ranked.itertuples()):
        c, r = divmod(i, per)
        x0 = c * colw
        yy = h - 0.36 - r * 0.19
        tb.text(x0, yy, row.name[:20], va="top", fontsize=8.2)
        tb.text(x0 + colw - 0.2, yy, f"{row.value:.0f}", va="top", ha="right", fontsize=8.2)
    absent_names = ", ".join(sorted(full[full.value.isna()].NAME))
    cv.footnote([
        f"Source: Our World in Data, 'Share of electricity generated by coal' (Ember; Energy Institute Statistical Review of World Energy), CC BY 4.0; latest year per country. Boundaries: Natural Earth 1:10m. Hatched = no value in the source: {absent_names or 'none'}.",
        f"Projection: Albers equal-area conic (parallels 14°N and 50°N, centred on 88°E). Classification: 5 classes on a fixed 0–100% scale. {len(truth)} Asian countries and territories chosen by name from the UN M49 Asia group (plus Taiwan); "
        "Russia is context. Labels give the share in %; the ranked list names every country.",
    ], width=200)
    finish(cv, map_id)
    return truth


# ==========================================================================
# Registry, spot checks, report


def builders() -> dict[str, Callable[[], pd.DataFrame]]:
    table = {i: globals().get(f"build_{i}") for i in IDS}
    try:
        import heldout_overlays as O
        for i in IDS:
            table[i] = table[i] or getattr(O, f"build_{i}", None)
    except ModuleNotFoundError:
        pass
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=IDS, help="map ids; default: all ten")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    ids = tuple(args.ids) or IDS
    if not args.check_only:
        table = builders()
        for map_id in ids:
            if table.get(map_id) is None:
                print(f"\n[{map_id}] not built yet")
                continue
            print(f"\n[{map_id}]", flush=True)
            table[map_id]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
