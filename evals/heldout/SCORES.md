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
