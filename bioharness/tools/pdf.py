"""A minimal PDF writer using only the standard library.

Writes PDF 1.4 with the base-14 fonts (no embedding) for one document shape:
a titled, sectioned text report.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

A4 = (595.28, 841.89)
MARGIN = 56.0

CHAR_W = {"F1": 0.50, "F2": 0.54, "F3": 0.50, "F4": 0.60}
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold",
         "F3": "Helvetica-Oblique", "F4": "Courier"}

FOLD = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "•": "-",
    " ": " ", "→": "->", "≥": ">=", "≤": "<=",
    "×": "x", "′": "'", "″": '"', "µ": "u",
}


def latin(text: str) -> str:
    for bad, good in FOLD.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def escape(text: str) -> str:
    return latin(text).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class Pdf:
    """Flowing text with automatic page breaks. Coordinates are PostScript
    points from the bottom-left, which is why `y` counts down as we go."""

    def __init__(self, title: str = "", subject: str = "",
                 size: tuple[float, float] = A4):
        self.w, self.h = size
        self.title, self.subject = title, subject
        self.pages: list[list[str]] = []
        self.ops: list[str] = []
        self.y = self.h - MARGIN
        self._new_page()

    def _new_page(self) -> None:
        if self.ops:
            self.pages.append(self.ops)
        self.ops = []
        self.y = self.h - MARGIN

    def _room(self, need: float) -> None:
        if self.y - need < MARGIN:
            self._new_page()

    def _wrap(self, text: str, font: str, size: float, width: float) -> list[str]:
        limit = max(8, int(width / (CHAR_W[font] * size)))
        out: list[str] = []
        for para in latin(text).replace("\r", "").split("\n"):
            words, line = para.split(), ""
            if not words:
                out.append("")
                continue
            for word in words:
                nxt = f"{line} {word}".strip()
                if len(nxt) <= limit:
                    line = nxt
                else:
                    if line:
                        out.append(line)
                    while len(word) > limit:
                        out.append(word[:limit - 1] + "-")
                        word = word[limit - 1:]
                    line = word
            if line:
                out.append(line)
        return out

    def text(self, body: str, font: str = "F1", size: float = 10.0,
             leading: float = 13.5, indent: float = 0.0,
             gap: float = 0.0, gray: float = 0.0) -> None:
        width = self.w - 2 * MARGIN - indent
        for line in self._wrap(body, font, size, width):
            self._room(leading)
            if line:
                self.ops.append(
                    f"BT /{font} {size:.1f} Tf {gray:.2f} g "
                    f"{MARGIN + indent:.1f} {self.y:.1f} Td ({escape(line)}) Tj ET")
            self.y -= leading
        self.y -= gap

    def heading(self, text: str) -> None:
        self._room(38)
        self.text(text, font="F2", size=17, leading=21, gap=2)

    def section(self, text: str, rule: bool = True) -> None:
        self._room(34)
        self.text(text, font="F2", size=11.5, leading=15, gap=1)
        if rule:
            self.rule(gap=6)

    def para(self, text: str, gray: float = 0.15) -> None:
        self.text(text, size=10, leading=13.5, gap=5, gray=gray)

    def quote(self, text: str) -> None:
        self.text(text, font="F3", size=10.5, leading=14, indent=12, gap=6, gray=0.2)

    def mono(self, text: str) -> None:
        self.text(text, font="F4", size=8.5, leading=11.5, indent=12, gap=4, gray=0.3)

    def bullet(self, text: str) -> None:
        self._room(14)
        self.ops.append(f"BT /F1 10 Tf 0.15 g {MARGIN + 4:.1f} {self.y:.1f} Td "
                        f"(-) Tj ET")
        self.text(text, size=10, leading=13.5, indent=16, gap=2)

    def kv(self, label: str, value: str) -> None:
        self._room(14)
        self.ops.append(f"BT /F2 9 Tf 0.35 g {MARGIN:.1f} {self.y:.1f} Td "
                        f"({escape(label)}) Tj ET")
        self.text(value, size=9.5, leading=13, indent=96, gap=1)

    def rule(self, gap: float = 10.0, gray: float = 0.75) -> None:
        self._room(gap + 4)
        self.ops.append(f"{gray:.2f} G 0.7 w {MARGIN:.1f} {self.y:.1f} m "
                        f"{self.w - MARGIN:.1f} {self.y:.1f} l S")
        self.y -= gap

    def space(self, amount: float = 8.0) -> None:
        self.y -= amount

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._render())
        return path

    def _render(self) -> bytes:
        pages = [*self.pages, self.ops] if self.ops else self.pages
        stamp = time.strftime("D:%Y%m%d%H%M%S+00'00'")
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_ids = {key: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
                             f"/Encoding /WinAnsiEncoding >>".encode())
                    for key, name in FONTS.items()}
        pages_id = len(objects) + 1 + 2 * len(pages)
        page_ids: list[int] = []
        for ops in pages:
            stream = "\n".join(ops).encode("latin-1", "replace")
            content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                          + stream + b"\nendstream")
            fonts = " ".join(f"/{k} {v} 0 R" for k, v in font_ids.items())
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {self.w:.2f} {self.h:.2f}] "
                f"/Resources << /Font << {fonts} >> >> "
                f"/Contents {content} 0 R >>".encode()))
        kids = " ".join(f"{i} 0 R" for i in page_ids)
        add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
        catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
        info = add(f"<< /Title ({escape(self.title)}) /Subject ({escape(self.subject)}) "
                   f"/Producer (bioharness) /CreationDate ({stamp}) >>".encode())

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R "
                f"/Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)
