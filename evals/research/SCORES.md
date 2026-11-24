# Research-agent eval scores

`uv run python -m evals.research.run --split tune --repeat 2` and `--split heldout`.
Replayed web (`MAPSVC_REPLAY=cached`, local SearXNG), live model (Codex backend), so
two repeats of twelve cases is a small sample: one case flipping moves the tune
score by 8 points. Held-out results are scores only.

## Read these with three caveats

- **The trap tests have not run.** No trap fixture exists yet (see README), so
  `japan_prefectures`, `meteorites_fell`, `canada_provinces` and `airports_africa`
  measure finding the data, not resisting a planted page or a wrong scale.
- **The registry may already answer two held-out cases.** `hurricane_tracks_2005`:
  the `web` overlay catalogue lists NOAA IBTrACS (every tropical cyclone, 1842 to
  present). `airports_africa`: the same catalogue lists Natural Earth airports. Their
  `need` text says the registry has neither. The cases are left unchanged for now;
  read them as possibly controls rather than research cases.
- `need` text is also loose elsewhere: `statcan` is registered (2016/2021 census,
  so `canada_provinces`' 2025 minimum still forces a search), and `wikidata` covers
  first-level population everywhere (the publisher check still requires the
  official source).

## Results

| run | code | tune | held-out |
|---|---|---|---|
| 2026-11-07 | before f2771e3 | 15/24, mean check fraction 73% | 4/8, 70% |
| 2026-11-23 | 1b09c7f (after f2771e3) | 12/24, 65% | 3/8, 68% |

Tune, passes out of 2:

| case | 11-07 | 11-23 |
|---|---|---|
| precip_country | 2 | 2 |
| refugees_asylum | 0 | 0 |
| brazil_states | 2 | 0 |
| japan_prefectures | 0 | 0 |
| mexico_states | 0 | 0 |
| whs_points | 2 | 2 |
| meteorites_fell | 2 | 2 |
| tornado_tracks | 1 | 0 |
| canada_large_fires | 0 | 0 |
| control_owid_lifeexp | 2 | 2 |
| refuse_dprk_villages | 2 | 2 |
| refuse_afghanistan_crime | 2 | 2 |

The 11-23 drop is in `brazil_states` and `tornado_tracks`. Brazil, run 0 found the
right IBGE table (27/27 joined) but declared its key as state names rather than
ISO 3166-2; run 1 chose a different IBGE workbook with duplicate rows and was
rejected by validation. Both are agent choices, not f2771e3 (which changes only how
search-hit URLs with `#` are matched). Not fixed here.

Held-out, pass (P) or fail (F):

| case | 11-07 | 11-23 |
|---|---|---|
| freshwater_country | P | F |
| refugees_origin | F | F |
| canada_provinces | P | P |
| australia_states | F | F |
| airports_africa | F | F |
| hurricane_tracks_2005 | F | F |
| control_eurostat_unemp | P | P |
| refuse_havana_rents | P | P |
