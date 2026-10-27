# Held-out scores

Measured at checkpoints only; never used for tuning (CLAUDE.md, "Held-out evals").

## Checkpoint 1 -- code of tuning round 9 (2026-10-26), agent gpt-5.6-terra

| map | score | top defect |
|---|---|---|
| south_america_life_expectancy | 3 | Jenks lumps 6 of 12 into the top class; French Guiana/Falklands unvalued, footnote says 0 excluded |
| poland_voivodeship_population | 3 | Wikidata series: 2014, mixed years, 5.9% median error vs GUS 2025 |
| us_median_age | 4 | data exact; bottom class lumps TX/ND/UT/AK, DC unlabelled, mixed codes/names |
| italy_gdp_regions | 4 | data exact; Trento unlabelled, crossing leaders, busy labels |
| japan_quakes_2024 | 3 | frame misses the south: 69 of 96 events, Bonin M6+ missing; Noto pile unreadable |
| london_underground | 3 | "Central Line"/"Central line" counted twice (12 lines); core hairball, no inset |
| asia_coal_electricity | 3 | Russia counted as Asia stretches the frame; Taiwan/HK/Macau/Palestine absent, footnote says 0 excluded |
| pacific_typhoons_2024 | 1 | refused: catalogue has IBTrACS for the North Atlantic only |
| europe_nuclear_plants | 1 | refused: no plant source found (agent did not try OSM power=plant) |
| zakarpattia_settlements | 1 | Overpass outage (upstream) |

**Mean 2.6 over 10; 3.29 over the 7 rendered.** Tuning set at the same code: 4.05.
Reading: rendering skills transfer partially (3-4 on familiar map types); data discovery
does not (2 of 3 non-maps are catalogue gaps). The tuning-set score overstates general quality.

## Checkpoint 2 -- after the general-mechanism work (2026-10-27), agent gpt-5.6-terra

| map | cp1 | cp2 | note |
|---|---|---|---|
| south_america_life_expectancy | 3 | 4 | French Guiana has no basemap shape (named in footnote) |
| poland_voivodeship_population | 3 | 3 | still stale Wikidata (no official Polish source registered) |
| us_median_age | 4 | 4 | DC invisible on the main map |
| italy_gdp_regions | 4 | 4 | Trento unlabelled |
| japan_quakes_2024 | 3 | 3 | frame still loses 27 of 96 events |
| london_underground | 3 | 1 | Overpass outage (route relations have no Overture fallback) |
| pacific_typhoons_2024 | 1 | 4 | now maps (IBTrACS any basin); Atlantic wording on a typhoon map |
| zakarpattia_settlements | 1 | 1 | Overpass query matched 0 features (new bug, not an outage) |
| asia_coal_electricity | 3 | 4 | Russia now context; territories present |
| europe_nuclear_plants | 1 | 2 | right tag now; no capacity sizing, Russia-wide frame, plants drawn as "lines" |

**Mean 3.0 over 10 (3.38 over 8 rendered)**, up from 2.6. This set has now informed two rounds of
decisions and is retired; held-out set 2 continues the measurement.
