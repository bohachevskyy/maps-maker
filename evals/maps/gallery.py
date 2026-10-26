"""Build evals/maps/results/index.html: every map, reference first, then each round.

    uv run python -m evals.maps.gallery

Columns are the reference map and every results/round<N>/ directory in order.
A round's critique.json, when present, puts its score under the image.
"""

import html
import json
import pathlib
import re
import tomllib

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results"
SPECS = HERE / "specs.toml"


def _rounds() -> list[pathlib.Path]:
    found = [p for p in RESULTS.glob("round*") if p.is_dir()]
    return sorted(found, key=lambda p: int(re.sub(r"\D", "", p.name) or 0))


def _score(run: pathlib.Path, map_id: str) -> str:
    critique = run / map_id / "critique.json"
    if not critique.exists():
        return ""
    data = json.loads(critique.read_text())
    scores = data.get("scores", {})
    detail = " · ".join(f"{k} {v}" for k, v in scores.items())
    return f"<b>{data.get('overall', '?')}</b>/5<br><small>{html.escape(detail)}</small>"


def _cell(image: pathlib.Path, caption: str) -> str:
    if not image.exists():
        return f"<td class=empty>—<div class=cap>{caption}</div></td>"
    rel = image.relative_to(RESULTS.parent).as_posix()
    return (f'<td><a href="../{rel}"><img src="../{rel}" loading=lazy></a>'
            f"<div class=cap>{caption}</div></td>")


def build() -> pathlib.Path:
    specs = tomllib.loads(SPECS.read_text())["map"]
    rounds = _rounds()
    head = "".join(f"<th>{r.name}</th>" for r in rounds)
    rows = []
    for spec in specs:
        map_id = spec["id"]
        cells = [_cell(SPECS.parent / map_id / "reference.png", "reference")]
        for run in rounds:
            png, svg = run / map_id / "map.png", run / map_id / "map.svg"
            auto = run / map_id / "auto.json"
            note = ""
            if auto.exists():
                a = json.loads(auto.read_text())
                if a.get("refused") or a.get("error"):
                    note = html.escape(str(a.get("error") or "refused"))[:240]
            cells.append(_cell(png if png.exists() else svg,
                               _score(run, map_id) or note))
        rows.append(f"<tr><th class=row>{map_id}<div class=cap>"
                    f"{html.escape(spec['prompt'])}</div></th>{''.join(cells)}</tr>")
    page = f"""<!doctype html><meta charset=utf-8><title>Map evals</title>
<style>
body{{font:14px -apple-system,Helvetica,sans-serif;margin:16px;background:#fff;color:#222}}
table{{border-collapse:collapse}} td,th{{border:1px solid #ddd;padding:6px;vertical-align:top}}
img{{width:420px;display:block}} .cap{{font-size:12px;color:#555;max-width:420px;margin-top:4px}}
th.row{{width:160px;text-align:left}} td.empty{{color:#aaa;text-align:center;width:200px}}
</style>
<h1>Reference maps vs. each round</h1>
<table><tr><th>map</th><th>reference</th>{head}</tr>{''.join(rows)}</table>"""
    out = RESULTS / "index.html"
    RESULTS.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:                      # e.g. evals/heldout/specs.toml
        SPECS = pathlib.Path(sys.argv[1]).resolve()
        RESULTS = SPECS.parent / "results"
    print(build())
