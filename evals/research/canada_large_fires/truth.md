# canada_large_fires (tune)

Prompt: Show the wildfires larger than 200 hectares in Canada in 2023

Primary source: CWFIS National Fire Database (NFDB) point dataset

Exact calls:

- `https://cwfis.cfs.nrcan.gc.ca/downloads/nfdb/fire_pnt/current_version/NFDB_point_txt.zip  (25 MB; filter YEAR=2023, SIZE_HA>=200)`

Vintage: release 2026-08-11
Licence: Open Government Licence - Canada (cwfis downloads/licence.txt); citation required
Rows in truth.csv: 965
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- 965 fires: BC 219, QC 187, NT 154, YT 105, SK 90, AB 71, ON 50, MB 47, PC 32, NL 7, NS 2, NB 1.
- Trap in the source: NFDB_point_large_fires_txt.zip sounds like the answer but contains only BC (3,175 rows over all years; 215 in 2023). A proposal using it fails min_coverage.
- No key. Directory listings are plain nginx indexes.
