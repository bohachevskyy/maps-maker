#!/usr/bin/env bash
# Build the static frontend for GitHub Pages.
#
#   tools/build_pages.sh https://mapsvc.up.railway.app ./pages-out
#
# The frontend only knows the public API URL; there are no secrets in the output.
set -euo pipefail
[[ $# -eq 2 ]] || { echo "usage: $0 <api_base_url> <out_dir>" >&2; exit 2; }
API_BASE="${1%/}"
OUT="$2"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$HERE/mapsvc/web"

mkdir -p "$OUT/static"
cp "$WEB/index.html" "$OUT/index.html"
cp "$WEB/app.js" "$WEB/app.css" "$OUT/static/"
# JSON-encode the URL so a stray quote cannot break out of the string.
printf 'window.MAPSVC_API_BASE = %s;\n' \
  "$(printf '%s' "$API_BASE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
  > "$OUT/static/config.js"
touch "$OUT/.nojekyll"
echo "built $OUT (API base: $API_BASE)"
