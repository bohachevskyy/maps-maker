"""A minimal ESRI shapefile reader: polygons and their attributes, nothing else.

The US Census Bureau publishes its cartographic boundary files as zipped
shapefiles only. The format is small enough to read by hand -- a .shp of
little-endian doubles and a .dbf of fixed-width text -- so it is, rather than
pulling GDAL in for one file type.

Only what those files use is supported: Polygon (5) and null shapes, and dBASE
character/numeric fields. Rings are grouped into GeoJSON polygons by winding:
the shapefile spec makes outer rings clockwise and holes counter-clockwise.
"""

import io
import struct
import zipfile

POLYGON = 5
NULL = 0


class ShapefileError(ValueError):
    pass


def read_zip(data: bytes) -> list[tuple[dict, dict | None]]:
    """[(properties, GeoJSON geometry or None), ...] from a zipped shapefile."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()

    def member(ext: str) -> str:
        found = [n for n in names if n.lower().endswith(ext)]
        if not found:
            raise ShapefileError(f"no {ext} member in the archive")
        return found[0]

    encoding = "utf-8"
    cpg = [n for n in names if n.lower().endswith(".cpg")]
    if cpg:
        declared = archive.read(cpg[0]).decode("ascii", "replace").strip()
        encoding = declared or encoding

    records = _dbf(archive.read(member(".dbf")), encoding)
    shapes = _shp(archive.read(member(".shp")))
    if len(records) != len(shapes):
        raise ShapefileError(
            f".dbf has {len(records)} records but .shp has {len(shapes)} shapes")
    return list(zip(records, shapes))


def _dbf(data: bytes, encoding: str) -> list[dict]:
    count, header_length, record_length = struct.unpack("<IHH", data[4:12])
    fields = []
    offset = 32
    while data[offset] != 0x0D:
        name = data[offset:offset + 11].split(b"\x00", 1)[0].decode("ascii")
        kind = chr(data[offset + 11])
        length = data[offset + 16]
        fields.append((name, kind, length))
        offset += 32

    records = []
    position = header_length
    for _ in range(count):
        record = data[position:position + record_length]
        position += record_length
        if record[:1] == b"*":
            continue  # deleted
        values, cursor = {}, 1
        for name, kind, length in fields:
            raw = record[cursor:cursor + length].decode(encoding, "replace").strip()
            cursor += length
            if kind in "NF" and raw:
                try:
                    values[name] = int(raw) if raw.lstrip("-").isdigit() else float(raw)
                except ValueError:
                    values[name] = None
            else:
                values[name] = raw
        records.append(values)
    return records


def _shp(data: bytes) -> list[dict | None]:
    if struct.unpack(">i", data[:4])[0] != 9994:
        raise ShapefileError("not a shapefile: bad file code")
    shapes = []
    position = 100
    while position + 8 <= len(data):
        _, words = struct.unpack(">ii", data[position:position + 8])
        content = data[position + 8:position + 8 + words * 2]
        position += 8 + words * 2
        kind = struct.unpack("<i", content[:4])[0]
        if kind == NULL:
            shapes.append(None)
            continue
        if kind != POLYGON:
            raise ShapefileError(f"shape type {kind} is not supported, only polygons")
        parts_count, points_count = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{parts_count}i", content[44:44 + 4 * parts_count]))
        start = 44 + 4 * parts_count
        flat = struct.unpack(f"<{2 * points_count}d",
                             content[start:start + 16 * points_count])
        points = [[flat[i], flat[i + 1]] for i in range(0, len(flat), 2)]
        bounds = parts + [points_count]
        rings = [points[bounds[i]:bounds[i + 1]] for i in range(parts_count)]
        shapes.append(_geometry(rings))
    return shapes


def _signed_area(ring: list) -> float:
    return sum(x0 * y1 - x1 * y0
               for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1])) / 2.0


def _contains(ring: list, point: list) -> bool:
    x, y = point
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def _geometry(rings: list) -> dict:
    """Group rings into polygons: clockwise outers, counter-clockwise holes."""
    outers = [[ring] for ring in rings if _signed_area(ring) <= 0]
    for ring in rings:
        if _signed_area(ring) <= 0:
            continue
        owner = next((polygon for polygon in outers
                      if _contains(polygon[0], ring[0])), None)
        if owner is None:
            outers.append([ring])  # a mis-wound outer; keep it rather than lose it
        else:
            owner.append(ring)
    if len(outers) == 1:
        return {"type": "Polygon", "coordinates": outers[0]}
    return {"type": "MultiPolygon", "coordinates": outers}
