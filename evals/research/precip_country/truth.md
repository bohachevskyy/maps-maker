# precip_country (tune)

Prompt: Average annual precipitation for every country in the world

Primary source: World Bank API AG.LND.PRCP.MM (FAO AQUASTAT)

Exact calls:

- `https://api.worldbank.org/v2/country/all/indicator/AG.LND.PRCP.MM?format=json&per_page=20000&date=2000:2025&mrnev=1`
- `https://api.worldbank.org/v2/country?format=json&per_page=400  (to drop aggregates)`

Vintage: 2022 (every country; AQUASTAT long-term averages, lastupdated 2026-10-08)
Licence: CC BY 4.0 (World Bank Open Data)
Rows in truth.csv: 182
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Units mm/yr. Egypt 18.1, Saudi Arabia 59: desert values are real, not missing.
- World Bank Open Data API v2 (keyless JSON), data from FAO AQUASTAT; most-recent-non-empty value per country (mrnev=1); aggregates (regions, income groups) dropped using /v2/country region.id=NA.
