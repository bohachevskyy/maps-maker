# Working agreement

**Discuss the approach before implementing it.** Research and measure freely —
empirical numbers are what make a design conversation useful — then lay out the
options, the trade-offs and a recommendation, and stop. Wait for agreement
before writing code.

Dmytro is a long-tenured software engineer and is steering the architecture
deliberately. He has redirected designs that were already built more than once:
a curated OWID indicator list became a live search API, `region` became `bbox`,
`bbox` then gained `within`. Every one of those rebuilds was avoidable with a
five-minute conversation first.

"Let's implement it", "ok, let's do it" or similar is the go-ahead. Absent that,
present and wait.

**Verify rather than assert.** Nearly every real bug in this project was found by
measuring, not by reasoning: Alaska's bounding box spanning 358.9°, OWID's
`csvType=filtered` silently returning five countries, Overture's `region` column
meaning the *parent* below admin_1. Check the data before believing the docs, and
say what was measured.

**Report failures plainly.** If something is slow, wrong or unbuilt, say so with
the numbers. Do not smooth over a known weakness — the eval suite deliberately
keeps a failing case visible rather than deleting it.

---

# The project

An HTTP service that turns a request into an SVG map. Hand-written geometry and
SVG — **no GeoPandas, GDAL, matplotlib or shapely.**

```
POST /map       manifest JSON        -> image/svg+xml
POST /describe  {"prompt": "..."}    -> {manifest, svg, reasoning}
POST /export    either of the above  -> {path, ...}, writes the SVG to disk
```

## Layout

```
mapsvc/
  manifest.py    validate(raw) -> Manifest. Runs before any I/O.
  cartography/   polygons, keyed for joining. natural_earth, overture.
  statistics/    values, keyed the same way. natural_earth (fixed), owid (searchable).
  harvest.py     the join, plus caching and the granularity guard.
  render.py      SVG. Must not import anything that does I/O.
  agent.py       prompt -> manifest, via OpenAI Structured Outputs.
  project.py classify.py colors.py    pure; no I/O.
evals/           prompts and the manifests they should produce. Costs money.
```

## Invariants worth not breaking

- **`render.py` performs no I/O.** `tests/test_no_io.py` walks the import graph
  to enforce it. A second test checks that the check itself still detects a
  violation.
- **Output is deterministic.** Sorted rows, fixed coordinate precision, no
  timestamps. The same manifest returns byte-identical bytes.
- **The footnote is mandatory** and states source, scale, projection,
  classification, vintage and how many features were excluded and why. Licences
  travel with the data: Overture is ODbL, OWID is CC BY 4.0, and both must be
  credited when used.
- **Missing values are never the bottom bin.** They are hatched.
- **Nothing about rendering is in the manifest.** Ramp, classification,
  projection and missing-value handling are derived. That is deliberate: it
  makes "nominal variable with a sequential ramp" unrepresentable rather than
  merely validated.
- **The agent is never trusted.** Its output goes through the same `validate()`
  as a hand-written manifest, with one repair attempt. A fixed source's variable
  id must be in its catalogue; a searchable source's must be one the live search
  actually returned.

## Traps that have already bitten

- **The antimeridian, three times.** A unit straddling 180° is stored with
  `xmin ≈ -180, xmax ≈ 180`, so its bounding box intersects every window on
  earth — Alaska's Unorganized Borough kept arriving on a 3° map of Kyiv. Boxes
  wider than 180° are excluded. Ring longitudes are also unwrapped onto a
  continuous branch before projecting.
- **`bbox.xmin BETWEEN …` is not an intersection test.** It asks whether a
  unit's *corner* is in the window, so a box drawn inside Ukraine returns no
  country at all.
- **Overture's `region` column is the unit's own ISO 3166-2 code at admin_1 but
  its parent's below that.** Using it as an id collapses 151 raions onto 27.
- **Stale caches look like broken code.** `CACHE_VERSION` in `harvest.py` must be
  bumped whenever the shape of a cached row changes.
- **Natural Earth's `ISO_A2` is `-99` for eight countries** including France and
  Norway; the `_EH` variants carry the real code. Kosovo is `KOS` or `XK`, never
  `XKX`.
- **Overture is slow at scale**, and the cost is geometry volume rather than
  unit count. The 800-unit guard does not catch a 21 MB Balkans map.

## Running it

```bash
uv sync
./run.sh                 # foreground on :8000; re-run to restart
uv run pytest            # 167 tests, offline, no API key, free

set -a; source .env; set +a
uv run python -m evals.run --repeat 3   # live model, costs a few cents
```

`.env` holds `OPENAI_API_KEY` and is gitignored. `POST /map` needs no
environment at all; only `/describe` and prompt-driven `/export` do.

Use `--repeat` before trusting a green eval run — the model is
non-deterministic, and that flag is how a case that passed once but works only
two times in three was found.
