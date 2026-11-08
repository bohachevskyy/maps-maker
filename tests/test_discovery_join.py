"""Resolving a found file's codes and names to boundary keys (synthetic units)."""

from mapsvc.discovery import join


def _country(iso3, name, a2, n3, **extra):
    return {"keys": {"ISO3": iso3}, "name": name,
            "properties": {"ISO_A2_EH": a2, "ISO_N3_EH": n3, "NAME": name, **extra}}


def _region(code, name, **props):
    return {"keys": {"ISO3166_2": code}, "name": name, "properties": props}


COUNTRIES = join._countries([
    _country("AFG", "Afghanistan", "AF", "004"),
    _country("FRA", "France", "FR", "250", ISO_A2="-99", FORMAL_EN="French Republic"),
    _country("KOR", "South Korea", "KR", "410", NAME_SORT="Korea, Rep."),
])


def test_countries_resolve_from_iso3_iso2_m49_and_names():
    r = COUNTRIES.resolve
    assert [r("AFG"), r("af"), r("4"), r("004"), r("4.0")] == ["AFG"] * 5
    assert [r("France"), r("French Republic"), r("250"), r("Korea, Rep.")] == ["FRA", "FRA", "FRA", "KOR"]
    assert r("-99") is None and r("") is None and r("Atlantis") is None


REGIONS = join._regions([
    _region("BR-SP", "São Paulo", postal="SP", code_hasc="BR.SP"),
    _region("BR-MG", "Minas Gerais", postal="MG"),
    _region("BR-DF", "Distrito Federal", postal="DF"),
    _region("MX-CMX", "Ciudad de México", postal="DF", name_alt="Distrito Federal"),
    _region("MX-JAL", "Jalisco", postal="JA"),
])


def test_a_shared_name_is_settled_by_where_the_rest_of_the_file_is():
    assert REGIONS.resolve("Distrito Federal") is None
    brazil = REGIONS.resolver(["São Paulo", "Minas Gerais", "Distrito Federal"])
    mexico = REGIONS.resolver(["Jalisco", "Distrito Federal"])
    assert brazil("Distrito Federal") == "BR-DF" and mexico("Distrito Federal") == "MX-CMX"


def test_postal_codes_need_a_country_context():
    assert REGIONS.resolve("DF") is None
    assert REGIONS.resolver(["BR-SP", "BR-MG", "DF"])("DF") == "BR-DF"
    assert REGIONS.resolve("BR.SP") == "BR-SP" and REGIONS.resolve("br-mg") == "BR-MG"
