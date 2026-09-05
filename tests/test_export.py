import json

import pytest

from mapsvc import export
from mapsvc.manifest import validate
from tests.conftest import build_manifest


def test_the_same_manifest_always_lands_at_the_same_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    manifest = build_manifest()
    first, existed_first = export.write(manifest, "<svg/>")
    second, existed_second = export.write(manifest, "<svg/>")
    assert first == second
    assert (existed_first, existed_second) == (False, True)
    assert len(list(tmp_path.iterdir())) == 1, "a second call must not add a file"


def test_content_is_rewritten_rather_than_left_stale(monkeypatch, tmp_path):
    """Skipping the write when the path exists would freeze old output in place
    after any change to the renderer."""
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    manifest = build_manifest()
    path, _ = export.write(manifest, "<svg>old</svg>")
    path, _ = export.write(manifest, "<svg>new</svg>")
    assert path.read_text() == "<svg>new</svg>"


def test_the_window_and_level_change_the_filename(monkeypatch, tmp_path):
    """There are no render-time fields left to vary, so the file is a pure
    function of the data request."""
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    base = export.filename(build_manifest())
    assert base != export.filename(build_manifest(bbox=(0.0, 0.0, 1.0, 1.0)))
    assert base != export.filename(build_manifest(level="admin_1"))
    assert base != export.filename(build_manifest(basemap_source="overture"))


def test_the_name_states_the_window_and_the_variable():
    name = export.filename(build_manifest(variable_id="GDP_MD"))
    assert name.startswith("-5_35_40_60-GDP_MD-")
    assert name.endswith(".svg")


def test_a_bbox_cannot_produce_a_path_outside_the_output_directory(monkeypatch, tmp_path):
    """The window is numbers, so there is nothing to escape with -- but the
    level and variable still reach the filename."""
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    path, _ = export.write(build_manifest(variable_id="GDP_MD"), "<svg/>")
    assert path.parent == tmp_path
    assert "/" not in path.name and ".." not in path.name



def test_output_directory_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path / "nested" / "deep"))
    path, _ = export.write(build_manifest(), "<svg/>")
    assert path.parent == tmp_path / "nested" / "deep"
    assert path.exists()


def test_default_output_directory_when_unset(monkeypatch):
    monkeypatch.delenv("MAPSVC_OUTPUT", raising=False)
    assert export.output_dir().parts[-2:] == ("out", "maps")


def test_no_part_file_is_left_behind(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    export.write(build_manifest(), "<svg/>")
    assert not list(tmp_path.glob("*.part"))


def test_a_base_map_is_named_for_what_it_is_not_for_a_missing_variable():
    name = export.filename(build_manifest(level="admin_1",
                                          variable_id=None, variable_source=None))
    assert name.startswith("-5_35_40_60-admin_1-boundaries-")
    assert "None" not in name


def test_the_basemap_changes_the_filename():
    """Different polygons, different file -- even with the same variable."""
    ne = export.filename(build_manifest(basemap_source="natural_earth"))
    ov = export.filename(build_manifest(basemap_source="overture"))
    assert ne != ov
