#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "geopandas>=1.0,<2",
#   "matplotlib>=3.9,<4",
#   "numpy>=2,<3",
#   "pandas>=2.2,<3",
#   "openpyxl>=3.1,<4",
#   "pillow>=10",
#   "pypdf>=5,<7",
#   "pyproj>=3.7,<4",
#   "requests>=2.32,<3",
#   "shapely>=2.0,<3",
#   "xlrd>=2.0,<3",
# ]
# ///
"""Reference ("etalon") map for costco_bay_area. Never imports mapsvc.

    uv run evals/maps/etalons/costco.py

Truth = OpenStreetMap Costco warehouses (shop=wholesale and untagged Costco
buildings), clustered within 300 m so a node and a way (or two nodes) of one
store count once, assigned to a county by the FCC Area API (point-in-polygon
against Census 2020 blocks) and kept when the county is one of the nine.
The Overpass answer is cached in .cache/costco_overpass.json (2026-11-07);
delete it to re-query.  Overture Places was queried once by hand as a
cross-check; its numbers are recorded in truth.md, not re-derived here.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build as B  # noqa: E402  (sets the Agg backend)
import geopandas as gpd  # noqa: E402
import pandas as pd  # noqa: E402
from pyproj import Transformer  # noqa: E402

MAP_ID = "costco_bay_area"
BBOX = (36.9, -123.2, 38.6, -121.2)  # S, W, N, E
QL = ('[out:json][timeout:120];(nwr["brand"="Costco"]["shop"](36.9,-123.2,38.6,-121.2);'
      'nwr["brand:wikidata"="Q715583"](36.9,-123.2,38.6,-121.2);'
      'nwr[name~"^Costco",i](36.9,-123.2,38.6,-121.2););out center tags;')
ENDPOINT = "https://overpass-api.de/api/interpreter"
UA = "maps-maker-etalon/1.0 (dmytro.bohachevskyy@gmail.com)"
CACHE = HERE / ".cache" / "costco_overpass.json"
NINE = ["Alameda", "Contra Costa", "Marin", "Napa", "San Francisco", "San Mateo",
        "Santa Clara", "Solano", "Sonoma"]


def km(a, b) -> float:
    return math.hypot((a[0] - b[0]) * math.cos(math.radians(a[1])) * 111.32, (a[1] - b[1]) * 110.57)


def centre(e):
    c = e.get("center") or e
    return c["lon"], c["lat"]


def overpass() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    CACHE.parent.mkdir(exist_ok=True)
    r = B.HTTP.post(ENDPOINT, data={"data": QL}, headers={"User-Agent": UA}, timeout=200)
    r.raise_for_status()
    CACHE.write_text(r.text)
    return r.json()


def is_warehouse(e) -> bool:
    t = e["tags"]
    n = t.get("name") or ""
    return (t.get("shop") == "wholesale" or n == "Costco Warehouse"
            or (n == "Costco" and not t.get("shop") and not t.get("amenity"))
            or (not n and t.get("brand") == "Costco" and "addr:street" in t))


def county(lon, lat) -> str:
    for _ in range(4):
        try:
            r = B.HTTP.get("https://geo.fcc.gov/api/census/area",
                           params={"lat": lat, "lon": lon, "format": "json"}, timeout=30)
            return r.json()["results"][0]["county_name"].removesuffix(" County")
        except Exception:
            time.sleep(2)
    return "?"


def truth_rows(payload: dict) -> tuple[pd.DataFrame, pd.DataFrame, list[list]]:
    cand = [e for e in payload["elements"] if is_warehouse(e)]
    # lead = the shop=wholesale element, then ways before nodes
    cand.sort(key=lambda e: (e["tags"].get("shop") != "wholesale", e["type"] != "way", e["id"]))
    clusters: list[list] = []
    for e in cand:
        for c in clusters:
            if km(centre(e), centre(c[0])) < 0.3:
                c.append(e)
                break
        else:
            clusters.append([e])
    rows = []
    for c in clusters:
        lead = c[0]
        lon, lat = centre(lead)
        tags = {}
        for x in c:
            for k, v in x["tags"].items():
                tags.setdefault(k, v)
        names = " ".join(x["tags"].get("name", "") for x in c)
        rows.append({
            "id": f"{lead['type'][0]}{lead['id']}",
            "name": "Costco Business Center" if "Business Center" in names else "Costco",
            "city": tags.get("addr:city") or ("Pleasanton" if "Johnson" in tags.get("addr:street", "") else ""),
            "county": county(lon, lat), "lon": round(lon, 6), "lat": round(lat, 6),
            "kind": "business_center" if "Business Center" in names else "warehouse",
            "sources": "osm:" + "+".join(f"{x['type'][0]}{x['id']}" for x in c),
        })
    allrows = pd.DataFrame(rows).sort_values(["county", "city", "id"]).reset_index(drop=True)
    inside = allrows[allrows.county.isin(NINE)].reset_index(drop=True)
    outside = allrows[~allrows.county.isin(NINE)].reset_index(drop=True)
    return inside, outside, clusters


def main() -> int:
    payload = overpass()
    base = payload["osm3s"]["timestamp_osm_base"]
    inside, outside, clusters = truth_rows(payload)
    out = B.MAPS_DIR / MAP_ID
    out.mkdir(parents=True, exist_ok=True)
    inside.to_csv(out / "truth.csv", index=False, lineterminator="\n")
    print(f"OSM base {base}: {len(inside)} stores in nine counties "
          f"({int((inside.kind == 'business_center').sum())} Business Centers); "
          f"{len(outside)} outside: " + ", ".join(outside.city))
    print(inside.groupby("county").size().to_string())

    # ---- map: Web-Mercator-free, an Albers-ish local conic is overkill at 190 km; use UTM 10N
    crs = "EPSG:32610"
    counties = gpd.read_file(B.fetch(B.COUNTIES_URL, "cb_2023_us_county_500k.zip"))
    ca = counties[counties.STATEFP == "06"].to_crs(crs)
    nine = ca[ca.NAME.isin(NINE)]
    states = gpd.read_file(B.fetch(B.STATES_URL, "cb_2024_us_state_20m.zip")).to_crs(crs)
    t = Transformer.from_crs(4326, crs, always_xy=True)
    x0, y0 = t.transform(-123.2, 36.8)
    x1, y1 = t.transform(-121.0, 38.75)
    cv = B.Canvas((x0, x1, y0, y1), "Costco warehouses in the Bay Area",
                  f"{len(inside)} warehouses in the nine counties, {int((inside.kind == 'business_center').sum())} of them Business Centers · "
                  "OpenStreetMap, one symbol per store", map_w=11.0, footer=0.9)
    ax = cv.ax
    states.plot(ax=ax, color=B.LAND, edgecolor="white", linewidth=0.5, zorder=1)
    ca.plot(ax=ax, color=B.LAND, edgecolor="white", linewidth=0.5, zorder=1.1)
    nine.plot(ax=ax, color="#efe9da", edgecolor="#9aa3a6", linewidth=0.7, zorder=1.2)
    for _, r in nine.iterrows():
        p = r.geometry.representative_point()
        ax.text(p.x, p.y, r.NAME.upper(), fontsize=7.2, color="#8a9498", ha="center", va="center", zorder=2)
    pts = gpd.GeoDataFrame(inside, geometry=gpd.points_from_xy(inside.lon, inside.lat), crs=4326).to_crs(crs)
    wh, bc = pts[pts.kind == "warehouse"], pts[pts.kind == "business_center"]
    ax.scatter(wh.geometry.x, wh.geometry.y, s=70, color="#e31837", edgecolor="white", linewidth=0.8, zorder=5)
    ax.scatter(bc.geometry.x, bc.geometry.y, s=70, marker="D", color="#005daa", edgecolor="white", linewidth=0.8, zorder=6)
    for name, lon, lat in (("San Francisco", -122.42, 37.77), ("Oakland", -122.27, 37.80),
                           ("San Jose", -121.89, 37.34), ("Santa Rosa", -122.71, 38.44)):
        (x,), (y,) = (a for a in t.transform([lon], [lat]))
        ax.scatter([x], [y], s=10, marker="s", color=B.INK, zorder=4)
        ax.text(x + 4000, y + 3000, name, fontsize=8.6, color=B.INK, zorder=8, path_effects=B.HALO)
    for _, r in outside.iterrows():   # stores outside the nine counties: grey, not counted
        (x,), (y,) = (a for a in t.transform([r.lon], [r.lat]))
        if x0 < x < x1 and y0 < y < y1:
            ax.scatter([x], [y], s=26, color="#b9b9b9", edgecolor="white", linewidth=0.5, zorder=3)
    ax.scatter([], [], s=70, color="#e31837", edgecolor="white", label=f"Warehouse ({len(wh)})")
    ax.scatter([], [], s=70, marker="D", color="#005daa", edgecolor="white", label=f"Business Center ({len(bc)})")
    ax.scatter([], [], s=26, color="#b9b9b9", label="Costco outside the nine counties (not counted)")
    ax.legend(loc="lower left", fontsize=9, frameon=True, framealpha=0.9)
    cv.save(MAP_ID)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
