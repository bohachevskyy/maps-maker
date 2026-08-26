"""The seams, in order.

Kept apart from api.py so the whole path can be exercised without a web server.
"""

from mapsvc.harvest import harvest
from mapsvc.manifest import Manifest, validate
from mapsvc.render import render


def draw(manifest: Manifest) -> str:
    """Harvest the data for a validated manifest and render it."""
    return render(manifest, harvest(manifest))


def build_map(raw: dict) -> str:
    manifest = validate(raw)
    return draw(manifest)
