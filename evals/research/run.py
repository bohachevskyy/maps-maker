"""Run the research-agent eval set and score proposals on properties.

    uv run python -m evals.research.run                 # tune split, replay-or-record
    uv run python -m evals.research.run --cases whs_points --repeat 3
    uv run python -m evals.research.run --split heldout # scores only
    uv run python -m evals.research.run --refresh       # re-record from the live web

Each case runs with its own empty discovered-sources store, so nothing found
for one case helps another. Web traffic goes through the replay layer
(MAPSVC_REPLAY=cached): the first run records every response under
evals/research/cassettes/, later runs replay those bytes, and only requests
never seen before touch the network. The model itself is live, so use
--repeat before trusting a pass.

Non-control cases call the research step directly with a generic reason -- the
spec's `need` text names sources and would leak the answer. Control cases go
through the main agent, because what they test is that the registry answers
and research never runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import tomllib
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
LEVELS = {"country": "admin_0", "admin1": "admin_1", "admin2": "admin_2",
          "nuts1": "nuts_1", "nuts2": "nuts_2", "nuts3": "nuts_3"}
NEAR_KM = 5.0
GENERIC_REASON = "no source in the registry measures this at the requested granularity"


# ------------------------------------------------------------- geometry ---

def km(a, b):
    p = math.pi / 180
    h = (math.sin((b[1] - a[1]) * p / 2) ** 2
         + math.cos(a[1] * p) * math.cos(b[1] * p) * math.sin((b[0] - a[0]) * p / 2) ** 2)
    return 12742 * math.asin(min(1.0, math.sqrt(h)))


class Grid:
    """Points bucketed by whole degree, for 'is anything within 5 km' queries."""

    def __init__(self, points):
        self.cells: dict = {}
        for pt in points:
            self.cells.setdefault((math.floor(pt[0]), math.floor(pt[1])), []).append(pt)

    def near(self, pt, limit=NEAR_KM) -> bool:
        cx, cy = math.floor(pt[0]), math.floor(pt[1])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in self.cells.get((cx + dx, cy + dy), ()):
                    if km(pt, other) < limit:
                        return True
        return False


def _float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------- what was found ---

def _rows(record):
    from mapsvc.discovery import http, probe
    payload = http.get(record["url"], expect="data").content
    from mapsvc.discovery import rowfilter
    rows, fmt, _geo, _warn = probe.records(payload)
    return rowfilter.apply(rows, (record.get("roles") or {}).get("filter")), fmt, payload


def _features(record):
    """(points, groups): coordinates the proposal yields, and how many features."""
    roles = record.get("roles") or {}
    rows, fmt, payload = _rows(record)
    pts, groups = [], set()
    names = {_norm(r.get(roles["name"])) for r in rows} if roles.get("name") else set()
    if fmt == "geojson" and record.get("geometry_kind") == "lines":
        for i, feat in enumerate(json.loads(payload).get("features") or []):
            geom = feat.get("geometry") or {}
            lines = geom.get("coordinates") or []
            if geom.get("type") == "LineString":
                lines = [lines]
            for line in lines if geom.get("type") in ("LineString", "MultiLineString") else []:
                pts.extend((c[0], c[1]) for c in line if len(c) >= 2)
            groups.add(i)
        return pts, len(groups), names
    lon_col = roles.get("lon") or "__longitude"
    lat_col = roles.get("lat") or "__latitude"
    if roles.get("end_lon") and roles.get("end_lat"):      # one segment per row
        for row in rows:
            for a, b in ((lon_col, lat_col), (roles["end_lon"], roles["end_lat"])):
                lon, lat = _float(row.get(a)), _float(row.get(b))
                if lon is not None and lat is not None:
                    pts.append((lon, lat))
        return pts, len(rows), names
    for i, row in enumerate(rows):
        lon, lat = _float(row.get(lon_col)), _float(row.get(lat_col))
        if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        pts.append((lon, lat))
        groups.add(row.get(roles["name"]) if roles.get("name") else i)
    n = len(groups) if record.get("geometry_kind") == "lines" else len(pts)
    return pts, n, names


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


# ---------------------------------------------------------------- truth ---

def _truth(case):
    rows = list(csv.DictReader(open(HERE / case["truth"], encoding="utf-8")))
    return rows


def _truth_values(case, rows):
    key = list(rows[0])[0]
    numeric = [c for c in rows[0] if c != key and _float(rows[0][c]) is not None
               and c not in ("year", "ibge_code")]
    col = numeric[-1]
    return {r[key]: _float(r[col]) for r in rows if _float(r[col]) is not None}


def _truth_points(case, rows):
    lon = "slon" if "slon" in rows[0] else "lon"
    lat = "slat" if "slat" in rows[0] else "lat"
    return [((float(r[lon]), float(r[lat])), r) for r in rows if r[lon] and r[lat]]


# ---------------------------------------------------------------- spots ---

def check_spot(spot, values=None, count=None, grid=None):
    if m := re.match(r"count=(\d+)±(\d+)%$", spot):
        n, tol = int(m[1]), int(m[2])
        return count is not None and abs(count - n) <= n * tol / 100
    if spot.startswith("event_near="):
        lon, lat = (float(x) for x in spot[len("event_near="):].split(","))
        return grid is not None and grid.near((lon, lat))
    values = values or {}
    if m := re.match(r"(\S+)=([\d.]+)±([\d.]+)%$", spot):
        got = values.get(m[1])
        return got is not None and abs(got - float(m[2])) <= float(m[2]) * float(m[3]) / 100
    if m := re.match(r"(\S+)([<>])(\S+)$", spot):
        a = values.get(m[1])
        b = values.get(m[3]) if m[3] in values else _float(m[3])
        if a is None or b is None:
            return False
        return a < b if m[2] == "<" else a > b
    raise ValueError(f"unknown spot {spot!r}")


def _year(text):
    years = [int(y) for y in re.findall(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", str(text or ""))]
    return max(years) if years else None


def score(case, record):
    """Property checks on a registered record -> {check: bool}, plus details."""
    from mapsvc.discovery.proposal import LICENCE_UNSTATED
    exp = case["expected"]
    checks, detail = {}, {}
    kind = exp["geometry_kind"]
    checks["kind"] = record.get("geometry_kind") == kind
    if kind == "choropleth":
        checks["key_namespace"] = record.get("key_namespace") == exp["key_namespace"]
        checks["level"] = record.get("level") == LEVELS.get(exp["level"], exp["level"])
    year = _year(record.get("vintage"))
    checks["vintage"] = year is not None and year >= exp.get("min_vintage", 0)
    pubs = exp.get("acceptable_publishers") or []
    if pubs:
        hay = f"{record.get('publisher')} {record.get('url')} {record.get('landing_page')}".lower()
        checks["publisher"] = any(p.lower() in hay for p in pubs)
    if exp.get("must_have_licence"):
        checks["licence"] = bool(record.get("licence")) and record["licence"] != LICENCE_UNSTATED

    rows = _truth(case)
    try:
        if kind == "choropleth":
            from mapsvc.statistics import discovered
            values = discovered.load(record["id"], record.get("level")).values
            truth = _truth_values(case, rows)
            matched = [k for k in truth if k in values]
            detail["coverage"] = f"{len(matched)}/{len(truth)}"
            checks["coverage"] = len(matched) >= exp.get("min_coverage", 0) * len(truth)
            prefix = {k.split("-")[0] for k in truth if "-" in k}
            count = len([k for k in values if not prefix or k.split("-")[0] in prefix])
            for spot in case.get("spots") or []:
                checks[f"spot {spot}"] = check_spot(spot, values=values, count=count)
            detail["sample"] = {k: values.get(k) for k in list(truth)[:3]}
        else:
            pts, count, names = _features(record)
            grid = Grid(pts)
            truth = _truth_points(case, rows)
            # A truth feature is covered by a found point within 5 km, or by a
            # found row of the same name: two official sources place a large
            # site's point differently (measured: 27% of World Heritage sites
            # differ by >5 km between UNESCO's two lists, median 1,474 ha).
            tname = "name" if "name" in rows[0] else None
            hit = sum(1 for t, r in truth
                      if grid.near(t) or (tname and names and _norm(r[tname]) in names))
            detail["coverage"] = f"{hit}/{len(truth)} truth features within {NEAR_KM:g} km or same name"
            detail["count"] = count
            checks["coverage"] = hit >= exp.get("min_coverage", 0) * len(truth)
            for spot in case.get("spots") or []:
                checks[f"spot {spot}"] = check_spot(spot, count=count, grid=grid)
    except Exception as exc:  # a registered record that cannot be loaded fails loudly
        checks["loads"] = False
        detail["load_error"] = f"{type(exc).__name__}: {exc}"
    return checks, detail


# ------------------------------------------------------------------ run ---

def run_case(case, out_dir, rep):
    os.environ["MAPSVC_DISCOVERED_DB"] = str(out_dir / f"{case['id']}-{rep}.sqlite")
    Path(os.environ["MAPSVC_DISCOVERED_DB"]).unlink(missing_ok=True)
    from mapsvc import agent
    from mapsvc.discovery import pipeline

    result = {"id": case["id"], "rep": rep, "split": case["split"]}
    started = time.time()
    try:
        if case.get("control"):
            seen = []
            real = agent._discover
            agent._discover = lambda p, r: seen.append(r) or real(p, r)
            try:
                _m, raw, _why = agent.describe(case["prompt"])
            finally:
                agent._discover = real
            result["manifest_source"] = (raw.get("variable") or {}).get("source")
            result["checks"] = {"no_research": not seen,
                                "registry_answer": result["manifest_source"] not in (None, "discovered")}
        else:
            outcome = pipeline.discover(case["prompt"], GENERIC_REASON)
            result["trace"] = outcome.trace
            result["refusal"] = outcome.refusal
            result["record"] = outcome.record
            if case.get("should_refuse"):
                result["checks"] = {"refused": outcome.record is None}
            elif outcome.record is None:
                result["checks"] = {"found": False}
            else:
                checks, detail = score(case, outcome.record)
                result["checks"], result["detail"] = {"found": True} | checks, detail
    except Exception as exc:
        result["checks"] = {"ran": False}
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-3000:]
    result["seconds"] = round(time.time() - started, 1)
    result["passed"] = all(result["checks"].values())
    result["fraction"] = sum(result["checks"].values()) / max(1, len(result["checks"]))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="tune", choices=("tune", "heldout", "all"))
    ap.add_argument("--cases", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--refresh", action="store_true", help="re-record from the live web")
    args = ap.parse_args(argv)

    os.environ["MAPSVC_RESEARCH"] = "1"
    os.environ["MAPSVC_REPLAY"] = "record" if args.refresh else "cached"
    os.environ.setdefault("MAPSVC_CASSETTES", str(HERE / "cassettes"))
    os.environ.setdefault("MAPSVC_SEARXNG_URL", "http://127.0.0.1:8888")

    cases = tomllib.load(open(HERE / "specs.toml", "rb"))["case"]
    wanted = {c for c in args.cases.split(",") if c}
    cases = [c for c in cases if (args.split == "all" or c["split"] == args.split)
             and (not wanted or c["id"] in wanted)]
    heldout = any(c["split"] == "heldout" for c in cases)
    out_dir = RESULTS / ("heldout" if heldout else "tune")
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for case in cases:
        for rep in range(args.repeat):
            r = run_case(case, out_dir, rep)
            results.append(r)
            (out_dir / f"{case['id']}-{rep}.json").write_text(
                json.dumps(r, indent=2, default=str, ensure_ascii=False))
            failed = [k for k, v in r["checks"].items() if not v]
            line = f"{'PASS' if r['passed'] else 'FAIL'} {case['id']:26} rep {rep} " \
                   f"{r['fraction']:.0%} {r['seconds']:6.1f}s"
            if not heldout:   # held-out failures are scores only
                line += ("  " + ", ".join(failed) if failed else "")
                if r.get("error"):
                    line += f"  [{r['error'][:160]}]"
            print(line, flush=True)

    passed = sum(r["passed"] for r in results)
    mean = sum(r["fraction"] for r in results) / max(1, len(results))
    summary = f"{passed}/{len(results)} passed, mean check fraction {mean:.0%}"
    print(summary)
    (out_dir / "SUMMARY.txt").write_text(summary + "\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
