"""Run the reference-map evaluations and keep every artifact.

    uv run python -m evals.maps.run [ids...] [--repeat N] [--out DIR]

This is a live runner, not part of pytest.  Its parsing and automatic checks are
kept as pure functions so their unit tests remain offline.
"""

import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import statistics as stdlib_statistics
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET

from mapsvc import project
from mapsvc.agent import AgentRefusal, AgentUnavailable, backend_name, describe
from mapsvc.harvest import harvest
from mapsvc.pipeline import draw


HERE = pathlib.Path(__file__).resolve().parent
SPECS = HERE / "specs.toml"
# Truth and references live next to the specs file: evals/maps/<id>/ for the
# tuning set, evals/heldout/<id>/ for the held-out set (--specs).
REFS = HERE
SVG_NS = "{http://www.w3.org/2000/svg}"
RSVG_TIMEOUT = 120
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_EQUALITY = re.compile(
    rf"^\s*([^<>=±]+?)\s*=\s*({_NUMBER})\s*±\s*({_NUMBER})\s*(%)?\s*$"
)
_COMPARISON = re.compile(r"^\s*([^<>=±]+?)\s*([<>])\s*([^<>=±]+?)\s*$")
# event_near=lon,lat / line_near=lon,lat: an overlay feature passes within a
# tolerance of a point. LGVs stop 8-12 km short of city centres, hence 20 km.
_NEAR = re.compile(rf"^\s*(event_near|line_near)\s*=\s*({_NUMBER})\s*,\s*({_NUMBER})\s*$")
NEAR_KM = {"event_near": 100.0, "line_near": 20.0}
_ISO_3166_2 = re.compile(r"^[A-Z]{2}-(.+)$", re.IGNORECASE)


def parse_spot(expression: str) -> dict:
    """Parse the machine-checkable subset of the spot-check language."""
    equality = _EQUALITY.fullmatch(expression)
    if equality:
        key, expected, tolerance, percent = equality.groups()
        return {
            "kind": "approx", "key": key.strip(),
            "expected": float(expected), "tolerance": float(tolerance),
            "percent": bool(percent),
        }

    near = _NEAR.fullmatch(expression)
    if near:
        what, lon, lat = near.groups()
        return {"kind": "near", "what": what, "lon": float(lon), "lat": float(lat),
                "km": NEAR_KM[what]}

    comparison = _COMPARISON.fullmatch(expression)
    if comparison:
        left, operator, right = comparison.groups()
        right = right.strip()
        if re.fullmatch(_NUMBER, right):
            operand = {"literal": float(right)}
        else:
            operand = {"key": right}
        return {
            "kind": "comparison", "left": left.strip(),
            "operator": operator, "right": operand,
        }

    raise ValueError(
        "supported forms are key=value±tolerance, key>key, key<N, key>N, "
        "event_near=lon,lat and line_near=lon,lat"
    )


def _km(a, b) -> float:
    import math

    la1, la2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2)
         * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, h)))


def _segment_km(p, a, b) -> float:
    """Distance from p to segment ab, on a local flat projection around p."""
    import math

    k = math.cos(math.radians(p[1]))
    ax, ay = (a[0] - p[0]) * k, a[1] - p[1]
    bx, by = (b[0] - p[0]) * k, b[1] - p[1]
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy) * 111.195


def _near(parsed: dict, geometry: dict | None) -> tuple[float | None, str | None]:
    if geometry is None:
        return None, "no overlay geometry was harvested for this map"
    target = (parsed["lon"], parsed["lat"])
    if parsed["what"] == "event_near":
        points = geometry.get("points") or []
        if not points:
            return None, "the map has no overlay points"
        return min(_km(target, p) for p in points), None
    lines = geometry.get("lines") or []
    if not lines:
        return None, "the map has no overlay lines"
    best = float("inf")
    for line in lines:
        for i in range(len(line) - 1):
            best = min(best, _segment_km(target, line[i], line[i + 1]))
    return best, None


def overlay_geometry(result) -> dict:
    """Points and lines of every overlay in a harvest, for the near checks."""
    points, lines = [], []
    for layer in getattr(result, "overlays", []) or []:
        if layer["kind"] in ("points", "places"):
            points.extend((f["lon"], f["lat"]) for f in layer["features"])
        elif layer["kind"] == "lines":
            lines.extend([tuple(c) for c in f["coords"]] for f in layer["features"])
        elif layer["kind"] == "flow":
            groups: dict = {}
            for f in layer["features"]:
                groups.setdefault(f["group"], []).append(f)
            for rows in groups.values():
                rows.sort(key=lambda f: f["order"])
                lines.append([(f["lon"], f["lat"]) for f in rows])
    return {"points": points, "lines": lines}


def evaluate_spot(expression: str, values: dict[str, object],
                  geometry: dict | None = None) -> dict:
    """Evaluate one spot check, explaining every case that cannot be checked."""
    result = {"expression": expression}
    try:
        parsed = parse_spot(expression)
    except ValueError as exc:
        return {**result, "status": "unevaluable", "reason": str(exc)}

    def numeric(key: str) -> tuple[float | None, str | None]:
        if key not in values:
            return None, f"{key!r} has no data-value in the rendered map"
        try:
            return float(values[key]), None
        except (TypeError, ValueError):
            return None, f"{key!r} has non-numeric data-value {values[key]!r}"

    if parsed["kind"] == "near":
        distance, reason = _near(parsed, geometry)
        if reason:
            return {**result, "status": "unevaluable", "reason": reason}
        return {**result, "status": "pass" if distance <= parsed["km"] else "fail",
                "nearest_km": round(distance, 1), "tolerance_km": parsed["km"]}

    if parsed["kind"] == "approx":
        actual, reason = numeric(parsed["key"])
        if reason:
            return {**result, "status": "unevaluable", "reason": reason}
        expected = parsed["expected"]
        tolerance = parsed["tolerance"]
        allowed = abs(expected) * tolerance / 100 if parsed["percent"] else tolerance
        passed = abs(actual - expected) <= allowed
        return {
            **result, "status": "pass" if passed else "fail",
            "actual": actual, "expected": expected,
            "tolerance": (f"{tolerance:g}%" if parsed["percent"] else tolerance),
        }

    actual_left, reason = numeric(parsed["left"])
    if reason:
        return {**result, "status": "unevaluable", "reason": reason}
    right = parsed["right"]
    if "literal" in right:
        actual_right = right["literal"]
        right_label = actual_right
    else:
        actual_right, reason = numeric(right["key"])
        if reason:
            return {**result, "status": "unevaluable", "reason": reason}
        right_label = right["key"]
    passed = actual_left > actual_right if parsed["operator"] == ">" else actual_left < actual_right
    return {
        **result, "status": "pass" if passed else "fail",
        "left": actual_left, "operator": parsed["operator"],
        "right": actual_right, "right_source": right_label,
    }


def _load_specs(ids: list[str]) -> list[dict]:
    cases = tomllib.loads(SPECS.read_text(encoding="utf-8"))["map"]
    by_id = {case["id"]: case for case in cases}
    if not ids:
        return cases
    unknown = [case_id for case_id in ids if case_id not in by_id]
    if unknown:
        raise SystemExit(f"unknown map id(s): {', '.join(unknown)}")
    return [by_id[case_id] for case_id in ids]


def _svg_facts(svg: str) -> tuple[set[str], dict[str, str], str]:
    root = ET.fromstring(svg)
    features: set[str] = set()
    values: dict[str, str] = {}
    for path in root.findall(f".//{SVG_NS}path"):
        key = path.get("data-id")
        if key is None:
            continue
        features.add(key)
        if path.get("data-value") is not None:
            values[key] = path.get("data-value")
    # Overlays state their facts as data-stat-* on their group: count,
    # total_km, start_survivors... The map's feature count is then the
    # overlay features, not the base map's polygons.
    overlay_count = 0
    for group in root.iter(f"{SVG_NS}g"):
        if group.get("data-overlay") is None:
            continue
        for name, value in group.attrib.items():
            if name.startswith("data-stat-"):
                values[name[len("data-stat-"):]] = value
        overlay_count += int(float(group.get("data-stat-count") or 0))
    if overlay_count:
        features = {f"overlay-feature-{i}" for i in range(overlay_count)}
    text = " ".join(node.text or "" for node in root.findall(f".//{SVG_NS}text"))
    return features, values, text


def _check_units(spec: dict, features: set[str]) -> dict:
    low, high = spec["units"]
    actual = len(features)
    return {
        "status": "pass" if low <= actual <= high else "fail",
        "actual": actual, "expected": [low, high],
    }


def _point_checks(kind: str, points: list, extent) -> list[dict]:
    results = []
    for lon, lat in points:
        item = {"point": [lon, lat]}
        if extent is None:
            results.append({
                **item, "status": "unevaluable",
                "reason": "neither a manifest bbox nor a rendered geographic extent was available",
            })
            continue
        west, south, east, north = extent
        inside = west <= lon <= east and south <= lat <= north
        passed = inside if kind == "contains" else not inside
        results.append({
            **item, "status": "pass" if passed else "fail",
            "extent": list(extent),
        })
    return results


def _extent_for(manifest, result=None):
    # A bbox is the window only when nothing else frames the map. With `within`
    # or overlays the renderer frames on the data (and drops a bbox that only
    # adds empty space), so the manifest's bbox is not what was drawn -- the
    # Trans-Siberian failed its Beijing exclude on a world bbox the renderer
    # had ignored.
    if manifest.bbox is not None and not manifest.within and not getattr(manifest, "overlays", None):
        return manifest.bbox
    result = result or harvest(manifest)
    if result.provenance.get("overlay_bbox"):
        return tuple(result.provenance["overlay_bbox"])
    geometries = [row["geometry"] for row in result.rows]
    geometries.extend(item["geometry"] for item in result.dropped if item.get("geometry"))
    if getattr(result, "overlays", None):
        # The same framing the renderer uses: the units near the overlay.
        from mapsvc import overlay_render

        geometries = overlay_render.focus(geometries, result.overlays)
    return project.extent_of(geometries) if geometries else None


def _footnote_check(text: str) -> dict:
    """What CLAUDE.md makes mandatory, by the kind of map, not by exact wording.

    Every map names its boundary source and projection. A map with a variable
    also states the variable, its classification and how many units were
    excluded; a map with overlays states each overlay and its exclusions.
    """
    has_variable = "Variable:" in text
    has_overlay = "Overlay:" in text
    required = ["Projection:"]
    missing = [token for token in required if token not in text]
    if "Boundaries:" not in text and "Source:" not in text:
        missing.append("Boundaries:/Source:")
    if not (has_variable or has_overlay):
        missing.append("Variable:/Overlay:")
    if has_variable and "Classification:" not in text:
        missing.append("Classification:")
    if "excluded" not in text:
        missing.append("excluded")
    result = {"status": "pass" if not missing else "fail"}
    if missing:
        result["missing"] = missing
    return result


def _resolve_key(key: str, candidates: set[str]) -> str | None:
    """Resolve a truth key without guessing when a short id is ambiguous."""
    if key in candidates:
        return key
    prefixed = _ISO_3166_2.fullmatch(key)
    if prefixed:
        matches = [candidate for candidate in candidates
                   if _ISO_3166_2.fullmatch(candidate) is None
                   and candidate == prefixed.group(1)]
    else:
        matches = [candidate for candidate in candidates
                   if (match := _ISO_3166_2.fullmatch(candidate)) is not None
                   and match.group(1) == key]
    return matches[0] if len(matches) == 1 else None


def _truth_check(path: pathlib.Path, features: set[str], values: dict[str, str]) -> dict:
    if not path.exists():
        return {"status": "not_available", "reason": "truth.csv is absent"}
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            lowered = {name.lower(): name for name in fields}
            key_field = next((lowered[name] for name in ("key", "id", "data-id")
                              if name in lowered), fields[0] if fields else None)
            value_field = next((lowered[name] for name in ("value", "truth")
                                if name in lowered), fields[1] if len(fields) > 1 else None)
            if key_field is None or value_field is None:
                return {
                    "status": "unevaluable",
                    "reason": "truth.csv needs key and value columns",
                }
            rows = [(row.get(key_field, "").strip(), row.get(value_field, "").strip())
                    for row in reader]
    except (OSError, csv.Error) as exc:
        return {"status": "unevaluable", "reason": f"could not read truth.csv: {exc}"}

    rows = [(key, value) for key, value in rows if key]
    present = [(key, truth, rendered_key)
               for key, truth in rows
               if (rendered_key := _resolve_key(key, features)) is not None]
    matching = [(key, truth, rendered_key)
                for key, truth, rendered_key in present
                if rendered_key in values]
    errors = []
    skipped = []
    for key, truth, rendered_key in matching:
        try:
            expected = float(truth)
            actual = float(values[rendered_key])
        except (TypeError, ValueError):
            skipped.append(key)
            continue
        if expected == 0:
            if actual == 0:
                errors.append(0.0)
            else:
                skipped.append(key)
            continue
        errors.append(abs(actual - expected) / abs(expected) * 100)

    return {
        "status": "measured",
        "truth_keys": len(rows),
        "present_keys": len(present),
        "matching_value_keys": len(matching),
        "fraction_keys_present": (len(present) / len(rows) if rows else None),
        "median_abs_pct_error": (stdlib_statistics.median(errors) if errors else None),
        "error_pairs": len(errors),
        "skipped_error_keys": skipped,
    }


def _write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _make_png(svg_path: pathlib.Path, png_path: pathlib.Path) -> str | None:
    try:
        completed = subprocess.run(
            ["rsvg-convert", "-w", "1600", "-o", str(png_path), str(svg_path)],
            text=True, capture_output=True, timeout=RSVG_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"rsvg-convert failed: {exc}"
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "no diagnostic").strip()
        return f"rsvg-convert exited {completed.returncode}: {detail[:500]}"
    return None


def _checks_pass(checks: dict) -> bool:
    required = [checks["units"], checks["footnote"]]
    required.extend(checks["contains"])
    required.extend(checks["excludes"])
    required.extend(checks["spots"])
    return all(item.get("status") == "pass" for item in required)


def run_one(spec: dict, destination: pathlib.Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    auto = {
        "id": spec["id"], "prompt": spec["prompt"], "refused": False,
        "error": None, "checks": {}, "passed": False,
    }
    errors: list[str] = []
    try:
        manifest, raw, reasoning = describe(spec["prompt"])
    except AgentRefusal as exc:
        auto["refused"] = True
        auto["error"] = str(exc)
        _write_json(destination / "auto.json", auto)
        return auto
    except Exception as exc:
        auto["error"] = f"{type(exc).__name__}: {exc}"
        _write_json(destination / "auto.json", auto)
        return auto

    _write_json(destination / "manifest.json", raw)
    (destination / "reasoning.txt").write_text(reasoning + "\n", encoding="utf-8")

    try:
        svg = draw(manifest)
        svg_path = destination / "map.svg"
        svg_path.write_text(svg, encoding="utf-8")
        features, values, text = _svg_facts(svg)
        harvested = harvest(manifest)        # cached by now; draw() just ran it
        extent = _extent_for(manifest, harvested)
        geometry = overlay_geometry(harvested) if manifest.overlays else None
        checks = {
            "units": _check_units(spec, features),
            "contains": _point_checks("contains", spec.get("contains", []), extent),
            "excludes": _point_checks("excludes", spec.get("excludes", []), extent),
            "spots": [evaluate_spot(item, values, geometry)
                      for item in spec.get("spot", [])],
            "footnote": _footnote_check(text),
            "truth": _truth_check(REFS / spec["id"] / "truth.csv", features, values),
        }
        auto["checks"] = checks
        if png_error := _make_png(svg_path, destination / "map.png"):
            errors.append(png_error)
        auto["passed"] = not errors and _checks_pass(checks)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    auto["error"] = "; ".join(errors) or None
    _write_json(destination / "auto.json", auto)
    return auto


def _destination(root: pathlib.Path, case_id: str, run: int, repeat: int) -> pathlib.Path:
    base = root / case_id
    return base if repeat == 1 else base / f"run-{run:02d}"


def _default_output() -> pathlib.Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return HERE / "results" / timestamp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", help="map ids from specs.toml (default: all)")
    parser.add_argument("--repeat", type=int, default=1, help="runs per map")
    parser.add_argument("--out", type=pathlib.Path, help="artifact root")
    parser.add_argument("--specs", type=pathlib.Path,
                        help="specs file (default: evals/maps/specs.toml); truth is read "
                             "from <its directory>/<id>/truth.csv")
    args = parser.parse_args()
    if args.specs:
        global SPECS, REFS
        SPECS, REFS = args.specs.resolve(), args.specs.resolve().parent
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    cases = _load_specs(args.ids)
    output = (args.out or _default_output()).resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        backend = backend_name()
    except AgentUnavailable as exc:
        backend = f"unavailable ({exc})"
    print(f"{len(cases)} map(s) x {args.repeat}; backend={backend}; out={output}")

    rows = []
    for spec in cases:
        for run in range(1, args.repeat + 1):
            destination = _destination(output, spec["id"], run, args.repeat)
            result = run_one(spec, destination)
            checks = result.get("checks", {})
            units = checks.get("units", {}).get("actual", "-")
            spots = checks.get("spots", [])
            spot_summary = (f"{sum(s.get('status') == 'pass' for s in spots)}/{len(spots)}"
                            if spots else "-")
            status = "PASS" if result["passed"] else ("REFUSED" if result["refused"] else "FAIL")
            label = spec["id"] if args.repeat == 1 else f"{spec['id']}#{run}"
            rows.append((label, status, units, spot_summary, destination))

    widths = [max(len(str(row[index])) for row in (["map", "status", "units", "spots", "output"], *rows))
              for index in range(5)]
    header = ("map", "status", "units", "spots", "output")
    print()
    print("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)))
    passed = sum(status == "PASS" for _, status, _, _, _ in rows)
    print(f"\n{passed}/{len(rows)} runs passed automatic checks")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
