"""Bounded, service-side inspection of a proposed data URL."""

import csv
from dataclasses import asdict, dataclass
import datetime
import hashlib
import io
import json
import math
import re
import zipfile

from mapsvc.discovery import http
from mapsvc.xlsx import sheets as xlsx_sheets


MAX_ROWS = 50_000
MAX_UNZIPPED = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 40


class ProbeError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeReport:
    url: str
    format: str
    size: int
    sha256: str
    n_rows: int
    columns: list[dict]
    geometry_kind: str | None
    warnings: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def probe(url: str, *, max_bytes: int = 25_000_000,
          timeout: float = 30) -> ProbeReport:
    response = http.get(url, max_bytes=max_bytes, timeout=timeout, expect="data")
    return probe_bytes(response.content, url=response.url)


def probe_bytes(payload: bytes, *, url: str = "") -> ProbeReport:
    original_size = len(payload)
    digest = hashlib.sha256(payload).hexdigest()
    warnings: list[str] = []
    inner_name = ""
    if payload.startswith(b"PK\x03\x04") and not _looks_like_xlsx(payload):
        payload, inner_name = _safe_unzip(payload)
        warnings.append(f"inspected {inner_name} inside zip archive")

    if _looks_like_xlsx(payload):
        format_name = "xlsx"
        rows = _xlsx_records(payload, warnings)
        geometry_types: list[str] = []
    else:
        text, encoding = _decode(payload)
        if encoding != "utf-8":
            warnings.append(f"decoded as {encoding}")
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            rows, geometry_types, format_name = _json_records(text)
        else:
            rows, delimiter, skipped = _delimited_records(text)
            geometry_types = []
            format_name = "tsv" if delimiter == "\t" else "csv"
            if skipped:
                warnings.append(f"skipped {skipped} leading metadata line(s)")

    if len(rows) > MAX_ROWS:
        warnings.append(f"only the first {MAX_ROWS:,} of {len(rows):,} rows were inspected")
        rows = rows[:MAX_ROWS]
    columns = _column_reports(rows)
    geometry_kind = _geometry_kind(geometry_types, columns)
    return ProbeReport(url, format_name, original_size, digest, len(rows), columns,
                       geometry_kind, warnings)


def _looks_like_xlsx(payload: bytes) -> bool:
    if not payload.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
        return "xl/workbook.xml" in names and "[Content_Types].xml" in names
    except (zipfile.BadZipFile, OSError):
        return False


def _safe_unzip(payload: bytes) -> tuple[bytes, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [member for member in archive.infolist()
                       if not member.is_dir()
                       and not member.filename.startswith(("__MACOSX/", "."))
                       and member.filename.lower().endswith(
                           (".csv", ".tsv", ".txt", ".json", ".geojson", ".xlsx"))]
            if len(members) != 1:
                raise ProbeError(
                    f"zip contains {len(members)} supported data files; expected one")
            member = members[0]
            if member.flag_bits & 0x1:
                raise ProbeError("zip data file is encrypted")
            if (member.file_size > MAX_UNZIPPED
                    or member.file_size > MAX_COMPRESSION_RATIO * max(1, member.compress_size)):
                raise ProbeError("zip data file exceeds the size or compression-ratio limit")
            with archive.open(member) as handle:
                content = handle.read(MAX_UNZIPPED + 1)
    except ProbeError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError,
            NotImplementedError) as exc:
        raise ProbeError(f"unreadable zip: {exc}") from exc
    if len(content) > MAX_UNZIPPED:
        raise ProbeError("zip expands beyond the size limit")
    return content, member.filename


def _decode(payload: bytes) -> tuple[str, str]:
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig"), "utf-8-sig"
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return payload.decode("latin-1"), "latin-1"


def _delimited_records(text: str) -> tuple[list[dict], str, int]:
    lines = text.splitlines()
    if not lines:
        return [], ",", 0
    best = None
    for index in range(min(25, len(lines))):
        sample = "\n".join(lines[index:index + 12])
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            parsed = list(csv.reader(lines[index:index + 5], dialect))
        except csv.Error:
            continue
        widths = [len(row) for row in parsed if row]
        if len(widths) >= 2 and widths[0] >= 2:
            consistent = sum(width == widths[0] for width in widths)
            candidate = (consistent, widths[0], -index, dialect.delimiter, index)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        raise ProbeError("could not find a delimited header row")
    _, _, _, delimiter, skipped = best
    reader = csv.DictReader(lines[skipped:], delimiter=delimiter)
    if not reader.fieldnames or len(reader.fieldnames) < 2:
        raise ProbeError("delimited file has no usable header")
    headers = _unique_headers(reader.fieldnames)
    records = []
    for cells in reader:
        values = list(cells.values())
        records.append({header: (values[index] if index < len(values) else None)
                        for index, header in enumerate(headers)})
    return records, delimiter, skipped


def _unique_headers(headers) -> list[str]:
    result = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(headers):
        base = str(raw or "").strip() or f"column_{index + 1}"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result


def _xlsx_records(payload: bytes, warnings: list[str]) -> list[dict]:
    try:
        books = xlsx_sheets(payload)
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ProbeError(f"unreadable xlsx: {exc}") from exc
    populated = [(name, rows) for name, rows in books.items()
                 if any(any(str(value).strip() for value in row.values()) for row in rows)]
    if not populated:
        return []
    name, rows = populated[0]
    if len(populated) > 1:
        warnings.append(f"inspected first populated worksheet {name!r} of {len(populated)}")
    header_index = next(index for index, row in enumerate(rows)
                        if sum(bool(str(value).strip()) for value in row.values()) >= 2)
    header_row = rows[header_index]
    letters = sorted(header_row, key=_column_number)
    headers = _unique_headers([header_row.get(letter) for letter in letters])
    records = []
    for raw in rows[header_index + 1:]:
        if any(str(raw.get(letter, "")).strip() for letter in letters):
            records.append({headers[index]: raw.get(letter, "")
                            for index, letter in enumerate(letters)})
    if header_index:
        warnings.append(f"skipped {header_index} leading spreadsheet row(s)")
    return records


def _column_number(letter: str) -> int:
    result = 0
    for char in letter.upper():
        if "A" <= char <= "Z":
            result = result * 26 + ord(char) - ord("A") + 1
    return result


def _json_records(text: str) -> tuple[list[dict], list[str], str]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"invalid JSON: {exc}") from exc
    if isinstance(value, dict) and value.get("type") == "FeatureCollection":
        rows = []
        geometries = []
        for feature in value.get("features") or []:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties") or {}
            row = dict(props) if isinstance(props, dict) else {}
            geometry = feature.get("geometry") or {}
            kind = str(geometry.get("type") or "")
            if kind:
                geometries.append(kind)
            if kind == "Point" and len(geometry.get("coordinates") or []) >= 2:
                row.setdefault("__longitude", geometry["coordinates"][0])
                row.setdefault("__latitude", geometry["coordinates"][1])
            rows.append(row)
        return rows, geometries, "geojson"
    records = value.get("data") if isinstance(value, dict) else value
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ProbeError("JSON must be an array of records or an object with a data array")
    return [dict(row) for row in records], [], "json"


def _column_reports(rows: list[dict]) -> list[dict]:
    names = []
    seen = set()
    for row in rows:
        for name in row:
            if name not in seen:
                names.append(str(name))
                seen.add(name)
    reports = []
    for name in names:
        values = [row.get(name) for row in rows]
        present = [value for value in values if value is not None and str(value).strip() != ""]
        sample = []
        sample_seen = set()
        for value in present:
            marker = json.dumps(value, sort_keys=True, default=str)
            if marker not in sample_seen:
                sample.append(value)
                sample_seen.add(marker)
            if len(sample) == 5:
                break
        reports.append({"name": name, "n_nonnull": len(present), "sample": sample,
                        "inferred": _infer(name, present)})
    return reports


def _normal_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def _ratio(values, predicate) -> float:
    if not values:
        return 0.0
    return sum(bool(predicate(str(value).strip())) for value in values) / len(values)


def _numeric(value: str) -> bool:
    try:
        number = float(value.replace(",", ""))
        return math.isfinite(number)
    except (ValueError, TypeError):
        return False


def _infer(name: str, values: list) -> list[dict]:
    normal = _normal_name(name)
    inferred: dict[str, float] = {}

    def add(role: str, confidence: float):
        inferred[role] = max(inferred.get(role, 0.0), min(1.0, confidence))

    aliases = {
        "lon": {"lon", "long", "longitude", "lng", "x", "__longitude"},
        "lat": {"lat", "latitude", "y", "__latitude"},
        "iso3": {"iso3", "iso_a3", "country_code_3", "alpha_3", "adm0_a3"},
        "iso2": {"iso2", "iso_a2", "country_code_2", "alpha_2"},
        "iso3166_2": {"iso3166_2", "iso_3166_2", "region_code", "subdivision_code"},
        "nuts": {"nuts", "nuts_id", "nuts_code"},
        "fips": {"fips", "geoid", "statefp", "countyfp"},
        "country/region name": {"country", "country_name", "region", "region_name", "name"},
        "year/date": {"year", "date", "period", "time", "vintage"},
        "unit-like": {"unit", "units", "measure", "uom", "scale"},
    }
    for role, names in aliases.items():
        if normal in names:
            add(role, 0.98)
    if normal in {"value", "values", "amount", "count", "total", "rate", "score",
                  "population", "gdp", "estimate", "number"}:
        add("numeric value", 0.95)

    upper = [str(value).strip().upper() for value in values]
    for role, expression in (
        ("iso3", r"[A-Z]{3}"), ("iso2", r"[A-Z]{2}"),
        ("iso3166_2", r"[A-Z]{2}-[A-Z0-9]{1,3}"),
        ("nuts", r"[A-Z]{2}[A-Z0-9]{1,3}"), ("fips", r"\d{2}|\d{5}")):
        ratio = _ratio(upper, lambda value, e=expression: re.fullmatch(e, value) is not None)
        if ratio >= 0.8:
            add(role, 0.75 + 0.2 * ratio)
    numeric_ratio = _ratio(values, _numeric)
    if numeric_ratio >= 0.8:
        if normal not in aliases["year/date"] and normal not in aliases["fips"]:
            add("numeric value", 0.65 + 0.25 * numeric_ratio)
    date_ratio = _ratio(values, lambda value: bool(
        re.fullmatch(r"(?:18|19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?", value)))
    if date_ratio >= 0.8:
        add("year/date", 0.7 + 0.25 * date_ratio)
    return [{"role": role, "confidence": round(confidence, 3)}
            for role, confidence in sorted(inferred.items(), key=lambda item: (-item[1], item[0]))]


def _geometry_kind(types: list[str], columns: list[dict]) -> str | None:
    if any(kind in {"Polygon", "MultiPolygon"} for kind in types):
        return "choropleth"
    if any(kind in {"LineString", "MultiLineString"} for kind in types):
        return "lines"
    if any(kind in {"Point", "MultiPoint"} for kind in types):
        return "points"
    roles = {item["role"] for column in columns for item in column["inferred"]}
    if {"lon", "lat"} <= roles:
        return "points"
    if roles & {"iso3", "iso3166_2", "nuts", "fips", "country/region name"}:
        return "choropleth"
    return None


def coverage(values, key_namespace: str, level: str, scope=None) -> dict:
    """Measure proposed join values against keys from a real boundary loader."""
    namespace = key_namespace
    if namespace not in {"ISO3", "ISO3166_2", "FIPS", "NUTS", "name"}:
        raise ValueError(f"coverage does not support key namespace {namespace!r}")
    bbox, within, detail, source = _scope(scope)
    if namespace in {"ISO3", "ISO3166_2", "name"}:
        from mapsvc.cartography import natural_earth as loader
        source = source or "natural_earth"
        boundary_level = "admin_0" if namespace in {"ISO3", "name"} else "admin_1"
    elif namespace == "NUTS":
        from mapsvc.cartography import gisco as loader
        source = source or "gisco"
        boundary_level = level
    else:
        from mapsvc.cartography import census as loader
        source = source or "census"
        boundary_level = level or "admin_2"
    boundaries = loader.load(boundary_level, bbox, detail, within)
    if namespace == "name":
        aliases = _name_aliases(boundaries.units)
        proposed = {_normalise_place(value) for value in values if str(value).strip()}
        matched = len({aliases[value] for value in proposed if value in aliases})
    else:
        expected_keys = {str(unit.get("keys", {}).get(namespace)).strip().upper()
                         for unit in boundaries.units
                         if unit.get("keys", {}).get(namespace) not in (None, "")}
        proposed = {str(value).strip().upper() for value in values
                    if value is not None and str(value).strip()}
        matched = len(expected_keys & proposed)
    expected = len(boundaries.units)
    return {"matched": matched, "expected": expected,
            "ratio": matched / expected if expected else 0.0, "scope": scope}


def _scope(scope):
    if isinstance(scope, dict):
        return scope.get("bbox"), scope.get("within"), scope.get("detail", "simplified"), scope.get("source")
    if isinstance(scope, str):
        return None, [scope], "simplified", None
    if isinstance(scope, (list, tuple)):
        if len(scope) == 4 and all(isinstance(value, (int, float)) for value in scope):
            return tuple(scope), None, "simplified", None
        return None, list(scope), "simplified", None
    return None, None, "simplified", None


def _normalise_place(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _name_aliases(units: list[dict]) -> dict[str, str]:
    aliases = {}
    variants = ("NAME", "NAME_LONG", "FORMAL_EN", "NAME_EN", "name", "name_long",
                "formal_en", "name_en")
    for unit in units:
        code = unit.get("keys", {}).get("ISO3") or unit.get("id")
        names = [unit.get("name")]
        props = unit.get("properties") or {}
        names.extend(props.get(field) for field in variants)
        for name in names:
            if name:
                aliases[_normalise_place(name)] = str(code)
    return aliases
