"""The single network boundary for data-source discovery.

Requests can be recorded as small, content-addressed cassettes and replayed
offline.  URL validation happens before either a live request or replay so a
cassette cannot turn a forbidden URL into an allowed one.
"""

from dataclasses import dataclass
import datetime as _datetime
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from mapsvc.fetch import USER_AGENT


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    content: bytes
    url: str
    from_replay: bool = False


class DiscoveryHTTPError(OSError):
    """Base class for failures at the discovery HTTP boundary."""


class ReplayMiss(DiscoveryHTTPError):
    pass


class SSRFError(DiscoveryHTTPError):
    pass


class ResponseTooLarge(DiscoveryHTTPError):
    pass


class UnexpectedContent(DiscoveryHTTPError):
    pass


class HTTPStatusError(DiscoveryHTTPError):
    def __init__(self, url: str, status: int):
        super().__init__(f"{url} returned HTTP {status}")
        self.status = status


_RETRIES = 3
_RESPONSE_HEADERS = {
    "accept-ranges", "cache-control", "content-disposition", "content-encoding",
    "content-language", "content-length", "content-type", "etag", "expires",
    "last-modified",
}


def get(url: str, *, params=None, headers=None, max_bytes: int = 25_000_000,
        timeout: float = 30, expect: str | None = None, record_if=None) -> Response:
    """record_if(response) -> bool keeps a degraded answer (a rate-limited search
    returning nothing) out of the cassettes, where it would replay forever."""
    return _request("GET", url, params=params, headers=headers, body=b"",
                    max_bytes=max_bytes, timeout=timeout, expect=expect,
                    record_if=record_if)


def post_json(url: str, payload, *, params=None, headers=None,
              max_bytes: int = 25_000_000, timeout: float = 30,
              expect: str | None = None) -> Response:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    supplied = dict(headers or {})
    supplied.setdefault("Content-Type", "application/json")
    return _request("POST", url, params=params, headers=supplied, body=body,
                    max_bytes=max_bytes, timeout=timeout, expect=expect)


def _mode() -> str:
    mode = os.environ.get("MAPSVC_REPLAY", "off").strip().lower()
    if mode not in {"off", "record", "replay", "cached"}:
        raise DiscoveryHTTPError(
            "MAPSVC_REPLAY must be one of off, record, replay or cached")
    return mode


def _cassette_dir() -> Path:
    return Path(os.environ.get("MAPSVC_CASSETTES", "evals/research/cassettes"))


def _query_pairs(params) -> list[tuple[str, str]]:
    if params is None:
        return []
    if hasattr(params, "items"):
        pairs = []
        for key, value in params.items():
            values = value if isinstance(value, (list, tuple)) else (value,)
            pairs.extend((str(key), str(item)) for item in values)
        return pairs
    return [(str(key), str(value)) for key, value in params]


def _normalised_url(url: str, params=None) -> str:
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    pairs.extend(_query_pairs(params))
    query = urllib.parse.urlencode(sorted(pairs), doseq=True)
    host = (parts.hostname or "").lower()
    port = parts.port
    default = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443)
    netloc = host + ((f":{port}") if port and not default else "")
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", query, ""))


def _key(method: str, url: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(method.encode("ascii"))
    digest.update(url.encode("utf-8"))
    digest.update(body)
    return digest.hexdigest()


def _searx_origin() -> tuple[str, str, int | None] | None:
    configured = os.environ.get("MAPSVC_SEARXNG_URL", "").strip()
    if not configured:
        return None
    parts = urllib.parse.urlsplit(configured)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def _localhost_allowed(parts) -> bool:
    allowed = _searx_origin()
    return bool(allowed and allowed == (
        parts.scheme.lower(), (parts.hostname or "").lower(), parts.port))


def _validate_url(url: str, *, resolve: bool) -> None:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SSRFError(f"invalid discovery URL {url!r}: {exc}") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise SSRFError(f"discovery URL must use http(s): {url!r}")
    if parts.username or parts.password:
        raise SSRFError("discovery URLs must not contain credentials")
    host = (parts.hostname or "").lower()
    if not host:
        raise SSRFError(f"discovery URL has no host: {url!r}")
    allow_local = _localhost_allowed(parts)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global and not allow_local:
        raise SSRFError(f"discovery URL points to a private or loopback host: {host}")
    if host.endswith((".local", ".internal", ".localhost")) and not allow_local:
        raise SSRFError(f"discovery URL points to a private host: {host}")
    if not resolve or allow_local:
        return
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise DiscoveryHTTPError(f"could not resolve {host}: {exc}") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise SSRFError(f"{host} resolves to a private or loopback address")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _headers_subset(headers) -> dict[str, str]:
    return {str(key): str(value) for key, value in headers.items()
            if str(key).lower() in _RESPONSE_HEADERS}


def _is_html(headers: dict[str, str], content: bytes) -> bool:
    kind = next((value for key, value in headers.items()
                 if key.lower() == "content-type"), "").lower()
    beginning = content[:1024].lstrip().lower()
    return ("text/html" in kind or "application/xhtml" in kind
            or beginning.startswith((b"<!doctype html", b"<html", b"<head",
                                     b"<body")))


def _read_limited(handle, limit: int, url: str) -> bytes:
    if limit < 0:
        raise ValueError("max_bytes must be non-negative")
    chunks = []
    total = 0
    while True:
        chunk = handle.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(f"{url} exceeds the {limit:,}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _request(method: str, url: str, *, params, headers, body: bytes,
             max_bytes: int, timeout: float, expect: str | None,
             record_if=None) -> Response:
    normalised = _normalised_url(url, params)
    _validate_url(normalised, resolve=False)
    key = _key(method, normalised, body)
    mode = _mode()
    if mode == "replay":
        return _replay(key, normalised, max_bytes, expect)
    if mode == "cached":
        # Replay what was recorded, fetch and record what was not: an eval run
        # pays for the web once per request, and reruns see the same bytes.
        try:
            return _replay(key, normalised, max_bytes, expect)
        except ReplayMiss:
            mode = "record"

    _validate_url(normalised, resolve=True)
    # Request credentials may be needed by a provider.  They are deliberately
    # absent from both the cassette key and metadata, so they are sent but
    # never persisted.
    request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    request_headers.setdefault("User-Agent", USER_AGENT)
    request = urllib.request.Request(normalised, data=(body if method == "POST" else None),
                                     headers=request_headers, method=method)
    opener = urllib.request.build_opener(_SafeRedirect)
    last_status = None
    for attempt in range(_RETRIES):
        try:
            with opener.open(request, timeout=timeout) as handle:
                status = int(getattr(handle, "status", handle.getcode()))
                response_headers = _headers_subset(handle.headers)
                content = _read_limited(handle, max_bytes, normalised)
                final_url = handle.geturl()
            if status == 429 or status >= 500:
                last_status = status
                if attempt + 1 < _RETRIES:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise HTTPStatusError(final_url, status)
            if status >= 400:
                raise HTTPStatusError(final_url, status)
            response = Response(status, response_headers, content, final_url, False)
            _check_expected(response, expect)
            if mode == "record" and (record_if is None or record_if(response)):
                _record(key, method, normalised, params, response)
            return response
        except DiscoveryHTTPError:
            raise
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            if (exc.code == 429 or exc.code >= 500) and attempt + 1 < _RETRIES:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise HTTPStatusError(normalised, exc.code) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            if attempt + 1 < _RETRIES:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise DiscoveryHTTPError(f"could not fetch {normalised}: {exc}") from exc
    raise HTTPStatusError(normalised, last_status or 500)


def _check_expected(response: Response, expect: str | None) -> None:
    if expect == "data" and _is_html(response.headers, response.content):
        raise UnexpectedContent(
            f"{response.url} returned HTML where a data file was expected")


def _record(key: str, method: str, url: str, params, response: Response) -> None:
    directory = _cassette_dir()
    directory.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256(response.content).hexdigest()
    metadata = {
        "method": method,
        "url": url,
        "params": sorted(_query_pairs(params)),
        "status": response.status,
        "headers": response.headers,
        "final_url": response.url,
        "sha256": content_hash,
        "size": len(response.content),
        "recorded_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
    }
    binary = directory / f"{key}.bin"
    meta = directory / f"{key}.json"
    temporary_binary = binary.with_suffix(".bin.part")
    temporary_meta = meta.with_suffix(".json.part")
    temporary_binary.write_bytes(response.content)
    temporary_meta.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n",
                              encoding="utf-8")
    temporary_binary.replace(binary)
    temporary_meta.replace(meta)


def _replay(key: str, url: str, max_bytes: int, expect: str | None) -> Response:
    directory = _cassette_dir()
    meta_path = directory / f"{key}.json"
    binary_path = directory / f"{key}.bin"
    if not meta_path.is_file() or not binary_path.is_file():
        raise ReplayMiss(f"replay miss for {key}: {url}")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    content = binary_path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != metadata.get("sha256") or len(content) != metadata.get("size"):
        raise DiscoveryHTTPError(f"cassette {key} failed its integrity check")
    if len(content) > max_bytes:
        raise ResponseTooLarge(f"{url} exceeds the {max_bytes:,}-byte limit")
    response = Response(int(metadata["status"]), dict(metadata.get("headers") or {}),
                        content, metadata.get("final_url") or metadata["url"], True)
    _check_expected(response, expect)
    return response
