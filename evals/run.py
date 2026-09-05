"""Run the agent against evals/cases.toml and report what it got wrong.

    uv run python -m evals.run                  every case, once
    uv run python -m evals.run --repeat 3       three times each, to expose flakiness
    uv run python -m evals.run gdp oblast       only cases whose name matches

Needs OPENAI_API_KEY. Each case is one model call, so a full pass costs a few
cents. This is deliberately not part of `pytest`: the unit suite must stay
offline and free.
"""

import argparse
import collections
import pathlib
import sys
import tomllib

from mapsvc.agent import AgentError, AgentUnavailable, describe, model_name

CASES = pathlib.Path(__file__).parent / "cases.toml"

# Fields compared directly against the manifest.
DIRECT = {
    "level": "level", "variable": "variable_id",
    "basemap": "basemap_source", "detail": "basemap_detail",
    "source": "variable_source",
}
NULL = "__null__"
RESERVED = ({"name", "prompt", "refuse", "mentions", "contains", "excludes",
             "max_span"} | set(DIRECT))


class Failure(Exception):
    pass


def _bbox(manifest) -> str:
    return "[" + ",".join(f"{v:g}" for v in manifest.bbox) + "]"


def load_cases(patterns: list[str]) -> list[dict]:
    cases = tomllib.loads(CASES.read_text())["case"]
    for i, case in enumerate(cases):
        case.setdefault("name", f"case {i + 1}")
        unknown = set(case) - RESERVED
        if unknown:
            raise SystemExit(f"case {case['name']!r}: unknown key(s) {sorted(unknown)}")
    if patterns:
        cases = [c for c in cases
                 if any(p.lower() in c["name"].lower() for p in patterns)]
    return cases


def _matches(expected, actual) -> bool:
    """Expected may be a single value or a list meaning 'any of'."""
    options = expected if isinstance(expected, list) else [expected]
    for option in options:
        if option == NULL:
            if actual is None:
                return True
        elif isinstance(option, str) and isinstance(actual, str):
            if option.lower() == actual.lower():
                return True
        elif option == actual:
            return True
    return False


def check(case: dict, manifest, reasoning: str) -> None:
    """Raise Failure on the first expectation this manifest does not meet."""
    for key, attribute in DIRECT.items():
        if key in case:
            actual = getattr(manifest, attribute)
            if not _matches(case[key], actual):
                raise Failure(f"{key}: expected {case[key]!r}, got {actual!r}")

    # The window is asserted by what it covers, not by exact numbers: two
    # sensible bboxes for "the Balkans" differ by degrees and both are right.
    west, south, east, north = manifest.bbox
    for lon, lat in case.get("contains", []):
        if not (west <= lon <= east and south <= lat <= north):
            raise Failure(f"bbox {_bbox(manifest)} does not contain ({lon}, {lat})")
    for lon, lat in case.get("excludes", []):
        if west <= lon <= east and south <= lat <= north:
            raise Failure(f"bbox {_bbox(manifest)} should not contain ({lon}, {lat})")
    if "max_span" in case:
        span = max(east - west, north - south)
        if span > case["max_span"]:
            raise Failure(
                f"bbox spans {span:.1f} degrees, wider than {case['max_span']} "
                f"-- too coarse a window for the request")

    if case.get("mentions"):
        haystack = reasoning.lower()
        for needle in case["mentions"]:
            if needle.lower() not in haystack:
                raise Failure(f"reasoning does not mention {needle!r}: {reasoning[:120]!r}")


def run_once(case: dict) -> tuple[bool, str]:
    wants_refusal = case.get("refuse", False)
    try:
        manifest, _, reasoning = describe(case["prompt"])
    except AgentError as error:
        if not wants_refusal:
            return False, f"refused unexpectedly: {error}"
        for needle in case.get("mentions", []):
            if needle.lower() not in str(error).lower():
                return False, f"refusal does not mention {needle!r}: {error}"
        return True, str(error)[:100]

    if wants_refusal:
        return False, (f"expected a refusal, got {manifest.variable_id} "
                       f"at {manifest.level} for {_bbox(manifest)}")
    try:
        check(case, manifest, reasoning)
    except Failure as failure:
        return False, str(failure)
    return True, (f"{manifest.basemap_source}/{manifest.level} {_bbox(manifest)} "
                  f"{manifest.variable_id or 'base map'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patterns", nargs="*", help="only run cases matching these")
    parser.add_argument("--repeat", type=int, default=1,
                        help="runs per case; >1 exposes non-determinism")
    args = parser.parse_args()

    cases = load_cases(args.patterns)
    if not cases:
        print("no cases matched")
        return 1

    print(f"{len(cases)} case(s) x {args.repeat} against {model_name()}\n")
    results: dict[str, list[tuple[bool, str]]] = collections.OrderedDict()

    for case in cases:
        outcomes = []
        for _ in range(args.repeat):
            try:
                outcomes.append(run_once(case))
            except AgentUnavailable as exc:
                print(f"cannot run: {exc}")
                return 2
        results[case["name"]] = outcomes
        passed = sum(1 for ok, _ in outcomes if ok)
        if passed == args.repeat:
            mark = "pass"
        elif passed == 0:
            mark = "FAIL"
        else:
            mark = "FLAKY"
        tally = f" {passed}/{args.repeat}" if args.repeat > 1 else ""
        print(f"  {mark:5}{tally}  {case['name']}")
        for ok, detail in outcomes:
            if not ok:
                print(f"           {detail}")

    total = len(results)
    clean = sum(1 for o in results.values() if all(ok for ok, _ in o))
    flaky = sum(1 for o in results.values()
                if any(ok for ok, _ in o) and not all(ok for ok, _ in o))
    print(f"\n{clean}/{total} passed" + (f", {flaky} flaky" if flaky else ""))
    return 0 if clean == total else 1


if __name__ == "__main__":
    sys.exit(main())
