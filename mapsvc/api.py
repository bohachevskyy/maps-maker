"""HTTP surface.

    GET  /           browser interface
    POST /map       body: manifest JSON     ->  200 image/svg+xml
                                                422 {"error": "...", "field": "..."}
                                                503 if Overpass is unavailable

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
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from mapsvc import hosting
from mapsvc.agent import AgentError, AgentUnavailable, describe
from mapsvc.cartography import CartographyError
from mapsvc.export import write
from mapsvc.harvest import HarvestError
from mapsvc.statistics import StatisticsError
from mapsvc.manifest import Manifest, ManifestError, validate
from mapsvc.pipeline import build_map, draw
from mapsvc.overlays.overpass import UpstreamUnavailable
from mapsvc.render import RenderError

FAILURES = (AgentError, ManifestError, HarvestError, RenderError,
            CartographyError, StatisticsError)

# Keep the public surface small; API docs and the schema route remain disabled.
# Set docs_url="/docs", openapi_url="/openapi.json" to get them back.
app = FastAPI(title="mapsvc", docs_url=None, redoc_url=None, openapi_url=None)
WEB_DIR = Path(__file__).with_name("web")
hosting.apply_cors(app)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True}


@app.get("/", include_in_schema=False)
async def web_interface() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.post("/map")
async def make_map(request: Request) -> Response:
    if (refused := hosting.check_map(request)) is not None:
        return refused
    body = await request.body()
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        return _rejected(f"request body is not valid JSON: {exc}", "")

    try:
        svg = await run_in_threadpool(build_map, raw)
    except UpstreamUnavailable as exc:
        return _upstream_unavailable(exc)
    except FAILURES as exc:
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
    if (too_long := hosting.check_prompt(payload.get("prompt"))) is not None:
        return _rejected(too_long, "prompt")
    if (refused := hosting.check_describe(request)) is not None:
        return refused

    try:
        manifest, raw, reasoning, svg = await run_in_threadpool(
            _describe_and_draw, payload.get("prompt")
        )
    except UpstreamUnavailable as exc:
        return _upstream_unavailable(exc)
    except AgentUnavailable as exc:
        return JSONResponse(status_code=503, content={"error": str(exc), "field": ""})
    except FAILURES as exc:
        return _rejected(str(exc), exc.field)

    return JSONResponse(
        status_code=200,
        content={"manifest": raw, "svg": svg, "reasoning": reasoning},
    )


def _describe_and_draw(prompt: object) -> tuple[Manifest, dict, str, str]:
    """Run the complete blocking agent and render path off the event loop."""
    manifest, raw, reasoning = describe(prompt)
    return manifest, raw, reasoning, draw(manifest)


@app.post("/export")
async def export_map(request: Request) -> Response:
    """Render to a file and return its path.

    Accepts whatever /map or /describe accepts -- a manifest, or a prompt -- so
    a caller does not have to know which of the two produced the request.

    The path is on the server's filesystem, which is only useful to a caller
    that shares it (same host, same container, mounted volume). Callers over a
    network want /map or /describe instead.
    """
    if hosting.export_disabled():
        return JSONResponse(status_code=404, content={
            "error": "export is disabled on this server", "field": ""})
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return _rejected(f"request body is not valid JSON: {exc}", "")
    if not isinstance(payload, dict):
        return _rejected("request body must be a manifest object or "
                         '{"prompt": "..."}', "")
    if "prompt" in payload:
        if (too_long := hosting.check_prompt(payload["prompt"])) is not None:
            return _rejected(too_long, "prompt")
        if (refused := hosting.check_describe(request)) is not None:
            return refused

    try:
        manifest, raw, reasoning = _resolve(payload)
        svg = draw(manifest)
    except UpstreamUnavailable as exc:
        return _upstream_unavailable(exc)
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


def _upstream_unavailable(exc: UpstreamUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(exc.retry_after)},
        content={"error": str(exc), "field": "",
                 "endpoints_tried": list(exc.endpoints_tried)},
    )
