FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Runtime needs only mapsvc/ (including web/ and statistics/data/). Nothing under
# evals/ is read outside replay mode; tools/overpass/coverage.json is optional
# and absent here, which is the "no local Overpass" case.
COPY mapsvc ./mapsvc
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    MAPSVC_CACHE=/tmp/mapsvc-cache \
    MAPSVC_OUTPUT=/tmp/mapsvc-out

RUN useradd --create-home app && mkdir -p /tmp/mapsvc-cache /tmp/mapsvc-out && chown -R app /tmp/mapsvc-cache /tmp/mapsvc-out
USER app

CMD ["sh", "-c", "exec uvicorn mapsvc.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
