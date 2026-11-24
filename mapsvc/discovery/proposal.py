"""A dataset proposal from the research agent, and the service's verdict on it.

The proposal names a file and which of its columns play which role; it carries
no values. `validate` checks it against the service's own probe of that file --
the columns exist, the roles suit the kind of map, values are numeric where
they must be, the keys actually join to our boundaries -- before anything is
registered or drawn. A proposal that fails is rejected with reasons, never
"repaired" by guessing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re

from mapsvc.discovery import rowfilter

LICENCE_UNSTATED = "licence not stated by source"
# end_lon/end_lat: a row is a segment from (lon, lat) to its end -- a tornado track,
# a flight, a migration -- as in NOAA SPC's one-row-per-tornado table.
ROLE_NAMES = ("key", "name", "lon", "lat", "end_lon", "end_lat", "value", "unit", "date",
              "category", "size")

# Coverage a choropleth must reach inside its scope to be worth drawing. Below
# this the map is mostly hatching, which is a worse answer than an honest refusal.
MIN_CHOROPLETH_COVERAGE = 0.5
MIN_NUMERIC_SHARE = 0.8

# Text that tries to steer the agent. Web content is data; a source carrying it
# in its own title or publisher field is not trustworthy enough to cite.
_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|you are (now )?an? (ai|assistant)"
    r"|system prompt|report (success|that this)|disregard (the|your)", re.I)


@dataclasses.dataclass
class DatasetProposal:
    url: str
    title: str
    publisher: str
    licence: str
    unit: str
    vintage: str
    kind: str                     # choropleth | points | lines
    key_namespace: str            # ISO3 | ISO3166_2 | FIPS | NUTS | coords | name
    level: str
    measure: str
    roles: dict                   # role -> column name in the file
    query: str
    landing_page: str | None = None
    row_filter: list = dataclasses.field(default_factory=list)   # rowfilter conditions
    scale: float = 1.0
    derive: dict = dataclasses.field(default_factory=dict)       # {} or a ratio
    format: str | None = None
    content_sha256: str | None = None

    @property
    def id(self) -> str:
        """Stable id: the same file with the same roles is the same source."""
        basis = "|".join([self.url, self.kind, self.key_namespace,
                          ",".join(f"{k}={v}" for k, v in sorted(self.roles.items())),
                          rowfilter.describe(self.row_filter)]
                         + ([f"x{self.scale:g}"] if self.scale != 1 else [])
                         + ([f"ratio/{self.derive['denominator']}x{self.derive['factor']:g}"]
                            if self.derive else []))
        return "disc_" + hashlib.sha256(basis.encode()).hexdigest()[:16]


@dataclasses.dataclass
class Verdict:
    ok: bool
    problems: list
    coverage: dict | None = None
    warnings: list = dataclasses.field(default_factory=list)


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _numeric_share(values) -> float:
    vals = [v for v in values if str(v).strip() not in ("", "..", "-", "NA", "n/a", "null")]
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        try:
            float(str(v).replace(",", "").replace(" ", ""))
            ok += 1
        except ValueError:
            pass
    return ok / len(vals)


def validate(prop: DatasetProposal, report, *, column_values=None, coverage=None,
             scope=()) -> Verdict:
    """Check a proposal against the service's probe of the same file.

    column_values(name) -> list of that column's values (after row_filter);
    coverage(values, key_namespace, level, scope) -> {"matched","expected","ratio"}.
    Both are injected so tests and evals can run offline.
    """
    problems: list = []
    warnings: list = []
    columns = {_get(c, "name"): c for c in (_get(report, "columns") or [])}

    for role, column in prop.roles.items():
        if role not in ROLE_NAMES:
            problems.append(f"unknown role {role!r}")
        elif column not in columns:
            problems.append(f"role {role!r} names column {column!r}, which the file does not have")
    if prop.derive and prop.derive.get("denominator") not in columns:
        problems.append(f"the ratio's denominator {prop.derive.get('denominator')!r} is not "
                        "a column of the file")
    for column in [f["column"] for f in rowfilter.normalise(prop.row_filter)]:
        if column not in columns:
            problems.append(f"filter names column {column!r}, which the file does not have")

    if prop.kind == "choropleth":
        if "key" not in prop.roles and "name" not in prop.roles:
            problems.append("a choropleth needs a key or name column to join on")
        if "value" not in prop.roles:
            problems.append("a choropleth needs a value column")
    elif prop.kind == "points":
        embedded_geometry = (prop.format or "").lower() in {"geojson", "shapefile"}
        if not embedded_geometry and not {"lon", "lat"} <= set(prop.roles):
            problems.append("points need lon and lat columns (or a GeoJSON file)")
    elif prop.kind == "lines":
        if ((prop.format or "").lower() not in {"geojson", "shapefile"}
                and not {"lon", "lat"} <= set(prop.roles)):
            problems.append("lines need GeoJSON geometry, lon/lat plus end_lon/end_lat, "
                            "or ordered lon/lat columns")
        if ("end_lon" in prop.roles) != ("end_lat" in prop.roles):
            problems.append("end_lon and end_lat come together")
    else:
        problems.append(f"unknown kind {prop.kind!r}")

    for text in (prop.title, prop.publisher, prop.licence):
        if _INJECTION.search(text or ""):
            problems.append("source metadata contains instruction-like text; not trusted")
            break

    if not prop.licence or prop.licence.strip().lower() in ("", "unknown", "none"):
        prop.licence = LICENCE_UNSTATED
    if prop.licence == LICENCE_UNSTATED:
        warnings.append("the source states no licence; the map will say so")

    result_coverage = None
    if not problems and column_values is not None:
        if prop.derive and prop.derive.get("denominator") in columns:
            share = _numeric_share(column_values(prop.derive["denominator"]))
            if share < MIN_NUMERIC_SHARE:
                problems.append(f"denominator column {prop.derive['denominator']!r} is only "
                                f"{share:.0%} numeric")
        for role in ("value", "size"):
            if role in prop.roles:
                share = _numeric_share(column_values(prop.roles[role]))
                if share < MIN_NUMERIC_SHARE:
                    problems.append(f"{role} column {prop.roles[role]!r} is only "
                                    f"{share:.0%} numeric")
        if prop.kind == "choropleth" and coverage is not None:
            # Join on whichever column resolves better: a file's "code" column
            # may be M49 numbers or a national scheme while its names match.
            tried = []
            for role, namespace in (("key", prop.key_namespace), ("name", "name")):
                if role in prop.roles:
                    tried.append(coverage(column_values(prop.roles[role]), namespace,
                                          prop.level, scope))
            result_coverage = max(tried, key=lambda c: _get(c, "ratio", 0) or 0)
            ratio = _get(result_coverage, "ratio", 0) or 0
            if ratio < MIN_CHOROPLETH_COVERAGE:
                problems.append(f"keys join to only {ratio:.0%} of the expected units "
                                f"({_get(result_coverage, 'matched')}/"
                                f"{_get(result_coverage, 'expected')})")

    return Verdict(ok=not problems, problems=problems, coverage=result_coverage,
                   warnings=warnings)


def to_store_record(prop: DatasetProposal, verdict: Verdict, *, description="") -> dict:
    """The row the discovered-sources store keeps for a validated proposal."""
    return {
        "id": prop.id, "url": prop.url, "title": prop.title, "description": description,
        "publisher": prop.publisher, "licence": prop.licence, "format": prop.format,
        "geometry_kind": prop.kind, "key_namespace": prop.key_namespace, "level": prop.level,
        "roles": dict(prop.roles,
                      **({"filter": rowfilter.normalise(prop.row_filter)} if prop.row_filter else {}),
                      **({"scale": prop.scale} if prop.scale != 1 else {}),
                      **({"derive": prop.derive} if prop.derive else {})),
        "units": prop.unit, "vintage": prop.vintage, "coverage": verdict.coverage,
        "content_sha256": prop.content_sha256, "status": "active",
        "validation": {"problems": verdict.problems, "warnings": verdict.warnings},
        "keywords": prop.measure, "query_text": prop.query,
        "landing_page": prop.landing_page,
    }
