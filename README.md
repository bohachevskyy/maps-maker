# mapsvc

A small HTTP service that takes a JSON manifest and returns an SVG choropleth
map. One dataset, one map type. Three endpoints: `POST /map` renders a manifest,
`POST /describe` assembles one from a sentence, and `POST /export` does either
and writes the result to a file.

Geometry, projection and SVG generation are hand-written standard library —
no GeoPandas, GDAL, matplotlib or shapely.

## Run

```bash
uv sync
./run.sh
```

`run.sh` sources `.env`, frees the port if something is already on it, and runs
in the foreground so Ctrl-C stops it. **Restarting is just running it again** —
it clears the old process first.

```bash
./run.sh              # foreground on :8000
./run.sh 8080         # a different port
./run.sh --reload     # restart automatically when the code changes
```

To stop one you started elsewhere: `pkill -f "uvicorn mapsvc.api:app"`.

Without `run.sh`:

```bash
set -a; source .env; set +a
uv run uvicorn mapsvc.api:app --port 8000
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

`POST /map` returns `200 image/svg+xml`, or `422` with the offending field
named:

```json
{"error": "SUBREGION is nominal, so a sequential ramp like 'YlGnBu' would imply an ordering between categories; use a qualitative ramp (Dark2, Paired, Set2, Set3)", "field": "ramp"}
```

## Describe a map in words

Put the key in `.env` (gitignored) and source it before starting the server:

```bash
echo 'OPENAI_API_KEY=sk-...' > .env
set -a; source .env; set +a
uv run uvicorn mapsvc.api:app
```

```bash
curl -X POST localhost:8000/describe \
  -H 'content-type: application/json' \
  -d '{"prompt": "how rich is each country in Europe?"}'
```

```json
{
  "manifest": {"region": "europe", "variable": {"id": "GDP_MD", ...}, "normalize": "POP_EST", ...},
  "svg": "<?xml version=\"1.0\"...",
  "reasoning": "\"how rich\" implies a rate, so GDP_MD is normalised by POP_EST."
}
```

The manifest comes back alongside the map so you can see what the agent chose,
disagree with it, edit it, and POST it straight to `/map`.

`OPENAI_MODEL` selects the model; it defaults to `gpt-5.6-terra`. Without
`OPENAI_API_KEY`, `/describe` returns `503` and `/map` is unaffected.

**The agent is not trusted.** Its output goes through the same `validate()` as a
hand-written manifest. The response schema is generated from `registry.py`, so
Structured Outputs makes an unknown variable, ramp, method, projection or `k`
structurally impossible to emit — add a variable to the registry and the agent
can use it with no prompt edits. What a JSON schema cannot express is the
cross-field rule that a nominal variable rejects a sequential ramp; when the
validator catches that, the rejection is handed back to the model for one repair
attempt.

**It refuses rather than substitutes.** The registry has six variables and
customers will ask for a seventh. "Rainfall in Europe" returns `422` naming what
does exist, instead of a GDP map with a caveat nobody reads. Likewise there are
no sub-continental regions: "Scandinavia" is not a `CONTINENT` or an `ADM0_A3`
code.

Unlike `/map`, `/describe` is **not deterministic** — the same prompt may yield a
different manifest.

There is deliberately no GET route, no listing endpoint and no schema route.
To turn the FastAPI docs back on, pass `docs_url="/docs"` and
`openapi_url="/openapi.json"` in `mapsvc/api.py`.

## Export to a file

`POST /export` accepts whatever `/map` or `/describe` accepts — a bare manifest,
a manifest wrapped in `{"manifest": ...}`, or `{"prompt": "..."}` — renders it,
writes the SVG to disk and returns the path.

```bash
curl -X POST localhost:8000/export -H 'content-type: application/json' \
  -d '{"prompt": "which countries in Africa have the biggest economies?"}'
```

```json
{
  "path": "/srv/mapsvc/out/maps/Africa-GDP_MD-5c89d056a0.svg",
  "manifest": {"region": "Africa", "variable": {"id": "GDP_MD"}, "normalize": null, ...},
  "bytes": 37892,
  "overwrote": false,
  "reasoning": "\u201cBiggest economies\u201d is interpreted as total GDP, filtered to Africa."
}
```

`reasoning` is `null` when you supplied the manifest yourself — there was no
agent, so there is nothing to explain.

**The filename is derived from the manifest**, so the same request always lands
at the same path and calling twice leaves one file rather than two. The hash
covers the *whole* manifest, not the data-relevant subset the harvest cache keys
on: `ramp` and `classify` do not invalidate a fetch, but they do change the
file, so they must change its name. Content is rewritten on every call rather
than skipped when the path exists, so a file cannot go stale after a change to
the renderer.

`MAPSVC_OUTPUT` sets the directory; it defaults to `./out/maps`. Region names
are slugged down to `[A-Za-z0-9_]`, so a manifest asking for
`"region": "../../etc/passwd"` writes `etc-passwd-...svg` inside the output
directory and cannot escape it.

**This returns a path on the server's filesystem**, which is only useful to a
caller that shares it — same host, same container, a mounted volume. Callers
over a network want `/map` or `/describe`, which return the SVG itself.

## Environment

| variable | needed by | default |
|---|---|---|
| `OPENAI_API_KEY` | `/describe` only | none — endpoint returns 503 |
| `OPENAI_MODEL` | `/describe` only | `gpt-5.6-terra` |
| `MAPSVC_CACHE` | optional | `./cache` |
| `MAPSVC_OUTPUT` | `/export` only | `./out/maps` |
| `OVERTURE_RELEASE` | overture basemap | `2026-08-19.0` |

`POST /map` needs no environment at all; it has been verified serving under
`env -i`.

## Manifest

| field | values |
|---|---|
| `region` | `"world"`, a `CONTINENT` name (`europe`, `africa`, …), or an `ADM0_A3` code (`FRA`). A filter, not geography — it also derives the map extent. |
| `level` | `admin_0` … `admin_3`. What each basemap serves differs; variables belong to exactly one level. |
| `basemap` | `{"source": "natural_earth" \| "overture", "detail": "simplified" \| "full"}`. Optional; defaults to Natural Earth. |
| `variable` | `{"source": "natural_earth" \| "owid", "id": ...}`, or `null` for a base map. |
| `normalize` | a column name to divide by, or `null`. Optional. |
| `classify` | `{"method": "quantile" \| "equal_interval" \| "jenks", "k": 3–9}` |
| `ramp` | `YlGnBu`, `YlOrRd`, `Blues`, `Greens`, `PuBuGn` (sequential); `RdBu`, `BrBG` (diverging); `Set2`, `Set3`, `Dark2`, `Paired` (qualitative) |
| `projection` | `"auto"`, `"albers"`, `"mercator"`, `"mollweide"` |
| `missing` | `"hatch"`, `"grey"`, `"exclude"` |

`normalize`, `projection` and `missing` may be omitted; they default to `null`,
`"auto"` and `"hatch"`.

`projection: "auto"` picks by latitude span: Mollweide above 90°, Albers for
mid-latitude regions (centre at or beyond 25°), Mercator otherwise.

## Cartography and statistics are separate

Boundaries and numbers come from different places and are joined on an ISO code.

```
cartography/   polygons, keyed by ISO 3166-2 (or ISO3 at country level)
  natural_earth.py   admin_0, admin_1
  overture.py        admin_0 .. admin_3, queried live from S3 with DuckDB
statistics/    values, keyed the same way
  natural_earth.py   the six country attributes
  owid.py            Our World in Data, 10 curated indicators
harvest.py     the join
```

Adding a data source touches one directory. `basemap` chooses the boundaries,
`variable` chooses what is painted on them, and neither knows about the other.

### Statistics sources

| source | levels | licence | indicators |
|---|---|---|---|
| `natural_earth` | admin_0, admin_1 | public domain | 6 country columns, 4 sub-national |
| `owid` | admin_0 only | **CC BY 4.0** | 10 curated Our World in Data charts |

```bash
curl -X POST localhost:8000/map -H 'content-type: application/json' \
  -d '{"region":"world","level":"admin_0","basemap":{"source":"natural_earth"},
       "variable":{"source":"owid","id":"life-expectancy"},
       "classify":{"method":"quantile","k":6},"ramp":"YlGnBu"}' -o life.svg
```

Our World in Data is read through the Grapher CSV API
(`ourworldindata.org/grapher/<slug>.csv`), which is keyed by ISO3 in a `Code`
column — already the admin_0 join key, so no name matching is involved. Both
licences reach the footnote: the basemap's and the statistics source's.

**Adding an OWID indicator** — take the slug from its
`ourworldindata.org/grapher/<slug>` URL, confirm the CSV's fourth column is the
value you want, and add a row to `OWID_VARIABLES` in `registry.py`. The agent's
JSON schema is generated from that dict, so it can use the new indicator
immediately with no prompt edits.

Three things the OWID API does that will bite you:

- **`csvType=filtered` honours each chart's default country selection.** Asking
  for `life-expectancy` that way returns five countries, not 236. The provider
  fetches the full export instead.
- **`time=latest` is ignored on the full export**, so the latest year per country
  is chosen here rather than by the API.
- **Several series carry projections.** `population-density` and `median-age` run
  to 2100. Observations are capped at the current year so a forecast is never
  mapped as though it were measured.

A fourth: the CDN answers **403 without a User-Agent**.

Countries a source has no row for are dropped with reason `no_join` and hatched
— 17 of 242 for life expectancy, mostly small territories.

### Basemaps

| source | levels | licence | speed |
|---|---|---|---|
| `natural_earth` (default) | admin_0, admin_1 | public domain | instant, cached on disk |
| `overture` | admin_0 – admin_3 | **ODbL 1.0** | ~10 s per country, **no cache yet** |

```bash
curl -X POST localhost:8000/map -H 'content-type: application/json' \
  -d '{"region":"UKR","level":"admin_2","variable":null,
       "basemap":{"source":"overture"}}' -o raions.svg
```

Overture is queried straight off `s3://overturemaps-us-west-2` as Hive-partitioned
GeoParquet — no bulk download, DuckDB reads only the row groups a query touches.
Its data is **ODbL**, so the footnote carries the attribution automatically; that
is a licence obligation, not decoration.

**It is slow, and the numbers are worth knowing before you use it:**

| request | time | SVG |
|---|---|---|
| Ukraine admin_1 (27 oblasts) | ~12 s | 3.2 MB |
| Ukraine admin_2 (151 raions) | ~15 s | 7.2 MB |
| Europe admin_0 (45 countries) | **~156 s** | 12.4 MB |

Continent-scale Overture requests are not practical until the cache lands.
Single countries are usable now.

### Base maps

`"variable": null` draws boundaries with no shading and no legend — the only
honest option at admin_2 and admin_3, where no statistics exist. The footnote is
still mandatory.

### Joining across providers

Overture polygons with Natural Earth statistics works, keyed on ISO3:

```json
{"region":"europe","level":"admin_0","basemap":{"source":"overture"},
 "variable":{"source":"natural_earth","id":"GDP_MD"},"normalize":"POP_EST",
 "classify":{"method":"quantile","k":5},"ramp":"YlGnBu"}
```

The footnote then credits both. Below country level the join key is ISO 3166-2;
at admin_2 and below **no standard code exists**, so statistics cannot be joined
there and the harvester will not pretend otherwise.

## Admin-1: sub-national units

`level: "admin_1"` maps the units inside a country — Ukraine's 25 oblasts, the
50 US states, Brazil's 27.

```bash
curl -X POST localhost:8000/map -H 'content-type: application/json' \
  -d '{"region":"UKR","level":"admin_1",
       "variable":{"source":"natural_earth","id":"type"},
       "classify":{"method":"quantile","k":3},"ramp":"Set2"}' -o ukraine.svg
```

**There are no statistics at this level.** Of the 121 properties on an admin-1
unit, not one is population, GDP or income, and `area_sqkm` is `0` for every
unit on earth. The four mappable variables are classification and cartographic
prominence:

| variable | level | notes |
|---|---|---|
| `type` | nominal | `Oblast'`, `Voivodeship`, `State`, `Rada`… — native term |
| `type_en` | nominal | `Region`, `Municipality`, `Province` — English |
| `region` | nominal | sub-national grouping; **null for 53% of units**, including all of Ukraine |
| `labelrank` | ordinal | cartographic prominence |

So an admin-1 map shows *where* the units are and *what kind* they are. Anything
richer — population by oblast, GDP by state — needs a second data source joined
on `iso_3166_2`, which this service does not do.

Two constraints worth knowing:

- **Admin-1 is pinned to 10m** regardless of `SCALE`. The 50m admin-1 file
  carries only 294 units across nine large countries (RUS, USA, IND, IDN, CHN,
  BRA, CAN, AUS, ZAF) and contains no Ukrainian oblasts at all. The 10m file is
  a **39 MB** one-time download.
- **`region` must be `"world"` or a country code** at admin-1 — these features
  carry no `CONTINENT`. `"world"` means 4,596 units and a very large SVG.

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

`SCALE` in `mapsvc/registry.py` selects the geometry. Nothing in the manifest
controls border detail — this constant does.

| scale | download | features | France | Norway | Europe SVG |
|---|---|---|---|---|---|
| `110m` | 819 KB | 177 | 74 pts | 88 pts | 34 KB |
| `50m` (current) | 2.9 MB | 242 | 817 pts | 1,985 pts | 311 KB |
| `10m` | 13 MB | 249 | 4,641 pts | 15,817 pts | ~2 MB |

At `110m` a map of Europe is visibly faceted — Norway loses every fjord and
twelve countries (Malta, Monaco, Andorra, Liechtenstein, San Marino, Vatican,
Gibraltar, the Channel Islands, Faeroes, Isle of Man, Åland) are absent from the
file entirely. `50m` is the useful default for regional maps; `10m` is worth it
only when mapping one or two countries.

The scale is part of the harvest cache key, so changing it re-fetches and
re-harvests correctly. Deleting `cache/` is not required.

## Design notes

**Five seams.** `describe → validate → (cartography + statistics) → render`, of which the first is
optional and the other three are the original service. `/export` adds a write
after the last one; every endpoint is a different entry point into the same
chain, not a parallel implementation. `render.py` imports nothing that
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

162 tests, no network and no API key: the harvester's fetch and the OpenAI
client are both stubbed, and the renderer runs against a hand-written 5-feature
fixture in `tests/fixtures/`, which carries a polygon with a hole, a
MultiPolygon, and both sentinel values. Running the suite costs nothing.
