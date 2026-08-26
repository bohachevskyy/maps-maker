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


def test_render_time_fields_change_the_filename(monkeypatch, tmp_path):
    """Unlike the harvest cache key, ramp and classify DO change the output."""
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    base = export.filename(build_manifest(ramp="YlGnBu", method="quantile", k=5))
    assert base != export.filename(build_manifest(ramp="Blues", method="quantile", k=5))
    assert base != export.filename(build_manifest(ramp="YlGnBu", method="jenks", k=5))
    assert base != export.filename(build_manifest(ramp="YlGnBu", method="quantile", k=7))


def test_the_name_is_readable_before_the_hash():
    name = export.filename(build_manifest(region="europe", variable_id="GDP_MD",
                                          normalize="POP_EST"))
    assert name.startswith("europe-GDP_MD-per-POP_EST-")
    assert name.endswith(".svg")


def test_a_hostile_region_cannot_escape_the_output_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    for nasty in ("../../etc/passwd", "..", "/etc/shadow", "a/b/c", "..\\..\\win"):
        path, _ = export.write(build_manifest(region=nasty), "<svg/>")
        assert path.parent == tmp_path, f"{nasty!r} escaped to {path}"
        assert "/" not in path.name and ".." not in path.name
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


def test_an_empty_region_still_produces_a_usable_name(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSVC_OUTPUT", str(tmp_path))
    path, _ = export.write(build_manifest(region="..."), "<svg/>")
    assert path.name.startswith("map-")


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
