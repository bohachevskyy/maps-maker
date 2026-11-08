# refugees_asylum (tune)

Prompt: Refugee population by country of asylum

Primary source: UNHCR Refugee Data Finder API /population/v1/population

Exact calls:

- `https://api.unhcr.org/population/v1/population/?year=2024&coa_all=true&limit=1000`

Vintage: end-2024 (the API also serves 2025, but it is 28.5M against 31.0M and looked partial; 2024 chosen as the unambiguous full year)
Licence: CC BY 4.0 (UNHCR)
Rows in truth.csv: 181
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Field `refugees` only (UNHCR mandate refugees); asylum_seekers, idps, stateless are separate columns that must not be summed in.
- With coa_all=true and no coo filter the API returns one aggregated row per country of asylum (coo="-"). Adding coo_all=true as well returns coa x coo pairs.
- Colombia 1,306 is hosted refugees, not the 77,775 Colombians abroad (that is the origin view, case refugees_origin).
