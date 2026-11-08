import io
import json
import struct
import zipfile

import pytest

from mapsvc.discovery import probe
from mapsvc.discovery.shapefile import ShapefileError, to_geojson
from mapsvc.overlays import web


def _shp(records, shape_type=0):
    body = b"".join(
        struct.pack(">ii", number, len(content) // 2) + content
        for number, content in enumerate(records, 1)
    )
    length = 100 + len(body)
    header = (struct.pack(">7i", 9994, 0, 0, 0, 0, 0, length // 2)
              + struct.pack("<2i8d", 1000, shape_type, *([0.0] * 8)))
    return header + body


def _point(x, y, kind=1):
    content = struct.pack("<idd", kind, x, y)
    if kind in {11, 21}:
        content += struct.pack("<dd", 123.0, 456.0)
    return content


def _multipart(kind, parts):
    points = [point for part in parts for point in part]
    starts, offset = [], 0
    for part in parts:
        starts.append(offset)
        offset += len(part)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (struct.pack("<i4d2i", kind, min(xs), min(ys), max(xs), max(ys),
                        len(parts), len(points))
            + struct.pack(f"<{len(starts)}i", *starts)
            + b"".join(struct.pack("<dd", *point) for point in points))


def _dbf(fields, rows, encoding="ascii"):
    header_length = 32 + 32 * len(fields) + 1
    record_length = 1 + sum(length for _, _, length in fields)
    header = (bytes([3, 24, 1, 1])
              + struct.pack("<IHH", len(rows), header_length, record_length)
              + bytes(20))
    descriptors = b""
    for name, kind, length in fields:
        encoded_name = name.encode("ascii")[:11].ljust(11, b"\0")
        descriptors += (encoded_name + kind.encode("ascii") + bytes(4)
                        + bytes([length, 0]) + bytes(14))
    records = []
    for deleted, values in rows:
        cells = []
        for (name, kind, length), value in zip(fields, values):
            raw = str(value).encode(encoding)
            if kind in "NF":
                raw = raw.rjust(length)
            else:
                raw = raw.ljust(length)
            cells.append(raw[:length])
        records.append((b"*" if deleted else b" ") + b"".join(cells))
    return header + descriptors + b"\r" + b"".join(records) + b"\x1a"


def _zip(shp, dbf, *, stem="data", prj=None, cpg=None, extras=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"{stem}.shp", shp)
        archive.writestr(f"{stem}.dbf", dbf)
        if prj is not None:
            archive.writestr(f"{stem}.prj", prj)
        if cpg is not None:
            archive.writestr(f"{stem}.cpg", cpg)
        for name, contents in extras or []:
            archive.writestr(name, contents)
    return output.getvalue()


GEOGCS = 'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983"]]'


def test_point_dbf_types_deleted_record_and_cpg():
    fields = [("NAME", "C", 8), ("COUNT", "N", 5), ("DATE", "D", 8),
              ("ACTIVE", "L", 1), ("RATIO", "F", 6)]
    dbf = _dbf(fields, [
        (False, ["Café", 12, "20240229", "Y", "1.25"]),
        (True, ["Deleted", 99, "20200101", "N", "2.0"]),
    ], "latin-1")
    payload = _zip(_shp([_point(1.12345678, 2.0), _point(9, 9)], 1), dbf,
                   prj=GEOGCS, cpg="latin-1")
    result = to_geojson(payload)
    assert result == {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [1.123457, 2.0]},
        "properties": {"NAME": "Café", "COUNT": 12, "DATE": "2024-02-29",
                       "ACTIVE": True, "RATIO": 1.25},
    }]}


def test_polyline_with_two_parts_becomes_multiline():
    shape = _multipart(3, [[(0, 0), (1, 1)], [(2, 2), (3, 3)]])
    result = to_geojson(_zip(_shp([shape], 3), _dbf([("ID", "N", 2)],
                                                         [(False, [1])]),
                             prj=GEOGCS))
    assert result["features"][0]["geometry"] == {
        "type": "MultiLineString",
        "coordinates": [[[0.0, 0.0], [1.0, 1.0]], [[2.0, 2.0], [3.0, 3.0]]],
    }


def test_polygon_clockwise_outer_and_counterclockwise_hole():
    outer = [(0, 0), (0, 4), (4, 4), (4, 0), (0, 0)]
    hole = [(1, 1), (3, 1), (3, 3), (1, 3), (1, 1)]
    shape = _multipart(5, [outer, hole])
    result = to_geojson(_zip(_shp([shape], 5), _dbf([("ID", "N", 2)],
                                                         [(False, [1])]),
                             prj=GEOGCS))
    geometry = result["features"][0]["geometry"]
    assert geometry["type"] == "Polygon"
    assert len(geometry["coordinates"]) == 2


def test_z_point_drops_z_and_m():
    payload = _zip(_shp([_point(-80, 35, 11)], 11),
                   _dbf([("ID", "N", 2)], [(False, [1])]), prj=GEOGCS)
    assert to_geojson(payload)["features"][0]["geometry"] == {
        "type": "Point", "coordinates": [-80.0, 35.0]}


def test_largest_shp_wins_and_ignores_hidden_members():
    small_shp = _shp([_point(1, 2)], 1)
    small_dbf = _dbf([("ID", "N", 2)], [(False, [1])])
    large_shp = _shp([_point(3, 4), _point(5, 6)], 1)
    large_dbf = _dbf([("ID", "N", 2)], [(False, [2]), (False, [3])])
    payload = _zip(large_shp, large_dbf, stem="chosen", prj=GEOGCS,
                   extras=[("small.shp", small_shp), ("small.dbf", small_dbf),
                           (".hidden.shp", large_shp + b"padding"),
                           (".hidden.dbf", large_dbf)])
    assert [f["properties"]["ID"] for f in to_geojson(payload)["features"]] == [2, 3]


def test_projected_crs_and_unlabelled_out_of_range_coordinates_are_refused():
    shp = _shp([_point(500000, 4000000)], 1)
    dbf = _dbf([("ID", "N", 2)], [(False, [1])])
    projected = 'PROJCS["NAD_1983_StatePlane_Alabama_East_FIPS_0101_Feet"]'
    with pytest.raises(ShapefileError, match="projected CRS NAD_1983.*not supported"):
        to_geojson(_zip(shp, dbf, prj=projected))
    with pytest.raises(ShapefileError, match="outside longitude/latitude"):
        to_geojson(_zip(shp, dbf))


def test_probe_and_web_convert_a_zipped_shapefile():
    payload = _zip(_shp([_point(-86.5, 33.5)], 1),
                   _dbf([("NAME", "C", 8)], [(False, ["Track"])]),
                   stem="tracks", prj=GEOGCS)
    rows, format_name, geometry_types, warnings = probe.records(payload)
    assert format_name == "shapefile" and geometry_types == ["Point"]
    assert rows == [{"NAME": "Track", "__longitude": -86.5, "__latitude": 33.5}]
    assert warnings == [
        "read shapefile tracks.shp (1 features, GCS_North_American_1983)"]
    report = probe.probe_bytes(payload)
    assert report.format == "shapefile" and report.geometry_kind == "points"

    converted = web._unzip(payload, "https://example.org/tracks.zip")
    assert converted == json.dumps(to_geojson(payload), sort_keys=True,
                                   separators=(",", ":")).encode()
    layer = web.layer_from_bytes(payload, "https://example.org/tracks.zip",
                                 web.Window(-90, 30, -80, 40))
    assert layer["kind"] == "places" and len(layer["features"]) == 1
