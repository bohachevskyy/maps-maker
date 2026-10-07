"""USGS earthquakes, from the FDSN event service.

The id is a small query spec, never a URL: `start=2025-01-01;end=2026-01-01;
minmagnitude=6`. Only whitelisted keys are accepted; the window comes from the
manifest, so the agent cannot ask for one box and frame another.

Measured 2026-10-07: 2025, M6+, worldwide is 145 events, 122 kB, 0.3 s.
"""

import datetime
import hashlib
import json
import re
import urllib.parse

from mapsvc import fetch
from mapsvc.overlays import Capabilities, OverlayError, Window

ENDPOINT = "https://earthquake.usgs.gov/fdsnws/event/1/"

CAPABILITIES = Capabilities(
    source="usgs",
    license="public domain",
    attribution="U.S. Geological Survey, ComCat via the FDSN event service",
    description="earthquakes worldwide from the USGS ComCat catalogue, any "
                "date range since 1900, as points sized by magnitude and "
                "coloured by depth",
    id_help="semicolon-separated key=value pairs from start, end (YYYY-MM-DD, "
            "both required, end exclusive), minmagnitude, maxmagnitude, "
            "mindepth, maxdepth (km), e.g. "
            "'start=2024-01-01;end=2024-07-01;minmagnitude=5.5'",
)

KEYS = ("start", "end", "minmagnitude", "maxmagnitude", "mindepth", "maxdepth")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_RANGES = {"minmagnitude": (-2.0, 10.0), "maxmagnitude": (-2.0, 10.0),
           "mindepth": (-100.0, 1000.0), "maxdepth": (-100.0, 1000.0)}

# More points than this is a density surface, not a map of events; the service
# itself refuses above 20,000.
MAX_EVENTS = 5000

# Conventional seismological depth classes.
DEPTH_CLASSES = (
    {"value": "shallow", "label": "Shallow, < 70 km"},
    {"value": "intermediate", "label": "Intermediate, 70–300 km"},
    {"value": "deep", "label": "Deep, ≥ 300 km"},
)


def parse(overlay_id: str) -> str:
    return parse_spec(_spec(overlay_id))


def parse_spec(spec: dict) -> str:
    return ";".join(f"{k}={spec[k]}" for k in KEYS if k in spec)


def _spec(overlay_id: str) -> dict:
    spec: dict[str, str] = {}
    for part in overlay_id.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise OverlayError(f"usgs id part {part!r} is not key=value; "
                               f"{CAPABILITIES.id_help}", "overlays.id")
        key, value = (s.strip() for s in part.split("=", 1))
        key = key.lower()
        if key not in KEYS:
            raise OverlayError(f"usgs id key {key!r} is not one of "
                               f"{', '.join(KEYS)}", "overlays.id")
        if key in spec:
            raise OverlayError(f"usgs id repeats {key!r}", "overlays.id")
        if key in ("start", "end"):
            if not _DATE.fullmatch(value):
                raise OverlayError(f"usgs {key} must be YYYY-MM-DD, got {value!r}",
                                   "overlays.id")
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise OverlayError(f"usgs {key} {value!r} is not a date",
                                   "overlays.id") from None
        else:
            if not _NUMBER.fullmatch(value):
                raise OverlayError(f"usgs {key} must be a number, got {value!r}",
                                   "overlays.id")
            low, high = _RANGES[key]
            if not low <= float(value) <= high:
                raise OverlayError(f"usgs {key} must be within {low:g}..{high:g}",
                                   "overlays.id")
            value = f"{float(value):g}"
        spec[key] = value
    for key in ("start", "end"):
        if key not in spec:
            raise OverlayError(f"usgs id needs {key}; {CAPABILITIES.id_help}",
                               "overlays.id")
    if spec["start"] >= spec["end"]:
        raise OverlayError("usgs start must be before end", "overlays.id")
    return spec


def _params(spec: dict, window: Window) -> dict:
    params = {"format": "geojson", "starttime": spec["start"], "endtime": spec["end"]}
    for key in KEYS[2:]:
        if key in spec:
            params[key] = spec[key]
    box = window.bbox or window.extent
    if box and tuple(box) != (-180.0, -90.0, 180.0, 90.0):
        params.update(minlongitude=f"{box[0]:g}", minlatitude=f"{box[1]:g}",
                      maxlongitude=f"{box[2]:g}", maxlatitude=f"{box[3]:g}")
    return params


def load(overlay_id: str, window: Window) -> dict:
    spec = _spec(overlay_id)
    params = _params(spec, window)
    query = urllib.parse.urlencode(params)
    digest = hashlib.sha256(query.encode()).hexdigest()[:16]
    target = fetch.path("overlays", "usgs", f"{digest}.json")
    if not target.exists():
        # Counting first is cheap and turns an enormous request into a clear
        # refusal instead of a 20,000-point map or a 400 from the service.
        try:
            counted = json.loads(fetch.get(ENDPOINT + "count?" + query, timeout=60))
        except fetch.FetchError as exc:
            raise OverlayError(f"USGS could not count events: {exc}") from exc
        n = int(counted.get("count", 0))
        if n > MAX_EVENTS:
            raise OverlayError(
                f"{n:,} earthquakes match {overlay_id!r} in this window, over the "
                f"{MAX_EVENTS:,} a map can show; raise minmagnitude or shorten "
                "the period", "overlays.id")
    try:
        payload = fetch.cached(ENDPOINT + "query?" + query + "&orderby=time-asc",
                               "overlays", "usgs", f"{digest}.json", timeout=120)
    except fetch.FetchError as exc:
        raise OverlayError(f"USGS query failed: {exc}") from exc
    return layer_from_geojson(json.loads(payload), spec, ENDPOINT + "query?" + query)


def layer_from_geojson(data: dict, spec: dict, url: str) -> dict:
    features = []
    skipped = 0
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        mag = props.get("mag")
        if len(coords) < 2 or mag is None:
            skipped += 1
            continue
        lon, lat = float(coords[0]), float(coords[1])
        depth = float(coords[2]) if len(coords) > 2 and coords[2] is not None else 0.0
        when = datetime.datetime.fromtimestamp(int(props.get("time") or 0) / 1000,
                                               datetime.timezone.utc)
        features.append({
            "id": str(feature.get("id")),
            "lon": round(lon, 4), "lat": round(lat, 4),
            "size": float(mag),
            "category": ("shallow" if depth < 70 else
                         "intermediate" if depth < 300 else "deep"),
            "name": _label(float(mag), props.get("place") or "", when),
            "depth_km": round(depth, 1),
            "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    features.sort(key=lambda f: f["id"])

    generated = (data.get("metadata") or {}).get("generated")
    retrieved = (datetime.datetime.fromtimestamp(generated / 1000, datetime.timezone.utc)
                 .strftime("%Y-%m-%d") if generated else None)
    period = _period(spec)
    floor = spec.get("minmagnitude")
    title = f"Every magnitude {floor}+ earthquake" if floor else "Earthquakes"
    title += f" in {period}" if period.isdigit() else f", {period}"
    mags = [f["size"] for f in features]
    excluded = [{"count": skipped, "reason": "no magnitude or location"}] if skipped else []
    return {
        "source": "usgs", "id": parse_spec(spec),
        "kind": "points",
        "title": title,
        "label": "Earthquakes",
        "size": {"field": "mag", "label": "Magnitude", "unit": "", "scale": "magnitude"},
        "category": {"field": "depth", "label": "Depth of focus", "ordered": True,
                     "classes": list(DEPTH_CLASSES)},
        "features": features,
        "stats": {"count": len(features),
                  **({"max_magnitude": max(mags)} if mags else {})},
        "summary": (f"{len(features)} events recorded by the USGS · circle area "
                    "grows with magnitude, colour shows depth"),
        "encoding": ("Circle area ∝ 10^(M/2), 3.2× per magnitude step (radiated "
                     "energy grows 32× per step, so area deliberately "
                     "understates it); translucent, smallest drawn first; "
                     "colour = depth class"),
        "excluded": excluded,
        "provenance": {
            "name": "U.S. Geological Survey",
            "attribution": CAPABILITIES.attribution,
            "license": CAPABILITIES.license,
            "url": url,
            "vintage": (f"events {_human_date(spec['start'])} to before "
                        f"{_human_date(spec['end'])} (UTC)"
                        + (f", retrieved {retrieved}" if retrieved else "")),
        },
        "label_top": 6,
    }


def _period(spec: dict) -> str:
    start, end = spec["start"], spec["end"]
    if start.endswith("-01-01") and end.endswith("-01-01"):
        first, last = int(start[:4]), int(end[:4]) - 1
        return str(first) if first == last else f"{first}–{last}"
    return f"{_human_date(start)} to {_human_date(end)}"


def _human_date(iso: str) -> str:
    day = datetime.date.fromisoformat(iso)
    return f"{day.day} {day.strftime('%b')} {day.year}"


_DISTANCE = re.compile(r"^\d+\s*km\s+[NSEW]{1,3}\s+of\s+", re.IGNORECASE)


def _label(mag: float, place: str, when: datetime.datetime) -> str:
    """'M8.8 Kamchatka Peninsula, Russia, 29 Jul' from the USGS place string."""
    where = re.sub(r"^\d{4}\s+", "", place.strip())
    where = re.sub(r"\s+Earthquake$", "", where)
    if _DISTANCE.match(where):
        where = "off " + _DISTANCE.sub("", where)
    return f"M{mag:.1f} {where}, {when.day} {when.strftime('%b')}"
