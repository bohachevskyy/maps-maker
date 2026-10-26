"""Overlay references (points and lines) for the held-out set.

Imported by build_heldout.py; also runnable alone to prefetch the slow
Overpass queries:  uv run heldout_overlays.py fetch
"""

from __future__ import annotations

import json
import math
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hbase as H  # noqa: E402
from hbase import (  # noqa: E402
    B, HALO, INK, LAND, MUTED, PAPER, WATER, WIDTH_IN, Canvas, check_window, compact, fetch,
    haversine_km, project_points, write_csv, write_md,
)

import geopandas as gpd  # noqa: E402
import matplotlib as mpl  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402
from pyproj import Geod, Transformer  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import LineString, MultiLineString, Point, box  # noqa: E402
from shapely.ops import linemerge, unary_union  # noqa: E402

GEOD = Geod(ellps="WGS84")
NOW = pd.Timestamp.now().strftime("%Y-%m-%d")

LONDON_Q = """
[out:json][timeout:600];
relation["route"="subway"]["network"~"^London Underground"];
out body geom;
"""
BOROUGH_Q = """
[out:json][timeout:600];
area["name"="Greater London"]["boundary"="administrative"]["admin_level"="5"]->.a;
rel(area.a)["boundary"="administrative"]["admin_level"="8"];
out body geom;
way["waterway"="river"]["name"="River Thames"](51.30,-0.75,51.75,0.35);
out geom;
"""
ZAK_Q = """
[out:json][timeout:900];
area["ISO3166-2"="UA-21"]["admin_level"="4"]->.v;
node["place"](area.v);
out body;
rel(area.v)["boundary"="administrative"]["admin_level"~"^(6|7)$"];
out body geom;
rel["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"~"^UA-(21|46|26|77)$"];
out body geom;
"""
NUKE_Q = """
[out:json][timeout:900];
nwr["power"="plant"]["plant:source"="nuclear"](34,-25,72,62);
out center tags;
"""
FILES = {"london": (LONDON_Q, "london_underground_overpass.json"),
         "boroughs": (BOROUGH_Q, "london_boroughs_overpass.json"),
         "zak": (ZAK_Q, "zakarpattia_overpass.json"),
         "nuke": (NUKE_Q, "europe_nuclear_overpass.json")}

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "fetch":
    for name, (q, f) in FILES.items():
        try:
            p = H.overpass(q, f)
            print(name, len(p["elements"]), H.osm_base(p), flush=True)
        except Exception as e:
            print(name, "FAILED", e, flush=True)



def thin_box(ax):
    for sp in ax.spines.values():
        sp.set_visible(True)
        sp.set_linewidth(0.6)
        sp.set_color("#9aa3a7")


# --------------------------------------------------------------------------
# 5. Japan: M5+ earthquakes of 2024 (USGS)


USGS_JAPAN = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=2024-01-01&endtime=2025-01-01"
              "&minmagnitude=5&minlatitude=24&maxlatitude=46&minlongitude=125&maxlongitude=150&orderby=time-asc")
DEPTH_CLASSES = [(0, 70, "#d7301f", "Shallow, < 70 km"), (70, 300, "#f6a23c", "Intermediate, 70–300 km"),
                 (300, 1000, "#2b6ca3", "Deep, ≥ 300 km")]


def depth_colour(d):
    return next(c for lo, hi, c, _ in DEPTH_CLASSES if lo <= d < hi)


def quake_size(m):
    return 26 * np.power(2.9, np.asarray(m) - 5)


def build_japan_quakes_2024() -> pd.DataFrame:
    map_id = "japan_quakes_2024"
    payload = json.loads(fetch(USGS_JAPAN, "usgs_japan_2024_m5.geojson").read_text())
    rows = []
    for f in payload["features"]:
        lon, lat, depth = f["geometry"]["coordinates"]
        pr = f["properties"]
        rows.append({"id": f["id"], "time": pd.to_datetime(pr["time"], unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "lon": lon, "lat": lat, "depth_km": depth, "mag": pr["mag"], "magType": pr.get("magType", ""), "place": pr["place"]})
    truth = pd.DataFrame(rows).sort_values(["time", "id"]).reset_index(drop=True)
    write_csv(map_id, truth)
    generated = pd.to_datetime(payload["metadata"]["generated"], unit="ms", utc=True).strftime("%Y-%m-%d")
    noto = truth[(truth.lon.between(136, 138)) & (truth.lat.between(36.5, 37.8)) & (truth.time < "2024-02-01")]
    write_md(map_id, [
        f"- URL/API: {USGS_JAPAN}",
        f"- Vintage: USGS ComCat as of {generated}; events 2024-01-01T00:00Z <= t < 2025-01-01T00:00Z, magnitude >= 5.0, in the box 24-46N, 125-150E (Japan and its surrounding seas: Ryukyu, Izu-Bonin, Kuril-Japan trench; excludes Taiwan, Korea and Primorye).",
        "- Licence: U.S. Geological Survey Earthquake Hazards Program, U.S. government work (public domain). Plate boundaries: Bird (2003) PB2002 via github.com/fraxen/tectonicplates (ODC-BY 1.0). Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} events — id, UTC time, lon, lat, depth_km, mag, magType, place. M{truth.mag.max():g} Noto Peninsula (1 Jan 2024, 07:10 UTC) is the largest; {len(noto)} of the events are the Noto sequence of January 2024; Hyuganada M7.1 on 8 Aug.",
        "- Caveat: the region is a rectangle, not a boundary, so it includes some events nearer Russia/Korea than Japan only if they fall in the box (none stated as such by USGS 'place'); ComCat revises and adds events, so the count can drift by a few. The spec's `count=110±40%` passes.",
    ])
    crs = "+proj=lcc +lat_1=30 +lat_2=44 +lat_0=36 +lon_0=137 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    ring = [(lo, la) for lo in np.linspace(124, 149.5, 30) for la in (23.5, 45.8)] + [(lo, la) for la in np.linspace(23.5, 45.8, 30) for lo in (124, 149.5)]
    xs, ys = t.transform(*zip(*ring))
    extent = (min(xs), max(xs), min(ys), max(ys))
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "Earthquakes of magnitude 5 or more in and around Japan, 2024",
                f"{len(truth)} events recorded by the USGS · circle area grows with magnitude, colour shows depth · "
                f"the M{truth.mag.max():g} Noto Peninsula earthquake of 1 January began a sequence of {len(noto)} M5+ events",
                map_w=10.6, footer=0.95)
    ax = cv.ax
    world = H.world10()
    world.clip(box(110, 15, 160, 60)).to_crs(crs).plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    H.local_graticule(ax, crs, range(125, 150, 5), range(25, 46, 5), lon_span=(120, 155), lat_span=(20, 50))
    plates = gpd.read_file(fetch(B.PLATES_URL, "pb2002_boundaries.json")).clip(box(120, 18, 160, 52)).to_crs(crs)
    plates.plot(ax=ax, color="#c28f6b", linewidth=0.8, alpha=0.8, zorder=2)
    pts = gpd.GeoDataFrame(truth, geometry=gpd.points_from_xy(truth.lon, truth.lat), crs=4326).to_crs(crs).sort_values("mag", kind="stable")
    ax.scatter(pts.geometry.x, pts.geometry.y, s=quake_size(pts.mag), c=[depth_colour(d) for d in pts.depth_km],
               alpha=0.72, edgecolor="white", linewidth=0.6, zorder=4)
    # labels for the largest events (Noto handled in the inset)
    in_noto = pts.lon.between(136, 138.2) & pts.lat.between(36.2, 37.9)
    big = pts[(pts.mag >= 6.0) & ~in_noto].sort_values("mag", ascending=False)
    offsets = {"Hyuganada": (-70, -30), "Bonin": (60, -30), "Uwajima": (-70, -8), "Noto": (-70, 40)}
    taken = []
    for r in big.itertuples():
        place = re.sub(r"^\d{4} ", "", r.place).replace(" Earthquake", "")
        place = re.sub(r"^\d+ km [NSEW]+ of ", "near ", place).replace(", Japan region", "").replace(", Japan", "")
        date = pd.Timestamp(r.time).strftime("%-d %b")
        key = next((k for k in offsets if k in place), None)
        dx, dy = offsets.get(key, (60, 24 * (1 if len(taken) % 2 else -1)))
        taken.append(r.Index)
        ax.annotate(f"M{r.mag:g} {place}, {date}", (r.geometry.x, r.geometry.y), xytext=(dx, dy), textcoords="offset points",
                    ha="left" if dx > 0 else "right", va="center", fontsize=8.5, fontweight="bold" if r.mag >= 7 else "normal",
                    zorder=9, path_effects=HALO, arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6, shrinkA=0, shrinkB=3))
    for name, lon, lat in (("JAPAN", 138.4, 38.8), ("SOUTH KOREA", 127.9, 36.6), ("CHINA", 126.0, 42.2), ("RUSSIA", 134.5, 45.2)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=8.5, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("Sea of Japan", 134.0, 40.4), ("Pacific Ocean", 146.0, 33.0), ("Philippine Sea", 136.5, 27.0),
                           ("East China Sea", 127.8, 29.8), ("Japan Trench", 144.2, 38.3)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=9, color="#5f8796" if "Trench" not in name else "#a0724f", style="italic", ha="center", va="center", zorder=2, rotation=-65 if "Trench" in name else 0)
    (nx_, ny_), = project_points(crs, [(138.0, 37.9)])
    ax.annotate("Noto Peninsula sequence\n(enlarged in the inset)", (nx_, ny_), xytext=(70, 38), textcoords="offset points", fontsize=8.6,
                fontweight="bold", ha="left", zorder=9, path_effects=HALO, arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6))
    # Noto inset
    nb = (136.0, 138.2, 36.2, 37.9)
    ncrs = "+proj=tmerc +lat_0=37 +lon_0=137.1 +datum=WGS84 +units=m"
    nt = Transformer.from_crs(4326, ncrs, always_xy=True)
    corners = nt.transform([nb[0], nb[1], nb[0], nb[1]], [nb[2], nb[2], nb[3], nb[3]])
    nx0, nx1, ny0, ny1 = min(corners[0]), max(corners[0]), min(corners[1]), max(corners[1])
    iw = 4.0
    ih = iw * (ny1 - ny0) / (nx1 - nx0)
    inset = cv.add_axes(cv.left + 0.12, cv.footer + cv.map_h - ih - 0.12, iw, ih, frame=True)
    inset.set_facecolor(WATER)
    inset.set_xlim(nx0, nx1)
    inset.set_ylim(ny0, ny1)
    inset.set_aspect("equal")
    world.clip(box(nb[0], nb[2], nb[1], nb[3])).to_crs(ncrs).plot(ax=inset, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    pn = pts.to_crs(ncrs)
    sel = pn[(truth.set_index("id").loc[pn.id, "lon"].between(nb[0], nb[1]).values) & (truth.set_index("id").loc[pn.id, "lat"].between(nb[2], nb[3]).values)]
    inset.scatter(sel.geometry.x, sel.geometry.y, s=quake_size(sel.mag) * 1.6, c=[depth_colour(d) for d in sel.depth_km],
                  alpha=0.75, edgecolor="white", linewidth=0.6, zorder=4)
    mainshock = sel[sel.mag == sel.mag.max()].iloc[0]
    inset.annotate("M7.5 mainshock\n1 Jan 2024", (mainshock.geometry.x, mainshock.geometry.y), xytext=(46, -46), textcoords="offset points",
                   fontsize=8.2, ha="left", zorder=9, path_effects=HALO,
                   arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6))
    inset.text(0.03, 0.97, f"Noto Peninsula window\n{len(sel)} events in 2024, {len(noto)} of them in January", transform=inset.transAxes, fontsize=8.6, va="top", fontweight="bold", path_effects=HALO)
    # frame of the inset on the main map
    bx = [t.transform(lo, la) for lo, la in ((nb[0], nb[2]), (nb[1], nb[2]), (nb[1], nb[3]), (nb[0], nb[3]), (nb[0], nb[2]))]
    ax.plot([p[0] for p in bx], [p[1] for p in bx], color=INK, linewidth=0.9, zorder=6)
    # legend column
    col_x = cv.left + cv.map_w + 0.45
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 6.2)
    lg.text(0, 6.15, "Magnitude (circle area)", va="top", fontsize=10.5, fontweight="bold")
    y = 5.55
    for m in (5, 6, 7, 7.5):
        d = quake_size(m) ** 0.5 / 72
        lg.add_patch(Circle((0.45, y), d / 2, facecolor="#8d979c", alpha=0.72, edgecolor="white", linewidth=0.6))
        lg.text(1.05, y, f"M{m:g}", va="center", fontsize=9.5)
        y -= max(d, 0.25) + 0.22
    y -= 0.2
    lg.text(0, y, "Depth of focus (colour)", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.4
    n_by = pd.cut(truth.depth_km, [-1, 70, 300, 1000], right=False).value_counts().sort_index().tolist()
    for (lo, hi, colour, label), n in zip(DEPTH_CLASSES, n_by):
        lg.add_patch(Circle((0.2, y), 0.09, facecolor=colour, alpha=0.8, edgecolor="white"))
        lg.text(0.45, y, f"{label} ({n})", va="center", fontsize=9.3)
        y -= 0.3
    y -= 0.15
    lg.plot([0, 0.4], [y, y], color="#c28f6b", linewidth=1.3)
    lg.text(0.55, y, "Plate boundary (Bird 2003)", va="center", fontsize=9.3)
    cv.footnote([
        f"Source: U.S. Geological Survey, ComCat via the FDSN event service: all events of magnitude ≥ 5.0 in 24–46°N, 125–150°E from 1 Jan to 31 Dec 2024 (UTC), retrieved {generated}; public domain. "
        "Plate boundaries: P. Bird (2003), PB2002 (ODC-BY). Land: Natural Earth 1:10m.",
        f"Projection: Lambert conformal conic (parallels 30°N and 44°N, centred on 137°E). Encoding: circle area ∝ 2.9^M, translucent, largest drawn last; colour = focal depth. All {len(truth)} events shown, none excluded; "
        "labelled: every event of M ≥ 6 outside the inset window. The inset enlarges the Noto Peninsula.",
    ], width=200)
    cv.save(map_id)
    return truth


# --------------------------------------------------------------------------
# 7. Western Pacific typhoon season 2024 (IBTrACS WP)


IBTRACS_WP = ("https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/"
              "v04r01/access/csv/ibtracs.WP.list.v04r01.csv")
JMA = [  # (min WMO 10-min wind kt, colour, label)
    (0, "#8fbad3", "Tropical depression (< 34 kt)"),
    (34, "#4fb39a", "Tropical storm (34–47 kt)"),
    (48, "#f2d24b", "Severe tropical storm (48–63 kt)"),
    (64, "#f5a03a", "Typhoon (64–84 kt)"),
    (85, "#e4572e", "Very strong typhoon (85–104 kt)"),
    (105, "#7a1236", "Violent typhoon (≥ 105 kt)"),
]


def jma_colour(w: float) -> str:
    return next(c for lo, c, _ in reversed(JMA) if w >= lo)


def jma_rank(w: float) -> int:
    return max(i for i, (lo, _, _) in enumerate(JMA) if w >= lo)


LANDFALL_PLACE = {"YAGI": "Hainan", "GAEMI": "Taiwan", "KRATHON": "Taiwan", "KONG-REY": "Taiwan", "MAN-YI": "Luzon",
                  "SHANSHAN": "Kyushu", "BEBINCA": "Shanghai", "TRAMI": "Luzon", "TORAJI": "Luzon",
                  "USAGI": "Luzon", "PRAPIROON": "Vietnam", "EWINIAR": "Philippines", "SON-TINH": "Hainan/Vietnam",
                  "PULASAN": "China", "SOULIK": "Taiwan strait"}


def build_pacific_typhoons_2024() -> pd.DataFrame:
    map_id = "pacific_typhoons_2024"
    path = fetch(IBTRACS_WP, "ibtracs_WP_v04r01.csv")
    cols = ["SID", "SEASON", "BASIN", "NAME", "ISO_TIME", "NATURE", "LAT", "LON", "WMO_WIND", "WMO_PRES", "USA_WIND", "USA_SSHS",
            "DIST2LAND", "LANDFALL", "TRACK_TYPE", "USA_RECORD"]
    raw = pd.read_csv(path, skiprows=[1], dtype=str, keep_default_na=False, usecols=cols)
    season = raw[(raw.SEASON == "2024") & (raw.TRACK_TYPE == "main")].copy()
    first_basin = season.groupby("SID").BASIN.first()
    unnamed = season[season.NAME == "UNNAMED"].SID.nunique()
    first_lon = season.groupby("SID").LON.first().astype(float)
    keep = [sid for sid in season.SID.unique()
            if season[season.SID == sid].NAME.iloc[0] != "UNNAMED" and first_basin[sid] == "WP" and first_lon[sid] < 175]
    other = sorted(set(season.SID[season.NAME != "UNNAMED"]) - set(keep))
    season = season[season.SID.isin(keep)].copy()

    def num(c):
        return pd.to_numeric(season[c].str.strip().replace("", np.nan), errors="coerce")

    truth = pd.DataFrame({
        "sid": season.SID, "name": season.NAME.str.capitalize(), "iso_time": season.ISO_TIME, "lon": num("LON"), "lat": num("LAT"),
        "nature": season.NATURE, "wmo_wind_kt": num("WMO_WIND").astype("Int64"), "wmo_pres_hpa": num("WMO_PRES").astype("Int64"),
        "usa_wind_kt": num("USA_WIND").astype("Int64"), "usa_sshs": num("USA_SSHS").astype("Int64"),
        "dist2land_km": num("DIST2LAND").astype("Int64"),
    }).sort_values(["sid", "iso_time"]).reset_index(drop=True)
    write_csv(map_id, truth)
    storms = truth.groupby("sid", sort=False).agg(name=("name", "first"), start=("iso_time", "first"), end=("iso_time", "last"),
                                                  peak=("wmo_wind_kt", "max"), n=("lon", "size")).reset_index().sort_values("start")
    n = len(storms)
    ty = int((storms.peak >= 64).sum())
    last_mod = pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d")
    write_md(map_id, [
        f"- URL/API: {IBTRACS_WP} (NOAA NCEI IBTrACS v04r01, western North Pacific basin file); rows with SEASON = 2024, TRACK_TYPE = main, storm named and first fix in basin WP west of 175E (this drops HONE, a Central Pacific storm named by the CPHC that only crosses 180E).",
        f"- Vintage: IBTrACS v04r01 as downloaded {last_mod}. Positions are IBTrACS LAT/LON (JMA-led for the WP basin); intensity along the track is WMO_WIND (JMA 10-minute sustained wind, knots), with USA (JTWC) wind and Saffir-Simpson category kept as extra columns.",
        "- Licence: IBTrACS is a NOAA product, U.S. government work (public domain); cite Knapp et al. (2010) BAMS 91:363 and Gahtan et al. (2024) IBTrACS v04r01, NOAA NCEI, doi:10.25921/82ty-9e16. Land: Natural Earth (public domain).",
        f"- Rows: {len(truth)} track fixes (3-hourly) of {n} named storms — sid, name, iso_time (UTC), lon, lat, nature (TS tropical, ET extratropical, DS disturbance, SS subtropical), wmo_wind_kt, wmo_pres_hpa, usa_wind_kt, usa_sshs, dist2land_km. `count` = distinct sid. {ty} reached typhoon strength (>= 64 kt, JMA).",
        f"- Caveat: {unnamed} unnamed 2024 depressions are excluded; {len(other)} named system(s) in the file that formed outside the WP basin ({', '.join(sorted(set(raw[raw.SID.isin(other)].NAME))) or 'none'}) are excluded. Pabuk formed on 23 Dec 2024 and ended in January 2025. Tracks include pre-genesis and extratropical stages (drawn dashed grey).",
    ])
    crs = "+proj=lcc +lat_1=10 +lat_2=40 +lat_0=24 +lon_0=136 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)
    truth["x"], truth["y"] = t.transform(truth.lon.to_numpy(), truth.lat.to_numpy())
    # JMA reports wind every 6 h; the 3-hourly rows between are filled linearly for colouring only
    truth["wind_draw"] = truth.groupby("sid").wmo_wind_kt.transform(lambda c: c.astype(float).interpolate(limit_area="inside"))
    cap_lat = 44.5
    sel = truth[truth.lat <= cap_lat]
    extent = (sel.x.min() - 300_000, sel.x.max() + 250_000, sel.y.min() - 260_000, sel.y.max() + 160_000)
    check_window(map_id, crs, extent)
    names = storms.name.tolist()
    # landfalls: first fix at the coast after being at sea, wind of the following 6 h maximum
    lf = []
    for sid, g in truth.groupby("sid", sort=False):
        g = g.reset_index(drop=True)
        d = g.dist2land_km.fillna(999).to_numpy()
        for i in range(1, len(g)):
            if d[i] == 0 and d[i - 1] > 0 and g.wmo_wind_kt.iloc[i:i + 3].notna().any():
                lf.append({"sid": sid, "name": g.name[0], "iso_time": g.iso_time[i], "lon": g.lon[i], "lat": g.lat[i],
                           "wind": int(g.wmo_wind_kt.iloc[i:i + 3].max()), "x": g.x[i], "y": g.y[i]})
    lf = pd.DataFrame(lf)
    print(lf.sort_values("wind", ascending=False).head(14)[["name", "iso_time", "lon", "lat", "wind"]].to_string())
    top = (lf.sort_values("wind", ascending=False).groupby("name").head(1).head(7))
    cv = Canvas(extent, "Tracks of the 2024 western Pacific typhoon season",
                f"All {n} named storms, coloured by JMA intensity along each track · {ty} reached typhoon strength; the strongest landfalls are named",
                map_w=12.0, footer=1.0)
    ax = cv.ax
    world = H.world10().clip(box(90, -5, 190, 62)).to_crs(crs)
    world.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.5, zorder=1)
    H.local_graticule(ax, crs, range(100, 181, 10), range(0, 61, 10), lon_span=(95, 185), lat_span=(0, 60))
    from matplotlib.collections import LineCollection
    for sid in storms.sort_values("peak").sid:
        g = truth[truth.sid == sid]
        pts = g[["x", "y"]].to_numpy()
        w = g.wind_draw.astype(float).to_numpy()
        trop = (~np.isnan(w)) & g.nature.isin(["TS", "SS"]).to_numpy()
        seg_w = np.fmax(w[:-1], w[1:])
        segs = np.stack([pts[:-1], pts[1:]], axis=1)
        ok = trop[:-1] & trop[1:]
        ax.add_collection(LineCollection(segs[~ok], colors="#8a8f93", linewidths=0.9, linestyles=(0, (2.2, 1.6)), zorder=3, capstyle="round"))
        lw = np.where(seg_w >= 64, 2.8, 2.0)
        ax.add_collection(LineCollection(segs[ok], colors="white", linewidths=lw[ok] + 1.5, zorder=3.5, capstyle="round"))
        ax.add_collection(LineCollection(segs[ok], colors=[jma_colour(v) for v in seg_w[ok]], linewidths=lw[ok], zorder=4, capstyle="round"))
    ax.scatter(lf.x, lf.y, s=14, facecolor="white", edgecolor=INK, linewidth=0.8, zorder=7)
    # landfall annotations for the strongest
    cv.fig.canvas.draw()
    rend = cv.fig.canvas.get_renderer()
    taken = []
    cands = [(70, 30), (-70, 30), (70, -30), (-70, -30), (0, 44), (0, -44), (110, 0), (-110, 0), (60, 60), (-60, 60), (60, -60), (-60, -60),
             (140, 40), (-140, 40), (140, -40), (-140, -40), (120, 80), (-120, 80), (120, -80), (-120, -80), (200, 0), (0, 100), (0, -100), (200, 60), (200, -60), (320, 90), (320, -20), (320, -90), (400, 40), (400, -60), (260, 140), (260, -140)]
    for r in top.sort_values("wind", ascending=False).itertuples():
        date = pd.Timestamp(r.iso_time).strftime("%-d %b")
        where = LANDFALL_PLACE.get(r.name.upper(), "")
        txt = f"{r.name} · {date}\n{where} · {r.wind} kt"
        for dx, dy in cands:
            a = ax.annotate(txt, (r.x, r.y), xytext=(dx, dy), textcoords="offset points", ha="left" if dx > 0 else ("right" if dx < 0 else "center"),
                            va="center", fontsize=8.6, fontweight="bold", zorder=9, path_effects=HALO, linespacing=1.15,
                            arrowprops=dict(arrowstyle="-", color=INK, linewidth=0.6, shrinkA=0, shrinkB=3))
            a.update_positions(rend)
            bb = mpl.text.Text.get_window_extent(a, rend).expanded(1.05, 1.15)
            if not any(bb.overlaps(o) for o in taken) and ax.bbox.padded(-4).contains(bb.x0, bb.y0) and ax.bbox.padded(-4).contains(bb.x1, bb.y1):
                taken.append(bb)
                break
            a.remove()
        else:
            print("  no free label position for", r.name)
    for name, lon, lat in (("CHINA", 112.0, 28.5), ("JAPAN", 137.0, 37.4), ("PHILIPPINES", 122.3, 16.8), ("TAIWAN", 121.0, 23.8),
                           ("VIETNAM", 106.5, 15.5), ("S. KOREA", 127.9, 36.2), ("RUSSIA", 134.0, 45.0)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=8.5, color="#9a958a", ha="center", va="center", zorder=2)
    for name, lon, lat in (("Pacific Ocean", 155.0, 14.0), ("Philippine Sea", 133.0, 21.5), ("South China Sea", 113.5, 14.0)):
        (x, y), = project_points(crs, [(lon, lat)])
        ax.text(x, y, name, fontsize=10, color="#5f8796", style="italic", ha="center", va="center", zorder=2)
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    h = 7.4
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, h)
    lg.text(0, h - 0.02, "Intensity along the track\n(JMA 10-min wind)", va="top", fontsize=10.5, fontweight="bold", linespacing=1.3)
    y = h - 0.85
    for _lo, colour, label in reversed(JMA):
        lg.plot([0, 0.42], [y, y], color=colour, linewidth=3.4, solid_capstyle="butt")
        lg.text(0.55, y, label, va="center", fontsize=8.6)
        y -= 0.26
    lg.plot([0, 0.42], [y, y], color="#8a8f93", linewidth=1.0, linestyle=(0, (2.2, 1.6)))
    lg.text(0.55, y, "Before genesis / extratropical", va="center", fontsize=8.6)
    y -= 0.26
    lg.scatter([0.21], [y], s=14, facecolor="white", edgecolor=INK, linewidth=0.8)
    lg.text(0.55, y, "Landfall (first fix over land)", va="center", fontsize=8.6)
    y -= 0.5
    lg.text(0, y, "Storm · peak wind (kt)", va="top", fontsize=10, fontweight="bold")
    y -= 0.32
    half = (len(storms) + 1) // 2
    for i, r in enumerate(storms.itertuples()):
        c, k = divmod(i, half)
        x0 = c * (col_w / 2 + 0.05)
        lg.text(x0, y - k * 0.2, f"{r.name}", va="top", fontsize=8.4)
        lg.text(x0 + col_w / 2 - 0.15, y - k * 0.2, f"{int(r.peak) if pd.notna(r.peak) else '–'}", va="top", ha="right", fontsize=8.4)
    cv.footnote([
        f"Source: NOAA NCEI, International Best Track Archive for Climate Stewardship (IBTrACS) v04r01, western North Pacific file, season 2024, retrieved {last_mod}; public domain (Knapp et al. 2010; Gahtan et al. 2024). Land: Natural Earth 1:10m.",
        f"Projection: Lambert conformal conic (parallels 10°N and 40°N, centred on 136°E). All {n} named storms shown, {len(truth)} 3-hourly fixes; colour and width = JMA wind class of each segment. "
        f"Excluded: {unnamed} unnamed depressions. Tracks end at the frame edge north of 44°N.",
    ], width=200)
    cv.save(map_id)
    return truth


# --------------------------------------------------------------------------
# 6. London Underground (OpenStreetMap)


CITY_Q = """
[out:json][timeout:120];
rel["name"="City of London"]["boundary"="administrative"]["admin_level"~"^[6-8]$"];
out body geom;
"""
LINE_ORDER = ["Metropolitan", "District", "Piccadilly", "Central", "Northern", "Jubilee", "Bakerloo", "Victoria",
              "Hammersmith & City", "Circle", "Waterloo & City"]
LINE_WIDTH = {"District": 4.8, "Metropolitan": 3.9, "Hammersmith & City": 3.0, "Circle": 2.0}
LANDMARKS = {  # name -> (lon, lat) of the station, snapped to the network for drawing; offsets in points
    "King’s Cross St Pancras": (-0.1239, 51.5304), "Bank": (-0.0889, 51.5133), "Oxford Circus": (-0.1415, 51.5152),
    "Paddington": (-0.1757, 51.5154), "Waterloo": (-0.1147, 51.5031), "Baker Street": (-0.1574, 51.5226),
    "Victoria": (-0.1448, 51.4965), "Liverpool Street": (-0.0823, 51.5178), "Stratford": (-0.0042, 51.5416),
    "Notting Hill Gate": (-0.1967, 51.5094),
}


def london_polys():
    from set2_overlays import relation_polygon
    p = H.overpass(BOROUGH_Q, "london_boroughs_overpass.json")
    polys = {}
    for e in p["elements"]:
        if e["type"] == "relation":
            polys[e["tags"]["name"]] = relation_polygon(e)
    try:
        c = H.overpass(CITY_Q, "london_city_overpass.json")
        for e in c["elements"]:
            if e["type"] == "relation":
                polys["City of London"] = relation_polygon(e)
    except Exception as exc:  # the City is a courtesy outline
        print("  City of London outline unavailable:", str(exc)[:100])
    thames = []
    for e in p["elements"]:
        if e["type"] == "way" and e.get("geometry"):
            thames.append(LineString([(q["lon"], q["lat"]) for q in e["geometry"]]))
    return polys, thames, H.osm_base(p)


def build_london_underground() -> pd.DataFrame:
    from set2_overlays import dissolved_route_km, geodesic_km, way_line
    map_id = "london_underground"
    payload = H.overpass(LONDON_Q, "london_underground_overpass.json")
    rels = [e for e in payload["elements"] if e["type"] == "relation" and "sidings" not in e["tags"].get("name", "")]
    rows = []
    for ref in LINE_ORDER:
        mine = [r for r in rels if r["tags"].get("ref") == ref]
        if not mine:
            raise ValueError(f"no relation for {ref}")
        ways = {}
        for r in mine:
            for m in r["members"]:
                if m["type"] == "way" and not re.search(r"platform|stop", m.get("role", "")) and m.get("geometry"):
                    ln = way_line(m)
                    if ln is not None:
                        ways[m["ref"]] = ln
        geom = unary_union(list(ways.values()))
        cols = [r["tags"]["colour"].upper() for r in mine if r["tags"].get("colour")]
        rows.append({
            "ref": ref, "name": f"{ref} line", "network": "London Underground", "colour": max(set(cols), key=cols.count),
            "osm_relations": ";".join(str(r["id"]) for r in sorted(mine, key=lambda r: r["id"])), "osm_ways": len(ways),
            "route_km": round(dissolved_route_km(list(ways.values()), buffer_m=100, spacing_m=10), 1),
            "half_track_km": round(sum(geodesic_km(w) for w in ways.values()) / 2, 1),
            "geometry_wkt": __import__("shapely").to_wkt((linemerge(geom) if geom.geom_type == "MultiLineString" else geom).simplify(0.00003), rounding_precision=5),
        })
    truth = pd.DataFrame(rows)
    write_csv(map_id, truth)
    base = H.osm_base(payload)
    write_md(map_id, [
        f"- URL/API: Overpass API (overpass-api.de): `{compact(LONDON_Q)}`; boroughs and the River Thames: `{compact(BOROUGH_Q)}` (+ the City of London relation).",
        f"- Vintage: OpenStreetMap as of {base}; delete the cached JSON in .cache to refresh. {len(rels)} route=subway relations (one per direction and service pattern) kept, the 'Central line sidings' relation dropped.",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0.",
        f"- Rows: {len(truth)} lines — ref, name, network, colour (the OSM colour tag, which every relation carries), osm_relations, osm_ways, route_km (both directions dissolved with 100 m buffers), half_track_km (sum of member ways ÷ 2), geometry_wkt (member track ways, merged, simplified ~3 m). Total {truth.route_km.sum():,.0f} route-km.",
        "- Caveat: the Circle and Hammersmith & City lines run on track shared with the District and Metropolitan lines, so their route_km overlaps those lines' (the network total double counts about 25 km); the Elizabeth line, DLR and Overground are separate OSM networks and are not included. Colours are the OSM tags, within a few RGB steps of TfL's standard colours.",
    ])
    lines = gpd.GeoDataFrame(truth, geometry=[wkt.loads(t) for t in truth.geometry_wkt], crs=4326).to_crs(27700)
    xmin, ymin, xmax, ymax = lines.total_bounds
    extent = (xmin - 2_500, xmax + 2_500, ymin - 2_500, ymax + 2_500)
    check_window(map_id, 27700, extent)
    cv = Canvas(extent, "The London Underground network",
                f"The {len(truth)} lines in their official colours, from OpenStreetMap · {truth.route_km.sum():,.0f} km of route over Greater London’s boroughs",
                map_w=10.4, footer=0.95)
    ax = cv.ax
    ax.set_facecolor("#efede6")
    polys, thames, base_b = london_polys()
    for name, g in polys.items():
        gp = gpd.GeoSeries([g], crs=4326).to_crs(27700)
        gp.plot(ax=ax, facecolor=LAND, edgecolor="white", linewidth=1.0, zorder=1)
    if thames:
        gpd.GeoSeries(thames, crs=4326).to_crs(27700).plot(ax=ax, color="#b9d3df", linewidth=3.0, zorder=1.5)
    for name, g in polys.items():
        c = gpd.GeoSeries([g], crs=4326).to_crs(27700).representative_point().iloc[0]
        short = name.replace("London Borough of ", "").replace("Royal Borough of ", "").replace("City of ", "City of ")
        if extent[0] < c.x < extent[1] and extent[2] < c.y < extent[3]:
            ax.text(c.x, c.y, short.upper(), fontsize=6.6, color="#aaa496", ha="center", va="center", zorder=2)
    order = sorted(range(len(lines)), key=lambda i: -LINE_WIDTH.get(lines.ref[i], 2.6))
    for i in order:
        ref = lines.ref[i]
        w = LINE_WIDTH.get(ref, 2.6)
        gpd.GeoSeries([lines.geometry[i]], crs=27700).plot(ax=ax, color="white", linewidth=w + 1.4, zorder=3 + 0.01 * (12 - w), capstyle="round")
    for i in order:
        ref = lines.ref[i]
        w = LINE_WIDTH.get(ref, 2.6)
        gpd.GeoSeries([lines.geometry[i]], crs=27700).plot(ax=ax, color=lines.colour[i], linewidth=w, zorder=4 + 0.01 * (12 - w), capstyle="round")
    net = lines.geometry.union_all()
    tr = Transformer.from_crs(4326, 27700, always_xy=True)
    snapped = {}
    for nm, (lon, lat) in LANDMARKS.items():
        x, y = tr.transform(lon, lat)
        d = net.distance(Point(x, y))
        snapped[nm] = (x, y, d)
        print(f"  landmark {nm}: {d:.0f} m from the network")
    for ln, (lon, lat) in (("spec King’s Cross", (-0.124, 51.530)), ("spec Bank", (-0.089, 51.513)), ("spec Baker St", (-0.158, 51.523))):
        x, y = tr.transform(lon, lat)
        print(f"  {ln}: {net.distance(Point(x, y)):.0f} m")
    # central inset
    cb = (-0.205, -0.045, 51.480, 51.552)
    cx0, cy0 = tr.transform(cb[0], cb[2])
    cx1, cy1 = tr.transform(cb[1], cb[3])
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    iw = col_w
    ih = iw * (cy1 - cy0) / (cx1 - cx0)
    top_in = cv.H - cv.header - 0.1
    lg_h = 2.35
    inset = cv.add_axes(col_x, top_in - lg_h - 0.2 - ih, iw, ih, frame=True)
    inset.set_facecolor("#efede6")
    inset.set_xlim(cx0, cx1)
    inset.set_ylim(cy0, cy1)
    inset.set_aspect("equal")
    for name, g in polys.items():
        gpd.GeoSeries([g], crs=4326).to_crs(27700).plot(ax=inset, facecolor=LAND, edgecolor="white", linewidth=1.0, zorder=1)
    if thames:
        gpd.GeoSeries(thames, crs=4326).to_crs(27700).plot(ax=inset, color="#b9d3df", linewidth=5.0, zorder=1.5)
    for i in order:
        ref = lines.ref[i]
        w = LINE_WIDTH.get(ref, 2.6) * 1.6
        gpd.GeoSeries([lines.geometry[i]], crs=27700).plot(ax=inset, color="white", linewidth=w + 1.8, zorder=3 + 0.01 * (12 - w), capstyle="round")
    for i in order:
        ref = lines.ref[i]
        w = LINE_WIDTH.get(ref, 2.6) * 1.6
        gpd.GeoSeries([lines.geometry[i]], crs=27700).plot(ax=inset, color=lines.colour[i], linewidth=w, zorder=4 + 0.01 * (12 - w), capstyle="round")
    offs = {"King’s Cross St Pancras": (-6, 14, "right"), "Bank": (10, -12, "left"), "Oxford Circus": (-10, 12, "right"), "Paddington": (-8, 12, "right"),
            "Waterloo": (10, -12, "left"), "Baker Street": (-10, 12, "right"), "Victoria": (10, -12, "left"), "Liverpool Street": (-8, 13, "right"),
            "Notting Hill Gate": (10, -13, "left")}
    for nm, (dx, dy, ha) in offs.items():
        x, y, d = snapped[nm]
        inset.scatter([x], [y], s=34, color="white", edgecolor=INK, linewidth=1.3, zorder=7)
        inset.annotate(nm, (x, y), xytext=(dx, dy), textcoords="offset points", ha=ha, va="center", fontsize=8, fontweight="bold",
                       color=INK, zorder=9, path_effects=HALO)
    inset.text(0.02, 0.98, "Central London", transform=inset.transAxes, va="top", fontsize=9, fontweight="bold", path_effects=HALO)
    bx = [(cx0, cy0), (cx1, cy0), (cx1, cy1), (cx0, cy1), (cx0, cy0)]
    ax.plot([p[0] for p in bx], [p[1] for p in bx], color=INK, linewidth=1.0, zorder=6)
    # legend
    lg = cv.panel(col_x, top_in, col_w, lg_h)
    lg.text(0, lg_h - 0.02, "Line (OSM colour tag), route km", va="top", fontsize=10.5, fontweight="bold")
    srt = truth.sort_values("route_km", ascending=False).reset_index(drop=True)
    for i, r in enumerate(srt.itertuples()):
        c, k = divmod(i, 6)
        x0 = c * (col_w / 2 + 0.05)
        y = lg_h - 0.55 - k * 0.27
        lg.plot([x0, x0 + 0.4], [y, y], color=r.colour, linewidth=5, solid_capstyle="butt")
        lg.text(x0 + 0.5, y, r.ref.replace("Hammersmith & City", "Hammersmith & C."), va="center", fontsize=8.8)
        lg.text(x0 + col_w / 2 - 0.1, y, f"{r.route_km:.0f}", va="center", ha="right", fontsize=8.8, color=MUTED)
    y = lg_h - 0.55 - 6 * 0.27 - 0.06
    lg.add_patch(Rectangle((0.0, y - 0.09), 0.4, 0.18, facecolor=LAND, edgecolor="white", linewidth=1.0))
    lg.text(0.5, y, "Borough (muted context)", va="center", fontsize=8.8)
    cv.footnote([
        f"Source: © OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {base[:10]}: route=subway relations of the London Underground network with their colour tags; borough boundaries (admin_level 8) and the River Thames likewise. "
        "Route lengths merge both directions (100 m).",
        "Projection: British National Grid (EPSG:27700). Where lines share track (Circle, Hammersmith & City, District, Metropolitan) the lines are drawn at staggered widths. Excluded: Elizabeth line, DLR, Overground and Tram (separate networks); sidings.",
    ], width=200)
    cv.save(map_id)
    return truth


# --------------------------------------------------------------------------
# 8. Zakarpattia oblast: every settlement (KATOTTH + OpenStreetMap)


KATOTTH_URL = "https://mindev.gov.ua/storage/app/sites/1/uploaded-files/kodifikator-07-07.xlsx"
KATOTTH_PAGE = ("https://mindev.gov.ua/diialnist/rozvytok-mistsevoho-samovriaduvannia/"
                "kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytorii-terytorialnykh-hromad")
KATOTTH_ORDER = "order No. 1321 of 7 July 2026"
ZAK = "UA21"


def zak_data():
    from set2_overlays import match_volyn, read_katotth, relation_polygon
    k = read_katotth(fetch(KATOTTH_URL, "katotth_2026-07-07.xlsx"))
    v = k[k.l1.str.startswith(ZAK)]
    raions = v[v.cat == "P"].set_index("l2").name.to_dict()
    hromadas = v[v.cat == "H"].set_index("l3").name.to_dict()
    sett = v[v.cat.isin(["M", "T", "X", "C"])].copy()
    sett = pd.DataFrame({"katotth": sett.l4, "category": sett.cat, "name": sett.name, "hromada_katotth": sett.l3,
                         "hromada": sett.l3.map(hromadas), "raion_katotth": sett.l2, "raion": sett.l2.map(raions)})
    sett = sett.sort_values("katotth").reset_index(drop=True)
    payload = H.overpass(ZAK_Q, "zakarpattia_overpass.json")
    nodes = [e for e in payload["elements"] if e["type"] == "node"]
    rels = [e for e in payload["elements"] if e["type"] == "relation"]
    polys = {}
    for r in rels:
        tags = r["tags"]
        lvl, code = tags.get("admin_level"), tags.get("katotth", "")
        if lvl == "4":
            polys[("oblast", tags.get("ISO3166-2"))] = (tags.get("name:en", tags.get("name")), relation_polygon(r))
        elif lvl in ("6", "7") and code.startswith(ZAK):
            polys[("raion" if lvl == "6" else "hromada", code)] = (tags.get("name"), relation_polygon(r))
    places = pd.DataFrame([{
        "osm_node_id": n["id"], "lon": n["lon"], "lat": n["lat"], "osm_place": n["tags"].get("place"),
        "osm_name": n["tags"].get("name:uk") or n["tags"].get("name", ""), "osm_katotth": n["tags"].get("katotth", ""),
        "osm_prefix": n["tags"].get("name:prefix", "")} for n in nodes])
    truth, leftovers = match_volyn(sett, places, polys)
    return truth, leftovers, polys, v, H.osm_base(payload), places


def build_zakarpattia_settlements() -> pd.DataFrame:
    from set2_overlays import OSM_SETTLEMENT, norm_name, translit
    map_id = "zakarpattia_settlements"
    truth, leftovers, polys, vrows, base, places = zak_data()
    write_csv(map_id, truth)
    cats = truth.category.value_counts()
    how = truth.match.value_counts()
    matched = int(truth.lon.notna().sum())
    unmatched = truth[truth.lon.isna()]
    n_h = int((vrows.cat == "H").sum())
    n_r = int((vrows.cat == "P").sum())
    osm_settl = places[places.osm_place.isin(OSM_SETTLEMENT)]
    write_md(map_id, [
        f"- URL/API: {KATOTTH_URL} (KATOTTH xlsx, linked from {KATOTTH_PAGE}; rows under {ZAK}, categories M/T/X/C). Coordinates and boundaries: Overpass API, `{compact(ZAK_Q)}`.",
        f"- Vintage: KATOTTH as amended by the Ministry for Development of Communities and Territories {KATOTTH_ORDER} (newest edition listed on {NOW}); OpenStreetMap as of {base}.",
        "- Licence: KATOTTH is an official act of a Ukrainian ministry (not subject to copyright, Law of Ukraine on Copyright Art. 10); coordinates and boundaries © OpenStreetMap contributors, ODbL 1.0.",
        f"- Rows: {len(truth)} KATOTTH settlements of Zakarpattia oblast — {cats.get('M', 0)} cities (M), {cats.get('T', 0)} urban-type settlements (T), {cats.get('X', 0)} селища (X), {cats.get('C', 0)} villages (C); {n_r} raions, {n_h} hromadas. Columns: katotth, category, name, hromada/raion codes and names, match, osm_node_id, osm_place, osm_name, lon, lat. Located via an OSM place node: {matched} ({', '.join(f'{v} {k}' for k, v in how.items() if k != 'unmatched')}); unmatched {int(how.get('unmatched', 0))}" + (f" ({'; '.join(f'{r.name}, {r.hromada} hromada' for r in unmatched.itertuples())})" if len(unmatched) else "") + ".",
        f"- Caveat: KATOTTH has no 'T' rows since the 2024 reform renamed every urban-type settlement to селище (X). {len(leftovers)} OSM settlement nodes in the oblast match no KATOTTH row" + (f" ({'; '.join(f'{r.osm_name} [{r.osm_place}]' for r in leftovers.head(10).itertuples())}{' …' if len(leftovers) > 10 else ''})" if len(leftovers) else "") + ". The spec's `count=609±12%` is checked against the KATOTTH count.",
    ])
    crs = "+proj=tmerc +lat_0=48.5 +lon_0=23.1 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    t = Transformer.from_crs(4326, crs, always_xy=True)

    def proj(geom):
        return gpd.GeoSeries([geom], crs=4326).to_crs(crs)

    zak = polys[("oblast", "UA-21")][1]
    others = {k[1]: g for k, (_n, g) in polys.items() if k[0] == "oblast" and k[1] != "UA-21"}
    x0, y0, x1, y1 = proj(zak).total_bounds
    extent = (x0 - 9_000, x1 + 1_000, y0 - 6_000, y1 + 6_000)
    check_window(map_id, crs, extent)
    n_loc = matched
    cv = Canvas(extent, "Every town and village of Zakarpattia oblast",
                f"All {len(truth):,} settlements in Ukraine’s official codifier (KATOTTH), by legal status · {n_loc:,} located on OpenStreetMap",
                map_w=11.6, footer=1.15)
    ax = cv.ax
    ax.set_facecolor("#f1efe9")
    for code, g in others.items():
        proj(g).plot(ax=ax, color="#e9e6de", edgecolor="none", zorder=1)
    proj(zak).plot(ax=ax, color="#e3ddcf", edgecolor="none", zorder=2)
    hroms = [g for (kind, _c), (_n, g) in polys.items() if kind == "hromada"]
    outlines = [unary_union([p for p in getattr(g, "geoms", [g]) if p.area > 1.5e-4]) for g in hroms]
    gpd.GeoSeries([g.boundary for g in outlines], crs=4326).to_crs(crs).plot(ax=ax, color="white", linewidth=0.7, zorder=2.1)
    raions = {code: (name, g) for (kind, code), (name, g) in polys.items() if kind == "raion"}
    for _c, (_n, g) in raions.items():
        proj(g.boundary.difference(zak.boundary.buffer(0.001))).plot(ax=ax, color="#7d7668", linewidth=1.5, zorder=3)
    other_u = unary_union(list(others.values())).buffer(0.002)
    proj(zak.boundary.intersection(other_u)).plot(ax=ax, color="#5d564a", linewidth=2.0, zorder=3.1)
    proj(zak.boundary.difference(other_u)).plot(ax=ax, color="#5d564a", linewidth=2.0, linestyle=(0, (5, 2.5)), zorder=3.2)
    for name, lon, lat in (("SLOVAKIA", 22.15, 48.95), ("HUNGARY", 22.2, 48.1), ("ROMANIA", 24.2, 47.85), ("POLAND", 22.95, 49.3),
                           ("LVIV OBLAST", 23.35, 49.2), ("IVANO-FRANKIVSK\nOBLAST", 24.4, 48.9)):
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
        "Projection: transverse Mercator centred on 23.1°E. Names transliterated with Ukraine’s 2010 national system. Every city and settlement labelled; villages unlabelled. "
        "Since 2024 KATOTTH has no urban-type settlements (T): all were renamed селище (X). Hromada exclaves under 1 km² not outlined.",
    ], width=200)
    cv.save(map_id)
    return truth


# --------------------------------------------------------------------------
# 10. Operating nuclear power plants in Europe (OpenStreetMap)


NUKE_EN = {  # English labels for non-Latin names (OSM element id -> name)
    28877213: "Zaporizhzhia", 28878631: "Khmelnytskyi", 28878854: "Rivne", 4546127: "South Ukraine", 41289744: "Rostov",
    48906740: "Balakovo", 1953975: "Leningrad", 102427749: "Leningrad II", 107986168: "Smolensk", 564414439: "Kalinin",
    147711211: "Kursk", 252671500: "Novovoronezh II", 394340868: "Novovoronezh", 6334623: "Kola", 866733225: "Belarusian (Astravets)",
    157471225: "Kozloduy", 132511267: "Cernavodă", 8653261: "Bohunice", 4660817: "Temelín", 5538984: "Dukovany", 6046981: "Mochovce",
}
NUKE_EXCLUDE = {  # id -> reason
    8645134918: "research reactor (Institut Laue-Langevin), not a power plant",
    24726877: "university training reactor, not a power plant",
    514718570: "research reactors (NIIAR), not a power plant",
    23652365: "Chornobyl: closed (plant:output 0)",
    30090162: "Lemoiz: never commissioned, disused=yes",
    880625927: "Akkuyu: under construction",
    4456817: "Beloyarsk: east of the Urals, Asia",
    271970600: "Metsamor (Armenia): South Caucasus, counted with Asia",
}
EUROPE_FRAME_LL = (-13.0, 33.0, 52.0, 70.0)


def parse_mw(v):
    if v is None:
        return np.nan
    m = re.match(r"^\s*([\d.,]+)\s*(GW|MW|kW)?", str(v))
    if not m:
        return np.nan
    x = float(m.group(1).replace(",", "."))
    return x * {"GW": 1000, "MW": 1, "kW": 0.001, None: 1}[m.group(2)]


def nuke_clean(name: str) -> str:
    name = re.sub(r"^(Centre Nucléaire de Production d.Electricité (de |du |d.)|Centrale nucléaire (de |du |d.)|Central nuclear (de |d.)|"
                  r"Centrale Nucléaire |Jaderná elektrárna |Jadrová elektráreň |Kernkraftwerk |Kerncentrale |Kernenergiecentrale |Central Nuclear de )", "", name)
    name = re.sub(r" (Nuclear Power Station|nuclear power plant|kärnkraftverk|Kärnkraftverk|ydinvoimalaitos|Nuclear Power Plant)$", "", name)
    name = name.replace("Olkiluoto Nuclear Power Plant", "Olkiluoto").replace("Olkiluodon ydinvoimalaitos", "Olkiluoto").replace("Forsmarks Kärnkraftverk", "Forsmark").replace("Oskarshamns kärnkraftverk", "Oskarshamn")
    name = name.replace("Loviisan ydinvoimalaitos", "Loviisa").replace("Paksi Atomerőmű", "Paks").replace("Ringhals kärnkraftverk", "Ringhals")
    name = name.replace("Jedrska elektrarna Krško", "Krško").replace("Atómové elektrárne Bohunice", "Bohunice")
    return name.strip()


def build_europe_nuclear_plants() -> pd.DataFrame:
    map_id = "europe_nuclear_plants"
    payload = H.overpass(NUKE_Q, "europe_nuclear_overpass.json")
    base = H.osm_base(payload)
    rows, dropped = [], []
    for e in payload["elements"]:
        t = e["tags"]
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if e["id"] in NUKE_EXCLUDE:
            dropped.append((e["id"], t.get("name"), NUKE_EXCLUDE[e["id"]]))
            continue
        nm = NUKE_EN.get(e["id"]) or nuke_clean(t.get("name:en") or t.get("name", ""))
        rows.append({"osm": f"{e['type'][0]}{e['id']}", "osm_id": e["id"], "name": nm, "name_osm": t.get("name", ""),
                     "lon": round(c["lon"], 5), "lat": round(c["lat"], 5), "capacity_mw": parse_mw(t.get("plant:output:electricity")),
                     "operator": t.get("operator", ""), "start_date": t.get("start_date", "")})
    df = pd.DataFrame(rows)
    # sites mapped as two plants within 5 km (Leningrad I+II, Novovoronezh I+II) are one site
    df["site"] = range(len(df))
    for i in range(len(df)):
        for j in range(i + 1, len(df)):
            if haversine_km((df.lon[i], df.lat[i]), (df.lon[j], df.lat[j])) < 5:
                df.loc[df.site == df.site[j], "site"] = df.site[i]
    merged = []
    for _s, g in df.groupby("site"):
        g = g.sort_values("capacity_mw", ascending=False)
        top = g.iloc[0].copy()
        if len(g) > 1:
            top["capacity_mw"] = g.capacity_mw.sum()
            top["name"] = re.sub(r" II$", "", top["name"]) + " (I+II)"
            top["osm"] = ";".join(g.osm)
            top["name_osm"] = " + ".join(g.name_osm)
        merged.append(top)
    plants = pd.DataFrame(merged).drop(columns=["site", "osm_id"]).reset_index(drop=True)
    world = H.world10()[["key", "NAME", "geometry"]]
    pts = gpd.GeoDataFrame(plants, geometry=gpd.points_from_xy(plants.lon, plants.lat), crs=4326)
    j = gpd.sjoin_nearest(pts.to_crs(3035), world.to_crs(3035), how="left")
    j = j[~j.index.duplicated()]
    plants["country"] = j.NAME.values
    plants["country_iso3"] = j.key.values
    plants = plants.sort_values(["country", "name"]).reset_index(drop=True)
    plants["status"] = "operating (OSM power=plant, not disused or under construction)"
    if plants.capacity_mw.isna().any():
        print("  NOTE capacity missing:", plants[plants.capacity_mw.isna()].name.tolist())
    write_csv(map_id, plants)
    total = plants.capacity_mw.sum()
    bycountry = plants.groupby("country").capacity_mw.sum().sort_values(ascending=False)
    write_md(map_id, [
        f"- URL/API: Overpass API (overpass-api.de): `{compact(NUKE_Q)}`; country by nearest Natural Earth 1:10m polygon.",
        f"- Vintage: OpenStreetMap as of {base}. Operating status is OSM's: `power=plant` objects that are neither `disused`, `construction` nor capacity 0; capacities are the `plant:output:electricity` tags (net MW for the plants cross-checked: Gravelines 5,460, Paluel 5,320, Cattenom 5,200, Zaporizhzhia 5,700).",
        "- Licence: © OpenStreetMap contributors, ODbL 1.0. Land: Natural Earth (public domain).",
        f"- Rows: {len(plants)} plant sites, {total / 1000:,.1f} GW in all; {bycountry.index[0]} {bycountry.iloc[0] / 1000:.1f} GW, {bycountry.index[1]} {bycountry.iloc[1] / 1000:.1f} GW. Dropped from the {len(payload['elements'])} OSM objects: " + "; ".join(f"{n} ({r})" for _i, n, r in dropped) + f". Leningrad I+II and Novovoronezh I+II are mapped as two plants 0.3 km apart and are one site each here.",
        "- Caveat: Zaporizhzhia (6 units, 5,700 MW) is listed as operating by OSM but all six units are in cold shutdown under Russian occupation; it is drawn and flagged. Germany shut its last three reactors in April 2023; no German plant remains. Russian plants east of the Urals and Armenia's Metsamor are out of the Europe frame by choice. OSM tags are not an audited register (GEM or IAEA PRIS would be): capacities may lag unit retirements.",
    ])
    crs = "EPSG:3035"
    tr = Transformer.from_crs(4326, crs, always_xy=True)
    plants["x"], plants["y"] = tr.transform(plants.lon.to_numpy(), plants.lat.to_numpy())
    extent = (plants.x.min() - 420_000, plants.x.max() + 330_000, plants.y.min() - 230_000, plants.y.max() + 230_000)
    check_window(map_id, crs, extent)
    cv = Canvas(extent, "Operating nuclear power plants in Europe",
                f"{len(plants)} plant sites, circle area proportional to electrical capacity · {total / 1000:,.0f} GW in all, "
                f"{bycountry.index[0]} {bycountry.iloc[0] / 1000:.0f} GW and {bycountry.index[1]} {bycountry.iloc[1] / 1000:.0f} GW lead",
                map_w=11.6, footer=1.0)
    ax = cv.ax
    w3 = world.to_crs(crs)
    w3.plot(ax=ax, color=LAND, edgecolor="white", linewidth=0.45, zorder=1)
    H.local_graticule(ax, crs, range(-10, 61, 10), range(30, 71, 10), lon_span=(-20, 70), lat_span=(25, 75))
    k = 0.26   # points^2 per MW
    for r in plants.sort_values("capacity_mw", ascending=False).itertuples():
        occ = "Zaporizhzhia" in r.name
        ax.scatter([r.x], [r.y], s=r.capacity_mw * k, color="#d94f1e" if not occ else "#7a7a7a", alpha=0.68, edgecolor="white", linewidth=0.8, zorder=5)
    zap = plants[plants.name == "Zaporizhzhia"].iloc[0]
    ax.scatter([zap.x], [zap.y], s=zap.capacity_mw * k, facecolor="none", edgecolor=INK, linewidth=1.3, zorder=6, linestyle=(0, (3, 2)))
    cv.fig.canvas.draw()
    rend = cv.fig.canvas.get_renderer()
    taken = []
    cands = [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)]
    label_set = plants[(plants.capacity_mw >= 3000) | plants.name.isin(["Zaporizhzhia", "Gravelines"])].sort_values("capacity_mw", ascending=False)
    for r in label_set.itertuples():
        rad = (r.capacity_mw * k / np.pi) ** 0.5
        text = r.name + (" ⚠" if False else "")
        for mult in (1, 2.2, 3.4):
            done = False
            for ux, uy in cands:
                dx, dy = ux * (rad + 4) * mult, uy * (rad + 4) * mult
                a = ax.annotate(text, (r.x, r.y), xytext=(dx, dy), textcoords="offset points", ha="left" if ux > 0 else ("right" if ux < 0 else "center"),
                                va="bottom" if uy > 0 else ("top" if uy < 0 else "center"), fontsize=8.6, fontweight="bold" if r.capacity_mw >= 5000 or r.name in ("Zaporizhzhia", "Gravelines") else "normal",
                                zorder=9, path_effects=HALO,
                                arrowprops=dict(arrowstyle="-", color="#5d6a70", linewidth=0.5, shrinkA=0, shrinkB=0) if mult > 1 else None)
                a.update_positions(rend)
                bb = mpl.text.Text.get_window_extent(a, rend).expanded(1.05, 1.15)
                if not any(bb.overlaps(o) for o in taken) and ax.bbox.padded(-3).contains(bb.x0, bb.y0) and ax.bbox.padded(-3).contains(bb.x1, bb.y1):
                    taken.append(bb)
                    done = True
                    break
                a.remove()
            if done:
                break
        else:
            print("  no label position for", r.name)
    for name, lon, lat in (("RUSSIA", 40.0, 56.5), ("UKRAINE", 31.0, 49.2), ("FRANCE", 2.2, 46.6), ("SPAIN", -3.8, 40.6), ("UNITED\nKINGDOM", -2.2, 53.0), ("SWEDEN", 15.5, 62.5),
                           ("FINLAND", 26.0, 64.3), ("BELARUS", 28.0, 53.4), ("GERMANY", 10.3, 51.1), ("POLAND", 19.5, 52.3), ("ROMANIA", 24.8, 46.0), ("NORWAY", 9.0, 62.0), ("ITALY", 12.0, 43.0)):
        (x, y), = project_points(crs, [(lon, lat)])
        if extent[0] < x < extent[1] and extent[2] < y < extent[3]:
            ax.text(x, y, name, fontsize=8.5, color="#a29d91", ha="center", va="center", zorder=2, linespacing=1.1)
    col_x = cv.left + cv.map_w + 0.4
    col_w = WIDTH_IN - col_x - 0.3
    lg = cv.panel(col_x, cv.H - cv.header - 0.05, col_w, 7.6)
    lg.text(0, 7.55, "Electrical capacity (circle area)", va="top", fontsize=10.5, fontweight="bold")
    y = 7.05
    for mw in (1000, 3000, 5000):
        d = (mw * k / np.pi) ** 0.5 * 2 / 72
        lg.add_patch(Circle((0.55, y), d / 2, facecolor="#d94f1e", alpha=0.68, edgecolor="white"))
        lg.text(1.25, y, f"{mw:,} MW", va="center", fontsize=9.4)
        y -= max(d, 0.25) + 0.22
    lg.add_patch(Circle((0.55, y), 0.17, facecolor="#7a7a7a", alpha=0.68, edgecolor=INK, linewidth=1.1, linestyle=(0, (3, 2))))
    lg.text(1.0, y, "Zaporizhzhia: listed as operating,\nall units in cold shutdown (occupied)", va="center", fontsize=8.3, linespacing=1.25)
    y -= 0.55
    lg.text(0, y, "Largest sites (MW)", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.33
    for r in plants.sort_values("capacity_mw", ascending=False).head(12).itertuples():
        lg.text(0, y, r.name[:24], va="top", fontsize=8.8)
        lg.text(col_w - 0.05, y, f"{r.capacity_mw:,.0f}", va="top", ha="right", fontsize=8.8)
        y -= 0.215
    y -= 0.15
    lg.text(0, y, "By country (GW)", va="top", fontsize=10.5, fontweight="bold")
    y -= 0.33
    for cn, v in bycountry.head(8).items():
        lg.text(0, y, cn, va="top", fontsize=8.8)
        lg.text(col_w - 0.05, y, f"{v / 1000:.1f}", va="top", ha="right", fontsize=8.8)
        y -= 0.215
    cv.footnote([
        f"Source: © OpenStreetMap contributors (ODbL 1.0), Overpass API, data as of {base[:10]}: power=plant with plant:source=nuclear, capacity from plant:output:electricity. Land: Natural Earth 1:10m.",
        f"Projection: ETRS89 Lambert azimuthal equal-area (EPSG:3035). Circle area ∝ MW; sites labelled if ≥ 3,000 MW. Excluded: research reactors, Chornobyl (closed), Lemoiz (never commissioned), Akkuyu (under construction), "
        "Beloyarsk (Urals, Asia) and Metsamor (Armenia); Germany has no operating plant since April 2023.",
    ], width=200)
    cv.save(map_id)
    return plants
