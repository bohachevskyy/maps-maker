"""Legacy .xls (Excel 97-2003, BIFF8 in an OLE2 compound file) as sheets. Pure.

Statistical offices still publish these (IBGE's population estimates are
.xls). The container is a FAT-style filesystem in one file; the "Workbook"
stream inside it is a sequence of typed records. Only cell values are read:
no formats, styles or dates (a date stays its serial number).

The result has the shape of `mapsvc.xlsx.sheets`: sheet name -> rows, each row
a dict of column letter -> value text. Empty cells are omitted.
"""

from __future__ import annotations

import struct

MAX_BYTES = 200 * 1024 * 1024
MAX_CELLS = 1_000_000

_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREE, _END, _FATSECT, _DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!",
           0x1D: "#NAME?", 0x24: "#NUM!", 0x2A: "#N/A"}


class XlsError(ValueError):
    pass


def is_ole2(payload: bytes) -> bool:
    return payload.startswith(_MAGIC)


def sheets(payload: bytes) -> dict[str, list[dict[str, object]]]:
    """{sheet name: [{column letter: value}, ...]} for every worksheet, in order."""
    if len(payload) > MAX_BYTES:
        raise XlsError(f"xls file exceeds the {MAX_BYTES // 2**20} MiB limit")
    return _parse_workbook(_workbook_stream(payload))


# ------------------------------------------------------------ OLE2 / CFB ---

def _workbook_stream(data: bytes) -> bytes:
    if len(data) < 512 or not data.startswith(_MAGIC):
        raise XlsError("not an OLE2 compound file")
    shift, mini_shift = struct.unpack_from("<HH", data, 0x1E)
    if not (7 <= shift <= 16) or not (2 <= mini_shift <= shift):
        raise XlsError("corrupt compound file header")
    size, mini_size = 1 << shift, 1 << mini_shift
    (n_fat, dir_start, _, cutoff, minifat_start, n_minifat,
     difat_start, n_difat) = struct.unpack_from("<IIIIIIII", data, 0x2C)

    def sector(sid: int) -> bytes:
        offset = (sid + 1) * size
        if offset >= len(data):
            raise XlsError("compound file sector is out of range")
        return data[offset:offset + size]

    # FAT sector ids: 109 in the header, the rest in a chain of DIFAT sectors.
    fat_ids = [v for v in struct.unpack_from("<109I", data, 0x4C) if v != _FREE]
    per = size // 4 - 1
    sid, guard = difat_start, 0
    while sid not in (_END, _FREE) and guard < max(n_difat, 1) + 4:
        block = sector(sid)
        fat_ids += [v for v in struct.unpack_from(f"<{per}I", block) if v != _FREE]
        sid = struct.unpack_from("<I", block, per * 4)[0]
        guard += 1
    fat: list[int] = []
    for fid in fat_ids[:max(n_fat, len(fat_ids))]:
        fat += struct.unpack(f"<{size // 4}I", sector(fid).ljust(size, b"\0"))

    def chain(start: int, table: list[int]) -> list[int]:
        out, seen = [], set()
        while start < _DIFSECT:
            if start in seen or start >= len(table):
                raise XlsError("corrupt compound file chain")
            seen.add(start)
            out.append(start)
            start = table[start]
        return out

    def read_chain(start: int) -> bytes:
        return b"".join(sector(s) for s in chain(start, fat))

    directory = read_chain(dir_start)
    entries = []
    for at in range(0, len(directory) - 127, 128):
        raw = directory[at:at + 128]
        name_len = struct.unpack_from("<H", raw, 64)[0]
        name = raw[:max(0, min(name_len, 64) - 2)].decode("utf-16-le", "replace")
        kind = raw[66]
        start = struct.unpack_from("<I", raw, 116)[0]
        length = struct.unpack_from("<Q", raw, 120)[0]
        entries.append((name, kind, start, length))
    root = next((e for e in entries if e[1] == 5), None)
    stream = next((e for e in entries if e[1] == 2 and e[0] in ("Workbook", "Book")), None)
    if stream is None:
        raise XlsError("no Workbook stream in the compound file (not an Excel workbook)")
    _, _, start, length = stream
    if length > MAX_BYTES:
        raise XlsError("workbook stream exceeds the size limit")
    if length >= cutoff:
        return read_chain(start)[:length]
    if root is None:
        raise XlsError("compound file has no root entry")
    minifat = []
    for s in chain(minifat_start, fat) if n_minifat else []:
        minifat += struct.unpack(f"<{size // 4}I", sector(s).ljust(size, b"\0"))
    ministream = read_chain(root[2])[:root[3]]
    out = b"".join(ministream[s * mini_size:(s + 1) * mini_size]
                   for s in chain(start, minifat))
    return out[:length]


# ----------------------------------------------------------------- BIFF8 ---

def _records(stream: bytes, at: int = 0):
    """Yield (id, payload, continues) with CONTINUE payloads kept separate."""
    n = len(stream)
    while at + 4 <= n:
        rid, length = struct.unpack_from("<HH", stream, at)
        yield rid, stream[at + 4:at + 4 + length]
        at += 4 + length


def _parse_workbook(stream: bytes) -> dict[str, list[dict[str, object]]]:
    if len(stream) < 8 or struct.unpack_from("<H", stream, 0)[0] != 0x0809:
        raise XlsError("workbook stream does not start with a BOF record")
    if struct.unpack_from("<H", stream, 4)[0] < 0x0600:
        raise XlsError("only BIFF8 (Excel 97-2003) workbooks are supported, not older BIFF")

    boundsheets: list[tuple[int, int, str]] = []
    sst_segments: list[bytes] = []
    in_sst = False
    for rid, body in _records(stream):
        if rid == 0x002F:
            raise XlsError("the workbook is encrypted (FILEPASS) and cannot be read")
        if rid == 0x00FC:
            sst_segments, in_sst = [body[8:]], True
            continue
        if rid == 0x003C and in_sst:
            sst_segments.append(body)
            continue
        in_sst = False
        if rid == 0x0085 and len(body) >= 8:
            offset, _, kind = struct.unpack_from("<IBB", body, 0)
            name, _ = _unicode(_Segments([body[6:]]), 1)
            boundsheets.append((offset, kind, name))
        elif rid == 0x000A:
            break
    strings = _read_sst(sst_segments)

    out: dict[str, list[dict[str, object]]] = {}
    budget = [MAX_CELLS]
    for offset, kind, name in boundsheets:
        if kind != 0:       # chart, macro, VB module
            continue
        base = name
        n = 2
        while name in out:
            name, n = f"{base}_{n}", n + 1
        out[name] = _parse_sheet(stream, offset, strings, budget)
    return out


class _Segments:
    """Record payloads read as one string of bytes that remembers its seams."""

    def __init__(self, parts: list[bytes]):
        self.parts = parts
        self.part = 0
        self.at = 0

    def left(self) -> int:
        return len(self.parts[self.part]) - self.at if self.part < len(self.parts) else 0

    def done(self) -> bool:
        while self.part < len(self.parts) and self.at >= len(self.parts[self.part]):
            self.part, self.at = self.part + 1, 0
        return self.part >= len(self.parts)

    def take(self, n: int) -> bytes:
        """n bytes within the current record (callers handle seams for chars)."""
        if self.done():
            raise XlsError("truncated string table")
        chunk = self.parts[self.part][self.at:self.at + n]
        self.at += len(chunk)
        return chunk

    def skip(self, n: int) -> None:
        while n > 0 and not self.done():
            step = min(n, self.left())
            self.at += step
            n -= step


def _read_sst(segments: list[bytes]) -> list[str]:
    if not segments:
        return []
    reader = _Segments(segments)
    out = []
    while not reader.done():
        try:
            text, _ = _unicode(reader, 2)
        except (XlsError, struct.error):
            break
        out.append(text)
    return out


def _unicode(reader: _Segments, length_size: int, *, rich: bool = True
             ) -> tuple[str, int]:
    """One XLUnicodeString from `reader`; characters may span CONTINUE seams."""
    head = reader.take(length_size)
    cch = int.from_bytes(head, "little")
    flags = reader.take(1)[0]
    runs = int.from_bytes(reader.take(2), "little") if flags & 0x08 else 0
    ext = int.from_bytes(reader.take(4), "little") if flags & 0x04 else 0
    wide = bool(flags & 0x01)
    chars: list[str] = []
    remaining = cch
    while remaining > 0:
        if reader.left() == 0:
            if reader.done():
                raise XlsError("truncated string")
            wide = bool(reader.take(1)[0] & 0x01)    # flag byte re-read at a seam
            continue
        width = 2 if wide else 1
        n = min(remaining, reader.left() // width)
        if n == 0:               # a wide char cut by the seam; cannot happen in valid files
            raise XlsError("string character split across records")
        raw = reader.take(n * width)
        chars.append(raw.decode("utf-16-le", "replace") if wide else raw.decode("latin-1"))
        remaining -= n
    reader.skip(4 * runs + ext)
    return "".join(chars), cch


def _column(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _number(value: float) -> str:
    if value == value and abs(value) < 1e15 and value == int(value):
        return str(int(value))
    return repr(value)


def _rk(raw: int) -> str:
    if raw & 2:
        value = float(struct.unpack("<i", struct.pack("<I", raw))[0] >> 2)
    else:
        value = struct.unpack("<d", struct.pack("<Q", (raw & 0xFFFFFFFC) << 32))[0]
    return _number(value / 100 if raw & 1 else value)


def _parse_sheet(stream: bytes, offset: int, strings: list[str], budget: list[int]
                 ) -> list[dict[str, object]]:
    cells: dict[int, dict[str, object]] = {}
    pending: tuple[int, int] | None = None     # formula awaiting its STRING record
    depth = 0

    def put(row: int, col: int, value: object) -> None:
        budget[0] -= 1
        if budget[0] < 0:
            raise XlsError(f"workbook exceeds the {MAX_CELLS:,} cell limit")
        if value != "":
            cells.setdefault(row, {})[_column(col)] = value

    for rid, body in _records(stream, offset):
        if rid == 0x0809:
            depth += 1
            continue
        if rid == 0x000A:
            depth -= 1
            if depth <= 0:
                break
            continue
        if depth != 1:
            continue
        if rid == 0x00FD and len(body) >= 10:              # LABELSST
            row, col, _, index = struct.unpack_from("<HHHI", body)
            put(row, col, strings[index] if index < len(strings) else "")
        elif rid in (0x0204, 0x00D6) and len(body) >= 8:   # LABEL, RSTRING
            row, col = struct.unpack_from("<HH", body)
            try:
                put(row, col, _unicode(_Segments([body[6:]]), 2)[0])
            except (XlsError, struct.error):
                pass
        elif rid == 0x0203 and len(body) >= 14:            # NUMBER
            row, col = struct.unpack_from("<HH", body)
            put(row, col, _number(struct.unpack_from("<d", body, 6)[0]))
        elif rid == 0x027E and len(body) >= 10:            # RK
            row, col = struct.unpack_from("<HH", body)
            put(row, col, _rk(struct.unpack_from("<I", body, 6)[0]))
        elif rid == 0x00BD and len(body) >= 6:             # MULRK
            row, first = struct.unpack_from("<HH", body)
            count = (len(body) - 6) // 6
            for k in range(count):
                put(row, first + k, _rk(struct.unpack_from("<I", body, 4 + 6 * k + 2)[0]))
        elif rid == 0x0006 and len(body) >= 14:            # FORMULA
            row, col = struct.unpack_from("<HH", body)
            result = body[6:14]
            if result[6:8] == b"\xff\xff":
                if result[0] == 0:
                    pending = (row, col)
                elif result[0] == 1:
                    put(row, col, "TRUE" if result[2] else "FALSE")
                elif result[0] == 2:
                    put(row, col, _ERRORS.get(result[2], "#ERR"))
            else:
                put(row, col, _number(struct.unpack("<d", result)[0]))
        elif rid == 0x0207 and pending is not None:        # STRING
            try:
                put(*pending, _unicode(_Segments([body]), 2)[0])
            except (XlsError, struct.error):
                pass
            pending = None
        elif rid == 0x0205 and len(body) >= 8:             # BOOLERR
            row, col = struct.unpack_from("<HH", body)
            value, is_error = body[6], body[7]
            put(row, col, _ERRORS.get(value, "#ERR") if is_error
                else ("TRUE" if value else "FALSE"))
    return [cells[row] for row in sorted(cells)]
