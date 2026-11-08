# australia_states (heldout)

Prompt: Population of each Australian state and territory

Primary source: ABS Data API (SDMX), dataflow ABS_ANNUAL_ERP_ASGS2021

Exact calls:

- `https://data.api.abs.gov.au/rest/data/ABS,ABS_ANNUAL_ERP_ASGS2021/ERP.STE..A?startPeriod=2024&endPeriod=2024  (Accept: application/vnd.sdmx.data+csv)`

Vintage: ERP at 30 June 2024
Licence: CC BY 4.0 (ABS)
Rows in truth.csv: 9
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Needs the Accept header for CSV. A wrong key returns 404 NoRecordsFound or 422 "Not enough key values".
- State codes 1..8 -> NSW,VIC,QLD,SA,WA,TAS,NT,ACT; code 9 is Other Territories (4,995), not an ISO 3166-2 subdivision, truth keeps it as "OT" (8 mappable units).
