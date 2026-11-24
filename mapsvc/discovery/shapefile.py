"""Small, dependency-free reader for zipped ESRI shapefiles.

The public entry point is deliberately bytes-in/dict-out.  Archives are never
extracted, and projected coordinates are rejected rather than guessed at or
silently drawn in the wrong place.
"""

from __future__ import annotations

import codecs
import io
import math
import re
import struct
import zipfile


MAX_UNZIPPED = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 40
MAX_FEATURES = 500_000

_POINT = {1, 11, 21}
_POLYLINE = {3, 13, 23}
_POLYGON = {5, 15, 25}
_MULTIPOINT = {8, 18, 28}
_SUPPORTED = {0} | _POINT | _POLYLINE | _POLYGON | _MULTIPOINT


class ShapefileError(ValueError):
    pass


def to_geojson(zip_bytes: bytes) -> dict:
    """Convert the largest shapefile in *zip_bytes* to a FeatureCollection."""
    geojson, _name, _crs = _read_zip(zip_bytes)
    return geojson


def _read_zip(zip_bytes: bytes) -> tuple[dict, str, str]:
    """Conversion plus the archive name and CRS label used by callers' logs."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            members = [m for m in archive.infolist() if _usable(m)]
            shapes = [m for m in members if m.filename.lower().endswith(".shp")]
            if not shapes:
                raise ShapefileError("zip contains no .shp file")
            shp = sorted(shapes, key=lambda m: (-m.file_size, m.filename))[0]
            stem = shp.filename[:-4].lower()
            dbf = _companion(members, stem, ".dbf")
            if dbf is None:
                raise ShapefileError(f"shapefile {shp.filename} has no matching .dbf")
            selected = (shp, dbf)
            if any(m.flag_bits & 0x1 for m in selected):
                raise ShapefileError("shapefile archive members are encrypted")
            size = sum(m.file_size for m in selected)
            compressed = sum(m.compress_size for m in selected)
            if (size > MAX_UNZIPPED
                    or size > MAX_COMPRESSION_RATIO * max(1, compressed)):
                raise ShapefileError(
                    "shapefile exceeds the size or compression-ratio limit")
            shp_bytes = _read_member(archive, shp, MAX_UNZIPPED)
            dbf_bytes = _read_member(archive, dbf, MAX_UNZIPPED - len(shp_bytes))
            prj_member = _companion(members, stem, ".prj")
            cpg_member = _companion(members, stem, ".cpg")
            prj = archive.read(prj_member) if prj_member is not None else None
            cpg = archive.read(cpg_member) if cpg_member is not None else None
    except ShapefileError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError,
            NotImplementedError, KeyError) as exc:
        raise ShapefileError(f"unreadable shapefile zip: {exc}") from exc

    properties = _dbf(dbf_bytes, _encoding(cpg))
    geometries, bounds = _shp(shp_bytes)
    if len(properties) != len(geometries):
        raise ShapefileError(
            f".dbf has {len(properties)} records but .shp has {len(geometries)} shapes")
    if len(geometries) > MAX_FEATURES:
        raise ShapefileError(f"shapefile has more than {MAX_FEATURES:,} features")
    transform, label = _projection(prj)
    if transform is not None:
        geometries = [_reproject(g, transform) for g in geometries]
        crs = f"{label}, reprojected to longitude/latitude"
    else:
        crs = _crs(prj, bounds)
    features = [
        {"type": "Feature", "properties": props, "geometry": geometry}
        for props, geometry in zip(properties, geometries)
        if props is not None
    ]
    return {"type": "FeatureCollection", "features": features}, shp.filename, crs


def _usable(member: zipfile.ZipInfo) -> bool:
    if member.is_dir():
        return False
    parts = member.filename.replace("\\", "/").split("/")
    return not any(part == "__MACOSX" or part.startswith(".") for part in parts)


def _companion(members: list[zipfile.ZipInfo], stem: str,
               extension: str) -> zipfile.ZipInfo | None:
    wanted = stem + extension
    return next((member for member in members
                 if member.filename.lower() == wanted), None)


def _read_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo,
                 remaining: int) -> bytes:
    if remaining < 0:
        raise ShapefileError("shapefile expands beyond the size limit")
    with archive.open(member) as handle:
        data = handle.read(remaining + 1)
    if len(data) > remaining:
        raise ShapefileError("shapefile expands beyond the size limit")
    return data


def _encoding(cpg: bytes | None) -> str | None:
    if cpg is None:
        return None
    declared = cpg.decode("ascii", "replace").strip().strip("\ufeff")
    aliases = {"65001": "utf-8", "1252": "cp1252", "28591": "latin-1"}
    declared = aliases.get(declared, declared)
    try:
        codecs.lookup(declared)
    except LookupError as exc:
        raise ShapefileError(f"unknown .cpg encoding {declared!r}") from exc
    return declared


def _dbf(data: bytes, encoding: str | None) -> list[dict | None]:
    if len(data) < 33:
        raise ShapefileError("truncated .dbf header")
    count, header_length, record_length = struct.unpack_from("<IHH", data, 4)
    if count > MAX_FEATURES:
        raise ShapefileError(f"shapefile has more than {MAX_FEATURES:,} features")
    if header_length < 33 or record_length < 1 or header_length > len(data):
        raise ShapefileError("invalid .dbf header lengths")
    fields: list[tuple[str, str, int]] = []
    offset = 32
    while offset < header_length:
        if data[offset] == 0x0D:
            break
        if offset + 32 > header_length:
            raise ShapefileError("truncated .dbf field descriptor")
        name = data[offset:offset + 11].split(b"\0", 1)[0].decode(
            "ascii", "replace").strip()
        kind = chr(data[offset + 11])
        length = data[offset + 16]
        fields.append((name, kind, length))
        offset += 32
    else:
        raise ShapefileError(".dbf field descriptors have no terminator")
    if 1 + sum(length for _, _, length in fields) > record_length:
        raise ShapefileError(".dbf fields exceed its record length")
    if header_length + count * record_length > len(data):
        raise ShapefileError("truncated .dbf records")

    records: list[dict | None] = []
    for index in range(count):
        start = header_length + index * record_length
        record = data[start:start + record_length]
        if record[:1] == b"*":
            records.append(None)
            continue
        values: dict = {}
        cursor = 1
        for name, kind, length in fields:
            raw = record[cursor:cursor + length]
            cursor += length
            values[name] = _dbf_value(raw, kind, encoding)
        records.append(values)
    return records


def _text(raw: bytes, encoding: str | None) -> str:
    if encoding is not None:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ShapefileError(f".dbf text is not valid {encoding}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _dbf_value(raw: bytes, kind: str, encoding: str | None):
    value = _text(raw, encoding).strip()
    if kind == "C":
        return value
    if kind in {"N", "F"}:
        if not value:
            return None
        try:
            number = int(value) if re.fullmatch(r"[+-]?\d+", value) else float(value)
            return number if isinstance(number, int) or math.isfinite(number) else None
        except ValueError:
            return None
    if kind == "D":
        if not value:
            return None
        if re.fullmatch(r"\d{8}", value):
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return value
    if kind == "L":
        logical = value.upper()
        if logical in {"T", "Y"}:
            return True
        if logical in {"F", "N"}:
            return False
        return None
    return value


def _shp(data: bytes) -> tuple[list[dict | None], tuple[float, float, float, float] | None]:
    if len(data) < 100 or struct.unpack_from(">i", data, 0)[0] != 9994:
        raise ShapefileError("not a shapefile: bad or truncated header")
    declared_length = struct.unpack_from(">i", data, 24)[0] * 2
    if declared_length < 100 or declared_length > len(data):
        raise ShapefileError("truncated shapefile")
    geometries: list[dict | None] = []
    all_points: list[tuple[float, float]] = []
    position = 100
    while position < declared_length:
        if position + 8 > declared_length:
            raise ShapefileError("truncated shapefile record header")
        _number, words = struct.unpack_from(">ii", data, position)
        end = position + 8 + words * 2
        if words < 2 or end > declared_length:
            raise ShapefileError("truncated shapefile record")
        content = data[position + 8:end]
        kind = struct.unpack_from("<i", content, 0)[0]
        if kind not in _SUPPORTED:
            raise ShapefileError(f"shape type {kind} is not supported")
        geometry, points = _shape(content, kind)
        geometries.append(geometry)
        all_points.extend(points)
        if len(geometries) > MAX_FEATURES:
            raise ShapefileError(f"shapefile has more than {MAX_FEATURES:,} features")
        position = end
    bounds = None
    if all_points:
        xs = [p[0] for p in all_points]
        ys = [p[1] for p in all_points]
        bounds = min(xs), min(ys), max(xs), max(ys)
    return geometries, bounds


def _shape(content: bytes, kind: int) -> tuple[dict | None, list[tuple[float, float]]]:
    if kind == 0:
        return None, []
    if kind in _POINT:
        if len(content) < 20:
            raise ShapefileError("truncated Point record")
        point = _coordinate(*struct.unpack_from("<dd", content, 4))
        return {"type": "Point", "coordinates": list(point)}, [point]
    if kind in _MULTIPOINT:
        if len(content) < 40:
            raise ShapefileError("truncated MultiPoint record")
        count = struct.unpack_from("<i", content, 36)[0]
        points = _points(content, 40, count)
        return {"type": "MultiPoint", "coordinates": [list(p) for p in points]}, points

    if len(content) < 44:
        raise ShapefileError("truncated multipart shape record")
    part_count, point_count = struct.unpack_from("<ii", content, 36)
    if part_count < 0 or point_count < 0 or part_count > point_count:
        raise ShapefileError("invalid shapefile part counts")
    parts_end = 44 + part_count * 4
    if parts_end > len(content):
        raise ShapefileError("truncated shapefile parts")
    starts = list(struct.unpack_from(f"<{part_count}i", content, 44)) if part_count else []
    if starts and (starts[0] != 0 or any(a >= b for a, b in zip(starts, starts[1:]))
                   or starts[-1] >= point_count):
        raise ShapefileError("invalid shapefile part indexes")
    points = _points(content, parts_end, point_count)
    ends = starts[1:] + [point_count]
    pieces = [[list(p) for p in points[start:end]] for start, end in zip(starts, ends)]
    if kind in _POLYLINE:
        geometry = ({"type": "LineString", "coordinates": pieces[0]}
                    if len(pieces) == 1 else
                    {"type": "MultiLineString", "coordinates": pieces})
    else:
        geometry = _polygon(pieces)
    return geometry, points


def _points(content: bytes, offset: int, count: int) -> list[tuple[float, float]]:
    if count < 0 or offset + count * 16 > len(content):
        raise ShapefileError("truncated shapefile points")
    return [_coordinate(*struct.unpack_from("<dd", content, offset + i * 16))
            for i in range(count)]


def _coordinate(x: float, y: float) -> tuple[float, float]:
    if not math.isfinite(x) or not math.isfinite(y):
        raise ShapefileError("shapefile contains non-finite coordinates")
    rounded = (round(x, 6), round(y, 6))
    return tuple(0.0 if value == 0 else value for value in rounded)


def _signed_area(ring: list[list[float]]) -> float:
    return sum(x0 * y1 - x1 * y0
               for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1])) / 2


def _polygon(rings: list[list[list[float]]]) -> dict:
    polygons: list[list[list[list[float]]]] = []
    for ring in rings:
        if _signed_area(ring) < 0 or not polygons:
            polygons.append([ring])
        else:
            polygons[-1].append(ring)
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _projection(prj: bytes | None):
    """(inverse, name) for a projected .prj we can undo; (None, None) if geographic."""
    if prj is None:
        return None, None
    text = prj.decode("utf-8-sig", "replace").strip()
    if not re.search(r"\bPROJCS\s*\[", text, re.I):
        return None, None
    from mapsvc.discovery import crs as crs_mod
    try:
        return crs_mod.inverse(text)
    except crs_mod.CRSError as exc:
        raise ShapefileError(str(exc)) from exc


def _reproject(geometry, transform):
    if geometry is None:
        return None

    def walk(coords):
        if coords and isinstance(coords[0], (int, float)):
            lon, lat = transform(coords[0], coords[1])
            return [round(lon, 6), round(lat, 6)]
        return [walk(c) for c in coords]
    return {**geometry, "coordinates": walk(geometry["coordinates"])}


def _crs(prj: bytes | None,
         bounds: tuple[float, float, float, float] | None) -> str:
    if prj is not None:
        text = prj.decode("utf-8-sig", "replace").strip()
        projected = re.search(r"\bPROJCS\s*\[\s*[\"']([^\"']+)", text, re.I)
        if projected:
            raise ShapefileError(
                f"projected CRS {projected.group(1)}; only geographic coordinates are supported")
        geographic = re.search(r"\bGEOGCS\s*\[\s*[\"']([^\"']+)", text, re.I)
        if not geographic:
            raise ShapefileError("unrecognised .prj; only geographic coordinates are supported")
        name = geographic.group(1)
        upper = text.upper().replace(" ", "").replace("_", "")
        if not ("WGS84" in upper or "NAD83" in upper or name.upper().startswith("GCS_")):
            raise ShapefileError(f"unsupported geographic CRS {name}")
        return name
    if bounds is not None:
        xmin, ymin, xmax, ymax = bounds
        if xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90:
            raise ShapefileError(
                "shapefile has no .prj and coordinates are outside longitude/latitude bounds")
    return "unlabelled geographic coordinates"
