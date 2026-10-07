"""HTTP GET with an on-disk cache, shared by the newer sources.

Every file lands under the harvest cache directory, written via a temporary
name so an interrupted download cannot leave a truncated file that looks like
a valid cache entry.
"""

import pathlib
import urllib.error
import urllib.request

USER_AGENT = "mapsvc/0.1 (+https://github.com/bohachevskyy/maps-maker)"
TIMEOUT = 120


class FetchError(OSError):
    """A remote file could not be retrieved. Callers translate it."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def get(url: str, timeout: int = TIMEOUT, accept: str | None = None) -> bytes:
    return _send(url, None, timeout, accept)


def post(url: str, body: bytes, timeout: int = TIMEOUT,
         content_type: str = "application/x-www-form-urlencoded") -> bytes:
    return _send(url, body, timeout, None, content_type)


def _send(url: str, body: bytes | None, timeout: int, accept: str | None,
          content_type: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # the body is a courtesy, never the point
            pass
        raise FetchError(f"{url} returned {exc.code} {body}".strip(), exc.code) from exc
    except OSError as exc:
        raise FetchError(f"could not reach {url}: {exc}") from exc


def path(*parts: str) -> pathlib.Path:
    from mapsvc.harvest import cache_dir

    return pathlib.Path(cache_dir()).joinpath(*parts)


def cached(url: str, *parts: str, timeout: int = TIMEOUT,
           accept: str | None = None) -> bytes:
    """The bytes at `url`, fetched once and kept at cache/<parts...>."""
    target = path(*parts)
    if target.exists():
        return target.read_bytes()
    payload = get(url, timeout=timeout, accept=accept)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return payload


def cached_path(url: str, *parts: str, timeout: int = TIMEOUT) -> pathlib.Path:
    """Like `cached`, but returns the file path -- for archives read lazily."""
    target = path(*parts)
    if not target.exists():
        cached(url, *parts, timeout=timeout)
    return target
