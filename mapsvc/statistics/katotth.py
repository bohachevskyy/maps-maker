"""Ukraine's official settlement categories from the KATOTTH codifier."""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

from mapsvc import fetch

URL = "https://mindev.gov.ua/storage/app/sites/1/uploaded-files/kodifikator-07-07.xlsx"
VINTAGE = "Ministry order No. 1321 of 7 July 2026"
ATTRIBUTION = "Ministry for Development of Communities and Territories of Ukraine"
LICENSE = "official act of a Ukrainian ministry (Copyright Law of Ukraine, Art. 10)"
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_SETTLEMENT = {"M": "city", "X": "selyshche", "C": "village", "T": "selyshche"}


def load() -> dict[str, dict]:
    payload = fetch.cached(URL, "statistics", "katotth-2026-07-07.xlsx", timeout=180)
    return parse(payload)


def parse(payload: bytes) -> dict[str, dict]:
    """Return settlement code -> official category/name/raion.

    XLSX is a small ZIP of XML files. Reading those directly avoids adding a
    dataframe dependency to the service for seven simple columns.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = ["".join(node.text or "" for node in item.iter(_NS + "t"))
                  for item in shared_root.findall(_NS + "si")]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows = []
    for row in sheet.iter(_NS + "row"):
        values = {}
        for cell in row.findall(_NS + "c"):
            match = re.match(r"[A-Z]+", cell.get("r", ""))
            value = cell.find(_NS + "v")
            if not match or value is None or value.text is None:
                continue
            text = shared[int(value.text)] if cell.get("t") == "s" else value.text
            values[match.group()] = text.strip()
        rows.append(values)

    raions = {row.get("B"): row.get("G") for row in rows
              if row.get("F") == "P" and row.get("B")}
    result = {}
    for row in rows:
        category = row.get("F")
        code = row.get("D")
        if category not in _SETTLEMENT or not code:
            continue
        result[code] = {
            "category": _SETTLEMENT[category],
            "category_code": category,
            "name": row.get("G") or code,
            "raion_code": row.get("B"),
            "raion": raions.get(row.get("B")),
        }
    return result
