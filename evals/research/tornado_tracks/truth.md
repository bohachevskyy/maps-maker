# tornado_tracks (tune)

Prompt: Show the tornado tracks of the April 25-28, 2011 outbreak, coloured by EF rating

Primary source: NOAA SPC WCM database 1950-2024 actual tornadoes

Exact calls:

- `https://www.spc.noaa.gov/wcm/data/1950-2024_actual_tornadoes.csv`

Vintage: 1950-2024 file (8 MB)
Licence: US Government work, public domain
Rows in truth.csv: 349
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- Rows with date 2011-04-25..28 and elat != 0: 349 (all 349 rows in the window have an end point).
- EF5 tracks: Hackleburg AL (72 deaths), Smithville MS, Rainsville AL; 4 in total.
- The file name carries the end year; a later release will be 1950-2025_actual_tornadoes.csv.
- Track = line from (slon,slat) to (elon,elat); mag is the F/EF rating; some rows repeat the same om (multi-state tracks).
