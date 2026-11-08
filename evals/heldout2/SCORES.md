# Held-out set 2 scores

Never used for tuning (CLAUDE.md, "Held-out evals").

## Checkpoint 1 -- baseline (2026-10-27), agent gpt-5.6-terra

| map | score |
|---|---|
| south_asia_literacy | 4 |
| german_states_population | 3 |
| turkey_quakes_2023 | 3 |
| madrid_metro | 3 |
| ivano_frankivsk_settlements | 3 |
| spain_solar_plants | 2 |
| atlantic_hurricanes_2017 | 4 |
| latin_america_fertility | 3 |
| spain_unemployment_regions | 4 |
| iceland_volcanoes | 1 |

**Mean 3.0 over 10 (3.22 over 9 rendered).** Tuning set at comparable code: ~4.

## Checkpoint 2 -- research agent (2026-11-08), agent gpt-5.6-terra, branch feat/research-agent

| map | baseline | now |
|---|---|---|
| south_asia_literacy | 4 | 4 |
| german_states_population | 3 | 4 |
| turkey_quakes_2023 | 3 | 3 |
| madrid_metro | 3 | 3 |
| ivano_frankivsk_settlements | 3 | 4 |
| spain_solar_plants | 2 | 3 |
| atlantic_hurricanes_2017 | 4 | 1 (no map: AgentError) |
| latin_america_fertility | 3 | 1 (no map: CartographyError) |
| spain_unemployment_regions | 4 | 4 |
| iceland_volcanoes | 1 | 4 |

**Mean 3.1 over 10 (3.63 over the 8 rendered); baseline 3.0 (3.22 over 9).** The two
no-map runs are suspected regressions; investigated on the tuning set only.
Research eval held-out split at the same code: 4/8 pass, mean check fraction 70%.
