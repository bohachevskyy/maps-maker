# Scoreboard

Critic scores (1–5, overall) per round; see results/index.html for the maps.
Target: mean ≥ 4.0, none below 3, stable over --repeat 3.

| map | r0 | r1 | r2 | r3 |
|---|---|---|---|---|
| gdp_world | 1* | 3 | 3 | 4 |
| ukraine_oblast_population | 1 | 3 | 3 | 4 |
| us_diesel | 1 | 2 | 3 | 4 |
| eu_nuts2_gdp | 1 | 2 | 4 | 4 |
| ca_county_income | 1 | 3 | 3 | 3 |
| population_change | 1 | 3 | 3 | 4 |
| france_lgv | 1 | 1 | 4 | 4 |
| napoleon_1812 | 1 | 1 | 4 | 4 |
| quakes_2025 | 1 | 1 | 4 | 4 |
| eu_renewables | 1* | 3 | 4 | 4 |
| **mean** | **1.0** | **2.2** | **3.5** | **3.9** |

\* r0 rendered but was not critiqued; missing USA/Russia and the wrong indicator respectively.

## r1 → r2 work (done)
agent per-capita/PPS/within; render follow-ups (I/O invariant, insets, context land, simplification 20-80x smaller, titles); overlays (usgs, overpass, web/Minard); Overpass time budget; NE ids are ISO codes.
r2 france_lgv is a retry: the first attempt hit an Overpass outage (504 + timeouts, budget exhausted cleanly at 300 s).

## r2 → r3 work (done; r3 auto checks 10/10)
- classification: rounded/log/diverging/0-100 breaks (behind the top defect on 7/10 maps)
- labels: names and values with collision avoidance and leader lines
- footnote boilerplate on overlay maps; rivers, plate boundaries; inset grid; OSM slugs in legends
- r3 also fixed: NE GDP in millions (per-capita read 0.11), agent deriving GDP per capita instead of using OWID, Ukraine on a 25-unit basemap, a too-literal footnote check.

## r3 → r4 work
- labels: name+value always together or a leader-lined callout; halos; Bay Area / NE US / central Europe; context names (seas, neighbours, rivers); Crimea and Sevastopol separately
- basemap detail: Census 1:500k for counties; thinner outlines, no rings on sub-pixel units; lighter 0-20% class
- insets inside the frame; EU inset scale bars; plate-boundary legend; Minard temperature panel; title hints (PPP, heating oil)
