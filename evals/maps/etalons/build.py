#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "geopandas>=1.0,<2",
#   "matplotlib>=3.9,<4",
#   "numpy>=2,<3",
#   "pandas>=2.2,<3",
#   "pypdf>=5,<7",
#   "pyproj>=3.7,<4",
#   "requests>=2.32,<3",
#   "shapely>=2.0,<3",
# ]
# ///
"""Build the independent, primary-source reference maps in evals/maps.

Delete ``evals/maps/etalons/.cache`` to refresh all live datasets.  This file
deliberately does not import mapsvc: it is an independent oracle and renderer.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import textwrap
import time
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.colors import BoundaryNorm, LogNorm, Normalize, TwoSlopeNorm
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from pypdf import PdfReader
from pyproj import Geod
from requests.adapters import HTTPAdapter
from shapely import wkt
from shapely.geometry import LineString, Point, box
from urllib3.util.retry import Retry


MAPS_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
SPECS_PATH = MAPS_DIR / "specs.toml"
WIDTH_PX = 1600
DPI = 100
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

NE_COUNTRIES_URL = (
    "https://naturalearth.s3.amazonaws.com/50m_cultural/"
    "ne_50m_admin_0_countries.zip"
)
NE_LAKES_URL = (
    "https://naturalearth.s3.amazonaws.com/50m_physical/ne_50m_lakes.zip"
)
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
ACS_URL = (
    "https://api.census.gov/data/2023/acs/acs5"
    "?get=NAME,B19013_001E&for=county:*&in=state:06"
)
NUTS_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
    "NUTS_RG_20M_2024_3035_LEVL_2.geojson"
)
EUROSTAT_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
    "nama_10r_2gdp?freq=A&unit=PPS_EU27_2020_HAB"
)
USGS_URL = (
    "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
    "&starttime=2025-01-01&endtime=2026-01-01&minmagnitude=6&orderby=time-asc"
)
MINARD_BASE = (
    "https://raw.githubusercontent.com/vincentarelbundock/Rdatasets/master/"
    "csv/HistData"
)

EU27 = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
}
EUROPE = {
    "ALB", "AND", "AUT", "BEL", "BGR", "BIH", "BLR", "CHE", "CYP", "CZE",
    "DEU", "DNK", "ESP", "EST", "FIN", "FRA", "GBR", "GRC", "HRV", "HUN",
    "IRL", "ISL", "ITA", "LIE", "LTU", "LUX", "LVA", "MDA", "MKD", "MLT",
    "MNE", "NLD", "NOR", "POL", "PRT", "ROU", "RUS", "SMR", "SRB", "SVK",
    "SVN", "SWE", "UKR", "VAT", "XKX",
}
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

COLORS = {
    "ink": "#18242b",
    "muted": "#60717a",
    "paper": "#f8f6f1",
    "water": "#dcecf1",
    "missing": "#dedbd2",
    "border": "#f8f6f1",
}


def session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    s = requests.Session()
    s.headers.update(
        {"User-Agent": "maps-maker-etalon/1.0 (independent evaluation builder)"}
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


HTTP = session()


def fetch(url: str, filename: str, *, timeout: int = 180) -> Path:
    """Download once, atomically, and return a stable cache path."""
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
            partial = target.with_suffix(".json.part")
            partial.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            partial.replace(target)
            return target
        except Exception as exc:  # try independent public mirrors
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("All Overpass endpoints failed: " + "; ".join(errors))


def variable_column(frame: pd.DataFrame) -> str:
    columns = [c for c in frame.columns if c not in {"Entity", "Code", "Year"}]
    if len(columns) != 1:
        raise ValueError(f"Expected one OWID value column, found {columns}")
    return columns[0]


def iso_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[
        frame["Code"].fillna("").str.fullmatch(r"[A-Z]{3}")
        & ~frame["Code"].fillna("").str.startswith("OWID")
    ].copy()


def latest_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return (
        frame.sort_values(["Code", "Year"])
        .dropna(subset=[variable_column(frame)])
        .groupby("Code", as_index=False)
        .tail(1)
        .sort_values("Code")
    )


def standard_truth(
    frame: pd.DataFrame, value_column: str, unit: str, *, key="Code", name="Entity"
) -> pd.DataFrame:
    result = frame[[key, name, value_column, "Year"]].rename(
        columns={key: "key", name: "name", value_column: "value", "Year": "year"}
    )
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


def load_world() -> gpd.GeoDataFrame:
    world = gpd.read_file(fetch(NE_COUNTRIES_URL, "ne_50m_admin_0_countries.zip"))
    candidates = ("ADM0_A3", "ISO_A3_EH", "ISO_A3")
    world["key"] = ""
    for field in reversed(candidates):
        if field in world:
            valid = world[field].fillna("").astype(str).str.fullmatch(r"[A-Z]{3}")
            world.loc[valid, "key"] = world.loc[valid, field]
    world.loc[world.get("ADMIN", "") == "Kosovo", "key"] = "XKX"
    return world


def setup_figure(title: str, subtitle: str, *, height: float = 9.0):
    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.titlesize": 22,
            "axes.titleweight": 700,
            "axes.edgecolor": COLORS["ink"],
            "text.color": COLORS["ink"],
            "axes.labelcolor": COLORS["ink"],
            "xtick.color": COLORS["muted"],
            "ytick.color": COLORS["muted"],
        }
    )
    fig, ax = plt.subplots(figsize=(16, height), dpi=DPI)
    fig.patch.set_facecolor(COLORS["paper"])
    ax.set_facecolor(COLORS["water"])
    fig.suptitle(title, x=0.055, y=0.965, ha="left", fontsize=24, fontweight=750)
    fig.text(0.056, 0.916, subtitle, ha="left", color=COLORS["muted"], fontsize=12)
    return fig, ax


def finish_map(fig, ax, map_id: str, footnote: str, *, tight=True) -> None:
    ax.set_axis_off()
    fig.text(
        0.055,
        0.028,
        footnote,
        ha="left",
        va="bottom",
        fontsize=8.4,
        color=COLORS["muted"],
        wrap=True,
    )
    if tight:
        fig.subplots_adjust(left=0.045, right=0.955, top=0.885, bottom=0.09)
    output = MAPS_DIR / map_id / "reference.png"
    fig.savefig(
        output,
        dpi=DPI,
        facecolor=fig.get_facecolor(),
        metadata={"Software": "maps etalon builder"},
    )
    plt.close(fig)
    with output.open("rb") as handle:
        from PIL import Image

        with Image.open(handle) as image:
            if image.width != WIDTH_PX:
                raise AssertionError(f"{output}: expected 1600px, got {image.width}px")


def colorbar(fig, ax, mappable, label: str, *, ticks=None, formatter=None) -> None:
    bar = fig.colorbar(mappable, ax=ax, fraction=0.028, pad=0.018, shrink=0.75)
    bar.set_label(label, fontsize=10, fontweight=650, labelpad=10)
    if ticks is not None:
        bar.set_ticks(ticks)
    if formatter is not None:
        bar.ax.yaxis.set_major_formatter(formatter)
    bar.outline.set_linewidth(0.6)


def build_gdp_world() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_GDP_URL, "owid_gdp_per_capita.csv"))
    col = variable_column(raw)
    values = latest_rows(iso_rows(raw))
    truth = standard_truth(values, col, "constant 2021 international-$ per person")
    write_csv("gdp_world", truth)
    year_min, year_max = int(truth.year.min()), int(truth.year.max())
    write_md(
        "gdp_world",
        [
            f"- URL/API: {OWID_GDP_URL}",
            f"- Vintage: latest available observation per ISO country ({year_min}–{year_max}); downloaded OWID Grapher release cached by this builder.",
            "- Source/licence: Eurostat, OECD, IMF and World Bank via Our World in Data; OWID grapher data are CC BY 4.0.",
            f"- Rows: {len(truth)} countries; unit is PPP-adjusted constant 2021 international dollars per person.",
            "- Caveat: countries have different latest years; non-country OWID aggregates and rows without ISO3 codes are excluded.",
        ],
    )
    world = load_world().to_crs(8857)
    merged = world.merge(truth[["key", "value"]], on="key", how="left")
    fig, ax = setup_figure(
        "GDP per person around the world",
        "Latest available year · purchasing-power adjusted · constant 2021 international dollars",
        height=8.8,
    )
    merged.plot(ax=ax, color=COLORS["missing"], edgecolor="#87949a", linewidth=0.25)
    norm = LogNorm(vmin=1_000, vmax=150_000)
    merged.dropna(subset=["value"]).plot(
        ax=ax, column="value", cmap="viridis", norm=norm,
        edgecolor=COLORS["border"], linewidth=0.18,
    )
    ax.set_xlim(-17_300_000, 17_300_000)
    ax.set_ylim(-8_300_000, 8_600_000)
    sm = mpl.cm.ScalarMappable(norm=norm, cmap="viridis")
    colorbar(
        fig, ax, sm, "GDP per person (constant 2021 int-$, log scale)",
        ticks=[1_000, 3_000, 10_000, 30_000, 100_000],
        formatter=mpl.ticker.FuncFormatter(lambda x, _p: f"${x/1000:g}k"),
    )
    finish_map(
        fig, ax, "gdp_world",
        "Sources: Eurostat/OECD/IMF/World Bank via OWID (CC BY 4.0), latest by country; Natural Earth 1:50m (public domain). Equal Earth; logarithmic continuous colour. Grey = no value.",
    )
    return truth


UKR_LABELS = {
    "UA-43": ("Autonomous Republic of Crimea", None),
    "UA-05": ("Vinnytsia Oblast", "Вінницька"),
    "UA-07": ("Volyn Oblast", "Волинська"),
    "UA-12": ("Dnipropetrovsk Oblast", "Дніпропетровська"),
    "UA-14": ("Donetsk Oblast", "Донецька"),
    "UA-18": ("Zhytomyr Oblast", "Житомирська"),
    "UA-21": ("Zakarpattia Oblast", "Закарпатська"),
    "UA-23": ("Zaporizhzhia Oblast", "Запорізька"),
    "UA-26": ("Ivano-Frankivsk Oblast", "Івано-Франківська"),
    "UA-32": ("Kyiv Oblast", "Київська"),
    "UA-35": ("Kirovohrad Oblast", "Кіровоградська"),
    "UA-09": ("Luhansk Oblast", "Луганська"),
    "UA-46": ("Lviv Oblast", "Львівська"),
    "UA-48": ("Mykolaiv Oblast", "Миколаївська"),
    "UA-51": ("Odesa Oblast", "Одеська"),
    "UA-53": ("Poltava Oblast", "Полтавська"),
    "UA-56": ("Rivne Oblast", "Рівненська"),
    "UA-59": ("Sumy Oblast", "Сумська"),
    "UA-61": ("Ternopil Oblast", "Тернопільська"),
    "UA-63": ("Kharkiv Oblast", "Харківська"),
    "UA-65": ("Kherson Oblast", "Херсонська"),
    "UA-68": ("Khmelnytskyi Oblast", "Хмельницька"),
    "UA-71": ("Cherkasy Oblast", "Черкаська"),
    "UA-77": ("Chernivtsi Oblast", "Чернівецька"),
    "UA-74": ("Chernihiv Oblast", "Чернігівська"),
    "UA-30": ("Kyiv city", "м.Київ"),
    "UA-40": ("Sevastopol", None),
}


def parse_ukrstat() -> pd.DataFrame:
    pdf = fetch(UKRSTAT_PDF, "ukrstat_population_2022.pdf")
    reader = PdfReader(str(pdf))
    page = reader.pages[6].extract_text() or ""
    rows = []
    for code, (name, label) in UKR_LABELS.items():
        value: int | None = None
        if label is not None:
            match = re.search(re.escape(label) + r"\s+(\d{5,8})", page)
            if not match:
                raise ValueError(f"Could not parse {label!r} from Ukrstat table 1")
            value = int(match.group(1))
        rows.append(
            {"key": code, "name": name, "value": value,
             "unit": "persons", "year": 2022}
        )
    truth = pd.DataFrame(rows).sort_values("key").reset_index(drop=True)
    if truth.loc[truth.key == "UA-30", "value"].iloc[0] != 2_952_301:
        raise ValueError("Ukrstat table validation failed for Kyiv city")
    return truth


def build_ukraine_oblast_population() -> pd.DataFrame:
    truth = parse_ukrstat()
    write_csv("ukraine_oblast_population", truth)
    write_md(
        "ukraine_oblast_population",
        [
            f"- URL/API: {UKRSTAT_PAGE} (file: {UKRSTAT_PDF}).",
            "- Vintage: estimated present population on 1 January 2022, table 1 of the official bilingual Ukrstat publication.",
            "- Source/licence: State Statistics Service of Ukraine; reproduced with attribution. Boundaries: geoBoundaries gbOpen (ODbL 1.0), 2017 representation.",
            f"- Rows: {len(truth)} — 24 oblasts, Crimea, Kyiv city and Sevastopol; key is ISO 3166-2.",
            "- Caveat: the official table prints ellipses for Crimea and Sevastopol, so both values are blank and are hatched on the map.",
        ],
    )
    shapes = gpd.read_file(fetch(UKRAINE_GEO_URL, "ukraine_adm1.geojson"))
    shapes = shapes.rename(columns={"shapeISO": "key", "shapeName": "shape_name"})
    merged = shapes.merge(truth[["key", "value"]], on="key", how="left").to_crs(3035)
    fig, ax = setup_figure(
        "Ukraine’s population by oblast",
        "Estimated present population · 1 January 2022 · proportional circles",
        height=9.8,
    )
    merged.plot(ax=ax, color="#e9e5dc", edgecolor="white", linewidth=0.75)
    missing = merged[merged.value.isna()]
    missing.plot(
        ax=ax, facecolor=COLORS["missing"], edgecolor="#7a8184", linewidth=0.9,
        hatch="////",
    )
    known = merged.dropna(subset=["value"]).copy()
    points = known.geometry.representative_point()
    sizes = known.value / known.value.max() * 1750 + 22
    ax.scatter(
        points.x, points.y, s=sizes, color="#256fa1", alpha=0.68,
        edgecolor="white", linewidth=0.8, zorder=4,
    )
    kyiv = known[known.key == "UA-30"]
    kp = kyiv.geometry.representative_point().iloc[0]
    ax.scatter([kp.x], [kp.y], s=210, facecolor="#ee9b00", edgecolor="white", linewidth=1.2, zorder=6)
    ax.annotate("Kyiv city", (kp.x, kp.y), xytext=(12, 9), textcoords="offset points", fontsize=9, fontweight=650)
    legend_sizes = [1_000_000, 2_000_000, 4_000_000]
    handles = [
        ax.scatter([], [], s=v / known.value.max() * 1750 + 22, color="#256fa1", alpha=.68, edgecolor="white")
        for v in legend_sizes
    ]
    handles.append(Patch(facecolor=COLORS["missing"], edgecolor="#777", hatch="////"))
    labels = ["1 million", "2 million", "4 million", "No Ukrstat estimate"]
    ax.legend(handles, labels, title="Resident population", loc="lower left", frameon=False, labelspacing=1.3)
    minx, miny, maxx, maxy = merged.total_bounds
    ax.set_xlim(minx - 80_000, maxx + 80_000)
    ax.set_ylim(miny - 80_000, maxy + 80_000)
    finish_map(
        fig, ax, "ukraine_oblast_population",
        "Population: State Statistics Service of Ukraine, 1 Jan 2022. Boundaries: geoBoundaries/OSM (ODbL 1.0). ETRS89-LAEA Europe (EPSG:3035); proportional area symbols. Crimea and Sevastopol hatched: Ukrstat published no 2022 estimates.",
    )
    return truth


def load_eia() -> pd.DataFrame:
    source = fetch(EIA_URL, "Complete_SEDS.csv", timeout=360)
    selected: list[pd.DataFrame] = []
    for chunk in pd.read_csv(source, chunksize=250_000, usecols=["MSN", "StateCode", "Year", "Data"]):
        keep = chunk[chunk.MSN.isin(["DFTCP", "TPOPP"]) & chunk.StateCode.isin(STATE_CODES)]
        if len(keep):
            selected.append(keep)
    data = pd.concat(selected, ignore_index=True)
    common_years = set(data.loc[data.MSN == "DFTCP", "Year"]) & set(data.loc[data.MSN == "TPOPP", "Year"])
    year = max(common_years)
    latest = data[data.Year == year].pivot(index="StateCode", columns="MSN", values="Data")
    if set(latest.index) != STATE_CODES:
        raise ValueError(f"EIA state mismatch: missing {sorted(STATE_CODES - set(latest.index))}")
    return latest.reset_index().rename(columns={"StateCode": "key"}).assign(year=year)


def build_us_diesel() -> pd.DataFrame:
    eia = load_eia()
    states = gpd.read_file(fetch(STATES_URL, "cb_2024_us_state_20m.zip"))
    names = states.set_index("STUSPS").NAME.to_dict()
    truth = pd.DataFrame(
        {
            "key": eia.key,
            "name": eia.key.map(names),
            "value": eia.DFTCP,
            "unit": "thousand barrels per year",
            "year": eia.year,
            "total_thousand_barrels": eia.DFTCP,
            "per_capita_barrels": eia.DFTCP / eia.TPOPP,
        }
    ).sort_values("key").reset_index(drop=True)
    write_csv("us_diesel", truth)
    year = int(truth.year.iloc[0])
    write_md(
        "us_diesel",
        [
            f"- URL/API: {EIA_URL} (code definitions: {EIA_CODES_URL}).",
            f"- Vintage: EIA SEDS complete release, {year}; DFTCP is total distillate fuel oil consumption and TPOPP is resident population.",
            "- Source/licence: U.S. Energy Information Administration (U.S. government public domain); boundaries are U.S. Census TIGER/Line 2024 (public domain).",
            f"- Rows: {len(truth)} — 50 states plus DC. `value`/`total_thousand_barrels` are annual total; `per_capita_barrels` is DFTCP ÷ TPOPP.",
            "- Caveat: both EIA series are reported in thousands (barrels and persons), so their ratio is barrels per resident without an additional scale factor.",
        ],
    )
    shapes = states[states.STUSPS.isin(STATE_CODES)].rename(columns={"STUSPS": "key"})
    shapes = shapes.merge(truth[["key", "per_capita_barrels"]], on="key", how="left")
    conus = shapes[~shapes.key.isin(["AK", "HI"])].to_crs(5070)
    fig, ax = setup_figure(
        "Diesel consumption across the United States",
        f"Barrels per resident · {year} · total distillate fuel oil consumption",
        height=9.5,
    )
    limits = np.quantile(truth.per_capita_barrels, [0.02, 0.98])
    norm = Normalize(*limits)
    conus.plot(
        ax=ax, column="per_capita_barrels", cmap="YlOrBr", norm=norm,
        edgecolor="white", linewidth=0.7,
    )
    minx, miny, maxx, maxy = conus.total_bounds
    ax.set_xlim(minx - 150_000, maxx + 150_000)
    ax.set_ylim(miny - 120_000, maxy + 120_000)
    for key, bounds, anchor in (
        ("AK", (-180, 50, -128, 72), [0.07, 0.13, 0.23, 0.25]),
        ("HI", (-161, 18, -154, 23), [0.29, 0.13, 0.15, 0.18]),
    ):
        inset = fig.add_axes(anchor)
        inset.set_facecolor(COLORS["paper"])
        one = shapes[shapes.key == key].to_crs(3338 if key == "AK" else 3759)
        one.plot(
            ax=inset, column="per_capita_barrels", cmap="YlOrBr", norm=norm,
            edgecolor="white", linewidth=.6,
        )
        inset.set_title(key, fontsize=10, loc="left", pad=1)
        inset.set_axis_off()
    sm = mpl.cm.ScalarMappable(norm=norm, cmap="YlOrBr")
    colorbar(fig, ax, sm, "Barrels per resident per year")
    finish_map(
        fig, ax, "us_diesel",
        f"Source: EIA State Energy Data System, {year} (U.S. public domain); Census TIGER/Line 2024. CONUS: USA Contiguous Albers Equal Area (EPSG:5070); AK/HI insets. Continuous per-capita colour; all 50 states + DC, none excluded.",
    )
    return truth


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
        flat = int(flat_text)
        positions = []
        remainder = flat
        for size in reversed(sizes):
            positions.append(remainder % size)
            remainder //= size
        positions.reverse()
        row = {dimension: labels[i][positions[i]] for i, dimension in enumerate(ids)}
        row["value"] = value
        rows.append(row)
    return pd.DataFrame(rows)


def load_eurostat() -> pd.DataFrame:
    payload = json.loads(fetch(EUROSTAT_URL, "eurostat_nama_10r_2gdp.json").read_text())
    return jsonstat_rows(payload)


def build_eu_nuts2_gdp() -> pd.DataFrame:
    shapes = gpd.read_file(fetch(NUTS_URL, "nuts2_2024_3035.geojson"))
    shapes = shapes[shapes.CNTR_CODE.isin(EU27)].copy()
    data = load_eurostat()
    data["time"] = pd.to_numeric(data.time)
    current_codes = set(shapes.NUTS_ID)
    selected = data[data.geo.isin(current_codes | {"EU27_2020"})].copy()
    selected = selected.sort_values(["geo", "time"]).groupby("geo", as_index=False).tail(1)
    names = shapes.set_index("NUTS_ID").NAME_LATN.to_dict()
    names["EU27_2020"] = "European Union (27 countries)"
    truth = pd.DataFrame(
        {
            "key": selected.geo,
            "name": selected.geo.map(names),
            "value": selected.value,
            "unit": "PPS per inhabitant",
            "year": selected.time,
        }
    ).sort_values("key").reset_index(drop=True)
    write_csv("eu_nuts2_gdp", truth)
    region_count = int((truth.key != "EU27_2020").sum())
    years = truth.loc[truth.key != "EU27_2020", "year"]
    write_md(
        "eu_nuts2_gdp",
        [
            f"- URL/API: {EUROSTAT_URL}; geometry: {NUTS_URL}.",
            f"- Vintage: latest observation per current EU NUTS-2 code ({int(years.min())}–{int(years.max())}); NUTS 2024 geometry in EPSG:3035.",
            "- Source/licence: Eurostat reuse policy; administrative boundaries © EuroGeographics and national statistical/geographic institutes.",
            f"- Rows: {len(truth)} — {region_count} EU NUTS-2 regions plus EU27_2020 as the reference needed to verify the `LU00>200%_EU` spot check.",
            "- Caveat: outermost regions are retained in truth but excluded from the main map extent; they are named in the map footnote.",
        ],
    )
    regions = shapes.merge(
        truth[truth.key != "EU27_2020"][["key", "value"]],
        left_on="NUTS_ID", right_on="key", how="left",
    )
    mainland_window = box(2_400_000, 1_300_000, 7_400_000, 5_500_000)
    main = regions[regions.geometry.intersects(mainland_window)].copy()
    excluded = len(regions) - len(main)
    fig, ax = setup_figure(
        "Regional prosperity across the European Union",
        "GDP per inhabitant · purchasing power standards · latest available year",
        height=10,
    )
    main.plot(ax=ax, color=COLORS["missing"], edgecolor="white", linewidth=.35)
    bounds = [10_000, 20_000, 30_000, 40_000, 55_000, 75_000, 110_000]
    norm = BoundaryNorm(bounds, ncolors=256, clip=True)
    main.dropna(subset=["value"]).plot(
        ax=ax, column="value", cmap="YlGnBu", norm=norm,
        edgecolor="white", linewidth=.28,
    )
    ax.set_xlim(2_400_000, 7_400_000)
    ax.set_ylim(1_300_000, 5_500_000)
    sm = mpl.cm.ScalarMappable(norm=norm, cmap="YlGnBu")
    colorbar(
        fig, ax, sm, "GDP per inhabitant (PPS)", ticks=bounds,
        formatter=mpl.ticker.FuncFormatter(lambda x, _p: f"{x/1000:.0f}k"),
    )
    finish_map(
        fig, ax, "eu_nuts2_gdp",
        f"Eurostat nama_10r_2gdp, latest by region; GISCO NUTS 2024, © EuroGeographics. ETRS89-LAEA Europe (EPSG:3035); classed PPS/person. {excluded} outermost/island regions outside the main extent (including Canaries, Azores and French overseas) are explicitly excluded from the drawing, not truth.",
    )
    return truth


def build_ca_county_income() -> pd.DataFrame:
    payload = json.loads(fetch(ACS_URL, "acs_2023_ca_county_income.json").read_text())
    data = pd.DataFrame(payload[1:], columns=payload[0])
    data["value"] = pd.to_numeric(data.B19013_001E, errors="coerce")
    data["key"] = data.state + data.county
    data["name"] = data.NAME.str.replace(", California$", "", regex=True)
    truth = data[["key", "name", "value"]].copy()
    truth["unit"] = "2023 inflation-adjusted US dollars"
    truth["year"] = 2023
    truth = truth[["key", "name", "value", "unit", "year"]].sort_values("key")
    if len(truth) != 58:
        raise ValueError(f"ACS returned {len(truth)} California counties, expected 58")
    write_csv("ca_county_income", truth)
    write_md(
        "ca_county_income",
        [
            f"- URL/API: {ACS_URL}",
            "- Vintage: 2019–2023 American Community Survey 5-year estimate; variable B19013_001E (median household income in the past 12 months).",
            "- Source/licence: U.S. Census Bureau ACS API and TIGER/Line 2023 county boundaries (U.S. government public domain).",
            f"- Rows: {len(truth)} California counties; key is the five-digit state+county FIPS code.",
            "- Caveat: values are survey estimates in 2023 inflation-adjusted dollars; margins of error are not mapped.",
        ],
    )
    counties = gpd.read_file(fetch(COUNTIES_URL, "cb_2023_us_county_500k.zip"))
    counties = counties[counties.STATEFP == "06"].copy()
    counties["key"] = counties.STATEFP + counties.COUNTYFP
    merged = counties.merge(truth[["key", "value"]], on="key", how="left").to_crs(3310)
    fig, ax = setup_figure(
        "Household income across California",
        "Median household income · ACS 2019–2023 five-year estimate",
        height=10,
    )
    bounds = [45_000, 65_000, 80_000, 100_000, 125_000, 160_000, 210_000]
    norm = BoundaryNorm(bounds, ncolors=256, clip=True)
    merged.plot(
        ax=ax, column="value", cmap="BuPu", norm=norm,
        edgecolor="white", linewidth=.75,
    )
    sm = mpl.cm.ScalarMappable(norm=norm, cmap="BuPu")
    colorbar(
        fig, ax, sm, "Median household income (2023 US$)", ticks=bounds,
        formatter=mpl.ticker.StrMethodFormatter("${x:,.0f}"),
    )
    minx, miny, maxx, maxy = merged.total_bounds
    ax.set_xlim(minx - 35_000, maxx + 35_000)
    ax.set_ylim(miny - 35_000, maxy + 35_000)
    finish_map(
        fig, ax, "ca_county_income",
        "Source: U.S. Census Bureau, ACS 2019–2023 5-year B19013_001E; TIGER/Line 2023 (public domain). California Albers (EPSG:3310); classed median income. Exactly 58 California counties; no neighboring states shown.",
    )
    return truth


def build_population_change() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_POP_URL, "owid_population.csv"))
    col = variable_column(raw)
    data = iso_rows(raw)
    pivot = data[data.Year.isin([2000, 2023])].pivot(index=["Code", "Entity"], columns="Year", values=col).dropna()
    pivot["value"] = (pivot[2023] / pivot[2000] - 1) * 100
    pivot = pivot.reset_index()
    pivot["unit"] = "percent change"
    pivot["year"] = "2000–2023"
    truth = pivot[["Code", "Entity", "value", "unit", "year"]].rename(
        columns={"Code": "key", "Entity": "name"}
    ).sort_values("key")
    write_csv("population_change", truth)
    write_md(
        "population_change",
        [
            f"- URL/API: {OWID_POP_URL}",
            "- Vintage: population estimates for 2000 and 2023; percent change is computed as `(2023 / 2000 - 1) × 100`.",
            "- Source/licence: UN World Population Prospects via Our World in Data, CC BY 4.0.",
            f"- Rows: {len(truth)} ISO3 countries with non-missing observations in both years; unit is percent.",
            "- Caveat: OWID aggregates and entities without standard three-letter country codes are excluded.",
        ],
    )
    world = load_world().to_crs(8857)
    merged = world.merge(truth[["key", "value"]], on="key", how="left")
    fig, ax = setup_figure(
        "Population change, 2000–2023",
        "Percent change · blue indicates growth, orange indicates decline",
        height=8.8,
    )
    merged.plot(ax=ax, color=COLORS["missing"], edgecolor="white", linewidth=.2)
    norm = TwoSlopeNorm(vmin=-30, vcenter=0, vmax=200)
    merged.dropna(subset=["value"]).plot(
        ax=ax, column="value", cmap="PuOr_r", norm=norm,
        edgecolor="white", linewidth=.18,
    )
    ax.set_xlim(-17_300_000, 17_300_000)
    ax.set_ylim(-8_300_000, 8_600_000)
    sm = mpl.cm.ScalarMappable(norm=norm, cmap="PuOr_r")
    colorbar(
        fig, ax, sm, "Population change (%)", ticks=[-30, -15, 0, 50, 100, 150, 200],
        formatter=mpl.ticker.StrMethodFormatter("{x:.0f}%"),
    )
    finish_map(
        fig, ax, "population_change",
        "Source: UN WPP via OWID (CC BY 4.0), computed 2000→2023; Natural Earth 1:50m (public domain). Equal Earth; continuous diverging colour centred exactly at zero. Grey = incomplete two-year data.",
    )
    return truth


def overpass_rows() -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    payload = json.loads(fetch_overpass().read_text())
    geod = Geod(ellps="WGS84")
    rows = []
    geoms = []
    for element in payload["elements"]:
        coordinates = [(p["lon"], p["lat"]) for p in element.get("geometry", [])]
        if len(coordinates) < 2:
            continue
        line = LineString(coordinates)
        length_km = geod.geometry_length(line) / 1000
        tags = element.get("tags", {})
        rows.append(
            {
                "osm_id": element["id"],
                "name": tags.get("name") or tags.get("ref") or "unnamed high-speed railway",
                "length_km": length_km,
                "geometry_wkt": line.wkt,
            }
        )
        geoms.append(line)
    truth = pd.DataFrame(rows).sort_values("osm_id").reset_index(drop=True)
    return truth, gpd.GeoDataFrame(truth.drop(columns="geometry_wkt"), geometry=geoms, crs=4326)


def build_france_lgv() -> pd.DataFrame:
    truth, lines = overpass_rows()
    write_csv("france_lgv", truth)
    total = truth.length_km.sum()
    write_md(
        "france_lgv",
        [
            "- URL/API: Overpass QL at https://overpass-api.de/api/interpreter (with public mirror fallback): France ISO area; `way[railway=rail][highspeed=yes]`; `out tags geom`.",
            "- Vintage: live OpenStreetMap snapshot on first uncached run; delete `.cache/france_lgv_overpass.json` to refresh.",
            "- Source/licence: © OpenStreetMap contributors, Open Database Licence (ODbL) 1.0; regional basemap is geoBoundaries gbOpen/IGN (Etalab 2.0).",
            f"- Rows: {len(truth)} OSM ways; `length_km` is WGS84 ellipsoidal way length and `geometry_wkt` makes line-near checks reproducible. Sum is {total:,.0f} track-km.",
            "- Caveat: OSM ways are individual mapped tracks/segments, so parallel tracks and way splits are retained rather than dissolved into a route-kilometre estimate.",
        ],
    )
    regions = gpd.read_file(fetch(FRANCE_GEO_URL, "france_adm1.geojson")).to_crs(2154)
    lines = lines.to_crs(2154)
    fig, ax = setup_figure(
        "France’s high-speed railway network",
        "OpenStreetMap railway tracks tagged highspeed=yes · metropolitan France",
        height=10,
    )
    regions.plot(ax=ax, color="#e7e4dc", edgecolor="white", linewidth=.7)
    lines.plot(ax=ax, color="#c1121f", linewidth=1.5, alpha=.80, zorder=3)
    cities = {
        "Paris": (2.3522, 48.8566), "Lyon": (4.8357, 45.7640),
        "Marseille": (5.3698, 43.2965), "Bordeaux": (-0.5792, 44.8378),
        "Lille": (3.0573, 50.6292), "Strasbourg": (7.7521, 48.5734),
        "Rennes": (-1.6778, 48.1173),
    }
    city_gdf = gpd.GeoDataFrame(
        {"name": list(cities)}, geometry=[Point(v) for v in cities.values()], crs=4326
    ).to_crs(2154)
    ax.scatter(city_gdf.geometry.x, city_gdf.geometry.y, s=18, color=COLORS["ink"], zorder=5)
    offsets = {"Paris": (8, 7), "Lyon": (8, -12), "Marseille": (8, -3), "Bordeaux": (-54, 5), "Lille": (8, 2), "Strasbourg": (8, 0), "Rennes": (-45, 5)}
    for row in city_gdf.itertuples():
        ax.annotate(row.name, (row.geometry.x, row.geometry.y), xytext=offsets[row.name], textcoords="offset points", fontsize=9, fontweight=600)
    ax.legend(
        [Line2D([0], [0], color="#c1121f", linewidth=3)],
        ["High-speed railway track"], loc="lower left", frameon=False,
    )
    ax.set_xlim(80_000, 1_280_000)
    ax.set_ylim(6_030_000, 7_190_000)
    finish_map(
        fig, ax, "france_lgv",
        "Railways: © OpenStreetMap contributors, ODbL 1.0, live snapshot cached by builder. Regions: geoBoundaries/IGN, Etalab 2.0. Lambert-93 (EPSG:2154); metropolitan extent only, overseas departments excluded. Red line is every OSM way tagged railway=rail + highspeed=yes.",
    )
    return truth


def load_minard(name: str) -> pd.DataFrame:
    url = f"{MINARD_BASE}/Minard.{name}.csv"
    return pd.read_csv(fetch(url, f"minard_{name}.csv"))


def build_napoleon_1812() -> pd.DataFrame:
    troops = load_minard("troops").rename(columns={"long": "lon"})
    cities = load_minard("cities").rename(columns={"long": "lon"})
    temps = load_minard("temp").rename(columns={"long": "lon", "temp": "temperature_c"})
    records = []
    for row in troops.itertuples():
        records.append({"record_type": "troop", "lon": row.lon, "lat": row.lat, "survivors": row.survivors, "direction": row.direction, "group": row.group, "city": None, "temperature_c": None, "date": None})
    for row in cities.itertuples():
        records.append({"record_type": "city", "lon": row.lon, "lat": row.lat, "survivors": None, "direction": None, "group": None, "city": row.city, "temperature_c": None, "date": None})
    for row in temps.itertuples():
        records.append({"record_type": "temperature", "lon": row.lon, "lat": None, "survivors": None, "direction": "R", "group": None, "city": None, "temperature_c": row.temperature_c, "date": row.date})
    truth = pd.DataFrame(records)
    write_csv("napoleon_1812", truth)
    urls = ", ".join(f"{MINARD_BASE}/Minard.{n}.csv" for n in ("troops", "cities", "temp"))
    write_md(
        "napoleon_1812",
        [
            f"- URL/API: {urls}",
            "- Vintage: Charles Joseph Minard’s 1869 graphic, transcribed by Lee Wilkinson for *The Grammar of Graphics* and distributed in R HistData/Rdatasets.",
            "- Source/licence: historical Minard data via the HistData package (GPL-2/GPL-3 package); attribution to Minard (1869), Wilkinson and Michael Friendly.",
            f"- Rows: {len(truth)} combined records; `record_type` distinguishes troop vertices, city labels and retreat temperatures.",
            "- Caveat: the 422,000 start is the sum of the first survivor count in each of three army groups (340,000 + 60,000 + 22,000).",
        ],
    )
    fig = plt.figure(figsize=(16, 10), dpi=DPI, facecolor=COLORS["paper"])
    grid = fig.add_gridspec(4, 1, height_ratios=[3.6, .08, 1, .08], hspace=.04)
    ax = fig.add_subplot(grid[0])
    tax = fig.add_subplot(grid[2], sharex=ax)
    ax.set_facecolor("#eeeae1")
    tax.set_facecolor(COLORS["paper"])
    fig.suptitle("Napoleon’s 1812 campaign", x=.055, y=.965, ha="left", fontsize=24, fontweight=750)
    fig.text(.056, .918, "Army size encoded directly in the width of Minard’s advance and retreat", color=COLORS["muted"], fontsize=12)
    max_survivors = troops.survivors.max()
    for (_group, _direction), group in troops.groupby(["group", "direction"], sort=True):
        group = group.reset_index(drop=True)
        colour = "#c99a45" if group.direction.iloc[0] == "A" else "#2f3e46"
        for a, b in zip(group.iloc[:-1].itertuples(), group.iloc[1:].itertuples()):
            width = max(1.2, ((a.survivors + b.survivors) / 2) / max_survivors * 36)
            ax.plot([a.lon, b.lon], [a.lat, b.lat], color=colour, linewidth=width, solid_capstyle="round", alpha=.90, zorder=2 if colour == "#c99a45" else 3)
    ax.scatter(cities.lon, cities.lat, s=12, color=COLORS["ink"], zorder=5)
    for row in cities.itertuples():
        if row.city in {"Kowno", "Wilna", "Smorgoni", "Minsk", "Mohilow", "Orscha", "Smolensk", "Dorogobouge", "Wixma", "Mojaisk", "Moscou"}:
            ax.annotate(row.city, (row.lon, row.lat), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8.5)
    ax.text(24.1, 55.23, "422,000 set out", fontsize=11, fontweight=700, color="#8d641e")
    ax.text(37.55, 55.55, "100,000 reach Moscow", fontsize=10, ha="right", fontweight=700)
    ax.legend(
        [Line2D([0], [0], color="#c99a45", lw=8), Line2D([0], [0], color="#2f3e46", lw=8)],
        ["Advance", "Retreat"], loc="lower left", frameon=False, ncol=2,
    )
    ax.set_xlim(23.5, 38.2)
    ax.set_ylim(53.7, 56.3)
    ax.set_yticks([])
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    tax.plot(temps.lon, temps.temperature_c, color="#457b9d", linewidth=2.4, marker="o", markersize=4)
    tax.fill_between(temps.lon, temps.temperature_c, 3, color="#a8dadc", alpha=.35)
    for row in temps.itertuples():
        tax.annotate(f"{int(row.temperature_c)}°", (row.lon, row.temperature_c), xytext=(0, -14), textcoords="offset points", ha="center", fontsize=8)
    tax.set_ylabel("Retreat temp. (°C)", fontsize=9)
    tax.set_ylim(-32, 5)
    tax.grid(axis="y", linewidth=.4, color="#bbc2c3")
    tax.set_xlabel("Longitude east", fontsize=9)
    for side in ("top", "right"):
        tax.spines[side].set_visible(False)
    fig.text(.055, .025, "Source: Charles Joseph Minard (1869), transcription by Lee Wilkinson via R HistData/Rdatasets. Geographic plate carrée; line width proportional to survivors at every segment. Historical place-name spellings retained.", fontsize=8.5, color=COLORS["muted"])
    fig.subplots_adjust(left=.055, right=.97, top=.885, bottom=.09)
    output = MAPS_DIR / "napoleon_1812" / "reference.png"
    fig.savefig(output, dpi=DPI, facecolor=fig.get_facecolor(), metadata={"Software": "maps etalon builder"})
    plt.close(fig)
    return truth


def build_quakes_2025() -> pd.DataFrame:
    payload = json.loads(fetch(USGS_URL, "usgs_quakes_2025_m6.geojson").read_text())
    rows = []
    for feature in payload["features"]:
        lon, lat, _depth = feature["geometry"]["coordinates"]
        props = feature["properties"]
        rows.append(
            {"id": feature["id"], "time": pd.to_datetime(props["time"], unit="ms", utc=True).isoformat(), "lon": lon, "lat": lat, "mag": props["mag"], "place": props["place"]}
        )
    truth = pd.DataFrame(rows).sort_values(["time", "id"]).reset_index(drop=True)
    write_csv("quakes_2025", truth)
    write_md(
        "quakes_2025",
        [
            f"- URL/API: {USGS_URL}",
            "- Vintage: all reviewed/reported USGS events from 2025-01-01 inclusive to 2026-01-01 exclusive with magnitude ≥6.0; cached GeoJSON response.",
            "- Source/licence: U.S. Geological Survey Earthquake Hazards Program; U.S. government data are public domain.",
            f"- Rows: {len(truth)} earthquakes; one row per USGS event with UTC time, WGS84 longitude/latitude, magnitude and place.",
            "- Caveat: USGS event parameters can be revised; delete the cache to fetch the current catalogue interpretation.",
        ],
    )
    world = load_world().to_crs(8857)
    points = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(8857)
    points = points.sort_values("mag")
    fig, ax = setup_figure(
        "Magnitude 6+ earthquakes in 2025",
        f"{len(truth)} events · symbol area increases steeply with earthquake magnitude",
        height=8.8,
    )
    world.plot(ax=ax, color="#e6e2d9", edgecolor="white", linewidth=.2)
    sizes = 16 * np.power(2.15, points.mag - 6)
    ax.scatter(
        points.geometry.x, points.geometry.y, s=sizes,
        c=points.mag, cmap="inferno", norm=Normalize(6, 9),
        alpha=.62, edgecolor="white", linewidth=.45, zorder=3,
    )
    for magnitude in (6, 7, 8, 8.8):
        ax.scatter([], [], s=16 * 2.15 ** (magnitude - 6), facecolor="#d1495b", alpha=.62, edgecolor="white", label=f"M{magnitude:g}")
    ax.legend(title="Magnitude", loc="lower left", ncol=4, frameon=False)
    largest = points.nlargest(2, "mag")
    for row in largest.itertuples():
        ax.annotate(f"M{row.mag:g}\n{str(row.place).split(',')[0]}", (row.geometry.x, row.geometry.y), xytext=(8, 10), textcoords="offset points", fontsize=8.5, fontweight=650)
    ax.set_xlim(-17_300_000, 17_300_000)
    ax.set_ylim(-8_300_000, 8_600_000)
    finish_map(
        fig, ax, "quakes_2025",
        "Source: USGS FDSN Event Web Service, 1 Jan–31 Dec 2025, M≥6 (public domain); Natural Earth 1:50m. Equal Earth; translucent proportional symbols, smallest first/largest last. No events excluded.",
    )
    return truth


def build_eu_renewables() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_RENEW_URL, "owid_share_electricity_renewables.csv"))
    col = variable_column(raw)
    data = latest_rows(iso_rows(raw))
    data = data[data.Code.isin(EUROPE)]
    truth = standard_truth(data, col, "percent of electricity")
    write_csv("eu_renewables", truth)
    write_md(
        "eu_renewables",
        [
            f"- URL/API: {OWID_RENEW_URL}",
            f"- Vintage: latest observation per explicitly named European country ({int(truth.year.min())}–{int(truth.year.max())}).",
            "- Source/licence: Ember and Energy Institute via Our World in Data, CC BY 4.0; Natural Earth boundaries are public domain.",
            f"- Rows: {len(truth)} countries selected by an explicit ISO/name list, never by a bounding box; unit is percent of electricity generation.",
            "- Caveat: microstates/countries absent from the source have no row; Russia is clipped by the European map extent, while Armenia and the other South Caucasus states are excluded by the country list.",
        ],
    )
    world = load_world()
    world = world[world.key.isin(EUROPE)].merge(truth[["key", "value"]], on="key", how="left").to_crs(3035)
    fig, ax = setup_figure(
        "Renewable electricity across Europe",
        "Share of electricity generated from renewable sources · latest available year",
        height=10,
    )
    world.plot(ax=ax, color=COLORS["missing"], edgecolor="white", linewidth=.5)
    norm = Normalize(0, 100)
    world.dropna(subset=["value"]).plot(
        ax=ax, column="value", cmap="YlGn", norm=norm,
        edgecolor="white", linewidth=.5,
    )
    ax.set_xlim(2_300_000, 6_800_000)
    ax.set_ylim(1_250_000, 5_600_000)
    sm = mpl.cm.ScalarMappable(norm=norm, cmap="YlGn")
    colorbar(
        fig, ax, sm, "Renewable share of electricity (0–100%)",
        ticks=[0, 20, 40, 60, 80, 100],
        formatter=mpl.ticker.StrMethodFormatter("{x:.0f}%"),
    )
    finish_map(
        fig, ax, "eu_renewables",
        "Source: Ember/Energy Institute via OWID (CC BY 4.0), latest by country; Natural Earth 1:50m (public domain). ETRS89-LAEA Europe (EPSG:3035); continuous fixed 0–100% scale. Countries selected explicitly; Armenia excluded, Russia clipped at map edge.",
    )
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


def truth_value(frame: pd.DataFrame, key: str) -> float:
    row = frame[frame.key.astype(str) == key]
    if row.empty or pd.isna(row.value.iloc[0]):
        raise ValueError(f"no numeric truth for {key}")
    return float(row.value.iloc[0])


def geodesic_distance_km(lon1, lat1, lon2, lat2) -> float:
    return Geod(ellps="WGS84").inv(lon1, lat1, lon2, lat2)[2] / 1000


def evaluate_spot(map_id: str, spot: str, frame: pd.DataFrame) -> tuple[bool, str]:
    try:
        if spot.startswith("count="):
            expected, tolerance = re.fullmatch(r"count=([\d.]+)±([\d.]+)%", spot).groups()
            actual = len(frame)
            ok = abs(actual - float(expected)) <= float(expected) * float(tolerance) / 100
            return ok, f"{actual}"
        if spot.startswith("total_km="):
            expected, tolerance = re.fullmatch(r"total_km=([\d.]+)±([\d.]+)%", spot).groups()
            actual = float(frame.length_km.sum())
            ok = abs(actual - float(expected)) <= float(expected) * float(tolerance) / 100
            return ok, f"{actual:.0f} km"
        if spot.startswith("line_near="):
            lon, lat = map(float, spot.split("=", 1)[1].split(","))
            point = Point(lon, lat)
            actual = min(wkt.loads(text).distance(point) for text in frame.geometry_wkt)
            return actual <= .20, f"{actual:.3f}°"
        if spot.startswith("event_near="):
            lon, lat = map(float, spot.split("=", 1)[1].split(","))
            actual = min(geodesic_distance_km(lon, lat, row.lon, row.lat) for row in frame.itertuples())
            return actual <= 400, f"{actual:.0f} km"
        if map_id == "napoleon_1812":
            troops = frame[frame.record_type == "troop"].copy()
            troops["survivors"] = pd.to_numeric(troops.survivors)
            if spot.startswith("start_survivors="):
                actual = troops.groupby("group", dropna=True).first().survivors.sum()
            elif spot.startswith("moscow_survivors="):
                actual = troops.loc[(troops.lon - 37.6).abs() < .05, "survivors"].max()
            elif spot == "end_survivors<15000":
                actual = troops.loc[troops.direction == "R", "survivors"].min()
                return actual < 15_000, f"{actual:.0f}"
            else:
                raise ValueError("unknown Minard check")
            expected, tolerance = re.search(r"=([\d.]+)±([\d.]+)%", spot).groups()
            ok = abs(actual - float(expected)) <= float(expected) * float(tolerance) / 100
            return ok, f"{actual:.0f}"
        if "_per_capita" in spot:
            left, right = spot.split(">")
            left = left.removesuffix("_per_capita")
            right = right.removesuffix("_per_capita")
            a = float(frame.loc[frame.key == left, "per_capita_barrels"].iloc[0])
            b = float(frame.loc[frame.key == right, "per_capita_barrels"].iloc[0])
            return a > b, f"{a:.2f}>{b:.2f}"
        if spot == "LU00>200%_EU":
            value = truth_value(frame, "LU00")
            eu = truth_value(frame, "EU27_2020")
            actual = value / eu * 100
            return actual > 200, f"{actual:.0f}% EU"
        comparison = re.fullmatch(r"([A-Z0-9-]+)([<>])([A-Z0-9-]+|[\d.]+)", spot)
        if comparison:
            left, operator, right = comparison.groups()
            a = truth_value(frame, left)
            b = truth_value(frame, right) if re.search(r"[A-Z]", right) else float(right)
            ok = a > b if operator == ">" else a < b
            return ok, f"{a:.4g}{operator}{b:.4g}"
        equality = re.fullmatch(r"([A-Z0-9-]+)=([\d.]+)±([\d.]+)(%)?", spot)
        if equality:
            key, expected, tolerance, percent = equality.groups()
            actual = truth_value(frame, key)
            expected_f = float(expected)
            tolerance_f = expected_f * float(tolerance) / 100 if percent else float(tolerance)
            return abs(actual - expected_f) <= tolerance_f, f"{actual:.4g}"
        raise ValueError("unsupported expression")
    except Exception as exc:
        return False, f"unverifiable: {exc}"


def evaluate(ids: Iterable[str]) -> tuple[dict[str, list[tuple[str, bool, str]]], dict[str, int]]:
    specs = tomllib.loads(SPECS_PATH.read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in specs["map"]}
    results = {}
    counts = {}
    for map_id in ids:
        frame = pd.read_csv(MAPS_DIR / map_id / "truth.csv")
        counts[map_id] = len(frame)
        results[map_id] = [
            (spot, *evaluate_spot(map_id, spot, frame)) for spot in by_id[map_id]["spot"]
        ]
    return results, counts


def render_report(ids: Iterable[str], results, counts) -> str:
    lines = [
        "# Etalon build report",
        "",
        "Generated by `uv run evals/maps/etalons/build.py`. Values are evaluated only from the generated `truth.csv` files.",
        "",
        "| Map | Rows | Spot checks |",
        "|---|---:|---|",
    ]
    failures = []
    for map_id in ids:
        cells = []
        for spot, ok, measured in results[map_id]:
            state = "PASS" if ok else "FAIL"
            cells.append(f"{state} `{spot}` ({measured})")
            if not ok:
                failures.append((map_id, spot, measured))
        lines.append(f"| `{map_id}` | {counts[map_id]} | {'<br>'.join(cells)} |")
    lines.extend(["", "## Problems and measured caveats", ""])
    if failures:
        for map_id, spot, measured in failures:
            lines.append(f"- **FAIL** `{map_id}` / `{spot}`: measured {measured}; the source value is preserved rather than adjusted.")
    else:
        lines.append("- All configured spot checks pass against the generated truth files.")
    lines.extend(
        [
            "- `ukraine_oblast_population`: Ukrstat’s official table has no 1 January 2022 estimate for Crimea or Sevastopol. Both rows are present with blank values and both geometries are hatched.",
            "- `eu_nuts2_gdp`: the EU27 aggregate row is included solely to make the percent-of-EU spot check independently reproducible; it is not drawn as a region.",
            "- `france_lgv`: OpenStreetMap stores split and parallel tracks as separate ways. The truth total is track/way length, not an editorially dissolved route length.",
            "- Live-source vintages are fixed by the cache. Remove `evals/maps/etalons/.cache/` to refresh them.",
            "",
        ]
    )
    return "\n".join(lines)


def print_results(ids: Iterable[str], results, counts) -> None:
    print("\nSpot-check results")
    print(f"{'map id':31} {'rows':>5}  checks")
    print("-" * 100)
    for map_id in ids:
        checks = "; ".join(
            f"{'PASS' if ok else 'FAIL'} {spot} [{measured}]"
            for spot, ok, measured in results[map_id]
        )
        print(f"{map_id:31} {counts[map_id]:5d}  {checks}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=ALL_IDS, help="map ids; default: all ten")
    args = parser.parse_args()
    ids = tuple(args.ids) or ALL_IDS
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for map_id in ids:
        print(f"\n[{map_id}]", flush=True)
        BUILDERS[map_id]()
    # REPORT always covers every currently built output when possible.  A partial
    # invocation reports the requested maps, so it remains useful while iterating.
    results, counts = evaluate(ids)
    print_results(ids, results, counts)
    report = render_report(ids, results, counts)
    (Path(__file__).resolve().parent / "REPORT.md").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
