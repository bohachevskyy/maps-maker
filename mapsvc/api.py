"""HTTP surface.

    POST /map       body: manifest JSON     ->  200 image/svg+xml
                                                422 {"error": "...", "field": "..."}

    POST /describe  body: {"prompt": "..."} ->  200 {"manifest", "svg", "reasoning"}
                                                422 {"error": "...", "field": "..."}
                                                503 if the agent is not configured

    POST /export    body: either of the above ->  200 {"path", "manifest", ...}
                                                  writes the SVG to disk

The request body is read as a plain dict rather than a Pydantic model on
purpose: manifest.validate is the single source of truth for what a valid
manifest is, and it is the only thing that can name the offending field in the
shape this contract promises.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mapsvc.agent import AgentError, AgentUnavailable, describe
from mapsvc.export import write
from mapsvc.harvest import HarvestError
from mapsvc.manifest import Manifest, ManifestError, validate
from mapsvc.pipeline import build_map, draw
from mapsvc.render import RenderError

FAILURES = (AgentError, ManifestError, HarvestError, RenderError)

# No docs, no schema route: the spec asks for one endpoint and nothing else.
# Set docs_url="/docs", openapi_url="/openapi.json" to get them back.
app = FastAPI(title="mapsvc", docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/map")
async def make_map(request: Request) -> Response:
    body = await request.body()
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        return _rejected(f"request body is not valid JSON: {exc}", "")

    try:
        svg = build_map(raw)
    except (ManifestError, HarvestError, RenderError) as exc:
        return _rejected(str(exc), exc.field)

    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml")


@app.post("/describe")
async def describe_map(request: Request) -> Response:
    """Assemble a manifest from a sentence, then render it.

    The manifest is returned alongside the SVG: the caller should be able to see
    what the agent decided, disagree with it, edit it and POST it to /map.
    """
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return _rejected(f"request body is not valid JSON: {exc}", "")
    if not isinstance(payload, dict):
        return _rejected('request body must be an object of the form '
                         '{"prompt": "..."}', "")

    try:
        manifest, raw, reasoning = describe(payload.get("prompt"))
        svg = draw(manifest)
    except AgentUnavailable as exc:
        return JSONResponse(status_code=503, content={"error": str(exc), "field": ""})
    except FAILURES as exc:
        return _rejected(str(exc), exc.field)

    return JSONResponse(
        status_code=200,
        content={"manifest": raw, "svg": svg, "reasoning": reasoning},
    )


@app.post("/export")
async def export_map(request: Request) -> Response:
    """Render to a file and return its path.

    Accepts whatever /map or /describe accepts -- a manifest, or a prompt -- so
    a caller does not have to know which of the two produced the request.

    The path is on the server's filesystem, which is only useful to a caller
    that shares it (same host, same container, mounted volume). Callers over a
    network want /map or /describe instead.
    """
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return _rejected(f"request body is not valid JSON: {exc}", "")
    if not isinstance(payload, dict):
        return _rejected("request body must be a manifest object or "
                         '{"prompt": "..."}', "")

    try:
        manifest, raw, reasoning = _resolve(payload)
        svg = draw(manifest)
    except AgentUnavailable as exc:
        return JSONResponse(status_code=503, content={"error": str(exc), "field": ""})
    except FAILURES as exc:
        return _rejected(str(exc), exc.field)

    path, existed = write(manifest, svg)
    return JSONResponse(status_code=200, content={
        "path": str(path),
        "manifest": raw,
        "bytes": len(svg.encode("utf-8")),
        "overwrote": existed,
        "reasoning": reasoning,
    })


def _resolve(payload: dict) -> tuple[Manifest, dict, str | None]:
    """Accept a prompt, a wrapped manifest, or a bare manifest.

    `reasoning` is None when the caller supplied the manifest themselves --
    there was no agent, so there is nothing to explain.
    """
    if "prompt" in payload:
        return describe(payload["prompt"])
    raw = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else payload
    return validate(raw), raw, None


def _rejected(error: str, field: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": error, "field": field})
