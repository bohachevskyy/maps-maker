#!/bin/bash
# Start (or restart) the local SearXNG used by the research agent.
set -euo pipefail
cd "$(dirname "$0")"
docker rm -f maps-searxng >/dev/null 2>&1 || true
docker run -d --name maps-searxng -p 127.0.0.1:8888:8080 \
  -v "$PWD/settings.yml:/etc/searxng/settings.yml:ro" \
  -e SEARXNG_BASE_URL=http://127.0.0.1:8888/ searxng/searxng:latest >/dev/null
for i in $(seq 1 30); do curl -sf -o /dev/null "http://127.0.0.1:8888/healthz" && { echo "SearXNG up on :8888"; exit 0; }; sleep 2; done
echo "SearXNG did not come up; docker logs maps-searxng" >&2; exit 1
