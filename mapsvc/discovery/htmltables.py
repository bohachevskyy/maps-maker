"""HTML <table>s as data files. Pure: bytes of text in, rows out, no I/O.

A page's data tables are numbered 0, 1, ... in document order and addressed as
`<page url>#table=N`. Layout and navigation tables are not data and are not
counted. The scale a caption or header states ("in thousands") is reported in
`notes` verbatim and never applied: the agent carries it as `scale`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re

MIN_ROWS = 5
MIN_COLS = 2
CELL_CHARS = 300
CAPTION_CHARS = 200

_SKIP_TABLE_CLASSES = {"navbox", "infobox", "metadata", "ambox", "vertical-navbox",
                       "sidebar", "toc", "navbox-inner", "mbox-small"}
_VOID = {"br", "img", "hr", "input", "meta", "link", "wbr", "col", "area", "base"}
_FOOTNOTE = re.compile(r"(?:\s*(?:\[[^\]\[]{1,16}\]|[†‡*§]))+\s*$")
_SCALE = re.compile(r"in\s+(?:thousands|millions|billions|%)|thousands|millions|billions"
                    r"|\(000s?\)|['’]000", re.IGNORECASE)
_TOTAL = re.compile(r"^\s*(total|world|sum)\b", re.IGNORECASE)
_DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_FRAGMENT = re.compile(r"#table=(\d{1,3})$")
_SEP = " – "
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LISTING_HEADERS = {"name", "last modified", "size", "description", "type",
                    "date modified"}
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LISTING_HEADERS = {"name", "last modified", "size", "description", "type",
                    "date modified"}


@dataclass
class Table:
    index: int
    caption: str
    headers: list[str]
    rows: list[dict]
    n_rows: int
    n_cols: int
    notes: list[str] = field(default_factory=list)


def split_table_fragment(url: str) -> tuple[str, int | None]:
    """('https://e.org/p#table=2') -> ('https://e.org/p', 2); no selector -> (url, None)."""
    match = _FRAGMENT.search(url)
    if not match:
        return url, None
    return url[:match.start()], int(match.group(1))


def tables(html_text: str) -> list[Table]:
    parser = _Parser()
    parser.feed(html_text)
    parser.close()
    out: list[Table] = []
    title = _TITLE.search(html_text)
    listing_page = bool(title) and title.group(1).strip().lower().startswith("index of")
    for raw in parser.done:
        table = _build(raw, len(out))
        if table is not None and (listing_page or _is_listing(table.headers)):
            continue    # a web server's file listing is navigation, not data
        if table is not None:
            out.append(table)
    return out


# ----------------------------------------------------------------- parsing ---

class _Raw:
    def __init__(self, order: int, skipped: bool):
        self.order = order
        self.skipped = skipped
        self.caption: list[str] = []
        self.rows: list[list[dict]] = []     # cells: text, rowspan, colspan, th
        self.row_in_head: list[bool] = []
        self.in_head = False
        self.row: list[dict] | None = None
        self.cell: dict | None = None
        self.in_caption = False


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[_Raw] = []
        self.done: list[_Raw] = []
        self.count = 0
        self._skip: list | None = None      # [tag, depth] of a hidden element

    # --- structure
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip is not None:
            if tag == self._skip[0]:
                self._skip[1] += 1
            return
        attributes = {k.lower(): (v or "") for k, v in attrs}
        classes = set(attributes.get("class", "").lower().split())
        if tag == "table":
            skipped = bool(classes & _SKIP_TABLE_CLASSES)
            self.stack.append(_Raw(self.count, skipped))
            self.count += 1
            return
        if tag in ("script", "style", "noscript", "template"):
            self._skip = [tag, 1]
            return
        top = self.stack[-1] if self.stack else None
        if top is None:
            return
        if tag not in _VOID and (
                _DISPLAY_NONE.search(attributes.get("style", ""))
                or "hidden" in attributes or "sortkey" in classes
                or (tag == "sup" and "reference" in classes)):
            self._skip = [tag, 1]
            return
        if tag == "caption":
            top.in_caption = True
        elif tag in ("thead", "tbody", "tfoot"):
            self._end_row(top)
            top.in_head = tag == "thead"
        elif tag == "tr":
            self._end_row(top)
            top.row = []
        elif tag in ("td", "th"):
            self._end_cell(top)
            if top.row is None:
                top.row = []
            top.cell = {"text": [], "th": tag == "th",
                        "rowspan": _span(attributes.get("rowspan")),
                        "colspan": _span(attributes.get("colspan"))}
        elif tag in ("br", "p", "div", "li"):
            self._text(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip is not None:
            if tag == self._skip[0]:
                self._skip[1] -= 1
                if self._skip[1] == 0:
                    self._skip = None
            return
        top = self.stack[-1] if self.stack else None
        if top is None:
            return
        if tag == "table":
            self._end_row(top)
            self.stack.pop()
            if not top.skipped:
                self.done.append(top)
                self.done.sort(key=lambda t: t.order)
        elif tag == "caption":
            top.in_caption = False
        elif tag in ("td", "th"):
            self._end_cell(top)
        elif tag == "tr":
            self._end_row(top)
        elif tag in ("thead", "tbody", "tfoot"):
            self._end_row(top)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br":
            self._text(" ")
        elif tag.lower() == "table":
            return

    def handle_data(self, data):
        if self._skip is None:
            self._text(data)

    def _text(self, text):
        if not self.stack:
            return
        top = self.stack[-1]
        if top.in_caption:
            top.caption.append(text)
        elif top.cell is not None:
            top.cell["text"].append(text)

    def _end_cell(self, top):
        if top.cell is not None and top.row is not None:
            top.row.append(top.cell)
        top.cell = None

    def _end_row(self, top):
        self._end_cell(top)
        if top.row is not None:
            if top.row:
                top.rows.append(top.row)
                top.row_in_head.append(top.in_head)
            top.row = None


def _span(value) -> int:
    try:
        return max(1, min(int(str(value).strip()), 100))
    except (TypeError, ValueError):
        return 1


def _clean(parts, limit=CELL_CHARS) -> str:
    text = " ".join("".join(parts).replace(" ", " ").split())
    text = _FOOTNOTE.sub("", text).strip()
    return text[:limit]


# ----------------------------------------------------------------- building ---

def _grid(raw: _Raw):
    """Rows expanded to a rectangle of (text, is_th, colspan) slots; None pads."""
    grid: list[list] = []
    pending: dict[int, list] = {}          # column -> [rows still to fill, slot]

    def fill(line: list, column: int) -> int:
        while column in pending:
            remaining, slot = pending[column]
            line.append(slot)
            if remaining <= 1:
                del pending[column]
            else:
                pending[column][0] -= 1
            column += 1
        return column

    for cells in raw.rows:
        line: list = []
        column = 0
        for cell in cells:
            slot = (_clean(cell["text"]), cell["th"], cell["colspan"])
            for _ in range(cell["colspan"]):
                column = fill(line, column)
                line.append(slot)
                if cell["rowspan"] > 1:
                    pending[column] = [cell["rowspan"] - 1, slot]
                column += 1
        while any(c >= column for c in pending):
            column = fill(line, column)
            if any(c >= column for c in pending):
                line.append(None)
                column += 1
        grid.append(line)
    width = max((len(line) for line in grid), default=0)
    return [line + [None] * (width - len(line)) for line in grid], width


def _build(raw: _Raw, index: int) -> Table | None:
    grid, width = _grid(raw)
    if width < MIN_COLS:
        return None
    # Header rows: the leading rows that are all <th> (or sit in <thead>).
    n_head = 0
    for line, in_head in zip(grid, raw.row_in_head):
        cells = [c for c in line if c is not None]
        if cells and (all(c[1] for c in cells) or (in_head and any(c[1] for c in cells))):
            n_head += 1
        else:
            break
    notes: list[str] = []
    if n_head == 0 and grid:
        n_head = 1
        notes.append("no header row was marked; the first row is used as the header")
    labels: list[str] = []
    for column in range(width):
        parts: list[str] = []
        for line in grid[:n_head]:
            cell = line[column]
            text = cell[0] if cell else ""
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        labels.append(_SEP.join(parts))
    headers = _unique(labels)

    rows: list[dict] = []
    dividers = 0
    totals = 0
    for line in grid[n_head:]:
        cells = [c for c in line if c is not None]
        if not any(c[0] for c in cells):
            continue
        if len({id(c) for c in cells}) == 1 and cells[0][2] >= width > 1:
            dividers += 1              # a section heading spanning every column
            continue
        row = {h: (c[0] if c else "") for h, c in zip(headers, line)}
        first = next((c[0] for c in line if c is not None and c[0]), "")
        if _TOTAL.match(first):
            totals += 1
        rows.append(row)
    if len(rows) < MIN_ROWS:
        return None

    caption = _clean(raw.caption, CAPTION_CHARS)
    scaled = [text for text in [caption, *headers] if text and _SCALE.search(text)]
    for text in dict.fromkeys(scaled):
        notes.append(f"states a scale: {text!r} -- carry it as the scale; "
                     "values are not rescaled here")
    if totals:
        notes.append(f"{totals} row(s) look like totals or subtotals (first cell starts "
                     "total/world/sum): filter them out")
    if dividers:
        notes.append(f"{dividers} section-heading row(s) spanning every column were dropped")
    return Table(index, caption, headers, rows, len(rows), width, notes)


def _is_listing(headers: list[str]) -> bool:
    """Apache/nginx/GitHub directory listings: Name / Last modified / Size ..."""
    names = {h.split(_SEP)[0].strip().lower().rstrip(":") for h in headers}
    names |= {h.strip().lower().rstrip(":") for h in headers}
    return len(names & _LISTING_HEADERS) >= 2


def _is_listing(headers: list[str]) -> bool:
    """Apache/nginx/GitHub directory listings: Name / Last modified / Size ..."""
    names = {h.strip().lower().rstrip(":") for h in headers}
    return len(names & _LISTING_HEADERS) >= 2


def _unique(labels: list[str]) -> list[str]:
    result = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(labels):
        base = raw.strip() or f"column_{index + 1}"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result
