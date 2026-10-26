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
"""Set 2 reference maps: the five choropleths.

    uv run evals/maps/etalons/set2_choropleth.py [ids...] [--check-only]

us_election_2024, germany_density_nuts3, texas_county_unemployment,
africa_internet, canada_population_change.  Downloads are cached in
``evals/maps/etalons/.cache`` (prefix ``s2c_``); delete a file to refresh it.
Figure scaffolding, downloads and spot evaluation are imported from
``build.py``.  Like it, this file never imports mapsvc.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import textwrap
import zipfile
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as base  # noqa: E402
from build import (  # noqa: E402
    CACHE_DIR, HALO, HATCH, INK, LAND, MUTED, NODATA, PAPER, WATER, WIDTH_IN,
    Canvas, check_window, classify, fetch, jsonstat_rows, latest_rows, iso_rows,
    load_world, plot_classes, plot_missing, project_points, ramp, reassign_crimea, spec, variable_column,
    write_csv, write_md,
)

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402
from shapely.geometry import box  # noqa: E402

IDS = (
    "us_election_2024",
    "germany_density_nuts3",
    "texas_county_unemployment",
    "africa_internet",
    "canada_population_change",
)

MIT_DATASET = "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/42MVDX"
MIT_META = ("https://dataverse.harvard.edu/api/datasets/:persistentId/"
            "?persistentId=doi:10.7910/DVN/42MVDX")
MIT_URL = "https://dataverse.harvard.edu/api/access/datafile/13887042"  # 1976-2024-president.csv
EUROSTAT_DENS_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
    "demo_r_d3dens?format=JSON&lang=EN&unit=PER_KM2&sinceTimePeriod=2019"
)
GISCO = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson"
NUTS3_URL = f"{GISCO}/NUTS_RG_01M_2024_4326_LEVL_3.geojson"
NUTS1_URL = f"{GISCO}/NUTS_RG_01M_2024_4326_LEVL_1.geojson"
BLS_URL = "https://www.bls.gov/lau/laucnty{yy}.xlsx"
OWID_INTERNET_URL = "https://ourworldindata.org/grapher/share-of-individuals-using-the-internet.csv"
STATCAN_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/98100001-eng.zip"
STATCAN_TABLE = "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=9810000101"
STATCAN_BOUNDARY = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
                    "boundary-limites/files-fichiers/lpr_000b21a_e.zip")
NE_ADMIN1_URL = f"{base.NE}/10m_cultural/ne_10m_admin_1_states_provinces.zip"
STATES_500K_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip"

STATE_NAMES_TO_FIX = {"District of Columbia": "District of Columbia"}


# --------------------------------------------------------------------------
# Downloads that need a browser-like request


def fetch_browser(url: str, filename: str, *, magic: bytes) -> Path:
    """BLS answers 403 'Access Denied' (HTML) to non-browser user agents, and
    sometimes even to a browser one; retry a few times and refuse anything
    whose first bytes are not the expected file signature."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    if target.exists() and target.stat().st_size and target.read_bytes()[:len(magic)] == magic:
        return target
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    last = ""
    for _ in range(4):
        print(f"  downloading {url}", flush=True)
        response = requests.get(url, headers=headers, timeout=120)
        if response.status_code == 200 and response.content[:len(magic)] == magic:
            target.write_bytes(response.content)
            return target
        last = f"HTTP {response.status_code}, {response.headers.get('content-type')}"
    raise RuntimeError(f"{url}: no data after retries ({last})")


# --------------------------------------------------------------------------
# Shared drawing helpers


def stepped_legend(ax, colors, edge_labels, title, *, box_w=0.62, box_h=0.2,
                   under: tuple[str, str] | None = None, missing: str | None = None,
                   end_labels: tuple[str, str] | None = None) -> None:
    """Horizontal class bar in inch units, labels on the class boundaries."""
    pos = ax.get_position()
    w_in = pos.width * ax.figure.get_figwidth()
    h_in = pos.height * ax.figure.get_figheight()
    ax.set_xlim(0, w_in)
    ax.set_ylim(0, h_in)
    ax.set_axis_off()
    top = h_in - 0.03
    ax.text(0, top, title, ha="left", va="top", fontsize=10.5, fontweight="bold", linespacing=1.3)
    y = top - 0.30 * (title.count("\n") + 1) - 0.08 - box_h
    for i, colour in enumerate(colors):
        ax.add_patch(Rectangle((i * box_w, y), box_w, box_h, facecolor=colour,
                               edgecolor="white", linewidth=1.0))
    for i, label in enumerate(edge_labels):
        if label:
            ax.text((i + 1) * box_w, y - 0.06, label, ha="center", va="top", fontsize=9)
    if end_labels:
        ax.text(0, y - 0.06, end_labels[0], ha="center", va="top", fontsize=9)
        ax.text(len(colors) * box_w, y - 0.06, end_labels[1], ha="center", va="top", fontsize=9)
    if under:
        ax.text(0, y - 0.30, under[0], ha="left", va="top", fontsize=9, color=MUTED)
        ax.text(len(colors) * box_w, y - 0.30, under[1], ha="right", va="top", fontsize=9, color=MUTED)
    if missing:
        x = len(colors) * box_w + 0.45
        ax.add_patch(Rectangle((x, y), box_w * 0.75, box_h, facecolor=NODATA, edgecolor=HATCH,
                               hatch="/////", linewidth=0.6))
        ax.text(x + box_w * 0.75 + 0.1, y + box_h / 2, missing, va="center", fontsize=9)


def column_legend(ax, colors, labels, title, *, missing: str | None = None,
                  extra: list[tuple[str, str]] | None = None, step=0.27, box=(0.45, 0.21)) -> float:
    """Vertical legend, highest class on top. Returns the y below it (inches)."""
    h = ax.get_ylim()[1]
    y = h - 0.02
    ax.text(0, y, title, va="top", fontsize=10.5, fontweight="bold", linespacing=1.3)
    y -= 0.30 * (title.count("\n") + 1) + 0.12
    for colour, label in zip(colors[::-1], labels[::-1]):
        y -= step
        ax.add_patch(Rectangle((0, y), *box, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        ax.text(box[0] + 0.12, y + box[1] / 2, label, va="center", fontsize=9.5)
    if missing:
        y -= step + 0.08
        ax.add_patch(Rectangle((0, y), *box, facecolor=NODATA, edgecolor=HATCH, hatch="/////", linewidth=0.5))
        ax.text(box[0] + 0.12, y + box[1] / 2, missing, va="center", fontsize=9.5)
    for colour, label in extra or []:
        y -= step + (0.08 if not missing else 0)
        ax.add_patch(Rectangle((0, y), *box, facecolor=colour, edgecolor="#c9c4b8", linewidth=0.4))
        ax.text(box[0] + 0.12, y + box[1] / 2, label, va="center", fontsize=9.5)
    return y


def text_colour(index: float, dark_from: int) -> str:
    return "white" if index >= dark_from else INK


def label(ax, x, y, text, *, dark=False, size=8.5, weight="normal", ha="center", va="center", z=7):
    ax.text(x, y, text, fontsize=size, ha=ha, va=va, zorder=z, fontweight=weight, linespacing=1.05,
            color="white" if dark else INK, path_effects=None if dark else HALO)


def callout(ax, x, y, text, dxdy, *, size=8.3, weight="normal", z=8, ha="center"):
    ax.annotate(text, (x, y), xytext=dxdy, textcoords="offset points", fontsize=size, ha=ha,
                va="center", zorder=z, linespacing=1.05, fontweight=weight, path_effects=HALO,
                arrowprops=dict(arrowstyle="-", color="#4d585d", linewidth=0.6, shrinkA=0, shrinkB=0))


def ranked_lists(ax, frame, fmt, *, n=5, top_title="Highest", bottom_title="Lowest",
                 name_col="name", col2=3.0, width=5.4, size=9.5) -> None:
    h = ax.get_ylim()[1]
    ranked = frame.sort_values("value", ascending=False)
    ax.text(0, h - 0.02, top_title, va="top", fontsize=10.5, fontweight="bold")
    ax.text(col2, h - 0.02, bottom_title, va="top", fontsize=10.5, fontweight="bold")
    for i, row in enumerate(ranked.head(n).itertuples()):
        ax.text(0, h - 0.38 - i * 0.26, getattr(row, name_col), va="top", fontsize=size)
        ax.text(col2 - 0.3, h - 0.38 - i * 0.26, fmt(row.value), va="top", ha="right", fontsize=size)
    for i, row in enumerate(ranked.tail(n).iloc[::-1].itertuples()):
        ax.text(col2, h - 0.38 - i * 0.26, getattr(row, name_col), va="top", fontsize=size)
        ax.text(width, h - 0.38 - i * 0.26, fmt(row.value), va="top", ha="right", fontsize=size)


def us_states() -> gpd.GeoDataFrame:
    states = gpd.read_file(fetch(base.STATES_URL, "cb_2024_us_state_20m.zip"))
    return states[states.STUSPS.isin(base.STATE_CODES)].copy()


# --------------------------------------------------------------------------
# 1. US presidential election 2024


NONVOTES = {"UNDERVOTES", "OVERVOTES", "VOID", "SPOILED"}
TRUMP, HARRIS = "TRUMP, DONALD J.", "HARRIS, KAMALA D."


def build_us_election_2024() -> pd.DataFrame:
    meta = json.loads(fetch(MIT_META, "s2c_mit_president_meta.json").read_text())["data"]["latestVersion"]
    raw = pd.read_csv(fetch(MIT_URL, "s2c_mit_president_1976_2024.csv"))
    d = raw[raw.year == 2024].copy()
    d["candidate"] = d.candidate.fillna("")
    rows = []
    for state, part in d.groupby("state_po"):
        rep = part.loc[part.candidate == TRUMP, "candidatevotes"]
        dem = part.loc[part.candidate == HARRIS, "candidatevotes"]
        if len(rep) != 1 or len(dem) != 1:
            raise ValueError(f"{state}: expected one Trump and one Harris row, got {len(rep)}, {len(dem)}")
        valid = part.loc[~part.candidate.isin(NONVOTES), "candidatevotes"].sum()
        mit_total = part.totalvotes.iloc[0]
        rows.append({
            "key": f"US-{state}", "state_po": state,
            "rep_votes": int(rep.iloc[0]), "dem_votes": int(dem.iloc[0]),
            "valid_votes": int(valid), "mit_totalvotes": int(mit_total),
        })
    t = pd.DataFrame(rows)
    if set(t.state_po) != base.STATE_CODES:
        raise ValueError(f"MIT: state mismatch {set(t.state_po) ^ base.STATE_CODES}")
    t["rep_pct"] = t.rep_votes / t.valid_votes * 100
    t["dem_pct"] = t.dem_votes / t.valid_votes * 100
    t["value"] = t.rep_pct - t.dem_pct
    t["margin_pp_mit_total"] = (t.rep_votes - t.dem_votes) / t.mit_totalvotes * 100
    states = us_states()
    names = states.set_index("STUSPS").NAME.to_dict()
    truth = pd.DataFrame({
        "key": t.key, "name": t.state_po.map(names), "value": t.value,
        "unit": "percentage points (Republican % minus Democratic % of valid votes)", "year": 2024,
        "rep_votes": t.rep_votes, "dem_votes": t.dem_votes, "valid_votes": t.valid_votes,
        "mit_totalvotes": t.mit_totalvotes, "rep_pct": t.rep_pct, "dem_pct": t.dem_pct,
        "margin_pp_mit_total": t.margin_pp_mit_total,
    }).sort_values("key").reset_index(drop=True)
    write_csv("us_election_2024", truth)
    differs = t[t.valid_votes != t.mit_totalvotes]
    drift = (t.value - t.margin_pp_mit_total).abs().max()
    write_md("us_election_2024", [
        f"- URL/API: {MIT_URL} (`1976-2024-president.csv` in {MIT_DATASET}; file list from {MIT_META}); rows `year == 2024`.",
        f"- Vintage: MIT Election Data + Science Lab, 'U.S. President 1976–2024', Harvard Dataverse V{meta['versionNumber']} released {meta['releaseTime'][:10]}; 2024 rows are version 2025-11-20 (certified results).",
        "- Licence: CC0 1.0 (public domain dedication). Boundaries: Census cartographic boundary file 2024, 1:20M (public domain).",
        f"- Rows: {len(truth)} — 50 states plus DC, key `US-XX`. `value` = Trump (R) % − Harris (D) % of valid presidential votes, in percentage points; positive = Republican lead. Each candidate has exactly one row per state in this release (fusion lines such as NY Working Families are pre-summed by MIT).",
        f"- Caveat: MIT's `totalvotes` includes undervotes, overvotes, void and spoiled ballots in {len(differs)} states ({', '.join(sorted(differs.state_po))}); the denominator here drops those rows and keeps third parties, write-ins and Nevada's 'None of these candidates'. Margin on MIT's `totalvotes` is kept as `margin_pp_mit_total`; the two differ by at most {drift:.2f} pp.",
    ])

    crs = "EPSG:5070"
    shapes = states.rename(columns={"STUSPS": "state_po"})
    shapes["key"] = "US-" + shapes.state_po
    shapes = shapes.merge(truth[["key", "value"]], on="key", how="left")
    if shapes.value.isna().any():
        raise ValueError("US election: unjoined state")
    conus = shapes[~shapes.state_po.isin(["AK", "HI"])].to_crs(crs)
    xmin, ymin, xmax, ymax = conus.total_bounds
    extent = (xmin - 120_000, xmax + 260_000, ymin - 420_000, ymax + 80_000)
    check_window("us_election_2024", crs, extent, contains=spec("us_election_2024")["contains"][:1], excludes=[])

    edges = [-30, -20, -10, -5, 0, 5, 10, 20, 30]
    blues = ["#173f7a", "#2f64a8", "#6b9bd1", "#a9c7e8", "#dbe7f4"]
    reds = ["#f6dcd5", "#eeaa9b", "#dc6b5b", "#bb3a33", "#85191d"]
    colors = blues + reds
    classes = dict(zip(truth.key, classify(truth.value, edges)))

    def fmt(v: float) -> str:
        party = "R" if v > 0 else "D"
        return f"{party}+{abs(v):.0f}" if abs(v) >= 9.95 else f"{party}+{abs(v):.1f}"

    def dark(key: str) -> bool:
        return classes[key] <= 1 or classes[key] >= 8

    natl_r = truth.rep_votes.sum() / truth.valid_votes.sum() * 100
    natl_d = truth.dem_votes.sum() / truth.valid_votes.sum() * 100
    cv = Canvas(extent, "2024 US presidential election: margin of victory by state",
                f"Statewide popular vote, Trump (Republican) minus Harris (Democratic), percentage points of all valid votes · "
                f"nationally R+{natl_r - natl_d:.1f} ({natl_r:.1f}% to {natl_d:.1f}%)",
                footer=2.3)
    ax = cv.ax
    world = load_world()
    world[world.key.isin(["CAN", "MEX", "CUB", "BHS"])].to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="none", zorder=1)
    plot_classes(ax, conus, "value", edges, colors, linewidth=0.8)

    offsets = {"VT": (-46, 34), "NH": (-18, 60), "MA": (52, 12), "RI": (46, -6), "CT": (46, -24),
               "NJ": (42, -10), "DE": (42, -16), "MD": (46, -32), "DC": (40, -50)}
    nudges = {"FL": (60_000, 0), "LA": (-60_000, 0), "MI": (95_000, -175_000), "CA": (-30_000, 0),
              "KY": (40_000, -20_000), "WV": (-15_000, -20_000), "ID": (0, -80_000)}
    lookup = truth.set_index("key").value.to_dict()
    for row in conus.itertuples():
        text = f"{row.state_po}\n{fmt(lookup[row.key])}"
        if row.state_po in offsets:
            c = row.geometry.centroid
            callout(ax, c.x, c.y, text.replace("\n", " "), offsets[row.state_po], size=8.3)
        else:
            p = row.geometry.representative_point()
            dx, dy = nudges.get(row.state_po, (0, 0))
            label(ax, p.x + dx, p.y + dy, text, dark=dark(row.key), size=8.3)
    dc = conus[conus.state_po == "DC"].geometry.centroid.iloc[0]
    ax.scatter([dc.x], [dc.y], s=22, marker="s", color=colors[int(classes["US-DC"])], edgecolor=INK,
               linewidth=0.6, zorder=8)

    # Insets: Alaska at 0.36x the main scale, Hawaii at 1x, each equal-area.
    scale = cv.map_w / (extent[1] - extent[0])
    ak = shapes[shapes.state_po == "AK"].to_crs("EPSG:3338")
    hi = shapes[shapes.state_po == "HI"].to_crs(
        "+proj=aea +lat_1=19 +lat_2=21.5 +lat_0=20 +lon_0=-157 +datum=WGS84 +units=m")
    x_cursor = cv.left + 0.15
    for key, frame, factor, point in (("AK", ak, 0.36, spec("us_election_2024")["contains"][1]),
                                      ("HI", hi, 1.0, spec("us_election_2024")["contains"][2])):
        b = frame.total_bounds
        pad = 60_000 if key == "AK" else 30_000
        w_in = (b[2] - b[0] + 2 * pad) * scale * factor
        h_in = (b[3] - b[1] + 2 * pad) * scale * factor
        inset = cv.add_axes(x_cursor, cv.footer + 0.12, w_in, h_in, frame=True)
        inset.set_facecolor(WATER)
        ext = (b[0] - pad, b[2] + pad, b[1] - pad, b[3] + pad)
        inset.set_xlim(ext[0], ext[1])
        inset.set_ylim(ext[2], ext[3])
        inset.set_aspect("equal")
        plot_classes(inset, frame, "value", edges, colors, linewidth=0.6)
        p = frame.geometry.representative_point().iloc[0]
        k = f"US-{key}"
        if key == "HI":  # the label would cover the Big Island: put it on the water
            label(inset, b[0] + 0.22 * (b[2] - b[0]), b[1] + 0.12 * (b[3] - b[1]),
                  f"HI {fmt(lookup[k])}", size=8.3)
        else:
            label(inset, p.x, p.y, f"{key}\n{fmt(lookup[k])}", dark=dark(k), size=8.3)
        inset.text(0.03, 0.96, {"AK": "Alaska (scale ×0.36)", "HI": "Hawaii (same scale)"}[key],
                   transform=inset.transAxes, fontsize=8, va="top", color=MUTED)
        check_window("us_election_2024", frame.crs, ext, contains=[point], excludes=[])
        x_cursor += w_in + 0.2

    lg = cv.add_axes(cv.left, 1.22, 9.0, 1.0)
    labels = [f"D+{-e}" if e < 0 else (f"R+{e}" if e > 0 else "0") for e in edges]
    stepped_legend(lg, colors, labels,
                   "Margin = Republican % − Democratic % of valid votes (percentage points)",
                   under=("◀ Harris (Democratic) ahead", "Trump (Republican) ahead ▶"), box_w=0.66)
    flipped = truth[(truth.value.abs() < 5)].sort_values("value")
    close = ", ".join(f"{k[3:]} {fmt(v)}" for k, v in zip(flipped.key, flipped.value))
    cv.footnote([
        f"Source: MIT Election Data + Science Lab, 'U.S. President 1976–2024' (Harvard Dataverse doi:10.7910/DVN/42MVDX, V10), CC0. Candidates compared: Donald J. Trump (Republican) and Kamala D. Harris (Democratic), "
        "each as a share of all valid presidential votes in the state (third parties and write-ins included; blank, over- and void ballots excluded). Popular vote, not electoral votes; "
        "Maine's and Nebraska's congressional-district electors are not shown. Boundaries: U.S. Census Bureau cartographic boundary file 2024, 1:20M.",
        "Projection: conterminous US Albers equal-area conic (EPSG:5070); Alaska (EPSG:3338, 0.36× scale) and Hawaii (Albers, same scale) inset. Classification: 10 manual classes diverging at a 0-point margin. "
        f"All 50 states and DC shown (DC as the small square); none excluded. Within 5 points: {close}.",
    ], width=205)
    cv.save("us_election_2024")
    return truth


# --------------------------------------------------------------------------
# 2. Germany: population density by Kreis (NUTS 3)


DE_CITIES = {  # NUTS 3 code -> (label, callout offset in points)
    "DE300": ("Berlin", (58, 30)), "DE600": ("Hamburg", (-62, 30)),
    "DE212": ("München", (62, -26)), "DEA23": ("Köln", (-70, -14)),
    "DE712": ("Frankfurt am Main", (-92, -26)), "DE111": ("Stuttgart", (-70, -30)),
    "DEA11": ("Düsseldorf", (-70, 12)), "DED51": ("Leipzig", (-66, -10)),
    "DED21": ("Dresden", (56, -24)), "DE254": ("Nürnberg", (60, 18)),
    "DE501": ("Bremen", (-58, 22)), "DE211": ("Ingolstadt", (64, 10)),
}


def build_germany_density_nuts3() -> pd.DataFrame:
    payload = json.loads(fetch(EUROSTAT_DENS_URL, "s2c_eurostat_demo_r_d3dens.json").read_text())
    data = jsonstat_rows(payload)
    data["time"] = pd.to_numeric(data.time)
    nuts3 = gpd.read_file(fetch(NUTS3_URL, "s2c_nuts3_2024_01m_4326.geojson"))
    nuts1 = gpd.read_file(fetch(NUTS1_URL, "s2c_nuts1_2024_01m_4326.geojson"))
    de = nuts3[nuts3.CNTR_CODE == "DE"].copy()
    legacy = sorted(set(data.loc[data.geo.str.fullmatch(r"DE[0-9A-G][0-9A-Z]{2}"), "geo"]) - set(de.NUTS_ID))
    sel = data[data.geo.isin(de.NUTS_ID)].sort_values(["geo", "time"]).groupby("geo").tail(1)
    names = de.set_index("NUTS_ID").NAME_LATN.to_dict()
    truth = pd.DataFrame({
        "key": sel.geo, "name": sel.geo.map(names), "value": sel.value,
        "unit": "persons per km2", "year": sel.time,
    }).sort_values("key").reset_index(drop=True)
    if len(truth) != len(de) or set(truth.key) != set(de.NUTS_ID):
        raise ValueError(f"Eurostat density: {len(truth)} values for {len(de)} German NUTS 3 regions")
    write_csv("germany_density_nuts3", truth)
    year = int(truth.year.max())
    write_md("germany_density_nuts3", [
        f"- URL/API: {EUROSTAT_DENS_URL} (JSON-stat; geo filtered to the German NUTS 2024 level-3 codes); geometry {NUTS3_URL}, Länder {NUTS1_URL}.",
        f"- Vintage: Eurostat demo_r_d3dens updated {payload.get('updated', '?')[:10]}; year {int(truth.year.min())}–{year} for every region. Geometry GISCO NUTS 2024, 1:1M.",
        "- Licence: Eurostat data, free reuse with attribution; © EuroGeographics for the administrative boundaries (GISCO).",
        f"- Rows: {len(truth)} — every German NUTS 3 region (Landkreise, kreisfreie Städte, Region Hannover, Regionalverband Saarbrücken, Städteregion Aachen), keyed by NUTS code.",
        f"- Caveat: the dataset also carries {len(legacy)} pre-2024 Thuringian codes ({', '.join(legacy)}), with values only to 2022; they have no NUTS 2024 geometry and are omitted. Density is resident population ÷ total area (land and inland water).",
    ])

    crs = "EPSG:3035"
    regions = de.merge(truth[["key", "value"]], left_on="NUTS_ID", right_on="key").to_crs(crs)
    lander = nuts1[nuts1.CNTR_CODE == "DE"].to_crs(crs)
    xmin, ymin, xmax, ymax = regions.total_bounds
    extent = (xmin - 30_000, xmax + 30_000, ymin - 25_000, ymax + 25_000)
    check_window("germany_density_nuts3", crs, extent)
    edges = [75, 100, 150, 250, 500, 1000, 2000, 3000]
    colors = ramp("magma_r", len(edges) + 1, 0.05, 0.86)
    classes = dict(zip(truth.key, classify(truth.value, edges)))
    lo, hi = truth.loc[truth.value.idxmin()], truth.loc[truth.value.idxmax()]
    cv = Canvas(extent, "Population density by German district",
                f"Residents per km² in the {len(truth)} Kreise and kreisfreie Städte (NUTS 3), {year} · "
                f"from {lo.value:,.0f} ({lo['name']}) to {hi.value:,.0f} ({hi['name'].split(',')[0]})",
                map_w=9.6, footer=0.3)
    ax = cv.ax
    neighbours = nuts1[nuts1.CNTR_CODE != "DE"].to_crs(crs).dissolve("CNTR_CODE")
    neighbours.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.8, zorder=1)
    plot_classes(ax, regions, "value", edges, colors, edgecolor="white", linewidth=0.22)
    lander.boundary.plot(ax=ax, color="#3b4549", linewidth=0.85, zorder=4)
    gpd.GeoSeries([lander.union_all()], crs=crs).boundary.plot(ax=ax, color=INK, linewidth=1.2, zorder=4)
    for name, lon, lat in (("DENMARK", 9.6, 55.25), ("POLAND", 15.6, 52.6), ("CZECHIA", 14.4, 50.15),
                           ("AUSTRIA", 13.6, 47.55), ("SWITZERLAND", 8.3, 47.25), ("FRANCE", 6.6, 48.3),
                           ("BELGIUM", 5.55, 50.3), ("NETHERLANDS", 6.15, 52.75), ("LUX.", 6.1, 49.62)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=9, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("North Sea", 7.2, 54.35), ("Baltic Sea", 12.4, 54.75)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10.5, color="#5f8796", style="italic", ha="center", zorder=2)
    for code, (city, offset) in DE_CITIES.items():
        row = regions[regions.NUTS_ID == code].iloc[0]
        p = row.geometry.representative_point()
        callout(ax, p.x, p.y, f"{city} {row.value:,.0f}", offset, size=8.4)
    p = regions[regions.NUTS_ID == lo.key].geometry.representative_point().iloc[0]
    callout(ax, p.x, p.y, f"{lo['name']} {lo.value:,.0f}", (-60, 40), size=8.4)

    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 3.9)
    labels = [f"< {edges[0]}"] + [f"{a:,} – {b:,}" for a, b in zip(edges[:-1], edges[1:])] + [f"≥ {edges[-1]:,}"]
    counts = np.bincount(classify(truth.value, edges).astype(int), minlength=len(colors))
    labels = [f"{lab}   ({n} districts)" for lab, n in zip(labels, counts)]
    y = column_legend(lg, colors, labels, f"Residents per km², {year}")
    lg.plot([0, 0.45], [y - 0.32, y - 0.32], color="#3b4549", linewidth=0.85)
    lg.text(0.57, y - 0.32, "Land (state) border", va="center", fontsize=9.5)
    lg.plot([0, 0.45], [y - 0.58, y - 0.58], color="#c9c4b8", linewidth=2.2)
    lg.plot([0, 0.45], [y - 0.58, y - 0.58], color="white", linewidth=0.5)
    lg.text(0.57, y - 0.58, "District (Kreis) border", va="center", fontsize=9.5)
    tb = cv.panel(col_x, top - 4.3, col_w, 1.9)
    short = truth.assign(short=truth["name"].str.replace(r",\s*(Kreisfreie Stadt|Stadtkreis|Landeshauptstadt|Wissenschaftsstadt)$", "", regex=True))
    ranked_lists(tb, short, lambda v: f"{v:,.0f}", name_col="short", top_title="Densest", bottom_title="Sparsest",
                 col2=2.75, width=col_w - 0.05, size=9.2)
    med = truth.value.median()
    cities = int((truth.value >= 1000).sum())
    summary = (f"Half of Germany's districts have fewer than {med:,.0f} residents per km². The {cities} districts above "
               "1,000 per km² are almost all kreisfreie Städte — cities that form their own district — "
               "which is why the densest units are the smallest on the map. The emptiest districts lie in the north-east, "
               "in Brandenburg, Mecklenburg-Vorpommern and Saxony-Anhalt.")
    cv.fig.text(col_x / WIDTH_IN, (top - 6.4) / cv.H, textwrap.fill(summary, 62), va="top", fontsize=9.5,
                color=MUTED, linespacing=1.45)
    cv.footnote([
        f"Source: Eurostat, 'Population density by NUTS 3 region' (demo_r_d3dens), {year}. © EuroGeographics for the administrative boundaries (GISCO NUTS 2024, 1:1M).",
        f"Projection: ETRS89 Lambert azimuthal equal-area (EPSG:3035). Classification: 9 manual classes at roughly logarithmic breaks (values span {lo.value:,.0f}–{hi.value:,.0f}). "
        f"All {len(truth)} districts valued, none excluded; Länder borders drawn heavier than district borders.",
    ], width=78, x_in=col_x, y_in=0.4)
    cv.save("germany_density_nuts3")
    return truth


# --------------------------------------------------------------------------
# 3. Texas: unemployment rate by county


TX_LABELS = {  # FIPS -> (label, callout offset in points; None = on the county)
    "48201": ("Harris (Houston)", (70, -40)), "48113": ("Dallas", (62, 26)),
    "48439": ("Tarrant (Fort Worth)", (-30, 52)), "48029": ("Bexar (San Antonio)", (-30, 44)),
    "48453": ("Travis (Austin)", (-72, 30)), "48141": ("El Paso", (20, -42)),
    "48215": ("Hidalgo (McAllen)", (40, -40)), "48303": ("Lubbock", (-54, 30)),
    "48375": ("Potter (Amarillo)", (-66, 26)), "48061": ("Cameron (Brownsville)", (84, 22)),
}


def load_bls_year(yy: int) -> pd.DataFrame:
    path = fetch_browser(BLS_URL.format(yy=yy), f"s2c_bls_laucnty{yy}.xlsx", magic=b"PK")
    raw = pd.read_excel(path, header=None, dtype=str)
    header = raw.index[raw[0].astype(str).str.strip() == "LAUS Code"][0]
    title = str(raw.iloc[0, 0])
    released = raw[0].dropna().astype(str).iloc[-1]
    table = raw.iloc[header + 1:, :9].dropna(subset=[1, 2])
    table.columns = ["laus", "state", "county", "name", "year", "labor_force", "employed", "unemployed", "rate"]
    table = table[table.state == "48"].copy()
    table["key"] = table.state + table.county
    for col in ("labor_force", "employed", "unemployed", "rate"):
        table[col] = pd.to_numeric(table[col])
    table.attrs.update(title=title, released=released)
    return table


def build_texas_county_unemployment() -> pd.DataFrame:
    full, partial = load_bls_year(24), load_bls_year(25)
    counties = gpd.read_file(fetch(base.COUNTIES_URL, "cb_2023_us_county_500k.zip"))
    counties = counties[counties.STATEFP == "48"].copy()
    counties["key"] = counties.STATEFP + counties.COUNTYFP
    if len(full) != 254 or set(full.key) != set(counties.key):
        raise ValueError(f"BLS 2024: {len(full)} Texas rows; join mismatch {set(full.key) ^ set(counties.key)}")
    names = counties.set_index("key").NAMELSAD.to_dict()
    later = partial.set_index("key").rate
    truth = pd.DataFrame({
        "key": full.key, "name": full.key.map(names), "value": full.rate,
        "unit": "percent of labour force, annual average, not seasonally adjusted", "year": full.year.astype(int),
        "labor_force": full.labor_force.astype(int), "unemployed": full.unemployed.astype(int),
        "rate_2025_11month": full.key.map(later),
    }).sort_values("key").reset_index(drop=True)
    write_csv("texas_county_unemployment", truth)
    write_md("texas_county_unemployment", [
        f"- URL/API: {BLS_URL.format(yy=24)} (LAUS 'Labor Force Data by County, 2024 Annual Averages', rows with State FIPS 48); also {BLS_URL.format(yy=25)} for the `rate_2025_11month` column. BLS answers 403 'Access Denied' to non-browser clients; the builder sends browser headers and refuses non-xlsx bytes.",
        f"- Vintage: 2024 annual averages as republished {full.attrs['released']} (after the annual benchmark revision). 2024 is the latest *full* year: the 2025 file is titled '{partial.attrs['title'].strip()}' — October 2025 was never collected (federal shutdown), so its 'annual' figures are 11-month averages.",
        "- Licence: U.S. Bureau of Labor Statistics, public domain. Boundaries: Census cartographic boundary file 2023, 1:500k (public domain).",
        f"- Rows: {len(truth)} Texas counties, key = 5-digit FIPS; `value` = published unemployment rate (%), not seasonally adjusted, one decimal; `labor_force` and `unemployed` as published.",
        f"- Caveat: rates are published to one decimal and are model-based for small counties — the smallest labour force, {truth.loc[truth.labor_force.idxmin(), 'name']}'s, is {int(truth.labor_force.min()):,} people — so neighbouring years can differ by whole points. `rate_2025_11month` (2025 average excluding October) is context only; it is not `value`.",
    ])

    crs = "EPSG:3083"
    merged = counties.merge(truth[["key", "value"]], on="key").to_crs(crs)
    xmin, ymin, xmax, ymax = merged.total_bounds
    extent = (xmin - 40_000, xmax + 40_000, ymin - 30_000, ymax + 30_000)
    notes = check_window("texas_county_unemployment", crs, extent)
    edges = [3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0]
    colors = ramp("YlGnBu", len(edges) + 1, 0.08, 1.0)
    classes = dict(zip(truth.key, classify(truth.value, edges)))
    lo, hi = truth.loc[truth.value.idxmin()], truth.loc[truth.value.idxmax()]
    state_rate = truth.unemployed.sum() / truth.labor_force.sum() * 100
    cv = Canvas(extent, "Unemployment rate by county, Texas, 2024",
                f"Annual average unemployment rate, not seasonally adjusted, all 254 counties · statewide {state_rate:.1f}%",
                map_w=9.6, footer=0.3)
    ax = cv.ax
    states = gpd.read_file(fetch(STATES_500K_URL, "s2c_cb_2023_us_state_500k.zip"))
    states[states.STUSPS.isin(["NM", "OK", "AR", "LA", "CO", "KS", "MO", "MS"])].to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="white", linewidth=0.8, zorder=1)
    admin1 = gpd.read_file(fetch(NE_ADMIN1_URL, "s2c_ne_10m_admin_1_states_provinces.zip"))
    admin1 = reassign_crimea(admin1)
    mexico = admin1[admin1.adm0_a3 == "MEX"].to_crs(crs).union_all().buffer(4_000)  # closes slivers on the Rio Grande
    gpd.GeoSeries([mexico], crs=crs).plot(ax=ax, color=LAND, edgecolor="none", zorder=0.9)
    plot_classes(ax, merged, "value", edges, colors, edgecolor="white", linewidth=0.3)
    gpd.GeoSeries([merged.union_all()], crs=crs).boundary.plot(ax=ax, color=INK, linewidth=0.9, zorder=4)
    for name, lon, lat in (("NEW MEXICO", -105.0, 33.6), ("OKLAHOMA", -97.4, 35.2), ("ARKANSAS", -93.4, 34.6),
                           ("LOUISIANA", -92.6, 31.3), ("MEXICO", -102.6, 28.0), ("COLORADO", -104.3, 37.25),
                           ("KANSAS", -99.5, 37.25)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=9.5, color="#9a958a", ha="center", va="center", zorder=2)
    for code, (text, offset) in TX_LABELS.items():
        row = merged[merged.key == code].iloc[0]
        p = row.geometry.representative_point()
        callout(ax, p.x, p.y, f"{text} {row.value:.1f}%", offset, size=8.4)
    for row, offset in ((hi, (-40, -46)), (lo, (-40, 44))):
        p = merged[merged.key == row.key].geometry.representative_point().iloc[0]
        tag = "highest" if row.key == hi.key else "lowest"
        callout(ax, p.x, p.y, f"{row['name'].replace(' County', '')} {row.value:.1f}% ({tag})", offset,
                size=8.4, weight="bold")

    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 3.4)
    labels = [f"< {edges[0]:.1f}%"] + [f"{a:.1f} – {b:.1f}%" for a, b in zip(edges[:-1], edges[1:])] + [f"≥ {edges[-1]:.1f}%"]
    counts = np.bincount(classify(truth.value, edges).astype(int), minlength=len(colors))
    labels = [f"{lab}   ({n} counties)" for lab, n in zip(labels, counts)]
    column_legend(lg, colors, labels, "Unemployment rate, 2024\n(annual average, % of labour force)")
    tb = cv.panel(col_x, top - 3.75, col_w, 1.9)
    short = truth.assign(short=truth["name"].str.replace(" County", "", regex=False))
    ranked_lists(tb, short, lambda v: f"{v:.1f}%", name_col="short", col2=2.75, width=col_w - 0.05, size=9.2)
    med = truth.value.median()
    summary = (f"The median county rate is {med:.1f}%. The highest rates are along the Rio Grande border "
               "(Starr, Maverick, Zapata, Dimmit) and in Kenedy; the lowest are in the oil counties of the Permian Basin "
               f"and the rural Rolling Plains. Small counties have small labour forces — {lo['name'].replace(' County', '')}'s is "
               f"{int(truth.loc[truth.key == lo.key, 'labor_force'].iloc[0]):,} people — so their rates can move by "
               "whole points from year to year.")
    cv.fig.text(col_x / WIDTH_IN, (top - 5.85) / cv.H, textwrap.fill(summary, 62), va="top", fontsize=9.5,
                color=MUTED, linespacing=1.45)
    cv.footnote([
        f"Source: U.S. Bureau of Labor Statistics, Local Area Unemployment Statistics (LAUS), county annual averages 2024 (revised {full.attrs['released']}), public domain. "
        "Boundaries: U.S. Census Bureau cartographic boundary file 2023, 1:500k; Mexico: Natural Earth 1:10m.",
        "Projection: Texas Centric Albers equal-area (EPSG:3083). Classification: 8 manual classes at half-point steps around the median. "
        "All 254 counties valued, none excluded. 2024 is the latest full year: BLS's 2025 county averages omit October 2025.",
    ], width=78, x_in=col_x, y_in=0.4)
    cv.save("texas_county_unemployment")
    if notes:
        raise AssertionError(f"texas window: {notes}")
    return truth


# --------------------------------------------------------------------------
# 4. Africa: share of the population using the internet


AFRICA = {  # the 54 sovereign African states, plus Western Sahara
    "DZA", "AGO", "BEN", "BWA", "BFA", "BDI", "CPV", "CMR", "CAF", "TCD", "COM", "COG", "COD",
    "CIV", "DJI", "EGY", "GNQ", "ERI", "SWZ", "ETH", "GAB", "GMB", "GHA", "GIN", "GNB", "KEN",
    "LSO", "LBR", "LBY", "MDG", "MWI", "MLI", "MRT", "MUS", "MAR", "MOZ", "NAM", "NER", "NGA",
    "RWA", "STP", "SEN", "SYC", "SLE", "SOM", "ZAF", "SSD", "SDN", "TZA", "TGO", "TUN", "UGA",
    "ZMB", "ZWE", "ESH",
}
ISLANDS = {  # island states too small to read at this scale -> (label, callout offset)
    "CPV": ("Cabo Verde", (0, 34)), "STP": ("São Tomé and Príncipe", (-70, -34)),
    "COM": ("Comoros", (-52, 16)), "MUS": ("Mauritius", (0, -30)), "SYC": ("Seychelles", (0, 28)),
}
SMALL_MAINLAND = {"GMB": (-52, 6), "GNB": (-50, -14), "SWZ": (46, -4), "LSO": (-38, -28), "DJI": (40, 10),
                  "RWA": (-52, 22), "BDI": (-50, -18)}


def build_africa_internet() -> pd.DataFrame:
    raw = pd.read_csv(fetch(OWID_INTERNET_URL, "s2c_owid_internet.csv"))
    col = variable_column(raw)
    data = latest_rows(iso_rows(raw))
    data = data[data.Code.isin(AFRICA)]
    truth = base.standard_truth(data, col, "percent of population (internet use in the last 3 months)")
    write_csv("africa_internet", truth)
    absent = sorted(AFRICA - set(truth.key))
    old = truth[truth.year < truth.year.max()].sort_values("year")
    write_md("africa_internet", [
        f"- URL/API: {OWID_INTERNET_URL} (Grapher CSV; latest non-missing year per ISO3 code; metadata at the same path with `.metadata.json`).",
        f"- Vintage: OWID 'Share of the population using the Internet', citing ITU World Telecommunication/ICT Indicators Database via World Bank (2026); latest year {int(truth.year.max())} for {int((truth.year == truth.year.max()).sum())} countries, older for {', '.join(f'{k} {y}' for k, y in zip(old.key, old.year))}.",
        "- Licence: OWID data CC BY 4.0 (ITU data via World Bank WDI, CC BY 4.0). Boundaries: Natural Earth 1:50m (public domain).",
        f"- Rows: {len(truth)} of {len(AFRICA)} units on an explicit list (54 sovereign African states plus Western Sahara). No value in the source: {', '.join(absent) or 'none'}.",
        "- Caveat: 'Internet user' is ITU's definition: used the internet from any device in the last three months. Natural Earth 1:50m puts the 178,000 km² of Western Sahara west of the berm inside its Morocco polygon; the reference cuts Morocco at 27°40′N so Western Sahara (no ITU value) is hatched whole rather than shown at Morocco's 91%. Somaliland is drawn and valued as part of Somalia; Réunion, Mayotte, St Helena, the Canaries and Madeira belong to non-African states and are context only.",
    ])

    crs = "+proj=laea +lat_0=3 +lon_0=17 +datum=WGS84 +units=m +no_defs"
    world = load_world()
    africa = world[world.key.isin(AFRICA)][["key", "NAME", "geometry"]].dissolve("key", as_index=False, aggfunc="first")
    if set(africa.key) != AFRICA:
        raise ValueError(f"Natural Earth Africa mismatch: {set(africa.key) ^ AFRICA}")
    # Natural Earth draws the de facto line: its Morocco polygon holds the
    # 178,000 km2 of Western Sahara west of the berm, and 'W. Sahara' is only
    # the strip east of it. Western Sahara is everything south of 27°40'N.
    ws_box = box(-20.0, 20.0, -8.0, 27 + 40 / 60)
    mar = africa.key == "MAR"
    esh = africa.key == "ESH"
    moved = africa.loc[mar, "geometry"].iloc[0].intersection(ws_box)
    africa.loc[esh, "geometry"] = [africa.loc[esh, "geometry"].iloc[0].union(moved)]
    africa.loc[mar, "geometry"] = [africa.loc[mar, "geometry"].iloc[0].difference(ws_box)]
    print(f"  moved {gpd.GeoSeries([moved], crs=4326).to_crs(crs).area.iloc[0] / 1e6:,.0f} km2 from MAR to ESH")
    africa = africa.merge(truth[["key", "value", "year"]], on="key", how="left").to_crs(crs)
    # Frame on the continent and the island states; South Africa's Prince
    # Edward Islands (47°S) would add 1,300 km of empty ocean.
    framed = africa.geometry.intersection(
        gpd.GeoSeries([box(-30, -36, 65, 40).segmentize(0.5)], crs=4326).to_crs(crs).iloc[0])
    xmin, ymin, xmax, ymax = framed.total_bounds
    extent = (xmin - 260_000, xmax + 330_000, ymin - 200_000, ymax + 220_000)
    notes = check_window("africa_internet", crs, extent)
    edges = list(range(10, 100, 10))
    colors = ramp("YlGnBu", len(edges) + 1, 0.02, 1.0)
    classes = dict(zip(truth.key, classify(truth.value, edges)))
    lo, hi = truth.loc[truth.value.idxmin()], truth.loc[truth.value.idxmax()]
    cv = Canvas(extent, "Share of people using the internet across Africa",
                f"Percent of the population who used the internet in the last three months · latest year, mostly {int(truth.year.max())}",
                map_w=10.4, footer=1.15)
    ax = cv.ax
    world[~world.key.isin(AFRICA) & (world.key != "ATA")].to_crs(crs).plot(
        ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    valued = africa.dropna(subset=["value"])
    # Island states first get a disc in their class colour so they read at all.
    for row in valued[valued.key.isin(ISLANDS)].itertuples():
        c = row.geometry.envelope.centroid
        ax.scatter([c.x], [c.y], s=330, color=colors[int(classes[row.key])], edgecolor="#4d585d",
                   linewidth=0.7, zorder=2, alpha=0.9)
    plot_classes(ax, valued, "value", edges, colors, edgecolor="white", linewidth=0.45)
    plot_missing(ax, africa[africa.value.isna()], linewidth=0.45)
    for row in valued[valued.key.isin(ISLANDS)].itertuples():
        c = row.geometry.envelope.centroid
        name, offset = ISLANDS[row.key]
        callout(ax, c.x, c.y, f"{name} {row.value:.0f}%", offset, size=8.4)
    for row in valued[valued.key.isin(SMALL_MAINLAND)].itertuples():
        p = row.geometry.representative_point()
        callout(ax, p.x, p.y, f"{row.NAME} {row.value:.0f}", SMALL_MAINLAND[row.key], size=8.0)
    nudges = {"SOM": (60_000, -150_000), "MOZ": (110_000, 120_000),
              "ZAF": (0, 80_000), "GHA": (0, -40_000), "COD": (-120_000, 180_000), "TZA": (0, 30_000),
              "MAR": (60_000, 0)}
    big = valued[(valued.geometry.area > 9e10) & ~valued.key.isin(ISLANDS) & ~valued.key.isin(SMALL_MAINLAND)]
    for row in big.itertuples():
        p = row.geometry.representative_point()
        dx, dy = nudges.get(row.key, (0, 0))
        dark = classes[row.key] >= 6
        size = 8.6 if row.geometry.area > 4e11 else 7.6
        name = {"Central African Rep.": "C. African Rep.", "Dem. Rep. Congo": "DR Congo"}.get(row.NAME, row.NAME)
        text = f"{name}\n{row.value:.0f}%" if row.geometry.area > 2e11 else f"{row.value:.0f}"
        label(ax, p.x + dx, p.y + dy, text, dark=dark, size=size)
    esh = africa[africa.key == "ESH"].geometry.representative_point().iloc[0]
    callout(ax, esh.x, esh.y, "Western Sahara\nno data", (-62, 22), size=8.0)
    for name, lon, lat in (("Atlantic\nOcean", -16.0, -12.0), ("Indian\nOcean", 52.0, -29.0),
                           ("Mediterranean Sea", 18.0, 34.4), ("Red Sea", 38.3, 20.2)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", va="center",
                zorder=2, rotation=-52 if name == "Red Sea" else 0)

    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 4.4)
    labels = [f"{a}–{a + 10}%" for a in range(0, 100, 10)]
    counts = np.bincount(classify(truth.value, edges).astype(int), minlength=len(colors))
    labels = [f"{lab}   ({n})" if n else f"{lab}   (none)" for lab, n in zip(labels, counts)]
    column_legend(lg, colors, labels, "Internet users, % of population\n(full 0–100% scale; countries per class)",
                  missing="No data (Western Sahara)", extra=[(LAND, "Outside Africa")])
    tb = cv.panel(col_x, top - 4.75, col_w, 1.9)
    ranked_lists(tb, truth.assign(short=truth["name"].replace({
        "Democratic Republic of Congo": "DR Congo", "Central African Republic": "C. African Rep."})),
        lambda v: f"{v:.0f}%", name_col="short", col2=2.45, width=col_w - 0.05, size=9.2)
    median = truth.value.median()
    summary = (f"The median African country is at {median:.0f}%. North Africa, South Africa and the island states "
               f"lead; the Sahel and Central and East Africa trail, with {(truth.value < 20).sum()} countries below 20%.")
    cv.fig.text(col_x / WIDTH_IN, (top - 6.85) / cv.H, textwrap.fill(summary, 56), va="top", fontsize=9.5,
                color=MUTED, linespacing=1.45)
    cv.footnote([
        "Source: Our World in Data, 'Share of the population using the Internet', from the ITU World Telecommunication/ICT Indicators Database "
        f"via the World Bank (2026), CC BY 4.0. Latest year per country: {int(truth.year.max())}, except "
        f"{', '.join(f'{n} {y}' for n, y in zip(old['name'], old.year))}. Boundaries: Natural Earth 1:50m, public domain.",
        "Projection: Lambert azimuthal equal-area centred on 3°N 17°E. Classification: ten equal 10-point classes spanning 0–100%. "
        f"All 54 African states shown; Western Sahara hatched (no value). Small island states drawn as discs in their class colour. "
        "Western Sahara is drawn in full (south of 27°40′N; Natural Earth's de facto Morocco polygon is cut there). Somaliland drawn as part of Somalia; Réunion, Mayotte and other non-African territories as context.",
    ], width=205)
    cv.save("africa_internet")
    if notes:
        raise AssertionError(f"africa window: {notes}")
    return truth


# --------------------------------------------------------------------------
# 5. Canada: population change 2016-2021 by province and territory


SGC_TO_ISO = {"10": "CA-NL", "11": "CA-PE", "12": "CA-NS", "13": "CA-NB", "24": "CA-QC", "35": "CA-ON",
              "46": "CA-MB", "47": "CA-SK", "48": "CA-AB", "59": "CA-BC", "60": "CA-YT", "61": "CA-NT",
              "62": "CA-NU"}
CA_LABEL_AT = {  # lon, lat of the on-map label; None = callout
    "CA-YT": (-135.5, 63.4), "CA-NT": (-119.0, 64.0), "CA-NU": (-95.0, 65.5), "CA-BC": (-124.0, 54.6),
    "CA-AB": (-114.6, 55.2), "CA-SK": (-106.0, 54.4), "CA-MB": (-97.5, 55.0), "CA-ON": (-86.0, 50.6),
    "CA-QC": (-72.5, 51.8), "CA-NL": (-61.8, 53.6),
}
CA_CALLOUTS = {"CA-PE": (-30, 46), "CA-NS": (40, -30), "CA-NB": (-60, -40)}


def build_canada_population_change() -> pd.DataFrame:
    archive = zipfile.ZipFile(fetch(STATCAN_URL, "s2c_statcan_98100001.zip"))
    table = pd.read_csv(io.BytesIO(archive.read("98100001.csv")), dtype=str, encoding="utf-8-sig")
    built = archive.getinfo("98100001.csv").date_time
    cols = {c.split(": ", 1)[1].split(" [")[0]: c for c in table.columns if ": " in c}
    table["sgc"] = table.DGUID.str[-2:]
    prov = table[table.DGUID.str.startswith("2021A0002")].copy()
    truth = pd.DataFrame({
        "key": prov.sgc.map(SGC_TO_ISO), "name": prov.GEO,
        "value": pd.to_numeric(prov[cols["Population percentage change, 2016 to 2021"]]),
        "unit": "percent change in population, 2016 to 2021", "year": "2016-2021",
        "pop_2016": pd.to_numeric(prov[cols["Population, 2016"]]),
        "pop_2021": pd.to_numeric(prov[cols["Population, 2021"]]),
    })
    truth["pct_exact"] = (truth.pop_2021 / truth.pop_2016 - 1) * 100
    truth = truth.sort_values("key").reset_index(drop=True)
    if len(truth) != 13 or truth.key.isna().any() or (truth.value - truth.pct_exact).abs().max() > 0.051:
        raise ValueError("StatCan 98-10-0001: expected 13 provinces/territories with consistent change")
    canada = table[table.GEO == "Canada"].iloc[0]
    write_csv("canada_population_change", truth)
    write_md("canada_population_change", [
        f"- URL/API: {STATCAN_URL} (full-table CSV of {STATCAN_TABLE}); rows with DGUID 2021A0002xx, keyed by ISO 3166-2 via the SGC province code.",
        f"- Vintage: Statistics Canada, Census of Population 2021, table 98-10-0001-01 'Population and dwelling counts: Canada, provinces and territories' (census release 2022-02-09; file dated {built[0]}-{built[1]:02d}-{built[2]:02d}). Canada: {int(canada[cols['Population, 2016']]):,} → {int(canada[cols['Population, 2021']]):,} ({canada[cols['Population percentage change, 2016 to 2021']]}%).",
        "- Licence: Statistics Canada Open Licence (Open Government Licence – Canada). Boundaries: Natural Earth 1:10m admin-1 (public domain), because StatCan's own 2021 boundary file (lpr_000b21a_e.zip) sits behind a Cloudflare JavaScript challenge that a script cannot pass.",
        f"- Rows: {len(truth)} — 10 provinces, 3 territories. `value` = StatCan's published percent change (one decimal); `pct_exact` recomputes it from `pop_2016` and `pop_2021` (max difference {(truth.value - truth.pct_exact).abs().max():.3f} points).",
        "- Caveat: 2016 counts are as published in 2021 for 2021 boundaries (provinces are unchanged). Two units declined: Newfoundland and Labrador and the Northwest Territories.",
    ])

    crs = "+proj=aea +lat_0=40 +lon_0=-96 +lat_1=50 +lat_2=70 +datum=NAD83 +units=m +no_defs"
    admin1 = gpd.read_file(fetch(NE_ADMIN1_URL, "s2c_ne_10m_admin_1_states_provinces.zip"))
    admin1 = reassign_crimea(admin1)
    provinces = admin1[admin1.adm0_a3 == "CAN"][["iso_3166_2", "name", "geometry"]].rename(columns={"iso_3166_2": "key"})
    if set(provinces.key) != set(truth.key):
        raise ValueError(f"Natural Earth Canada mismatch: {set(provinces.key) ^ set(truth.key)}")
    provinces = provinces.merge(truth[["key", "value"]], on="key").to_crs(crs)
    xmin, ymin, xmax, ymax = provinces.total_bounds
    extent = (xmin - 60_000, xmax + 60_000, ymin - 150_000, ymax + 60_000)
    check_window("canada_population_change", crs, extent)
    window = gpd.GeoSeries([box(*[extent[0], extent[2], extent[1], extent[3]])], crs=crs)
    context = admin1[admin1.adm0_a3.isin(["USA", "GRL", "RUS", "MEX"])]
    context = context.to_crs(crs).clip(window.buffer(300_000).iloc[0]).dissolve("adm0_a3")

    edges = [0, 2, 4, 6, 8, 10]
    colors = ["#e08214", "#ece9f4", "#cbc5e1", "#a69dcb", "#8070b3", "#5c4497", "#3c1f74"]
    classes = dict(zip(truth.key, classify(truth.value, edges)))
    cv = Canvas(extent, "Population change by Canadian province and territory, 2016 to 2021",
                f"Percent change in census population between the 2016 and 2021 censuses · Canada as a whole "
                f"+{float(canada[cols['Population percentage change, 2016 to 2021']]):.1f}%",
                map_w=11.0, footer=1.05)
    ax = cv.ax
    context.plot(ax=ax, color=LAND, edgecolor="none", zorder=1)
    context.boundary.plot(ax=ax, color="white", linewidth=0.6, zorder=1.1)
    plot_classes(ax, provinces, "value", edges, colors, edgecolor="white", linewidth=0.9)
    for row in provinces.itertuples():
        text = f"{row.name}\n{row.value:+.1f}%"
        dark = classes[row.key] >= 5
        if row.key in CA_CALLOUTS:
            p = row.geometry.representative_point()
            callout(ax, p.x, p.y, text, CA_CALLOUTS[row.key], size=8.6)
            continue
        (x, y), = project_points(crs, [CA_LABEL_AT[row.key]])
        label(ax, x, y, text, dark=dark, size=9.4 if row.key not in {"CA-NL"} else 8.8,
              weight="bold" if row.value < 0 else "normal")
    for name, lon, lat in (("UNITED STATES", -101.0, 45.8), ("ALASKA (US)", -150.5, 64.5),
                           ("GREENLAND", -42.0, 74.0)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=9.5, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("Hudson Bay", -85.5, 59.5), ("Pacific\nOcean", -136.0, 50.0),
                           ("Atlantic\nOcean", -55.0, 43.5), ("Arctic Ocean", -128.0, 74.0)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", va="center", zorder=2)

    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    top = cv.H - cv.header - 0.05
    lg = cv.panel(col_x, top, col_w, 2.6)
    labels = ["declined (< 0%)", "0 – 2%", "2 – 4%", "4 – 6%", "6 – 8%", "8 – 10%", "≥ 10%"]
    column_legend(lg, colors, labels, "Population change\n2016 → 2021 (%)")
    tb = cv.panel(col_x, top - 2.95, col_w, 4.3)
    h = tb.get_ylim()[1]
    x_cols = (0, 2.95, col_w - 0.05)
    for x, text, ha in zip(x_cols, ("", "Pop. 2021", "Change"), ("left", "right", "right")):
        tb.text(x, h - 0.02, text, ha=ha, va="top", fontsize=9.5, fontweight="bold")
    ranked = truth.sort_values("value", ascending=False)
    for i, row in enumerate(ranked.itertuples()):
        y = h - 0.36 - i * 0.255
        tb.add_patch(Rectangle((0, y - 0.17), 0.16, 0.16, facecolor=colors[int(classes[row.key])],
                               edgecolor="#c9c4b8", linewidth=0.4))
        tb.text(0.24, y, row.name, va="top", fontsize=9)
        tb.text(x_cols[1], y, f"{row.pop_2021:,}", ha="right", va="top", fontsize=9)
        tb.text(x_cols[2], y, f"{row.value:+.1f}%", ha="right", va="top", fontsize=9,
                fontweight="bold" if row.value < 0 else "normal")
    cv.footnote([
        "Source: Statistics Canada, Census of Population 2021, table 98-10-0001-01 'Population and dwelling counts: Canada, provinces and territories' (released 2022-02-09), "
        "Statistics Canada Open Licence (Open Government Licence – Canada). Boundaries: Natural Earth 1:10m admin-1, public domain.",
        "Projection: Canada Albers equal-area conic (standard parallels 50°N and 70°N, central meridian 96°W). Classification: 7 classes diverging at 0% "
        "(one decline class, six growth classes in 2-point steps). All 10 provinces and 3 territories shown; none excluded.",
    ], width=200)
    cv.save("canada_population_change")
    return truth


# --------------------------------------------------------------------------
# Runner


BUILDERS: dict[str, Callable[[], pd.DataFrame]] = {
    "us_election_2024": build_us_election_2024,
    "germany_density_nuts3": build_germany_density_nuts3,
    "texas_county_unemployment": build_texas_county_unemployment,
    "africa_internet": build_africa_internet,
    "canada_population_change": build_canada_population_change,
}

REPORT_PATH = HERE / "REPORT_set2_choropleth.md"
REPORT_NOTES = """
## Spot checks marked (verify)

Every spot written from memory holds against the primary source; no spec
change is required. True values: WY R+45.8, DC D+83.8, PA R+1.7, VT D+31.5
(margins of valid votes); Berlin 4,343/km², Hamburg 2,616/km² (2024); Harris
County 4.5% (2024); Morocco 91.2%, Niger 15.6%, Seychelles 87.8% (2024);
NL −1.8%, YT +12.1%, ON +5.8% (2016–2021).

## What was measured, and what the spec should know

- **`texas_county_unemployment`: the latest *full* year is 2024, not 2025.**
  BLS's `laucnty25.xlsx` is titled "2025 11-Month and Annual Averages":
  October 2025 was never collected (federal shutdown), so every 2025 county
  "annual" figure is an 11-month mean. The reference maps 2024 and keeps the
  2025 figure as `rate_2025_11month`. Harris County is 4.5% in both, so the
  spots pass either way; a service that says "2025 annual average" is
  overstating what BLS published.
- **`africa_internet`: Natural Earth draws Western Sahara as Morocco.**
  NE 1:50m's Morocco polygon contains 178,356 km² of Western Sahara (everything
  west of the berm); its `W. Sahara` feature is only the 90,592 km² strip east
  of it. The same areas were measured in the GitHub GeoJSON that mapsvc loads
  (`registry._BASE`, `ne_50m_admin_0_countries`), so a mapsvc Africa map
  paints two-thirds of Western Sahara with Morocco's 91%.
  The reference cuts Morocco at 27°40′N and hatches Western Sahara whole (no
  ITU value). 54 of 55 units valued; Sudan (2017), South Sudan (2019) and
  Eritrea (2020) are older than 2024.
- **`germany_density_nuts3`: the upper end is ~4,800, not ~4,000.** München
  (DE212) is 4,829/km², Berlin 4,343, Altmarkkreis Salzwedel the minimum at 35.
  Suggest the `must` line read "density spans from ~35 to ~4,800 per km2". The
  Eurostat table also carries 7 pre-2024 Thuringian codes (DEG04, DEG0B, DEG0F,
  DEG0H, DEG0I, DEG0N, DEG0P, values only to 2022) with no NUTS 2024 geometry; a
  join that keeps every `DE…` code gets 407 rows, not 400.
- **`us_election_2024`: the denominator matters by up to 0.49 points.** MIT's
  `totalvotes` includes undervotes, overvotes, void and spoiled ballots in 8
  states (HI, ID, MA, ME, NH, NY, VT, WY). The reference divides by valid
  presidential votes (third parties and write-ins kept); MIT's own total is
  kept as `margin_pp_mit_total`. No spot is near that margin.
- **`canada_population_change`: StatCan boundaries are not scriptable.** The
  2021 cartographic boundary file (`lpr_000b21a_e.zip`, www12.statcan.gc.ca)
  is behind a Cloudflare JavaScript challenge (HTTP 403 to curl and requests);
  the reference uses Natural Earth 1:10m admin-1, whose 13 `iso_3166_2` codes
  match StatCan's exactly. The data table itself (www150) downloads cleanly.
- **Revised excludes verified.** After commit d4a42f8, Denver and Little Rock
  are outside the Texas frame and Madrid and Istanbul outside the Africa
  frame; every `contains` point is inside its frame (or its inset, for
  Anchorage and Honolulu).

### Primary-source access, as measured on 2026-10-07

- BLS (`www.bls.gov/lau/`) returns 403 "Access Denied" HTML to any
  non-browser User-Agent, and intermittently even to a browser one; the
  builder sends browser headers, retries, and refuses bytes that are not a
  ZIP/XLSX signature.
- MIT's Dataverse file id for `1976-2024-president.csv` is 13887042 (dataset
  V10, released 2026-09-04; 2024 rows versioned 2025-11-20).
- Eurostat, GISCO, OWID and the StatCan full-table CSV needed nothing
  special. The Census API was not used.

### Reference images

All five are 1600 px wide. Projections: US Albers (EPSG:5070) with Alaska and
Hawaii insets; ETRS89-LAEA (EPSG:3035) for Germany; Texas Centric Albers
(EPSG:3083); Lambert azimuthal equal-area centred on 3°N 17°E for Africa;
Canada Albers (50°N/70°N). Every footnote names source, licence, vintage,
projection, classification and exclusions.
"""


def render_report(results, counts) -> str:
    lines = [
        "# Set 2 choropleth references — build report",
        "",
        "Generated by `uv run evals/maps/etalons/set2_choropleth.py`. Spot checks are evaluated "
        "from the written `truth.csv` files only (`build.evaluate_spot`).",
        "",
        "| Map | Rows | Spot | Result | Measured |",
        "|---|---:|---|---|---|",
    ]
    total = passed = 0
    for map_id in IDS:
        if map_id not in results:
            lines.append(f"| `{map_id}` | — | — | not built | |")
            continue
        for spot, ok, measured in results[map_id]:
            total += 1
            passed += ok
            lines.append(f"| `{map_id}` | {counts[map_id]} | `{spot}` | {'PASS' if ok else '**FAIL**'} | {measured} |")
    lines += ["", f"**{passed} of {total} spot checks pass.**", "", REPORT_NOTES.strip(), ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", choices=IDS, help="map ids; default: all five")
    parser.add_argument("--check-only", action="store_true", help="only evaluate existing truth.csv files")
    args = parser.parse_args()
    ids = tuple(args.ids) or IDS
    if not args.check_only:
        for map_id in ids:
            print(f"\n[{map_id}]", flush=True)
            BUILDERS[map_id]()
    results, counts = base.evaluate(IDS)
    base.print_results({k: v for k, v in results.items() if k in ids}, counts)
    REPORT_PATH.write_text(render_report(results, counts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
