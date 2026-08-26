"""Writing rendered maps to disk and naming them predictably.

The filename is derived from the manifest, so the same request always lands at
the same path. That makes the endpoint idempotent: call it twice and you get one
file, not two. The content is rewritten every time rather than skipped when the
path exists, so a file never goes stale after a change to the renderer.
"""

import hashlib
import json
import os
import pathlib
import re

from mapsvc.manifest import Manifest

# Anything outside this becomes "-", so a region of "../../etc/passwd" cannot
# walk out of the output directory.
UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
MAX_LABEL = 40


def output_dir() -> pathlib.Path:
    root = os.environ.get("MAPSVC_OUTPUT")
    if root:
        return pathlib.Path(root).expanduser()
    return pathlib.Path(__file__).resolve().parent.parent / "out" / "maps"


def _slug(value: str) -> str:
    return UNSAFE.sub("-", str(value)).strip("-")[:MAX_LABEL] or "map"


def filename(manifest: Manifest) -> str:
    """A readable name plus a hash of everything that affects the output.

    The hash covers the whole manifest, not just the data-relevant fields the
    harvest cache keys on -- here the ramp and the classification do change the
    file, so they must change its name.
    """
    canonical = json.dumps(
        {
            "region": manifest.region, "level": manifest.level,
            "variable": {"source": manifest.variable_source, "id": manifest.variable_id},
            "normalize": manifest.normalize, "method": manifest.method, "k": manifest.k,
            "ramp": manifest.ramp, "projection": manifest.projection,
            "missing": manifest.missing,
        },
        sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:10]

    label = f"{_slug(manifest.region)}-{_slug(manifest.variable_id)}"
    if manifest.normalize:
        label += f"-per-{_slug(manifest.normalize)}"
    return f"{label}-{digest}.svg"


def write(manifest: Manifest, svg: str) -> tuple[pathlib.Path, bool]:
    """Write `svg` for `manifest`. Returns the path and whether it already existed."""
    directory = output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename(manifest)
    existed = path.exists()

    # Write via a temporary name so a reader never sees a half-written map.
    temporary = path.with_suffix(".part")
    temporary.write_text(svg, encoding="utf-8")
    temporary.replace(path)
    return path, existed
