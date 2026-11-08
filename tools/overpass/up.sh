#!/bin/bash
# Start the local Overpass API (wiktorn/overpass-api) on 127.0.0.1:12345.
#
#   ./up.sh                    # serve the existing database (import if none)
#   PBF=ukraine.osm.pbf ./up.sh   # first run only: import ~/overpass-data/pbf/$PBF
#   ./up.sh --wipe             # delete the database volume, then start fresh
#
# Idempotent: running container -> nothing; stopped -> started; absent -> created.
# The database lives in the Docker volume maps-overpass-db, never in the repo.
# The first run imports (minutes for Ukraine, hours for a multi-country merge);
# poll `docker logs maps-overpass` until "Starting supervisord process".
set -euo pipefail
NAME=maps-overpass
VOLUME=maps-overpass-db
PBF_DIR="${OVERPASS_PBF_DIR:-$HOME/overpass-data/pbf}"
PBF="${PBF:-combined.osm.pbf}"
PORT="${OVERPASS_PORT:-12345}"

if [ "${1:-}" = "--wipe" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  echo "wiped $VOLUME"
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = "true" ]; then
  echo "$NAME already running on :$PORT"; exit 0
fi
if docker inspect "$NAME" >/dev/null 2>&1; then
  docker start "$NAME" >/dev/null; echo "$NAME started on :$PORT"; exit 0
fi
[ -f "$PBF_DIR/$PBF" ] || { echo "no $PBF_DIR/$PBF (only needed for the first import)" >&2; }
docker run -d --name "$NAME" --restart unless-stopped \
  -p "127.0.0.1:$PORT:80" \
  -v "$VOLUME:/db" -v "$PBF_DIR:/pbf:ro" \
  -e OVERPASS_MODE=init \
  -e "OVERPASS_PLANET_URL=file:///pbf/$PBF" \
  -e OVERPASS_PLANET_PREPROCESS='mv /db/planet.osm.bz2 /db/planet.osm.pbf && osmium cat -o /db/planet.osm.bz2 /db/planet.osm.pbf && rm /db/planet.osm.pbf' \
  -e OVERPASS_META=no \
  -e OVERPASS_COMPRESSION=gz \
  -e OVERPASS_USE_AREAS=true \
  -e OVERPASS_RULES_LOAD=1 \
  -e OVERPASS_STOP_AFTER_INIT=false \
  -e OVERPASS_MAX_TIMEOUT=1000s \
  -e OVERPASS_ALLOW_DUPLICATE_QUERIES=yes \
  wiktorn/overpass-api:latest >/dev/null
echo "$NAME created; importing if the volume is empty. Wait for 'Starting supervisord process' in: docker logs $NAME"
# The image leaves /db mode 700 after an import, so fcgiwrap (user nginx) cannot
# reach the dispatcher socket: "open64: 13 Permission denied". Wait for the
# import to finish, then open the directory.
(for i in $(seq 1 100000); do
   docker exec "$NAME" chmod 755 /db 2>/dev/null && docker exec "$NAME" test -f /db/init_done && break
   sleep 20
 done) >/dev/null 2>&1 &
