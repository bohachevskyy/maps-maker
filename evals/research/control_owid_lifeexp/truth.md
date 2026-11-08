# control_owid_lifeexp (tune)

Prompt: Life expectancy at birth for every country

Primary source: OWID grapher life-expectancy (CC BY 4.0)

Exact calls:

- `https://ourworldindata.org/grapher/life-expectancy.csv?v=1&csvType=full&useColumnShortNames=true`

Vintage: latest year per country, 2023
Licence: CC BY 4.0
Rows in truth.csv: 236
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Truth is the latest row per entity with a non-OWID_ ISO code (236 rows incl. territories).
- Pass = zero research-agent searches and proposals; the registry path answers.
