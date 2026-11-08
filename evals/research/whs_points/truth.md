# whs_points (tune)

Prompt: Map all UNESCO World Heritage sites, coloured by cultural or natural

Primary source: UNESCO Open Data portal, dataset whc001 (World Heritage List)

Exact calls:

- `https://data.unesco.org/api/explore/v2.1/catalog/datasets/whc001/exports/json?select=id_no,name_en,category,iso_codes,date_inscribed,danger,geo_point_2d`

Vintage: dataset modified 2026-11-02; sites inscribed to 2026
Licence: CC BY-SA 4.0
Rows in truth.csv: 1271
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- The obvious URL whc.unesco.org/en/list/xml (and /en/list/xml/) returns a Cloudflare 403 "Just a moment" page to non-browser clients: a candidate the service cannot fetch. The data.unesco.org Opendatasoft portal is the keyless route.
- The portal CSV export has multi-line fields and mis-parses with Python csv (928 of 1273 rows); the JSON export is clean. 1273 sites, 1271 with coordinates.
- Wikidata P1435=Q9259 is a poor substitute: 3,516 rows incl. 1,409 distinct items (component parts).
