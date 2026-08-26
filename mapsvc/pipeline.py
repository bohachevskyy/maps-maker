"""The three seams, in order.

Kept apart from api.py so the whole path can be exercised without a web server.
"""

from mapsvc.harvest import harvest
from mapsvc.manifest import validate
from mapsvc.render import render


def build_map(raw: dict) -> str:
    manifest = validate(raw)
    result = harvest(manifest)
    svg = render(manifest, result)
    return svg
