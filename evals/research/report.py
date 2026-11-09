"""Build evals/research/results/index.html: every research-eval result, browsable.

    uv run python -m evals.research.report

One section per results directory (tune, heldout, archive/*), one row per case
and repeat: pass/fail, the dataset found (linked), publisher, licence, vintage,
coverage, failed checks, the refusal if any, and the full trace on demand.
"""

import html
import json
import pathlib

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results"

STYLE = """
body{font:14px/1.45 -apple-system,system-ui,sans-serif;margin:24px;color:#222;background:#fff}
h1{font-size:22px}h2{font-size:17px;margin-top:32px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e3e3e3;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f6f6f6;position:sticky;top:0}.pass{color:#137a2a;font-weight:600}.fail{color:#b3261e;font-weight:600}
.url{max-width:340px;word-break:break-all;font-size:12px}.small{font-size:12px;color:#555}
details pre{white-space:pre-wrap;font-size:11px;background:#fafafa;padding:8px;max-height:420px;overflow:auto}
@media (prefers-color-scheme: dark){body{background:#151515;color:#ddd}th{background:#222}td,th{border-color:#333}
.small{color:#aaa}details pre{background:#1d1d1d}.pass{color:#6fd08c}.fail{color:#ff8a80}a{color:#8ab4f8}}
"""


def _row(r: dict) -> str:
    rec = r.get("record") or {}
    detail = r.get("detail") or {}
    failed = [k for k, v in (r.get("checks") or {}).items() if not v]
    url = rec.get("url") or ""
    found = (f'<a href="{html.escape(url)}">{html.escape(url)}</a>' if url else "")
    status = "pass" if r.get("passed") else "fail"
    trace = json.dumps(r.get("trace") or [], indent=1, ensure_ascii=False, default=str)
    return (
        f"<tr><td><b>{html.escape(r.get('id', ''))}</b><div class=small>rep {r.get('rep', 0)} · "
        f"{r.get('seconds', '?')} s</div></td>"
        f"<td class={status}>{status.upper()}<div class=small>{r.get('fraction', 0):.0%} of checks</div></td>"
        f"<td class=url>{found}<div class=small>{html.escape(str(rec.get('title') or ''))}</div></td>"
        f"<td class=small>{html.escape(str(rec.get('publisher') or ''))}<br>"
        f"{html.escape(str(rec.get('licence') or ''))}<br>vintage {html.escape(str(rec.get('vintage') or ''))}</td>"
        f"<td class=small>{html.escape(str(detail.get('coverage') or ''))}"
        f"{'<br>count ' + str(detail['count']) if 'count' in detail else ''}</td>"
        f"<td class=small>{html.escape(', '.join(failed))}"
        f"<div>{html.escape((r.get('refusal') or r.get('error') or '')[:400])}</div></td>"
        f"<td><details><summary class=small>trace</summary><pre>{html.escape(trace)}</pre></details></td></tr>")


def _section(title: str, folder: pathlib.Path) -> str:
    runs = []
    for path in sorted(folder.glob("*.json")):
        try:
            runs.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    if not runs:
        return ""
    passed = sum(1 for r in runs if r.get("passed"))
    mean = sum(r.get("fraction", 0) for r in runs) / len(runs)
    head = ("<tr><th>case</th><th>result</th><th>dataset found</th><th>publisher · licence · vintage</th>"
            "<th>coverage</th><th>failed checks / refusal</th><th></th></tr>")
    return (f"<h2>{html.escape(title)} — {passed}/{len(runs)} passed, mean check fraction {mean:.0%}</h2>"
            f"<table>{head}{''.join(_row(r) for r in runs)}</table>")


def build() -> pathlib.Path:
    parts = [_section("Tuning split (latest)", RESULTS / "tune"),
             _section("Held-out split (latest)", RESULTS / "heldout")]
    for old in sorted((RESULTS / "archive").glob("*"), reverse=True):
        if old.is_dir():
            parts.append(_section(f"Archive: {old.name}", old))
    out = RESULTS / "index.html"
    out.write_text("<!doctype html><meta charset=utf-8><title>Research evals</title>"
                   f"<style>{STYLE}</style><h1>Research agent evals</h1>"
                   "<p class=small>Specs: evals/research/specs.toml · how scoring works: "
                   "evals/research/README.md</p>" + "".join(p for p in parts if p))
    return out


if __name__ == "__main__":
    print(build())
