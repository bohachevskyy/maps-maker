"""Inverse Transverse Mercator, checked against the NWS file it was built for."""

import math

import pytest

from mapsvc.discovery import crs

UTM16 = ('PROJCS["NAD_1983_UTM_Zone_16N",GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
         'SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
         'UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
         'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],'
         'PARAMETER["Central_Meridian",-87.0],PARAMETER["Scale_Factor",0.9996],'
         'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')


def test_the_false_origin_is_the_central_meridian_on_the_equator():
    inv, name = crs.inverse(UTM16)
    assert name == "NAD_1983_UTM_Zone_16N"
    lon, lat = inv(500000.0, 0.0)
    assert lon == pytest.approx(-87.0, abs=1e-9) and lat == pytest.approx(0.0, abs=1e-9)


def test_an_nws_track_start_lands_on_its_recorded_begin_point():
    """042711_StatewideTracks.shp, track 2: raw UTM vertex vs its Beg_Lon/Beg_Lat (2 dp)."""
    lon, lat = crs.inverse(UTM16)[0](424960.188059, 3741702.583778)
    assert abs(lon - -87.81) < 0.01 and abs(lat - 33.81) < 0.01


def _forward(lon, lat, *, a=6378137.0, inv_f=298.257222101, k0=0.9996, lon0=-87.0):
    """Snyder's forward TM (eqs. 8-9..8-10), written independently of the inverse."""
    f = 1 / inv_f
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    phi, lam = math.radians(lat), math.radians(lon - lon0)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t, c, A = math.tan(phi) ** 2, ep2 * math.cos(phi) ** 2, lam * math.cos(phi)
    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))
    x = k0 * n * (A + (1 - t + c) * A ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * A ** 5 / 120)
    y = k0 * (m + n * math.tan(phi) * (A * A / 2 + (5 - t + 9 * c + 4 * c * c) * A ** 4 / 24
                                       + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * A ** 6 / 720))
    return x + 500000.0, y


@pytest.mark.parametrize("lon,lat", [(-87.0, 34.0), (-89.9, 30.2), (-84.1, 36.5), (-86.5, 45.0)])
def test_inverse_undoes_an_independent_forward_projection(lon, lat):
    back = crs.inverse(UTM16)[0](*_forward(lon, lat))
    assert back == pytest.approx((lon, lat), abs=1e-6)


def test_other_projections_are_refused_by_name():
    lcc = UTM16.replace("Transverse_Mercator", "Lambert_Conformal_Conic").replace("UTM_Zone_16N", "StatePlane_X")
    with pytest.raises(crs.CRSError, match="StatePlane_X"):
        crs.inverse(lcc)
