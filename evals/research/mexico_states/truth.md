# mexico_states (tune)

Prompt: Share of people without health service coverage in each Mexican state

Primary source: INEGI Censo de Poblacion y Vivienda 2020, ITER national CSV

Exact calls:

- `https://www.inegi.org.mx/contenidos/programas/ccpv/2020/datosabiertos/iter/iter_00_cpv2020_csv.zip`

Vintage: Census 2020 (15 Mar 2020)
Licence: INEGI Terminos de Libre Uso de la Informacion (free reuse with attribution)
Rows in truth.csv: 32
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- 36.6 MB zip. State rows: MUN=000 and LOC=0000 in conjunto_de_datos_iter_00CSV20.csv. value = PSINDER / POBTOT * 100.
- INEGI's indicator API (app/api/indicadores) needs a token; the ITER file is keyless.
- INEGI state codes 01..32 are not ISO 3166-2 (MX-CMX = 09). The 32nd unit is Ciudad de Mexico.
