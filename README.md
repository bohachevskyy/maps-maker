# mapsvc

A small HTTP service that takes a JSON manifest and returns an SVG choropleth
map. One dataset, one map type, one endpoint.

Geometry, projection and SVG generation are hand-written standard library —
no GeoPandas, GDAL, matplotlib or shapely.

## Run

```bash
uv sync
uv run uvicorn mapsvc.api:app
```

## Make a map

```bash
curl -X POST localhost:8000/map \
  -H 'content-type: application/json' \
  -d '{
    "region": "europe",
    "level": "admin_0",
    "variable": {"source": "natural_earth", "id": "GDP_MD"},
    "normalize": "POP_EST",
    "classify": {"method": "quantile", "k": 5},
    "ramp": "YlGnBu",
    "projection": "auto",
    "missing": "hatch"
  }' -o europe.svg
```

The same manifest is in `examples/europe_gdp.json`:

```bash
curl -X POST localhost:8000/map -H 'content-type: application/json' \
  -d @examples/europe_gdp.json -o europe.svg && open europe.svg
```

`POST /map` is the only endpoint. It returns `200 image/svg+xml`, or `422` with
the offending field named:

```json
{"error": "SUBREGION is nominal, so a sequential ramp like 'YlGnBu' would imply an ordering between categories; use a qualitative ramp (Dark2, Paired, Set2, Set3)", "field": "ramp"}
```

There is deliberately no GET route, no listing endpoint and no schema route.
To turn the FastAPI docs back on, pass `docs_url="/docs"` and
`openapi_url="/openapi.json"` in `mapsvc/api.py`.

## Manifest

| field | values |
|---|---|
| `region` | `"world"`, a `CONTINENT` name (`europe`, `africa`, …), or an `ADM0_A3` code (`FRA`). A filter, not geography — it also derives the map extent. |
| `level` | `"admin_0"` only. Other values are rejected. |
| `variable` | `{"source": "natural_earth", "id": "GDP_MD"}`. Only `natural_earth` is valid so far; the shape is fixed so stored manifests survive a second source. |
| `normalize` | a column name to divide by, or `null`. Optional. |
| `classify` | `{"method": "quantile" \| "equal_interval" \| "jenks", "k": 3–9}` |
| `ramp` | `YlGnBu`, `YlOrRd`, `Blues`, `Greens`, `PuBuGn` (sequential); `RdBu`, `BrBG` (diverging); `Set2`, `Set3`, `Dark2`, `Paired` (qualitative) |
| `projection` | `"auto"`, `"albers"`, `"mercator"`, `"mollweide"` |
| `missing` | `"hatch"`, `"grey"`, `"exclude"` |

`normalize`, `projection` and `missing` may be omitted; they default to `null`,
`"auto"` and `"hatch"`.

`projection: "auto"` picks by latitude span: Mollweide above 90°, Albers for
mid-latitude regions (centre at or beyond 25°), Mercator otherwise.

## Adding a variable

Add a key to `VARIABLES` in `mapsvc/registry.py`. Anything not a key there is
rejected by the validator.

```python
VARIABLES = {
    ...
    "LABELRANK": {"level": "ordinal", "unit": None, "year_col": None},
}
```

| key | meaning |
|---|---|
| `level` | `count`, `ordinal` or `nominal`. Drives classification and ramp validation. |
| `unit` | shown in the legend and the footnote, or `None`. |
| `year_col` | the sibling column holding the vintage, or `None`. Its value goes into the footnote automatically. |

`level` is the part that matters:

- **`count`** — graded bins via the chosen method. `-99` and `0` are treated as
  no-data.
- **`ordinal`** — numeric columns get graded bins; prefixed-string columns like
  `"4. Lower middle income"` get one class per category, ordered by the numeric
  prefix. `-99` is no-data; `0` is kept, because a rank of zero could be real.
- **`nominal`** — one class per category, and **sequential and diverging ramps
  are rejected**. Shading unordered categories light-to-dark asserts that one
  is "more" than another. Use a qualitative ramp.

The variable must exist as a property on the Natural Earth features; there is
no join, so nothing else is required.

## Switching scale

`SCALE` in `mapsvc/registry.py` is `"110m"` (838 KB, 177 features). Set it to
`"50m"` or `"10m"` and delete `cache/` to re-fetch. The URL is derived from it.

## Design notes

**Three seams.** `validate → harvest → render`. `render.py` imports nothing that
performs I/O, and `tests/test_no_io.py` enforces that by walking the import
graph rather than trusting convention.

**The extent is not a naive bounding box.** Two corrections, both needed for a
recognisable regional map:

- Only each feature's *largest* ring defines the extent. Otherwise France
  reaches the extent to French Guiana, Norway to Bouvet Island and Portugal to
  the Azores, and a map of Europe becomes a map of the Atlantic.
- Rings clipped at the antimeridian are skipped. Natural Earth files all of
  Russia under `CONTINENT='Europe'`, so its ring touches 180°E and alone
  stretches Europe across 205° of longitude. Such features are still drawn and
  still coloured; they are clipped by the viewport rather than defining it.
  When what survives already wraps most of the way round, the extent snaps to
  the whole globe, so a world map does not clip Antarctica.

Ring longitudes are unwrapped onto a continuous branch before projecting. A
vertex sitting exactly on ±180 would otherwise jump to the far edge of the
canvas and draw a spike across the map.

**One scale factor** is applied to both axes when fitting to the canvas. Two
would stretch the map and destroy the projection's area properties. The
letterboxing on a world map is the correct consequence of that.

**Missing values are never the bottom bin.** They are hatched or greyed, and
appear in the legend as "no data".

**The footnote is mandatory** and states source, scale, projection,
classification method and k, the variable's vintage, and how many features were
excluded and why. Where a column's year varies per feature — `GDP_YEAR` spans
1999–2019 in the 10m file — the footnote prints the range.

**Output is deterministic.** Rows are drawn in sorted id order, coordinates are
formatted to a fixed precision, and nothing carries a timestamp. The same
manifest returns byte-identical bytes.

**Caching.** The source GeoJSON is fetched once into `cache/`. Harvest results
are cached under a hash of the *data-relevant* manifest fields only — `region`,
`level`, `variable`, `normalize` — so changing `ramp` or `classify` re-renders
without re-fetching. Set `MAPSVC_CACHE` to move the directory.

### On the `-99` / `0` filter

Both are Natural Earth no-data markers and both are filtered before the value
domain is computed, then routed through `missing` handling. The reason is that
the Vatican's `GDP_MD = -99` would otherwise be shaded as the lowest *real*
value on the map, and a `POP_EST` of `0` becomes a silent divide-by-zero when
used as a `normalize` column.

It is worth being precise about what this filter does *not* fix. Measured
against the real 10m file, the sentinels barely move an `equal_interval`
domain — world `GDP_MD` already spans 10 to 21,433,226, so bin counts are
`[246,1,0,1,1]` with the sentinels and `[234,1,0,1,1]` without them. The
compression comes from the United States, not from the sentinels. That skew is
what `jenks` and `quantile` are for.

### On `normalize`

`normalize` divides, and nothing more — there is no rule relating it to the
variable's measurement level. `GDP_MD` normalised by `POP_EST` therefore yields
*million USD per person*, not dollars, and the legend says so. Bin ordering is
identical to GDP per capita either way.

## Tests

```bash
uv run pytest
```

80 tests, no network: the harvester's fetch is stubbed and the renderer runs
against a hand-written 5-feature fixture in `tests/fixtures/`, which carries a
polygon with a hole, a MultiPolygon, and both sentinel values.
