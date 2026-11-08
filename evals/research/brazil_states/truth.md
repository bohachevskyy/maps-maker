# brazil_states (tune)

Prompt: Population of each Brazilian state

Primary source: IBGE SIDRA API table 4714 (Censo Demografico 2022)

Exact calls:

- `https://apisidra.ibge.gov.br/values/t/4714/n3/all/v/93/p/last`

Vintage: Census 2022
Licence: IBGE open data (no explicit SPDX; Brazil Lei de Acesso a Informacao / open data policy). must_have_licence=false because the API states none.
Rows in truth.csv: 27
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- IBGE codes (11..53) are not ISO 3166-2; the agent must map them (BR-SP = 35). Distrito Federal (53) is the 27th unit.
- Values come back as strings.
