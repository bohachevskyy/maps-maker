# control_eurostat_unemp (heldout)

Prompt: Unemployment rate in each region of the EU

Primary source: Eurostat tgs00010

Exact calls:

- `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/tgs00010?format=JSON&lang=en&time=2024`

Vintage: 2024 (updated 2026-10-30)
Licence: CC BY 4.0 (Eurostat reuse policy)
Rows in truth.csv: 286
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Total, both sexes, 15-74. 286 NUTS2 values (4-character geo codes).
