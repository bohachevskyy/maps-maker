"""A minimal .xlsx reader on the standard library.

Statistical agencies publish their code lists as spreadsheets. An .xlsx file is
a zip of XML, so reading cell values needs nothing beyond zipfile and
ElementTree: no formulas, styles or dates, just each sheet's cells as strings.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def sheets(data: bytes) -> dict[str, list[dict[str, str]]]:
    """{sheet name: [{column letter: value}, ...]} for every sheet, in order."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = set(archive.namelist())

    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root.findall(f"{_MAIN}si"):
            shared.append("".join(t.text or "" for t in item.iter(f"{_MAIN}t")))

    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{_PKG_REL}Relationship")}

    out: dict[str, list[dict[str, str]]] = {}
    for sheet in workbook.iter(f"{_MAIN}sheet"):
        target = targets[sheet.get(f"{_REL}id")].lstrip("/")
        path = target if target.startswith("xl/") else f"xl/{target}"
        root = ET.fromstring(archive.read(path))
        rows = []
        for row in root.iter(f"{_MAIN}row"):
            cells: dict[str, str] = {}
            for cell in row.findall(f"{_MAIN}c"):
                column = re.sub(r"\d+", "", cell.get("r", ""))
                kind = cell.get("t")
                value = cell.find(f"{_MAIN}v")
                if kind == "s" and value is not None:
                    text = shared[int(value.text)]
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(f"{_MAIN}t"))
                else:
                    text = value.text if value is not None and value.text else ""
                cells[column] = text
            rows.append(cells)
        out[sheet.get("name")] = rows
    return out
