"""Projected coordinates back to longitude/latitude, by hand.

Agency shapefiles are often projected: the NWS damage tracks of the 2011
tornado outbreak are NAD83 / UTM zone 16N (measured 2026-11-07). Transverse
Mercator covers every UTM zone and many national grids; anything else is
refused by name rather than guessed. NAD83 and WGS84 differ by about a metre,
far below map precision, so the datum is not shifted.

Inverse Transverse Mercator: Snyder, "Map Projections -- A Working Manual"
(USGS PP 1395, 1987), eqs. 8-12 and 3-21.
"""

from __future__ import annotations

import math
import re


class CRSError(ValueError):
    pass


def _param(text: str, name: str, default=None) -> float:
    m = re.search(r'PARAMETER\s*\[\s*"' + name + r'"\s*,\s*([-+0-9.eE]+)', text, re.I)
    if m:
        return float(m.group(1))
    if default is None:
        raise CRSError(f"projection parameter {name} missing")
    return default


def inverse(prj: str):
    """A function (x, y) -> (lon, lat) for the .prj's projected CRS."""
    name = (re.search(r'PROJCS\s*\[\s*"([^"]+)"', prj) or [None, "projected CRS"])[1]
    method = re.search(r'PROJECTION\s*\[\s*"([^"]+)"', prj, re.I)
    if not method or method.group(1).lower().replace(" ", "_") not in (
            "transverse_mercator", "gauss_kruger"):
        raise CRSError(f"projected CRS {name} ({method.group(1) if method else 'unknown'}) "
                       "is not supported; only Transverse Mercator (UTM) is")
    spheroid = re.search(r'SPHEROID\s*\[\s*"[^"]*"\s*,\s*([0-9.eE+]+)\s*,\s*([0-9.eE+]+)', prj, re.I)
    a, inv_f = (float(spheroid.group(1)), float(spheroid.group(2))) if spheroid else (6378137.0, 298.257223563)
    # The linear UNIT is the last UNIT of a PROJCS (the GEOGCS one is angular).
    units = re.findall(r'UNIT\s*\[\s*"[^"]*"\s*,\s*([0-9.eE+]+)', prj, re.I)
    to_metres = float(units[-1]) if units else 1.0
    return transverse_mercator(
        a=a, inv_f=inv_f, k0=_param(prj, "Scale_Factor", 1.0),
        lon0=_param(prj, "Central_Meridian"), lat0=_param(prj, "Latitude_Of_Origin", 0.0),
        false_easting=_param(prj, "False_Easting", 0.0),
        false_northing=_param(prj, "False_Northing", 0.0), to_metres=to_metres), name


def transverse_mercator(*, a, inv_f, k0, lon0, lat0, false_easting, false_northing,
                        to_metres=1.0):
    f = 1 / inv_f if inv_f else 0.0
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    e4, e6 = e2 * e2, e2 * e2 * e2

    def meridian(phi):
        return a * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
                    - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                    + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
                    - (35 * e6 / 3072) * math.sin(6 * phi))

    m0 = meridian(math.radians(lat0))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    false_easting *= to_metres
    false_northing *= to_metres

    def to_lonlat(x, y):
        x, y = x * to_metres, y * to_metres
        m = m0 + (y - false_northing) / k0
        mu = m / (a * (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256))
        phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
                + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
                + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
                + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
        sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
        c1, t1 = ep2 * cos1 ** 2, tan1 ** 2
        n1 = a / math.sqrt(1 - e2 * sin1 ** 2)
        r1 = a * (1 - e2) / (1 - e2 * sin1 ** 2) ** 1.5
        d = (x - false_easting) / (n1 * k0)
        lat = phi1 - (n1 * tan1 / r1) * (
            d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
            + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
        lon = math.radians(lon0) + (
            d - (1 + 2 * t1 + c1) * d ** 3 / 6
            + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
        return math.degrees(lon), math.degrees(lat)

    return to_lonlat
