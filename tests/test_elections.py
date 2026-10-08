"""MIT Election Lab presidential returns, offline from recorded CSV rows."""

import pytest

from mapsvc.statistics import StatisticsError, elections

CSV = b'''year,state,state_po,state_fips,state_cen,state_ic,office,candidate,party_detailed,writein,candidatevotes,totalvotes,version,notes,party_simplified
2024,Wyoming,WY,56,83,68,US PRESIDENT,"HARRIS, KAMALA",DEMOCRAT,False,69527,271123,2025-11-20,,DEMOCRAT
2024,Wyoming,WY,56,83,68,US PRESIDENT,"TRUMP, DONALD",REPUBLICAN,False,192633,271123,2025-11-20,,REPUBLICAN
2024,Wyoming,WY,56,83,68,US PRESIDENT,OTHER,OTHER,False,6888,271123,2025-11-20,,OTHER
2024,Wyoming,WY,56,83,68,US PRESIDENT,UNDERVOTES,OTHER,False,2075,271123,2025-11-20,,OTHER
2024,Vermont,VT,50,13,6,US PRESIDENT,"HARRIS, KAMALA",DEMOCRAT,False,235791,372885,2025-11-20,,DEMOCRAT
2024,Vermont,VT,50,13,6,US PRESIDENT,"TRUMP, DONALD",REPUBLICAN,False,119395,372885,2025-11-20,,REPUBLICAN
2024,Vermont,VT,50,13,6,US PRESIDENT,OTHER,OTHER,False,14236,372885,2025-11-20,,OTHER
2024,Vermont,VT,50,13,6,US PRESIDENT,SPOILED,OTHER,False,3463,372885,2025-11-20,,OTHER
2024,Pennsylvania,PA,42,23,14,US PRESIDENT,"HARRIS, KAMALA",DEMOCRAT,False,3423042,7034206,2025-11-20,,DEMOCRAT
2024,Pennsylvania,PA,42,23,14,US PRESIDENT,"TRUMP, DONALD",REPUBLICAN,False,3543308,7034206,2025-11-20,,REPUBLICAN
2024,Pennsylvania,PA,42,23,14,US PRESIDENT,OTHER,OTHER,False,67856,7034206,2025-11-20,,OTHER
2024,District of Columbia,DC,11,53,55,US PRESIDENT,"HARRIS, KAMALA",DEMOCRAT,False,294185,325869,2025-11-20,,DEMOCRAT
2024,District of Columbia,DC,11,53,55,US PRESIDENT,"TRUMP, DONALD",REPUBLICAN,False,21076,325869,2025-11-20,,REPUBLICAN
2024,District of Columbia,DC,11,53,55,US PRESIDENT,OTHER,OTHER,False,10608,325869,2025-11-20,,OTHER
2020,Wyoming,WY,56,83,68,US PRESIDENT,"BIDEN, JOSEPH",DEMOCRAT,False,73491,278503,20210113,,DEMOCRAT
2020,Wyoming,WY,56,83,68,US PRESIDENT,"TRUMP, DONALD",REPUBLICAN,False,193559,278503,20210113,,REPUBLICAN
'''


@pytest.fixture
def election_data(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(elections.fetch, "get", lambda *args, **kwargs: CSV)
    elections._series.cache_clear()
    yield
    elections._series.cache_clear()


def test_margin_is_republican_minus_democratic_share_of_valid_votes(election_data):
    values = elections.load("republican_minus_democratic_margin", "admin_1", "world")
    assert values.values["US-WY"] == pytest.approx(45.756148)
    assert values.values["US-VT"] == pytest.approx(-31.507598)
    assert values.values["US-DC"] == pytest.approx(-83.809445)
    assert values.values["US-PA"] == pytest.approx(1.709731)
    assert "Republican minus Democratic" in values.provenance["unit"]
    assert values.provenance["diverging"]["positive_color"] == "red"
    assert "valid candidate votes" in values.provenance["note"]
    assert "popular vote, not electoral votes" in values.provenance["note"]


def test_shares_use_valid_vote_and_years_are_available(election_data):
    republican = elections.load("republican_share", "admin_1", "world")
    assert republican.values["US-WY"] == pytest.approx(100 * 192633 / 269048)
    old = elections.load("democratic_share", "admin_1", "world", year=2020)
    assert old.values["US-WY"] == pytest.approx(100 * 73491 / (73491 + 193559))


def test_html_is_rejected_before_it_can_be_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_CACHE", str(tmp_path))
    monkeypatch.setattr(elections.fetch, "get",
                        lambda *args, **kwargs: b"<!doctype html><title>Denied</title>")
    with pytest.raises(StatisticsError, match="not return"):
        elections._data()
    assert not (tmp_path / "elections" / elections.FILENAME).exists()
