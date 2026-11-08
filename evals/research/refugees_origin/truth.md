# refugees_origin (heldout)

Prompt: Refugees by country of origin

Primary source: UNHCR Refugee Data Finder API

Exact calls:

- `https://api.unhcr.org/population/v1/population/?year=2024&coo_all=true&limit=1000`

Vintage: end-2024
Licence: CC BY 4.0
Rows in truth.csv: 207
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- One aggregated row per origin (coa="-"). Field `refugees` only.
