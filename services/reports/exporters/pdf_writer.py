"""A minimal PDF 1.4 writer: pages of text in the standard Helvetica fonts, filled
rectangles and lines. Enough for a report — a header, KPI tiles, a bar or line chart and a
paginated table — and nothing else.

Why not a library (docs/design/08-reports.md §7): `fpdf2` is LGPL-3.0, which
THIRD-PARTY-NOTICES.md rules out of the runtime set; WeasyPrint needs Pango and friends in
the image (about 60 MB); ReportLab brings Pillow. The PDF format itself is an open ISO
standard, and the subset used here — the 14 standard fonts need no embedding — fits in one
small file that the tests read back.

Text is encoded as WinAnsi (cp1252), the encoding the standard fonts carry; a character
outside it is written as `?`. Coordinates are PDF points, origin at the bottom left.
"""
from __future__ import annotations

import zlib

#: Helvetica's advance widths for ASCII 32-126, in thousandths of the font size (the
#: Adobe AFM metrics). Used to fit text into table cells; other characters count as 556.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
#: Helvetica-Bold is wider; scaling the regular metrics is close enough to fit cells.
_BOLD_FACTOR = 1.07

A4_LANDSCAPE = (842.0, 595.0)


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = 0
    for ch in text:
        code = ord(ch)
        total += _HELVETICA[code - 32] if 32 <= code <= 126 else 556
    return total * size / 1000 * (_BOLD_FACTOR if bold else 1)


def fit(text: str, width: float, size: float, bold: bool = False) -> str:
    """`text`, cut with an ellipsis to fit `width` points."""
    if text_width(text, size, bold) <= width:
        return text
    ellipsis = "\u2026"
    while text and text_width(text + ellipsis, size, bold) > width:
        text = text[:-1]
    return text + ellipsis if text else ""


def _escape(text: str) -> bytes:
    data = text.encode("cp1252", errors="replace")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(
        b"\r", b" ").replace(b"\n", b" ")


def _color(rgb: tuple[float, float, float]) -> bytes:
    return f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f}".encode()


class PdfDocument:
    def __init__(self, size: tuple[float, float] = A4_LANDSCAPE, title: str = ""):
        self.width, self.height = size
        self.title = title
        self._pages: list[list[bytes]] = []

    def add_page(self) -> None:
        self._pages.append([])

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def _ops(self) -> list[bytes]:
        if not self._pages:
            self.add_page()
        return self._pages[-1]

    def text(self, x: float, y: float, text: str, size: float = 9, bold: bool = False,
             color: tuple[float, float, float] = (0.1, 0.1, 0.12)) -> None:
        font = b"/F2" if bold else b"/F1"
        self._ops().append(b"BT " + font + f" {size:.1f} Tf ".encode() + _color(color)
                           + b" rg " + f"{x:.2f} {y:.2f} Td (".encode() + _escape(text)
                           + b") Tj ET")

    def rect(self, x: float, y: float, w: float, h: float,
             fill: tuple[float, float, float] | None = None,
             stroke: tuple[float, float, float] | None = None, line_width: float = 0.5) -> None:
        ops = f"{line_width:.2f} w ".encode()
        if fill is not None:
            ops += _color(fill) + b" rg "
        if stroke is not None:
            ops += _color(stroke) + b" RG "
        ops += f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re ".encode()
        ops += b"B" if fill is not None and stroke is not None else (
            b"f" if fill is not None else b"S")
        self._ops().append(ops)

    def line(self, x1: float, y1: float, x2: float, y2: float,
             color: tuple[float, float, float] = (0.6, 0.6, 0.65), width: float = 0.5,
             dash: bool = False) -> None:
        ops = f"{width:.2f} w ".encode() + _color(color) + b" RG "
        ops += b"[3 2] 0 d " if dash else b"[] 0 d "
        ops += f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S".encode()
        self._ops().append(ops)

    def polyline(self, points: list[tuple[float, float]], color: tuple[float, float, float],
                 width: float = 1.2, dash: bool = False) -> None:
        if len(points) < 2:
            return
        ops = f"{width:.2f} w ".encode() + _color(color) + b" RG "
        ops += b"[3 2] 0 d " if dash else b"[] 0 d "
        ops += f"{points[0][0]:.2f} {points[0][1]:.2f} m ".encode()
        ops += b" ".join(f"{x:.2f} {y:.2f} l".encode() for x, y in points[1:])
        ops += b" S"
        self._ops().append(ops)

    def polygon(self, points: list[tuple[float, float]],
                fill: tuple[float, float, float]) -> None:
        if len(points) < 3:
            return
        ops = _color(fill) + b" rg " + f"{points[0][0]:.2f} {points[0][1]:.2f} m ".encode()
        ops += b" ".join(f"{x:.2f} {y:.2f} l".encode() for x, y in points[1:])
        ops += b" h f"
        self._ops().append(ops)

    def render(self) -> bytes:
        if not self._pages:
            self.add_page()
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog_id = add(b"")  # filled in below, once the pages object id is known
        pages_id = add(b"")
        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                           b"/Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                        b"/Encoding /WinAnsiEncoding >>")
        page_ids = []
        for ops in self._pages:
            stream = zlib.compress(b"\n".join(ops))
            content_id = add(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n"
                             .encode() + stream + b"\nendstream")
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {self.width:.0f} "
                f"{self.height:.0f}] /Resources << /Font << /F1 {font_regular} 0 R /F2 "
                f"{font_bold} 0 R >> >> /Contents {content_id} 0 R >>".encode()))
        objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"\
            .encode()
        info_id = add(b"<< /Producer (PMT reports) /Title (" + _escape(self.title) + b") >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
                f"/Info {info_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)
