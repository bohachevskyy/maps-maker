"""Shared scaffolding for the held-out reference builders.

Re-uses the figure scaffolding, downloader, palette and legend helpers of
``evals/maps/etalons/build.py`` (imported, never copied) but redirects its
output, cache and spec file to ``evals/heldout``.  Like it, this module never
imports mapsvc: the held-out references are an independent oracle.
"""

from __future__ import annotations

import json
import math
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELDOUT = HERE.parent
ETALONS = HELDOUT.parent / "maps" / "etalons"
sys.path.insert(0, str(ETALONS))

import build as B  # noqa: E402  (sets the Agg backend)

# build.py resolves these module globals at call time, so patching them
# redirects fetch(), write_csv(), write_md(), Canvas.save() and spec().
CACHE_DIR = HERE / ".cache"
B.CACHE_DIR = CACHE_DIR
B.MAPS_DIR = HELDOUT
B.SPECS_PATH = HELDOUT / "specs.toml"

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from pyproj import Transformer  # noqa: E402

from build import (  # noqa: E402,F401
    HALO, HATCH, INK, LAND, MUTED, NODATA, PAPER, WATER, WIDTH_IN,
    Canvas, check_window, class_legend, classify, fetch, plot_classes, plot_missing,
    project_points, ramp, write_csv, write_md,
)

# Overpass etiquette: a User-Agent with a contact address (overpass-api.de
# answers 406 without one).
CONTACT_UA = "maps-maker-heldout-etalon/1.0 (dmytro.bohachevskyy@gmail.com)"
B.HTTP.headers.update({"User-Agent": CONTACT_UA})

NE = B.NE
NE50_COUNTRIES = f"{NE}/50m_cultural/ne_50m_admin_0_countries.zip"
NE10_COUNTRIES = f"{NE}/10m_cultural/ne_10m_admin_0_countries.zip"
NE50_UNITS = f"{NE}/50m_cultural/ne_50m_admin_0_map_units.zip"
NE10_ADMIN1 = f"{NE}/10m_cultural/ne_10m_admin_1_states_provinces.zip"
NE10_LAKES = f"{NE}/10m_physical/ne_10m_lakes.zip"
NE10_RIVERS = f"{NE}/10m_physical/ne_10m_rivers_lake_centerlines.zip"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def spec_entry(map_id: str) -> dict:
    specs = tomllib.loads((HELDOUT / "specs.toml").read_text(encoding="utf-8"))
    return next(e for e in specs["map"] if e["id"] == map_id)


def overpass(query: str, filename: str, *, timeout: int = 900) -> dict:
    """POST an Overpass query once, cache the JSON, fall back across mirrors."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    if target.exists() and target.stat().st_size:
        return json.loads(target.read_text(encoding="utf-8"))
    errors = []
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


def world10() -> gpd.GeoDataFrame:
    """Natural Earth 1:10m countries, Crimea and Sevastopol drawn as Ukraine,
    keyed as ISO3 (ISO_A3_EH, ADM0_A3 where that is not a country code)."""
    world = gpd.read_file(fetch(NE10_COUNTRIES, "ne_10m_admin_0_countries.zip"))
    world = B.reassign_crimea(world)
    eh = world["ISO_A3_EH"].fillna("")
    world["key"] = np.where(eh.str.fullmatch(r"[A-Z]{3}"), eh, world["ADM0_A3"])
    world["key"] = world["key"].replace({"SOL": "SOM", "CYN": "CYP", "KAS": "", "ALA": "FIN"})
    return world


def local_graticule(ax, crs, lons, lats, *, lon_span, lat_span, label_lat_at=None,
                    label_lon_at=None, color="white", fontsize=7.5, zorder=0.5, frame=None):
    """Meridians and parallels in `crs`; labels where they enter the frame."""
    t = Transformer.from_crs(4326, crs, always_xy=True)
    for lon in lons:
        ys = np.linspace(lat_span[0], lat_span[1], 300)
        x, y = t.transform(np.full_like(ys, lon), ys)
        ax.plot(x, y, color=color, linewidth=0.55, alpha=0.9, zorder=zorder)
    for lat in lats:
        xs = np.linspace(lon_span[0], lon_span[1], 400)
        x, y = t.transform(xs, np.full_like(xs, lat))
        ax.plot(x, y, color=color, linewidth=0.55, alpha=0.9, zorder=zorder)


def haversine_km(a, b) -> float:
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2)
         * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, h)))


def line_segments_km(target, coords) -> float:
    """Distance from a point to a polyline (flat-earth, as evals/maps/run.py)."""
    best = float("inf")
    for a, b in zip(coords[:-1], coords[1:]):
        k = math.cos(math.radians(target[1]))
        ax_, ay = (a[0] - target[0]) * k, a[1] - target[1]
        bx, by = (b[0] - target[0]) * k, b[1] - target[1]
        dx, dy = bx - ax_, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax_ * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax_ + t * dx, ay + t * dy) * 111.195)
    return best
