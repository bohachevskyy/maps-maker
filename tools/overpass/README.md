# Local Overpass API

A development-only Overpass server (wiktorn/overpass-api, native arm64) so that
overlay queries answer in milliseconds instead of minutes, with no public-server
rate limits. It serves only the countries listed in `coverage.json`; every other
query still goes to the public endpoints.

## Use

```bash
tools/overpass/up.sh              # start (or create) the container on 127.0.0.1:12345
export MAPSVC_OVERPASS_LOCAL=http://127.0.0.1:12345/api/interpreter
```

`mapsvc/overlays/overpass.py` tries the local server first when the variable is
set and the window is covered: every `within` country is in `coverage.json`, and
a `bbox` lies inside one extract's box. On any local error (down, non-JSON,
runtime error) the public endpoints are used as before. Responses carry the
local snapshot's `osm3s.timestamp_osm_base`, so the footnote says "OpenStreetMap
data as of <snapshot date>" (ODbL attribution unchanged). Coverage boxes are
approximate -- a bbox that reaches a neighbouring country that was not loaded is
answered only partly; keep bboxes inside loaded countries.
`MAPSVC_OVERPASS_COVERAGE` points at a different coverage file (used by tests).

## What is loaded

See `coverage.json` (snapshot 2026-11-07, Geofabrik extracts). Currently Ukraine
only: 884 MB PBF -> 7.9 GB database. Data lives in the Docker volume
`maps-overpass-db`, downloads in `~/overpass-data/pbf/`, nothing in the repo.
No diff feed is configured: the snapshot never changes until re-imported.

## Add a region

1. Download `https://download.geofabrik.de/<region>-latest.osm.pbf` into
   `~/overpass-data/pbf/`.
2. Merge with all existing extracts (osmium is in the image):
   `docker run --rm -v ~/overpass-data/pbf:/pbf --entrypoint osmium wiktorn/overpass-api merge /pbf/a.osm.pbf /pbf/b.osm.pbf -o /pbf/combined.osm.pbf`
3. `PBF=combined.osm.pbf tools/overpass/up.sh --wipe` (import: ~30 min per GB of PBF
   measured on this machine, ~9 GB of database per GB of PBF).
4. Add the extract (bbox from `osmium fileinfo -g header.boxes`, ISO alpha-2
   `countries`) to `coverage.json`.

Areas (`area["ISO3166-1"=...]`) are generated at import, so `within` queries work.
After an import the image leaves `/db` mode 700, which makes every query fail with
"Permission denied"; `up.sh` runs `chmod 755 /db` once the import is done.
