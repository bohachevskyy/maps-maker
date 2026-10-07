# Scoreboard

Critic scores (1–5, overall) per round; see results/index.html for the maps.
Target: mean ≥ 4.0, none below 3, stable over --repeat 3.

| map | r0 | r1 |
|---|---|---|
| gdp_world | 1* | 3 |
| ukraine_oblast_population | 1 | 3 |
| us_diesel | 1 | 2 |
| eu_nuts2_gdp | 1 | 2 |
| ca_county_income | 1 | 3 |
| population_change | 1 | 3 |
| france_lgv | 1 | 1 |
| napoleon_1812 | 1 | 1 |
| quakes_2025 | 1 | 1 |
| eu_renewables | 1* | 3 |
| **mean** | **1.0** | **2.2** |

\* r0 rendered but was not critiqued; missing USA/Russia and the wrong indicator respectively.

## r1 → r2 work
- agent: per-capita for totals, PPS for GDP, `within` for named regions (codex, fix/agent-fidelity)
- render follow-ups: I/O invariant, insets, context land, honest footnote, titles (fix/render-followups)
- overlays: usgs points, overpass lines, web/Minard flows, city labels (feat/overlays)
- next: round-number / log-like class breaks, feature labels with collision avoidance
