"""Pure rewrites from known dataset landing pages to machine-readable URLs."""

import re
import urllib.parse


_SOCRATA_ID = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$", re.I)


def ckan_package(url: str) -> dict | None:
    """Describe a possible CKAN dataset/resource URL on any HTTP(S) host."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    match = re.fullmatch(
        r"(?P<prefix>/[a-z]{2}(?:-[a-z]{2})?)?/dataset/(?P<name>[^/]+)"
        r"(?:/resource/(?P<resource>[^/]+)(?P<tail>/.*)?)?/?",
        parts.path,
        re.I,
    )
    if not match:
        return None
    prefix = match.group("prefix") or ""
    name = urllib.parse.unquote(match.group("name"))
    api_url = urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, f"{prefix}/api/3/action/package_show",
        urllib.parse.urlencode({"id": name}), "",
    ))
    tail = match.group("tail") or ""
    return {
        "api_url": api_url,
        "resource_id": urllib.parse.unquote(match.group("resource") or ""),
        "is_download": bool(re.match(r"^/download(?:/|$)", tail, re.I)),
    }


def data_urls(url: str) -> list[str]:
    """Return machine-readable alternatives for a recognised landing-page URL."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path
    except ValueError:
        return []
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return []

    match = re.fullmatch(r"/indicator/([^/]+)/?", path, re.I)
    if host == "data.worldbank.org" and match:
        indicator = urllib.parse.quote(urllib.parse.unquote(match.group(1)), safe="._-")
        return [
            f"https://api.worldbank.org/v2/country/all/indicator/{indicator}"
            "?format=json&per_page=20000&mrnev=1"
        ]

    if host == "api.worldbank.org" and re.search(r"/indicator/[^/]+/?$", path, re.I):
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not any(key.casefold() == "format" and value.casefold() == "json"
                   for key, value in query):
            query = [(key, value) for key, value in query if key.casefold() != "format"]
            query.append(("format", "json"))
            return [urllib.parse.urlunsplit((
                parts.scheme, parts.netloc, path, urllib.parse.urlencode(query), ""
            ))]
        return []

    match = re.fullmatch(r"/grapher/([^/]+?)/?", path, re.I)
    if host in {"ourworldindata.org", "www.ourworldindata.org"} and match:
        csv_path = f"/grapher/{match.group(1)}.csv"
        return [urllib.parse.urlunsplit((parts.scheme, parts.netloc, csv_path,
                                        parts.query, ""))]

    match = re.fullmatch(r"/([^/]+)/([^/]+)/blob/([^/]+)/(.*)", path, re.I)
    if host == "github.com" and match and match.group(4):
        owner, repo, ref, file_path = match.groups()
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{file_path}"]

    match = re.match(r"^/spreadsheets/d/([^/]+)(?:/.*)?$", path, re.I)
    if host == "docs.google.com" and match:
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        fragment = urllib.parse.parse_qs(parts.fragment, keep_blank_values=True)
        output_query = [("format", "csv")]
        gid = query.get("gid") or fragment.get("gid")
        if gid:
            output_query.append(("gid", gid[0]))
        return [
            f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?"
            + urllib.parse.urlencode(output_query)
        ]

    package = ckan_package(url)
    if package and not package["is_download"]:
        return [package["api_url"]]

    segments = [segment for segment in path.split("/") if segment]
    socrata_id = segments[-1] if segments else ""
    is_socrata_path = (
        len(segments) == 2 and segments[0].casefold() == "d"
    ) or len(segments) >= 2
    if is_socrata_path and _SOCRATA_ID.fullmatch(socrata_id):
        return [f"https://{host}/resource/{socrata_id.lower()}.csv?$limit=50000"]

    return []
