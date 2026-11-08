"""Best-effort search providers and safe landing-page link extraction."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import os
import re
import threading
import urllib.parse

from mapsvc.discovery import adapters, htmltables, http


_SEARXNG_DEFAULT = "http://127.0.0.1:8888"
# Measured 2026-11-07: catalog.data.gov's CKAN API answers 404, data.gov.uk
# redirects its API to an HTML site, open.canada.ca's timed out after 25 s.
# The general web search (SearXNG) still reaches all three.
_CKAN_PORTALS = (
    ("data.humdata.org", "https://data.humdata.org", "/dataset/"),
    ("data.gov.ua", "https://data.gov.ua", "/dataset/"),
)
_DATA_EXTENSIONS = (".csv", ".tsv", ".xlsx", ".xls", ".json", ".geojson", ".zip")
_CKAN_FORMATS = {"CSV", "XLSX", "XLS", "JSON", "GEOJSON", "ZIP"}
_MAX_HITS = 30
_ERRORS = threading.local()


def last_errors() -> dict[str, str]:
    """Return failures from the most recent search in this thread."""
    return dict(getattr(_ERRORS, "value", {}))


def search(query: str) -> list[dict]:
    """Search configured public catalogues, returning up to 30 mixed hits."""
    configured = os.environ.get(
        "MAPSVC_SEARCH_PROVIDERS", "searxng,ckan,zenodo"
    )
    providers = [item.strip().lower() for item in configured.split(",") if item.strip()]
    errors: dict[str, str] = {}
    jobs: list[tuple[str, object]] = []
    slots: list[list[int]] = []

    for provider in providers:
        if provider == "searxng":
            slots.append([len(jobs)])
            jobs.append(("searxng", lambda: _search_searxng(query)))
        elif provider == "ckan":
            indices = []
            for name, base_url, page_path in _CKAN_PORTALS:
                indices.append(len(jobs))
                jobs.append((
                    name,
                    lambda n=name, b=base_url, p=page_path: _search_ckan(query, n, b, p),
                ))
            slots.append(indices)
        elif provider == "zenodo":
            slots.append([len(jobs)])
            jobs.append(("zenodo", lambda: _search_zenodo(query)))
        else:
            errors[provider] = "unknown search provider"

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = [pool.submit(operation) for _, operation in jobs]
        raw_batches = [_completed(name, future, errors)
                       for (name, _), future in zip(jobs, futures)]
    batches = [
        raw_batches[indices[0]] if len(indices) == 1 else
        _interleave_deduped([raw_batches[index] for index in indices], _MAX_HITS)
        for indices in slots
    ]
    _ERRORS.value = errors
    return _interleave_deduped(batches, _MAX_HITS)


def _completed(name: str, future, errors: dict[str, str]) -> list[dict]:
    try:
        return future.result()
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        errors[name] = f"{type(exc).__name__}: {exc}"
        return []


def _json_response(url: str, *, params: dict, record_if=None) -> dict:
    response = http.get(url, params=params, timeout=10, expect="json", record_if=record_if)
    value = json.loads(response.content)
    if not isinstance(value, dict):
        raise ValueError(f"{url} returned a non-object JSON response")
    return value


def _search_searxng(query: str) -> list[dict]:
    # The HTTP boundary permits a loopback exception only for the configured
    # SearXNG origin.  Materialise the documented default so the guard sees it.
    base_url = os.environ.setdefault("MAPSVC_SEARXNG_URL", _SEARXNG_DEFAULT).rstrip("/")
    payload = _json_response(f"{base_url}/search", params={"q": query, "format": "json"},
                             record_if=_searx_answered)
    if not payload.get("results") and payload.get("unresponsive_engines"):
        # Measured 2026-11-07: engines get suspended under load and SearXNG
        # then answers 200 with nothing. That is an outage, not "no results".
        raise RuntimeError("SearXNG engines unavailable: " + ", ".join(
            f"{e[0]} ({e[1]})" for e in payload["unresponsive_engines"][:4]))
    hits = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict) or not result.get("url"):
            continue
        hits.append({
            "title": _clip(result.get("title"), 240),
            "url": str(result["url"]),
            "snippet": _clip(result.get("content"), 500),
            "engine": "searxng",
            "kind": "page",
        })
    return hits


def _searx_answered(response) -> bool:
    try:
        return bool(json.loads(response.content).get("results"))
    except (ValueError, AttributeError):
        return False


def _search_ckan(query: str, portal: str, base_url: str, page_path: str) -> list[dict]:
    payload = _json_response(
        f"{base_url}/api/3/action/package_search", params={"q": query, "rows": 5}
    )
    result = payload.get("result") or {}
    packages = result.get("results") or [] if isinstance(result, dict) else []
    hits = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        title = _clip(package.get("title") or package.get("name"), 240)
        slug = str(package.get("name") or package.get("id") or "")
        licence = _clip(package.get("license_title") or package.get("licence_title"), 240)
        organisation = package.get("organization") or package.get("organisation") or {}
        publisher = _clip(
            organisation.get("title") or organisation.get("name")
            if isinstance(organisation, dict) else organisation,
            240,
        )
        snippet = _ckan_snippet(publisher, licence, package.get("notes"))
        for resource in package.get("resources") or []:
            if not isinstance(resource, dict) or not resource.get("url"):
                continue
            resource_url = str(resource["url"])
            if not _is_ckan_data_resource(resource, resource_url):
                continue
            name = _clip(resource.get("name") or resource.get("description")
                         or _url_filename(resource_url) or "data file", 240)
            hits.append(_catalog_hit(
                f"{title} — {name}", resource_url, snippet, "ckan", "file",
                licence, publisher,
            ))
        if slug:
            page_url = urllib.parse.urljoin(base_url, f"{page_path}{urllib.parse.quote(slug)}")
            hits.append(_catalog_hit(
                title, page_url, snippet, "ckan", "dataset", licence, publisher,
            ))
    return hits


def _catalog_hit(title, url, snippet, engine, kind, licence, publisher) -> dict:
    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "engine": engine,
        "kind": kind,
        "licence": licence,
        "publisher": publisher,
    }


def _is_ckan_data_resource(resource: dict, url: str) -> bool:
    resource_format = str(resource.get("format") or "").strip().upper().lstrip(".")
    return resource_format in _CKAN_FORMATS or _url_extension(url) in _DATA_EXTENSIONS


def _ckan_snippet(publisher, licence, notes) -> str:
    pieces = [str(value).strip() for value in (publisher, licence, notes) if str(value or "").strip()]
    return _clip(" · ".join(pieces), 500)


def _search_zenodo(query: str) -> list[dict]:
    payload = _json_response(
        "https://zenodo.org/api/records", params={"q": query, "size": 5}
    )
    container = payload.get("hits") or {}
    records = container.get("hits") or [] if isinstance(container, dict) else []
    hits = []
    for record in records:
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        title = _clip(metadata.get("title") or record.get("title"), 240)
        licence = _zenodo_licence(metadata.get("license") or metadata.get("licence"))
        snippet = _clip(_strip_html(metadata.get("description") or ""), 500)
        record_url = ((record.get("links") or {}).get("html")
                      or f"https://zenodo.org/records/{record.get('id')}")
        for file_info in record.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            key = str(file_info.get("key") or file_info.get("filename") or "")
            file_url = ((file_info.get("links") or {}).get("content")
                        or (file_info.get("links") or {}).get("self"))
            if file_url and (_url_extension(key) in _DATA_EXTENSIONS
                             or _url_extension(str(file_url)) in _DATA_EXTENSIONS):
                hits.append({
                    "title": _clip(f"{title} — {key or _url_filename(str(file_url))}", 240),
                    "url": str(file_url),
                    "snippet": snippet,
                    "engine": "zenodo",
                    "kind": "file",
                    "licence": licence,
                })
        if record.get("id") is not None or record_url:
            hits.append({
                "title": title,
                "url": str(record_url),
                "snippet": snippet,
                "engine": "zenodo",
                "kind": "dataset",
                "licence": licence,
            })
    return hits


def _zenodo_licence(value) -> str:
    if isinstance(value, dict):
        value = value.get("title") or value.get("id") or ""
    return _clip(value, 240)


def _normalised_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(
        parts.query, keep_blank_values=True
    )), doseq=True)
    return urllib.parse.urlunsplit((scheme, host, path, query, ""))


def _interleave_deduped(batches: list[list[dict]], limit: int) -> list[dict]:
    queues = [deque(batch) for batch in batches if batch]
    seen: set[str] = set()
    merged = []
    while queues and len(merged) < limit:
        remaining = []
        for queue in queues:
            while queue:
                hit = queue.popleft()
                marker = _normalised_url(str(hit.get("url") or ""))
                if marker and marker not in seen:
                    seen.add(marker)
                    merged.append(hit)
                    break
            if queue:
                remaining.append(queue)
            if len(merged) == limit:
                break
        queues = remaining
    return merged


def fetch(url: str) -> dict:
    """Fetch a URL and identify a file or links to data files on an HTML page."""
    errors: dict[str, str] = {}
    _ERRORS.value = errors
    ckan = adapters.ckan_package(url)
    adapted = adapters.data_urls(url)
    if adapted:
        if "/api/3/action/package_show?" in adapted[0]:
            try:
                return _fetch_ckan_package(
                    adapted[0], resource_id=(ckan or {}).get("resource_id", "")
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                errors["ckan"] = f"{type(exc).__name__}: {exc}"
        else:
            return {"kind": "page", "title": "", "links": adapted}
    response = http.get(url, expect=None)
    content_type = _header(response.headers, "content-type").lower()
    if _is_data_response(response.url, content_type, response.content):
        result = {"kind": "file", "title": _file_title(response), "links": []}
        if ckan and ckan["is_download"]:
            try:
                result.update(_ckan_metadata(_load_ckan_package(ckan["api_url"])))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                errors["ckan"] = f"{type(exc).__name__}: {exc}"
        return result

    parser = _LandingPageParser()
    parser.feed(_decode_html(response.content, content_type))
    ranked = []
    seen = set()
    for index, (href, anchor) in enumerate(parser.links):
        absolute = urllib.parse.urljoin(response.url, href)
        if not _is_candidate_data_link(absolute):
            continue
        marker = _normalised_url(absolute)
        if marker in seen:
            continue
        seen.add(marker)
        ranked.append((_link_rank(absolute, anchor), index, absolute))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    result = {
        "kind": "page",
        "title": _clip(parser.title, 160),
        "links": [item[2] for item in ranked[:30]],
    }
    # Data tables inside the page are offered as files: `page#table=N`.
    page_tables = htmltables.tables(_decode_html(response.content, content_type))
    if page_tables:
        result["tables"] = [
            {"index": t.index, "caption": _clip(t.caption, 120), "n_rows": t.n_rows,
             "n_cols": t.n_cols, "headers": [_clip(h, 60) for h in t.headers[:8]]}
            for t in page_tables]
    largest = sorted(page_tables, key=lambda t: (-t.n_rows * t.n_cols, t.index))[:5]
    result["links"].extend(f"{response.url}#table={t.index}" for t in largest)
    licence = next(iter(parser.licences), "")
    if licence:
        result["licence"] = licence
    zenodo_api = _zenodo_record_api_url(response.url)
    if zenodo_api:
        try:
            payload = _json_object(http.get(zenodo_api, expect="json"), zenodo_api)
            metadata = payload.get("metadata") or {}
            if isinstance(metadata, dict):
                api_licence = _zenodo_licence(
                    metadata.get("license") or metadata.get("licence")
                )
                if api_licence:
                    result["licence"] = api_licence
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors["zenodo"] = f"{type(exc).__name__}: {exc}"
    return result


def _json_object(response, url: str) -> dict:
    payload = json.loads(response.content)
    if not isinstance(payload, dict):
        raise ValueError(f"{url} returned a non-object JSON response")
    return payload


def _load_ckan_package(url: str) -> dict:
    payload = _json_object(http.get(url, expect="json"), url)
    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError(f"{url} did not return a successful CKAN package")
    return payload["result"]


def _fetch_ckan_package(url: str, *, resource_id: str = "") -> dict:
    result = _load_ckan_package(url)
    links = []
    resources = result.get("resources") or []
    if resource_id:
        resources = sorted(
            enumerate(resources),
            key=lambda item: (str((item[1] or {}).get("id") or "") != resource_id, item[0])
            if isinstance(item[1], dict) else (True, item[0]),
        )
        resources = [resource for _, resource in resources]
    for resource in resources:
        if not isinstance(resource, dict) or not resource.get("url"):
            continue
        resource_url = str(resource["url"])
        if _is_ckan_data_resource(resource, resource_url):
            links.append(resource_url)
    response = {
        "kind": "page",
        "title": _clip(result.get("title") or result.get("name"), 160),
        "links": links,
    }
    response.update(_ckan_metadata(result))
    return response


def _ckan_metadata(package: dict) -> dict:
    metadata = {}
    licence = _clip(package.get("license_title") or package.get("licence_title"), 240)
    organisation = package.get("organization") or package.get("organisation") or {}
    publisher = _clip(
        organisation.get("title") if isinstance(organisation, dict) else organisation, 240
    )
    dataset_title = _clip(package.get("title") or package.get("name"), 160)
    if licence:
        metadata["licence"] = licence
    if publisher:
        metadata["publisher"] = publisher
    if dataset_title:
        metadata["dataset_title"] = dataset_title
    return metadata


def _zenodo_record_api_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() not in {"zenodo.org", "www.zenodo.org"}:
        return ""
    match = re.fullmatch(r"/records/(\d+)/?", parts.path, re.I)
    return f"https://zenodo.org/api/records/{match.group(1)}" if match else ""


def _header(headers: dict, name: str) -> str:
    return next((str(value) for key, value in headers.items()
                 if str(key).lower() == name.lower()), "")


def _is_data_response(url: str, content_type: str, content: bytes) -> bool:
    media_type = content_type.partition(";")[0].strip()
    if media_type in {
        "text/csv", "text/tab-separated-values", "application/csv",
        "application/json", "application/geo+json", "application/geojson",
        "application/zip", "application/x-zip-compressed",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return True
    beginning = content[:4096].lstrip()
    if beginning.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        return True
    if beginning.startswith((b"{", b"[")):
        try:
            json.loads(content)
            return True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    if _looks_like_html(content_type, beginning):
        return False
    return _looks_like_delimited(beginning)


def _looks_like_html(content_type: str, beginning: bytes) -> bool:
    lower = beginning[:1024].lower()
    return ("html" in content_type or lower.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body")
    ))


def _looks_like_delimited(content: bytes) -> bool:
    try:
        lines = content.decode("utf-8-sig").splitlines()[:5]
    except UnicodeDecodeError:
        return False
    if len(lines) < 2:
        return False
    for delimiter in (",", "\t"):
        widths = [line.count(delimiter) for line in lines if line.strip()]
        if widths and widths[0] > 0 and sum(width == widths[0] for width in widths) >= 2:
            return True
    return False


def _file_title(response) -> str:
    disposition = _header(response.headers, "content-disposition")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    filename = urllib.parse.unquote(match.group(1).strip()) if match else _url_filename(response.url)
    return _clip(filename or response.url, 160)


def _decode_html(content: bytes, content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    encoding = match.group(1).strip("\"'") if match else "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


class _LandingPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._title_parts = []
        self._active_href = None
        self._anchor_parts = []
        self.links: list[tuple[str, str]] = []
        self.licences: list[str] = []

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self._in_title = True
        elif tag.lower() == "a":
            href = dict(attrs).get("href")
            self._active_href = href
            self._anchor_parts = []
            licence = _licence_from_href(href or "")
            if licence and licence not in self.licences:
                self.licences.append(licence)

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "a" and self._active_href is not None:
            self.links.append((self._active_href, " ".join(
                "".join(self._anchor_parts).split()
            )))
            self._active_href = None
            self._anchor_parts = []

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._active_href is not None:
            self._anchor_parts.append(data)


def _licence_from_href(href: str) -> str:
    """Return a short licence name based solely on an anchor href."""
    try:
        parts = urllib.parse.urlsplit(href)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parts.path.lower()).rstrip("/")

    if host == "creativecommons.org":
        match = re.match(
            r"^/licenses/(by(?:-(?:sa|nc|nd)){0,2})/(\d(?:\.\d)?)\b", path
        )
        if match and 2.0 <= float(match.group(2)) <= 4.0:
            variants = {"by", "by-sa", "by-nc", "by-nd", "by-nc-sa", "by-nc-nd"}
            if match.group(1) in variants:
                return f"CC {match.group(1).upper()} {match.group(2)}"
        if re.match(r"^/publicdomain/zero/1\.0\b", path):
            return "CC0 1.0"
        if re.match(r"^/publicdomain/mark(?:/|$)", path):
            return "Public Domain Mark"

    if host == "opendatacommons.org":
        match = re.match(r"^/licenses/(odbl|by|pddl)(?:/|$)", path)
        if match:
            return {"odbl": "ODbL", "by": "ODC-By", "pddl": "PDDL"}[match.group(1)]

    if host == "nationalarchives.gov.uk" and path.startswith(
        "/doc/open-government-licence"
    ):
        return "OGL"
    if host == "open.canada.ca" and path.startswith(
        "/en/open-government-licence-canada"
    ):
        return "OGL-Canada"
    if host in {"usa.gov", "data.gov"} and (
        path.startswith("/government-works")
        or path.startswith("/government-copyright")
        or path.startswith("/open-gov")
        or "government-works" in path
    ):
        return "US Government Work (public domain)"
    if host == "etalab.gouv.fr" and path.startswith("/licence-ouverte"):
        return "Licence Ouverte / Etalab"
    return ""


def _is_candidate_data_link(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        return False
    if _url_extension(url) in _DATA_EXTENSIONS:
        return True
    if re.search(r"/resource/[^/]+/download(?:/|$)", parts.path, re.I):
        return True
    # Download endpoints without an extension, e.g. e-Stat's
    # /stat-search/file-download?statInfId=...&fileKind=0 (measured 2026-11-07).
    if re.search(r"/(?:file-?)?download(?:file)?s?(?:/|$)", parts.path, re.I) and parts.query:
        return True
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    lowered = {key.lower(): [str(value).lower() for value in values]
               for key, values in query.items()}
    if "download" in lowered:
        return True
    return any(value.lstrip(".") in {ext[1:] for ext in _DATA_EXTENSIONS}
               for value in lowered.get("format", []))


def _link_rank(url: str, anchor: str) -> int:
    extension = _url_extension(url)
    formats = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("format", [])
    preferred = extension in {".csv", ".xlsx", ".geojson"} or any(
        str(value).lower().lstrip(".") in {"csv", "xlsx", "geojson"}
        for value in formats
    )
    score = 30 if preferred else 10
    haystack = f"{url} {anchor}".lower()
    score += sum(3 for word in ("download", "data", "csv") if word in haystack)
    return score


def _url_extension(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    return next((extension for extension in _DATA_EXTENSIONS if path.endswith(extension)), "")


def _url_filename(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def _strip_html(value) -> str:
    return re.sub(r"<[^>]*>", " ", str(value or ""))


def _clip(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
