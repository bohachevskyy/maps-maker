import io
import struct
import zipfile

import pytest

from mapsvc.discovery import htmltables, probe, xls


def rec(rid, body=b""):
    return struct.pack("<HH", rid, len(body)) + body


def ustr(text, length_size=2, wide=False):
    flag = 1 if wide else 0
    data = text.encode("utf-16-le") if wide else text.encode("latin-1")
    return len(text).to_bytes(length_size, "little") + bytes([flag]) + data


def rk_int(value, div100=False):
    return ((value << 2) | 2 | (1 if div100 else 0)) & 0xFFFFFFFF


def workbook(sst_split=True):
    long_text = "Alpha-beta-gamma"
    strings = ["Place", "Population", long_text, "São Paulo"]
    # SST: 4 strings; the third one is cut across a CONTINUE seam and the
    # remainder is stored wide (flag byte re-read at the seam).
    head = struct.pack("<II", 4, 4)
    body = head + ustr("Place") + ustr("Population")
    body += struct.pack("<H", len(long_text)) + b"\x00" + long_text[:5].encode()
    cont = b"\x01" + long_text[5:].encode("utf-16-le")
    cont += ustr("São Paulo", wide=True)
    sst = rec(0x00FC, body) + rec(0x003C, cont) if sst_split else None

    def sheet_stream(name_pos):
        out = rec(0x0809, struct.pack("<HHHH", 0x600, 0x10, 0, 0) + b"\0" * 8)
        row = lambda r, c, i: rec(0x00FD, struct.pack("<HHHI", r, c, 0, i))
        out += row(0, 0, 0) + row(0, 1, 1)
        out += row(1, 0, 3) + rec(0x0203, struct.pack("<HHHd", 1, 1, 0, 46081801.0))
        out += row(2, 0, 2) + rec(0x027E, struct.pack("<HHHI", 2, 1, 0, rk_int(1234)))
        out += rec(0x00BD, struct.pack("<HH", 3, 0) + struct.pack("<HI", 0, rk_int(5)) +
                   struct.pack("<HI", 0, rk_int(12345, True)) + struct.pack("<H", 1))
        out += rec(0x0204, struct.pack("<HHH", 4, 0, 0) + ustr("lbl"))
        out += rec(0x0006, struct.pack("<HHH", 4, 1, 0) + struct.pack("<d", 2.5) +
                   b"\0" * 6)
        out += rec(0x0006, struct.pack("<HHH", 5, 0, 0) + b"\x00\0\0\0\0\0\xff\xff" +
                   b"\0" * 6) + rec(0x0207, ustr("formula text"))
        out += rec(0x0205, struct.pack("<HHHBB", 5, 1, 0, 1, 0))
        return out + rec(0x000A)

    globals_ = rec(0x0809, struct.pack("<HHHH", 0x600, 5, 0, 0) + b"\0" * 8)
    sheet_name = b"\x06\x00" + b"Folha1"                      # cch=6, flags=0
    sheet_rec_len = 4 + 4 + 2 + len(b"\x06\x00Folha1")
    chart = rec(0x0085, struct.pack("<IBB", 0, 0, 2) + b"\x01\x00C")
    tail = (sst or b"") + rec(0x000A)
    size_before = len(globals_) + sheet_rec_len + len(chart) + len(tail)
    sheet_offset = size_before
    bs = rec(0x0085, struct.pack("<IBB", sheet_offset, 0, 0) + sheet_name)
    assert len(bs) == sheet_rec_len
    return globals_ + bs + chart + tail + sheet_stream(0), strings


def cfb(stream, *, mini, difat_sector=False):
    """A minimal compound file holding one 'Workbook' stream."""
    ss = 512
    if mini:
        assert len(stream) < 4096
    else:
        stream = stream.ljust(5000, b"\0")
    entries = []

    def entry(name, kind, start, size, child=0xFFFFFFFF):
        raw = name.encode("utf-16-le")
        e = raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw) + 2, kind, 1)
        e += struct.pack("<III", 0xFFFFFFFF, 0xFFFFFFFF, child)
        e += b"\0" * 36
        e = e[:116] + struct.pack("<IQ", start, size)
        return e.ljust(128, b"\0")

    pad = lambda b: b.ljust(-(-len(b) // ss) * ss, b"\0")
    sectors = []          # data sectors after FAT (and optional DIFAT)
    FAT_ID = 0
    next_id = 1
    fat = {0: 0xFFFFFFFD}
    difat_id = None
    if difat_sector:
        difat_id = next_id
        next_id += 1
        fat[difat_id] = 0xFFFFFFFC
    dir_id = next_id
    next_id += 1
    fat[dir_id] = 0xFFFFFFFE
    if mini:
        mini_data = pad(stream)
        # mini sectors of 64 bytes
        nmini = -(-len(stream) // 64)
        minifat = [i + 1 for i in range(nmini - 1)] + [0xFFFFFFFE]
        minifat_id = next_id
        fat[minifat_id] = 0xFFFFFFFE
        next_id += 1
        mini_stream_id = next_id
        n = len(mini_data) // ss
        for i in range(n):
            fat[mini_stream_id + i] = mini_stream_id + i + 1 if i < n - 1 else 0xFFFFFFFE
        next_id += n
        root = entry("Root Entry", 5, mini_stream_id, len(stream))
        wb = entry("Workbook", 2, 0, len(stream))
        body = pad(b"".join(struct.pack("<I", v) for v in minifat))
        body = body.ljust(ss, b"\xff")
        data_sectors = [pad(root + wb), body, mini_data]
        n_minifat, minifat_start = 1, minifat_id
    else:
        wb_id = next_id
        n = len(stream) // ss + (1 if len(stream) % ss else 0)
        for i in range(n):
            fat[wb_id + i] = wb_id + i + 1 if i < n - 1 else 0xFFFFFFFE
        next_id += n
        root = entry("Root Entry", 5, 0xFFFFFFFE, 0)
        wb = entry("Workbook", 2, wb_id, len(stream))
        data_sectors = [pad(root + wb), pad(stream)]
        n_minifat, minifat_start = 0, 0xFFFFFFFE
    fat_sector = b"".join(struct.pack("<I", fat.get(i, 0xFFFFFFFF)) for i in range(128))
    header = bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 16
    header += struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 10
    header += struct.pack("<IIIIIIII", 1, dir_id, 0, 4096, minifat_start, n_minifat,
                          difat_id if difat_sector else 0xFFFFFFFE, 1 if difat_sector else 0)
    if difat_sector:
        header += b"\xff" * (109 * 4)
    else:
        header += struct.pack("<I", FAT_ID) + b"\xff" * (108 * 4)
    assert len(header) == 512
    out = header + fat_sector
    if difat_sector:
        out += struct.pack("<I", FAT_ID).ljust(508, b"\xff") + struct.pack("<I", 0xFFFFFFFE)
    return out + b"".join(data_sectors)


def expected(sheets):
    rows = sheets["Folha1"]
    assert rows[0] == {"A": "Place", "B": "Population"}
    assert rows[1] == {"A": "São Paulo", "B": "46081801"}
    assert rows[2] == {"A": "Alpha-beta-gamma", "B": "1234"}
    assert rows[3] == {"A": "5", "B": "123.45"}
    assert rows[4] == {"A": "lbl", "B": "2.5"}
    assert rows[5] == {"A": "formula text", "B": "TRUE"}


@pytest.mark.parametrize("mini,difat", [(True, False), (False, False), (False, True)])
def test_round_trip(mini, difat):
    stream, _ = workbook()
    if mini and len(stream) >= 4096:
        pytest.skip("too big")
    found = xls.sheets(cfb(stream, mini=mini, difat_sector=difat))
    assert list(found) == ["Folha1"]            # the chart sheet is skipped
    expected(found)


def test_probe_reads_xls_and_zipped_xls():
    stream, _ = workbook()
    data = cfb(stream, mini=True)
    report = probe.probe_bytes(data, url="https://e.org/a.xls")
    assert report.format == "xls"
    assert report.n_rows == 5
    assert [c["name"] for c in report.columns] == ["Place", "Population"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.xls", data)
    zipped = probe.probe_bytes(buf.getvalue())
    assert zipped.format == "xls" and zipped.n_rows == 5
    rows, fmt, _, _ = probe.records(data)
    assert fmt == "xls" and rows[0] == {"Place": "São Paulo", "Population": "46081801"}


def test_errors_are_clear():
    with pytest.raises(xls.XlsError):
        xls.sheets(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600)
    stream, _ = workbook()
    encrypted = stream[:20] + rec(0x002F, b"\0" * 6) + stream[20:]
    with pytest.raises(xls.XlsError, match="encrypted"):
        xls.sheets(cfb(encrypted, mini=True))
    with pytest.raises(probe.ProbeError, match="unreadable xls"):
        probe.records(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600)


def test_directory_listing_tables_are_not_data():
    rows = "".join(f"<tr><td><a href='f{i}'>f{i}.xls</a></td><td>2025-01-0{i} 10:00</td>"
                   f"<td>{i}K</td><td>&nbsp;</td></tr>" for i in range(1, 8))
    listing = ("<html><head><title>Index of /pub</title></head><body><table>"
               "<tr><th>Name</th><th>Last modified</th><th>Size</th><th>Description</th></tr>"
               f"{rows}</table></body></html>")
    assert htmltables.tables(listing) == []
    untitled = listing.replace("Index of /pub", "Files")
    assert htmltables.tables(untitled) == []
    real = ("<table><tr><th>Name</th><th>Population</th></tr>"
            + "".join(f"<tr><td>P{i}</td><td>{i}</td></tr>" for i in range(6)) + "</table>")
    assert len(htmltables.tables(real)) == 1
