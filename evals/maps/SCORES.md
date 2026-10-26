# Scoreboard

Critic scores (1–5, overall) per round; see results/index.html for the maps.
Target: mean ≥ 4.0, none below 3, stable over --repeat 3.

| map | r0 | r1 | r2 | r3 | r4 |
|---|---|---|---|---|---|
| gdp_world | 1* | 3 | 3 | 4 | 4 |
| ukraine_oblast_population | 1 | 3 | 3 | 4 | 4 |
| us_diesel | 1 | 2 | 3 | 4 | 4 |
| eu_nuts2_gdp | 1 | 2 | 4 | 4 | 4 |
| ca_county_income | 1 | 3 | 3 | 3 | 4 |
| population_change | 1 | 3 | 3 | 4 | 4 |
| france_lgv | 1 | 1 | 4 | 4 | 4 |
| napoleon_1812 | 1 | 1 | 4 | 4 | 4 |
| quakes_2025 | 1 | 1 | 4 | 4 | 4 |
| eu_renewables | 1* | 3 | 4 | 4 | 4 |
| **mean** | **1.0** | **2.2** | **3.5** | **3.9** | **4.0** |

\* r0 rendered but was not critiqued; missing USA/Russia and the wrong indicator respectively.

## r1 → r2 work (done)
agent per-capita/PPS/within; render follow-ups (I/O invariant, insets, context land, simplification 20-80x smaller, titles); overlays (usgs, overpass, web/Minard); Overpass time budget; NE ids are ISO codes.
r2 france_lgv is a retry: the first attempt hit an Overpass outage (504 + timeouts, budget exhausted cleanly at 300 s).

## r2 → r3 work (done; r3 auto checks 10/10)
- classification: rounded/log/diverging/0-100 breaks (behind the top defect on 7/10 maps)
- labels: names and values with collision avoidance and leader lines
- footnote boilerplate on overlay maps; rivers, plate boundaries; inset grid; OSM slugs in legends
- r3 also fixed: NE GDP in millions (per-capita read 0.11), agent deriving GDP per capita instead of using OWID, Ukraine on a 25-unit basemap, a too-literal footnote check.

## r3 → r4 work (done)
- labels: name+value always together or a leader-lined callout; halos; Bay Area / NE US / central Europe; context names (seas, neighbours, rivers); Crimea and Sevastopol separately
- basemap detail: Census 1:500k for counties; thinner outlines, no rings on sub-pixel units; lighter 0-20% class
- insets inside the frame; EU inset scale bars; plate-boundary legend; Minard temperature panel; title hints (PPP, heating oil)

## Round 4: target met
Mean 4.0, none below 3; automatic checks 30/30 over --repeat 3. Across repeats the
agent produced identical manifests for 8/10 maps; population_change varied only
in basemap detail (byte-identical SVG) and us_diesel between the Overture and
Census basemaps (both scored 4).

No map reaches 5. Open defects, from the round-4 critic:
1. leaders: long and crossing on 6 maps; mixed label styles (names vs codes)
2. callout selection: population_change labels micro-states (Vatican, N. Marianas)
3. determinism/titles: pin the basemap per source; "Distillate fuel oil total
   consumption per person, United States of America" is clumsy; pipeline phrasing
   in footnotes ("39 no matching row")
4. reference anchors: EU27=100 scale, Napoleon dates, Sevastopol outline,
   Dijon/Mulhouse labels, ACS margin-of-error note
5. layout: crop frames to data (Napoleon, EU), merge repeated credit lines

## Cheaper agent models (2026-10-07)
Same code, same prompts, `--repeat 3`, reasoning effort low, via the Codex CLI
(`MAPSVC_CODEX_MODEL`). A run whose SVG is byte-identical to gpt-5.6-sol's
round 4 inherits its score (4); every differing run was critiqued separately.

| model | identical to sol | auto checks | mean (30 runs) | worst map | wall time |
|---|---|---|---|---|---|
| gpt-5.6-sol (baseline) | — | 30/30 | 4.00 | 4 | — |
| gpt-5.6-terra | 29/30 | 29/30* | 3.97 | 3 (gdp_world, 1 of 3 runs) | 623 s |
| gpt-5.6-luna | 25/30 | 27/30 | 3.67 | 1 (france_lgv, 0 of 3 runs) | 612 s |
| gpt-6-luna | 24/30 | 27/30 | 3.67 | 1 (france_lgv, 2 of 3 runs) | 690 s |

\* the failure was a service bug (Overture country ids were UUIDs), fixed; the re-rendered map scores 3 because Overture's admin_0 omits Greenland, Hong Kong, Macao, Puerto Rico and Bermuda.

What cheaper models got wrong, all of it in places where the manifest leaves them freedom:
- OSM tag filters: `service=highspeed` (matches nothing), `usage=main` (60,340 ways, refused), `highspeed=*` (288 extra ways)
- a bbox added to a `within` map: dropped Alaska/Hawaii (us_diesel) or Del Norte/San Joaquin/Stanislaus labels (ca_county_income)
- the wrong measure: OWID low-carbon (incl. nuclear) instead of renewables — France 95% vs 26%; spot checks could not catch it
- basemap: Overture for a world map (missing territories)

## Round 5 (2026-10-08): 21 maps, agent gpt-5.6-terra

| map | r5 |
|---|---|
| africa_internet | 3 |
| atlantic_hurricanes_2024 | 3 |
| ca_county_income | 4 |
| canada_population_change | 4 |
| eu_nuts2_gdp | 4 |
| eu_renewables | 4 |
| europe_largest_cities | 3 |
| france_lgv | 4 |
| gdp_world | 4 |
| germany_density_nuts3 | 3 |
| napoleon_1812 | 4 |
| population_change | 4 |
| quakes_2025 | 4 |
| texas_county_unemployment | 4 |
| tokyo_subway | 1 |
| trans_siberian | 4 |
| ukraine_oblast_population | 4 |
| us_diesel | 4 |
| us_election_2024 | 4 |
| volcanoes_holocene | 3 |
| volyn_settlements | 3 |

Set 1 mean 4.0 (unchanged from sol), set 2 mean 3.18 incl. tokyo_subway=1 (refused during an Overpass outage), overall 3.57.
Automatic checks 20/21 (Texas after the BLS fix).

## Round 6 (2026-10-08): 21 maps, terra; auto checks 21/21

| map | r5 | r6 |
|---|---|---|
| africa_internet | 3 | 4 |
| atlantic_hurricanes_2024 | 3 | 4 |
| ca_county_income | 4 | 4 |
| canada_population_change | 4 | 4 |
| eu_nuts2_gdp | 4 | 4 |
| eu_renewables | 4 | 4 |
| europe_largest_cities | 3 | 4 |
| france_lgv | 4 | 4 |
| gdp_world | 4 | 4 |
| germany_density_nuts3 | 3 | 4 |
| napoleon_1812 | 4 | 4 |
| population_change | 4 | 4 |
| quakes_2025 | 4 | 4 |
| texas_county_unemployment | 4 | 4 |
| tokyo_subway | 1 | 2 |
| trans_siberian | 4 | 4 |
| ukraine_oblast_population | 4 | 4 |
| us_diesel | 4 | 4 |
| us_election_2024 | 4 | 2 |
| volcanoes_holocene | 3 | 3 |
| volyn_settlements | 3 | 3 |

Set 1 4.00, set 2 3.45, overall 3.71. Below 3: us_election_2024, tokyo_subway.

## Round 7 (2026-10-25): 21 maps, terra; auto checks 21/21 (5 Overture maps re-run after the release fix)

| map | r6 | r7 |
|---|---|---|
| africa_internet | 4 | 4 |
| atlantic_hurricanes_2024 | 4 | 4 |
| ca_county_income | 4 | 4 |
| canada_population_change | 4 | 4 |
| eu_nuts2_gdp | 4 | 4 |
| eu_renewables | 4 | 4 |
| europe_largest_cities | 4 | 3 |
| france_lgv | 4 | 4 |
| gdp_world | 4 | 4 |
| germany_density_nuts3 | 4 | 4 |
| napoleon_1812 | 4 | 4 |
| population_change | 4 | 4 |
| quakes_2025 | 4 | 4 |
| texas_county_unemployment | 4 | 4 |
| tokyo_subway | 2 | 3 |
| trans_siberian | 4 | 4 |
| ukraine_oblast_population | 4 | 4 |
| us_diesel | 4 | 4 |
| us_election_2024 | 2 | 4 |
| volcanoes_holocene | 3 | 3 |
| volyn_settlements | 3 | 3 |

Set 1 4.00, set 2 3.64, overall 3.81. None below 3. At 3: europe_largest_cities, tokyo_subway, volcanoes_holocene, volyn_settlements.

## Round 8 (2026-10-26): 21 maps, terra -- TARGET MET on a single run

Crimea drawn as Ukraine (UN GA 68/262); callouts capped at 70 px with ranked side tables.

| map | r7 | r8 |
|---|---|---|
| africa_internet | 4 | 4 |
| atlantic_hurricanes_2024 | 4 | 4 |
| ca_county_income | 4 | 5 |
| canada_population_change | 4 | 4 |
| eu_nuts2_gdp | 4 | 4 |
| eu_renewables | 4 | 4 |
| europe_largest_cities | 3 | 4 |
| france_lgv | 4 | 4 |
| gdp_world | 4 | 4 |
| germany_density_nuts3 | 4 | 4 |
| napoleon_1812 | 4 | 4 |
| population_change | 4 | 4 |
| quakes_2025 | 4 | 4 |
| texas_county_unemployment | 4 | 4 |
| tokyo_subway | 3 | 3 |
| trans_siberian | 4 | 4 |
| ukraine_oblast_population | 4 | 5 |
| us_diesel | 4 | 4 |
| us_election_2024 | 4 | 4 |
| volcanoes_holocene | 3 | 4 |
| volyn_settlements | 3 | 4 |

Set 1 4.20, set 2 3.91, overall 4.05; lowest 3. Auto checks 21/21 (Trans-Siberian after a runner fix). Stability over --repeat 3: pending.

## Stability, round-8 code x 3 repeats (2026-10-26)
59/63 runs passed the automatic checks; 12 maps byte-identical to round 8 in all 3 runs.
Runs whose SVG matched round 8 inherit its score; the 13 that differed were critiqued.
Mean over the 61 runs that produced a map: 4.02, none below 3 (two 3s: tokyo, one us_election run).
Two runs produced no map: germany run-02 (agent refused without searching -> fixed: a first
refusal is now challenged with real search hits) and tokyo run-03 (Overpass outage, upstream;
reported as 503). Counting both as 1: 3.92 over 63.
Variance came from look-only agent choices (bbox beside within, overture vs census for US states,
admin_0 vs admin_1 under an overlay) -- being made derived (fix/determinism).

## Round 9 (2026-10-26): 21 maps x 3 repeats, determinism + search-before-refusal

62/63 runs produced a map; mean 4.05, lowest 3. 16/21 maps byte-identical to round 8 in all 3 runs. No-map runs: tokyo_subway/run-02 (Overpass outage, upstream). Variants: volyn run-03 dropped to 3 (agent filtered place=town|village, losing Lutsk); others held their scores.
