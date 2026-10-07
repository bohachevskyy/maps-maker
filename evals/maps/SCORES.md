# Scoreboard

Critic scores (1–5, overall) per round; see results/index.html for the maps.
Target: mean ≥ 4.0, none below 3, stable over --repeat 3.

| map | r0 | r1 | r2 |
|---|---|---|---|
| gdp_world | 1* | 3 | 3 |
| ukraine_oblast_population | 1 | 3 | 3 |
| us_diesel | 1 | 2 | 3 |
| eu_nuts2_gdp | 1 | 2 | 4 |
| ca_county_income | 1 | 3 | 3 |
| population_change | 1 | 3 | 3 |
| france_lgv | 1 | 1 | 4 |
| napoleon_1812 | 1 | 1 | 4 |
| quakes_2025 | 1 | 1 | 4 |
| eu_renewables | 1* | 3 | 4 |
| **mean** | **1.0** | **2.2** | **3.5** |

\* r0 rendered but was not critiqued; missing USA/Russia and the wrong indicator respectively.

## r1 → r2 work (done)
agent per-capita/PPS/within; render follow-ups (I/O invariant, insets, context land, simplification 20-80x smaller, titles); overlays (usgs, overpass, web/Minard); Overpass time budget; NE ids are ISO codes.
r2 france_lgv is a retry: the first attempt hit an Overpass outage (504 + timeouts, budget exhausted cleanly at 300 s).

## r2 → r3 work
- classification: rounded/log/diverging/0-100 breaks (behind the top defect on 7/10 maps)
- labels: names and values with collision avoidance and leader lines
- footnote boilerplate on overlay maps; rivers, plate boundaries; inset grid; OSM slugs in legends
