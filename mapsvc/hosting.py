"""Opt-in controls for a public deployment. Every one is off unless its
environment variable is set, so local use is unchanged.

    MAPSVC_CORS_ORIGINS          comma-separated origins allowed to call the API
    MAPSVC_DESCRIBE_PER_IP_HOUR  /describe (and prompt /export) per client per hour
    MAPSVC_DESCRIBE_PER_DAY      /describe (and prompt /export), all clients, rolling 24h
    MAPSVC_MAP_PER_IP_MINUTE     /map per client per minute
    MAPSVC_PROMPT_MAX_CHARS      longest accepted prompt
    MAPSVC_DISABLE_EXPORT=1      /export answers 404

The limiter is in-process and single-process: counters reset on restart and are
not shared between workers. That is acceptable for one small container; the
global daily cap is the real guard on the OpenAI bill.
"""

import math
import os
import threading
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

HOUR = 3600.0
MINUTE = 60.0
DAY = 86400.0

_now = time.time          # replaced in tests


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def apply_cors(app: FastAPI) -> None:
    origins = [o.strip() for o in os.environ.get("MAPSVC_CORS_ORIGINS", "").split(",")
               if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Content-Type"],
                           expose_headers=["Retry-After"])


def client_id(request: Request) -> str:
    """The caller's address behind a reverse proxy (Railway).

    Prefer X-Real-IP, which the proxy sets itself. Otherwise take the
    RIGHTMOST X-Forwarded-For entry: each proxy appends the address it saw, so
    the last one was written by our own proxy, while the leftmost is whatever
    the client chose to send and can be rotated to dodge the limit. (A second
    proxy hop in front would make the rightmost the proxy; the global daily cap
    still bounds the bill.) With no proxy headers, use the socket peer.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


class _Window:
    def __init__(self) -> None:
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def take(self, key: str, limit: int, span: float) -> float | None:
        """Record a hit; None if allowed, else seconds until a slot frees."""
        now = _now()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - span:
                q.popleft()
            if len(q) >= limit:
                return q[0] + span - now
            q.append(now)
            return None

    def peek(self, key: str, limit: int, span: float) -> float | None:
        now = _now()
        with self.lock:
            q = self.hits.get(key)
            if not q:
                return None
            while q and q[0] <= now - span:
                q.popleft()
            if len(q) >= limit:
                return q[0] + span - now
            return None


_describe_ip = _Window()
_describe_day = _Window()
_map_ip = _Window()


def reset() -> None:
    """Forget all counters (tests)."""
    for w in (_describe_ip, _describe_day, _map_ip):
        with w.lock:
            w.hits.clear()


def _refuse(wait: float, what: str) -> JSONResponse:
    seconds = max(1, math.ceil(wait))
    if seconds >= 3600:
        when = f"about {round(seconds / 3600)} hour(s)"
    elif seconds >= 90:
        when = f"about {round(seconds / 60)} minutes"
    else:
        when = f"{seconds} seconds"
    return JSONResponse(status_code=429, headers={"Retry-After": str(seconds)},
                        content={"error": f"{what} Please try again in {when}.",
                                 "field": ""})


def check_describe(request: Request) -> JSONResponse | None:
    """Count one agent call against the per-client and global caps. Returns a
    429 response when over a cap. Call BEFORE the agent."""
    per_ip, per_day = _int_env("MAPSVC_DESCRIBE_PER_IP_HOUR"), _int_env("MAPSVC_DESCRIBE_PER_DAY")
    if per_ip is None and per_day is None:
        return None
    ip = client_id(request)
    # Peek at both before recording either, so a refused request is not charged
    # to the cap that was still open.
    if per_ip is not None:
        wait = _describe_ip.peek(ip, per_ip, HOUR)
        if wait is not None:
            return _refuse(wait, "You have reached the hourly limit for described maps.")
    if per_day is not None:
        wait = _describe_day.peek("*", per_day, DAY)
        if wait is not None:
            return _refuse(wait, "The service has reached its daily limit for described maps.")
    if per_ip is not None:
        _describe_ip.take(ip, per_ip, HOUR)
    if per_day is not None:
        _describe_day.take("*", per_day, DAY)
    return None


def check_map(request: Request) -> JSONResponse | None:
    limit = _int_env("MAPSVC_MAP_PER_IP_MINUTE")
    if limit is None:
        return None
    wait = _map_ip.take(client_id(request), limit, MINUTE)
    if wait is not None:
        return _refuse(wait, "Too many map requests.")
    return None


def check_prompt(prompt: object) -> str | None:
    """An error message if the prompt is over MAPSVC_PROMPT_MAX_CHARS."""
    limit = _int_env("MAPSVC_PROMPT_MAX_CHARS")
    if limit is not None and isinstance(prompt, str) and len(prompt) > limit:
        return f"prompt is {len(prompt)} characters; the limit is {limit}"
    return None


def export_disabled() -> bool:
    return os.environ.get("MAPSVC_DISABLE_EXPORT", "").strip() in ("1", "true", "yes")
