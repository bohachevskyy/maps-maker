"""HTTP surface. One endpoint.

    POST /map   body: manifest JSON   ->   200 image/svg+xml
                                           422 {"error": "...", "field": "..."}

The request body is read as a plain dict rather than a Pydantic model on
purpose: manifest.validate is the single source of truth for what a valid
manifest is, and it is the only thing that can name the offending field in the
shape this contract promises.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mapsvc.harvest import HarvestError
from mapsvc.manifest import ManifestError
from mapsvc.pipeline import build_map
from mapsvc.render import RenderError

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


def _rejected(error: str, field: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": error, "field": field})
