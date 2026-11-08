# hurricane_tracks_2005 (heldout)

Prompt: Map the tracks of the 2005 Atlantic hurricane season

Primary source: IBTrACS v04r01, North Atlantic basin CSV (NOAA NCEI)

Exact calls:

- `https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/v04r01/access/csv/ibtracs.NA.list.v04r01.csv  (57 MB; SEASON=2005, TRACK_TYPE=main)`

Vintage: v04r01, season 2005
Licence: NOAA public data, citation requested
Rows in truth.csv: 908
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Caveats:
- count = storms (distinct SID), 31 incl. unnamed. truth.csv keeps 6-hourly points only (908 rows) as a lighter proxy for the line geometry.
- Katrina at 2005-08-29 12:00 is (-89.6, 29.5), Gulf Coast landfall. Wilma at 2005-10-24 12:00 is (-81.0, 26.2), crossing south Florida.
- 57 MB is large: the research agent should prefer a per-storm or per-season source if one exists (the file has no server-side filter).
