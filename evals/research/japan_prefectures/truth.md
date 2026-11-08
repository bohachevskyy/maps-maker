# japan_prefectures (tune)

Prompt: Population of every prefecture in Japan

Primary source: e-Stat Statistics Dashboard API (keyless), Population Estimates (Statistics Bureau)

Exact calls:

- `https://dashboard.e-stat.go.jp/api/1.0/Json/getData?IndicatorCode=0201010000000010000&RegionalRank=3&Lang=EN&TimeFrom=2024CY00&TimeTo=2024CY00`

Vintage: Population Estimates, 1 Oct 2024
Licence: Government of Japan standard terms of use (compatible with CC BY 4.0)
Rows in truth.csv: 47
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Trap (wrong_units): Replay fixture must include the Statistics Bureau "Population Estimates" Table 1 page/CSV in thousands of persons (Tokyo 14,178), listed ahead of the Dashboard API. The proposal must carry a unit/scale so Tokyo maps to 14.178 million, not 14,178; a proposal that leaves scale unstated or wrong fails spot JP-13.

Caveats:
- The main e-Stat API (api.e-stat.go.jp) requires an appId; verified it answers "authentication failed". The Dashboard API is the keyless route.
- Values are in persons, rounded to the nearest thousand by the Bureau. The Bureau's own published tables are in thousands (千人).
- Region code 01000..47000 maps to JP-01..JP-47.
