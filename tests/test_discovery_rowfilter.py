from mapsvc.discovery import rowfilter as F

ROWS = [{"fall": "Fell", "mass": "1,200", "date": "2011-04-27"},
        {"fall": "Found", "mass": "90", "date": "2011-04-24"},
        {"fall": "fell", "mass": "", "date": "2011-04-28"}]


def test_equality_ignores_case_and_numbers_compare_as_numbers():
    assert len(F.apply(ROWS, [{"column": "fall", "op": "eq", "value": "Fell"}])) == 2
    assert F.apply(ROWS, [{"column": "mass", "op": "ge", "value": "200"}]) == [ROWS[0]]


def test_dates_compare_as_iso_strings_and_blank_cells_never_pass_a_bound():
    kept = F.apply(ROWS, [{"column": "date", "op": "ge", "value": "2011-04-25"},
                          {"column": "date", "op": "le", "value": "2011-04-28"}])
    assert kept == [ROWS[0], ROWS[2]]
    assert F.apply(ROWS, [{"column": "mass", "op": "lt", "value": "100"}]) == [ROWS[1]]


def test_the_older_dict_form_still_means_equality():
    assert F.normalise({"fall": "Fell"}) == [{"column": "fall", "op": "eq", "value": "Fell"}]
    assert F.describe([{"column": "SIZE_HA", "op": "ge", "value": "200"}]) == "SIZE_HA ≥ 200"
