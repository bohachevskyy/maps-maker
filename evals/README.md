# Agent evals

Prompts, and the manifest each one should produce.

```bash
set -a; source ../.env; set +a
uv run python -m evals.run                 # every case, once
uv run python -m evals.run --repeat 5      # expose non-determinism
uv run python -m evals.run oblast ramp     # only matching case names
```

Exit code is 0 only when every case passes, so this drops into CI unchanged —
though note it costs real money and needs `OPENAI_API_KEY`. It is deliberately
**not** part of `pytest`: the unit suite stays offline and free.

## Adding a case

Append to `cases.toml`. Assert only what the prompt actually determines —
asserting every field would test the model's taste rather than its correctness,
and would be flaky for no benefit.

```toml
[[case]]
name = "population by oblast refuses on the variable, not the place"
prompt = "population of each Ukrainian oblast"
refuse = true
mentions = ["oblast"]
```

| key | meaning |
|---|---|
| `level` `region` `variable` `normalize` `method` `projection` `missing` `ramp` | exact value, or a list meaning "any of". Strings compare case-insensitively. |
| `normalize = "__null__"` | must NOT be normalised |
| `k` | exact integer or list |
| `ramp_kind` | `sequential` / `diverging` / `qualitative` — better than pinning an exact ramp |
| `refuse` | the agent must decline |
| `mentions` | substrings the refusal or reasoning must contain |

Unknown keys are an error rather than being silently ignored, so a typo in an
assertion fails loudly instead of quietly weakening the case.

## Use `--repeat` before trusting a green run

The model is non-deterministic. A single pass can hide a case that only works
two times in three; `--repeat` reports `FLAKY n/N` for those. This is not
hypothetical — it is how the known-flaky ramp-override case was found, after a
single run had reported it as passing.
