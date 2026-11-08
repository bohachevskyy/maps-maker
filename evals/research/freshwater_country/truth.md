# freshwater_country (heldout)

Prompt: Renewable freshwater resources per person by country

Primary source: World Bank API ER.H2O.INTR.PC (FAO AQUASTAT)

Exact calls:

- `https://api.worldbank.org/v2/country/all/indicator/ER.H2O.INTR.PC?format=json&per_page=20000&date=2000:2025&mrnev=1`

Vintage: 2022
Licence: CC BY 4.0
Rows in truth.csv: 183
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Cubic metres per person per year. Strongly skewed (Iceland 445,023; Kuwait 0): needs a log or quantile scale.
- World Bank Open Data API v2 (keyless JSON), data from FAO AQUASTAT; most-recent-non-empty value per country (mrnev=1); aggregates (regions, income groups) dropped using /v2/country region.id=NA.
