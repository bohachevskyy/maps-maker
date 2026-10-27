"""Run the reference-map evaluations and keep every artifact.

    uv run python -m evals.maps.run [ids...] [--repeat N] [--out DIR]
    uv run python -m evals.maps.run [ids...] --recheck DIR

This is a live runner, not part of pytest.  Its parsing and automatic checks are
kept as pure functions so their unit tests remain offline.
"""

import argparse
import collections
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
from mapsvc.manifest import validate
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
_YEAR = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
_YEAR_RANGE = re.compile(
    r"(?<!\d)((?:18|19|20)\d{2})\s*[-\N{EN DASH}\N{RIGHTWARDS ARROW}]\s*"
    r"((?:18|19|20)\d{2})(?!\d)"
)
_EXCLUDED = re.compile(
    r"(?:\b([\d,]+)\s+(?:of\s+[\d,]+\s+)?features?\s+excluded\b|"
    r"\bexcluded\s*:\s*([\d,]+)\b)", re.IGNORECASE,
)


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


def _svg_root(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _all_rendered_ids(root: ET.Element) -> set[str]:
    return {element.get("data-id") for element in root.iter()
            if element.get("data-id") is not None}


def _must_include_check(spec: dict, root: ET.Element) -> dict:
    required = spec.get("must_include", [])
    if not required:
        return {"status": "pass", "required": [],
                "reason": "the spec has no must_include entries"}
    ids = _all_rendered_ids(root)
    # Overlay renderers put feature names in labels rather than on the point or
    # line itself. Keep individual text nodes so a requirement cannot be made
    # by joining unrelated words from the title and footnote.
    labels = [" ".join("".join(node.itertext()).split())
              for node in root.iter(f"{SVG_NS}text")
              if node.get("fill", "").lower() != "#595959"]
    found, missing = [], []
    for item in required:
        present = item in ids or any(
            re.search(rf"(?<!\w){re.escape(item)}(?!\w)", label)
            for label in labels
        )
        (found if present else missing).append(item)
    result = {
        "status": "pass" if not missing else "fail",
        "required": required, "found": found, "missing": missing,
    }
    if missing:
        result["reason"] = "required rendered IDs or overlay feature names are absent"
    else:
        result["reason"] = "all required rendered IDs or overlay feature names are present"
    return result


def _read_truth(path: pathlib.Path) -> tuple[list[dict[str, str]] | None,
                                             dict[str, str] | None, str | None]:
    if not path.exists():
        return None, None, "truth.csv is absent"
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            rows = list(reader)
    except (OSError, csv.Error) as exc:
        return None, None, f"could not read truth.csv: {exc}"
    return rows, {name.lower(): name for name in fields}, None


def _terminal_years(text: str) -> list[int]:
    """Return vintage endpoints, treating 2000-2023 as one 2023 vintage."""
    ranges = list(_YEAR_RANGE.finditer(text))
    covered = [range(match.start(), match.end()) for match in ranges]
    years = [int(match.group(2)) for match in ranges]
    for match in _YEAR.finditer(text):
        if not any(match.start() in span for span in covered):
            years.append(int(match.group(1)))
    return years


def _vintage_check(spec: dict, root: ET.Element, truth_path: pathlib.Path) -> dict:
    rows, fields, error = _read_truth(truth_path)
    if error:
        return {"status": "unevaluable", "reason": error}
    year_field = fields.get("year") if fields else None
    if year_field is None:
        return {"status": "unevaluable", "reason": "truth.csv has no year column"}
    truth_years = [year for row in rows for year in _terminal_years(row.get(year_field, ""))]
    if not truth_years:
        return {"status": "unevaluable", "reason": "truth.csv's year column has no parseable years"}

    provenance = " ".join("".join(node.itertext())
                          for node in root.iter(f"{SVG_NS}text"))
    variable = re.search(r"\bVariable:(.*?)(?:\bProjection:|$)", provenance, re.DOTALL)
    overlay = re.search(r"\bOverlay:(.*?)(?:\bProjection:|$)", provenance, re.DOTALL)
    stated = " ".join(filter(None, [
        root.get("aria-label", ""),
        " ".join("".join(node.itertext()) for node in root.iter(f"{SVG_NS}title")),
        variable.group(1).split("[")[0] if variable else "",
        overlay.group(1).split("[")[0] if overlay else "",
        " ".join(value for element in root.iter() for name, value in element.attrib.items()
                 if name in ("data-year", "data-vintage")),
    ]))
    map_years = _terminal_years(stated)
    if not map_years:
        return {"status": "unevaluable", "reason": "the SVG states no parseable data year"}
    # Titles also contain base-price years ("constant 2021 dollars") and
    # classification reference years ("EU27 from 2020"). When the SVG states
    # years that actually occur in truth's vintage column, those are the data
    # vintages; retain unmatched years only when every stated year is stale.
    matching_years = [year for year in map_years if year in set(truth_years)]
    if matching_years:
        map_years = matching_years

    tolerance = spec.get("vintage_tolerance", 2)
    max_span = spec.get("vintage_max_span", 3)
    age = max(truth_years) - max(map_years)
    span = max(map_years) - min(map_years)
    reasons = []
    if age > tolerance:
        reasons.append(f"map vintage is {age} years older than truth (tolerance {tolerance})")
    if span > max_span:
        reasons.append(f"map mixes stated vintages across {span} years (maximum {max_span})")
    result = {
        "status": "fail" if reasons else "pass",
        "map_years": sorted(set(map_years)),
        "truth_years": sorted(set(truth_years)),
        "age_years": age, "tolerance_years": tolerance,
        "map_span_years": span, "max_span_years": max_span,
    }
    if reasons:
        result["reason"] = "; ".join(reasons)
    else:
        result["reason"] = "stated vintage is within the age and mixed-year tolerances"
    return result


def _effective_fill(element: ET.Element, parents: dict[ET.Element, ET.Element]) -> str | None:
    current = element
    while current is not None:
        if current.get("fill") is not None:
            return current.get("fill")
        current = parents.get(current)
    return None


def _class_balance_check(root: ET.Element) -> dict:
    parents = {child: parent for parent in root.iter() for child in parent}
    valued: dict[str, tuple[str, str]] = {}
    for element in root.iter():
        key, value = element.get("data-id"), element.get("data-value")
        fill = _effective_fill(element, parents)
        if key is not None and value is not None and fill and not fill.startswith("url("):
            valued.setdefault(key, (value, fill))
    if not valued:
        return {"status": "unevaluable", "severity": "warning",
                "reason": "the SVG has no valued units"}
    legend_fills = {element.get("fill") for element in root.iter(f"{SVG_NS}rect")
                    if element.get("fill")}
    classes = collections.Counter(fill for _, fill in valued.values() if fill in legend_fills)
    if len(classes) < 2:
        return {"status": "unevaluable", "severity": "warning",
                "reason": "fewer than two legend classes could be matched to valued units"}
    total = sum(classes.values())
    dominant_fill, dominant_count = classes.most_common(1)[0]
    share = dominant_count / total
    dominant_values = collections.Counter(
        value for value, fill in valued.values() if fill == dominant_fill
    )
    tied = max(dominant_values.values()) / len(valued) > 0.35
    failed = share > 0.35 and not tied
    result = {
        "status": "fail" if failed else "pass", "severity": "warning",
        "valued_units": total, "class_counts": dict(sorted(classes.items())),
        "largest_class_fill": dominant_fill,
        "largest_class_fraction": share, "maximum_fraction": 0.35,
        "tied_values_exception": tied,
    }
    if failed:
        result["reason"] = f"one legend class contains {dominant_count}/{total} valued units"
    elif tied and share > 0.35:
        result["reason"] = "the largest class exceeds 35% only because values are tied"
    else:
        result["reason"] = "no legend class contains more than 35% of valued units"
    return result


def _hatched_ids(root: ET.Element, parents: dict[ET.Element, ET.Element]) -> set[str]:
    result = set()
    for element in root.iter():
        key = element.get("data-id")
        fill = _effective_fill(element, parents)
        if key is not None and fill and ("hatch" in fill.lower() or "nodata" in fill.lower()):
            result.add(key)
    return result


def _footnote_consistency_check(root: ET.Element, truth_path: pathlib.Path,
                                scope: frozenset = frozenset()) -> dict:
    rows, fields, error = _read_truth(truth_path)
    if error:
        return {"status": "unevaluable", "reason": error}
    fields = fields or {}
    key_field = next((fields[name] for name in ("key", "id", "data-id") if name in fields), None)
    if key_field is None:
        return {"status": "unevaluable", "reason": "truth.csv has no key/id/data-id column"}
    truth_keys = {row.get(key_field, "").strip() for row in rows if row.get(key_field, "").strip()}
    # Reference tables occasionally carry one aggregate alongside hundreds of
    # geographic rows (EU27_2020 beside four-character NUTS 2 codes). It is a
    # comparison value, not a unit the SVG could draw. Ignore non-modal key
    # lengths only when the table's unit-key shape is overwhelmingly uniform.
    lengths = collections.Counter(map(len, truth_keys))
    ignored_truth_keys: set[str] = set()
    if truth_keys and lengths:
        modal_length, modal_count = lengths.most_common(1)[0]
        if modal_count / len(truth_keys) >= 0.9:
            ignored_truth_keys = {key for key in truth_keys if len(key) != modal_length}
            truth_keys -= ignored_truth_keys
    rendered = _all_rendered_ids(root)
    missing = {key for key in truth_keys if _resolve_key(key, rendered) is None}
    # A `within` list scopes the map: a truth row outside it (Russia on a
    # Europe map whose list leaves Russia out) is not an omission. Only applied
    # when truth keys share the scope's namespace (ISO3 within, ISO3 keys).
    if scope and truth_keys & scope:
        missing &= scope
    parents = {child: parent for parent in root.iter() for child in parent}
    hatched = _hatched_ids(root, parents)
    observed = len(missing) + len(hatched)
    text = " ".join("".join(node.itertext()) for node in root.iter(f"{SVG_NS}text"))
    claims = [int((left or right).replace(",", ""))
              for left, right in _EXCLUDED.findall(text)]
    if not claims:
        return {"status": "unevaluable", "reason": "the SVG footnote has no parseable exclusion count",
                "observed_excluded": observed}
    claimed = claims[-1]
    result = {
        "status": "fail" if claimed < observed else "pass",
        "claimed_excluded": claimed, "observed_excluded": observed,
        "absent_truth_keys": sorted(missing), "hatched_ids": sorted(hatched),
        "ignored_non_unit_truth_keys": sorted(ignored_truth_keys),
    }
    if claimed < observed:
        result["reason"] = f"footnote claims {claimed} excluded but at least {observed} are absent or hatched"
    else:
        result["reason"] = "footnote exclusion claim covers all absent truth keys and hatched units"
    return result


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
    if not all(item.get("status") == "pass" for item in required):
        return False
    # These checks are not applicable to every map kind (for example a route
    # overlay has neither valued polygon classes nor a keyed truth vintage).
    # Unevaluable is recorded with a reason; an observed defect still gates.
    added = (checks["must_include"], checks["vintage"],
             checks["class_balance"], checks["footnote_consistency"])
    return all(item.get("status") != "fail" for item in added)


def _added_checks(spec: dict, root: ET.Element, truth_path: pathlib.Path,
                  scope: frozenset = frozenset()) -> dict:
    return {
        "must_include": _must_include_check(spec, root),
        "vintage": _vintage_check(spec, root, truth_path),
        "class_balance": _class_balance_check(root),
        "footnote_consistency": _footnote_consistency_check(root, truth_path, scope),
    }


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
        root = _svg_root(svg)
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
        checks.update(_added_checks(spec, root, REFS / spec["id"] / "truth.csv",
                                    frozenset(manifest.within or ())))
        auto["checks"] = checks
        if png_error := _make_png(svg_path, destination / "map.png"):
            errors.append(png_error)
        auto["passed"] = not errors and _checks_pass(checks)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    auto["error"] = "; ".join(errors) or None
    _write_json(destination / "auto.json", auto)
    return auto


def _previous_extent(previous: dict, kind: str):
    for item in previous.get("checks", {}).get(kind, []):
        if item.get("extent") is not None:
            return tuple(item["extent"])
    return None


def recheck_one(spec: dict, destination: pathlib.Path) -> dict:
    """Re-run artifact-only checks without describing, drawing, or harvesting."""
    auto_path = destination / "auto.json"
    try:
        previous = json.loads(auto_path.read_text(encoding="utf-8")) if auto_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        previous = {}
    auto = {
        "id": spec["id"], "prompt": spec["prompt"], "refused": False,
        "error": None, "checks": {}, "passed": False, "rechecked": True,
    }
    errors = []
    try:
        svg = (destination / "map.svg").read_text(encoding="utf-8")
        raw = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        manifest = validate(raw)
        features, values, text = _svg_facts(svg)
        root = _svg_root(svg)
        if manifest.bbox is not None and not manifest.within and not manifest.overlays:
            extent = manifest.bbox
        else:
            extent = (_previous_extent(previous, "contains")
                      or _previous_extent(previous, "excludes"))
        previous_spots = {
            item.get("expression"): item
            for item in previous.get("checks", {}).get("spots", [])
        }
        spots = []
        for expression in spec.get("spot", []):
            try:
                is_near = parse_spot(expression)["kind"] == "near"
            except ValueError:
                is_near = False
            if is_near and expression in previous_spots:
                spots.append(previous_spots[expression])
            else:
                spots.append(evaluate_spot(expression, values))
        truth_path = REFS / spec["id"] / "truth.csv"
        checks = {
            "units": _check_units(spec, features),
            "contains": _point_checks("contains", spec.get("contains", []), extent),
            "excludes": _point_checks("excludes", spec.get("excludes", []), extent),
            "spots": spots,
            "footnote": _footnote_check(text),
            "truth": _truth_check(truth_path, features, values),
        }
        checks.update(_added_checks(spec, root, truth_path,
                                    frozenset(getattr(manifest, "within", None) or ())))
        auto["checks"] = checks
        auto["passed"] = _checks_pass(checks)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    auto["error"] = "; ".join(errors) or None
    _write_json(auto_path, auto)
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
    parser.add_argument("--recheck", type=pathlib.Path,
                        help="re-run checks on existing map.svg/manifest.json artifacts; "
                             "never calls the agent or redraws maps")
    parser.add_argument("--specs", type=pathlib.Path,
                        help="specs file (default: evals/maps/specs.toml); truth is read "
                             "from <its directory>/<id>/truth.csv")
    args = parser.parse_args()
    if args.specs:
        global SPECS, REFS
        SPECS, REFS = args.specs.resolve(), args.specs.resolve().parent
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.recheck and (args.repeat != 1 or args.out):
        parser.error("--recheck cannot be combined with --repeat or --out")

    cases = _load_specs(args.ids)
    if args.recheck:
        artifact_root = args.recheck.resolve()
        jobs = []
        for spec in cases:
            case_root = artifact_root / spec["id"]
            jobs.extend((spec, svg.parent) for svg in sorted(case_root.rglob("map.svg")))
        if not jobs:
            parser.error(f"no map.svg artifacts found under {artifact_root}")
        print(f"{len(jobs)} existing artifact(s); agent=disabled; recheck={artifact_root}")
        rows = []
        for spec, destination in jobs:
            result = recheck_one(spec, destination)
            checks = result.get("checks", {})
            units = checks.get("units", {}).get("actual", "-")
            spots = checks.get("spots", [])
            spot_summary = (f"{sum(s.get('status') == 'pass' for s in spots)}/{len(spots)}"
                            if spots else "-")
            status = "PASS" if result["passed"] else "FAIL"
            label = str(destination.relative_to(artifact_root))
            rows.append((label, status, units, spot_summary, destination))
        return _print_summary(rows)

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

    return _print_summary(rows)


def _print_summary(rows: list[tuple]) -> int:
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
