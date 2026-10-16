"""Turn the Markdown the engineering report emits into reportlab flowables.

The PDF report prints the same facts-layer strings as the engineering report, so
it reuses that report's Markdown builders instead of keeping a second copy of the
wording. Those builders emit a small, known subset of Markdown: ``#`` headings,
paragraphs, ``-`` bullets nested by two spaces, pipe tables, ``**bold**``,
``*italic*``, inline code and backslash escapes (``escaping.md_text``). This
module renders exactly that subset. Nothing is clipped: a long cell wraps and a
long table flows over pages with its header row repeated.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.ttfonts import TTFontFile
from reportlab.platypus import Flowable, Paragraph, Spacer, Table, TableStyle

_ARROW = re.compile(r"\s*→\s*")
# ``escaping.md_code`` strips backslashes from real code, so any inside a code span
# are escapes ``md_text`` added to free text that held a backtick pair.
_BACKSLASH = re.compile(r"\\(.)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)-\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")


@dataclass(frozen=True)
class MdStyles:
    """The paragraph styles the converter draws with (built by ``pdf_report``)."""

    body: ParagraphStyle
    h2: ParagraphStyle
    h3: ParagraphStyle
    cell: ParagraphStyle
    head_cell: ParagraphStyle
    bullets: tuple[ParagraphStyle, ...]
    code_font: str
    table_head_bg: Any
    table_grid: Any
    table_zebra: Any


_GLYPHS: dict[str, set[int]] = {}


def known_glyphs(font_path: str) -> set[int]:
    """Code points a TTF can draw, read once per file."""
    if font_path not in _GLYPHS:
        _GLYPHS[font_path] = set(TTFontFile(font_path).charToGlyph)
    return _GLYPHS[font_path]


def printable(text: str, glyphs: set[int] | None) -> str:
    """``text`` with what the bundled font cannot draw replaced, never dropped.

    Ember has no arrow (U+2192), which would render as an empty box; the readable
    form of "A -> B" is "A to B". Any other character without a glyph becomes "?"
    so the reader sees that something stood there.
    """
    text = _ARROW.sub(" to ", text)
    if glyphs is None:
        return text
    return "".join(ch if (ord(ch) in glyphs or ch in "\n\t ") else "?" for ch in text)


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(text: str, styles: MdStyles, glyphs: set[int] | None) -> str:
    """Inline Markdown -> reportlab paragraph markup."""
    out: list[str] = []
    i, n = 0, len(text)

    def literal(chunk: str) -> str:
        return _xml(printable(html.unescape(chunk), glyphs))

    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(literal("".join(buf)))
            buf.clear()

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            if j > i:
                flush()
                out.append(
                    f'<font name="{styles.code_font}" color="#0B5394">'
                    f"{literal(_BACKSLASH.sub(r'\1', text[i + 1 : j]))}</font>"
                )
                i = j + 1
                continue
        if text.startswith("**", i):
            j = text.find("**", i + 2)
            if j > i + 1:
                flush()
                out.append(f"<b>{inline(text[i + 2 : j], styles, glyphs)}</b>")
                i = j + 2
                continue
        if ch == "*":
            j = text.find("*", i + 1)
            if j > i + 1:
                flush()
                out.append(f"<i>{inline(text[i + 1 : j], styles, glyphs)}</i>")
                i = j + 1
                continue
        buf.append(ch)
        i += 1
    flush()
    return "".join(out)


def _split_row(line: str) -> list[str]:
    """Cells of one pipe-table row; ``\\|`` is a literal pipe."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if body[i] == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(body[i])
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _col_widths(rows: list[list[str]], total: float) -> list[float]:
    """Column widths in points, proportional to the longest cell (bounded), never clipped."""
    n = len(rows[0])
    weights = []
    for c in range(n):
        longest = max((len(r[c]) if c < len(r) else 0) for r in rows)
        weights.append(min(max(longest, 7), 70) ** 0.75)
    floor = 0.07 * total
    raw = [total * w / sum(weights) for w in weights]
    widths = [max(w, floor) for w in raw]
    scale = total / sum(widths)
    return [w * scale for w in widths]


def _table(rows: list[list[str]], styles: MdStyles, glyphs: set[int] | None, width: float):
    n = len(rows[0])
    data: list[list[Paragraph]] = []
    for r, row in enumerate(rows):
        padded = [*row, *[""] * (n - len(row))][:n]
        style = styles.head_cell if r == 0 else styles.cell
        data.append([Paragraph(inline(c, styles, glyphs) or "&nbsp;", style) for c in padded])
    tbl = Table(data, colWidths=_col_widths(rows, width), repeatRows=1)
    cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), styles.table_head_bg),
        ("GRID", (0, 0), (-1, -1), 0.4, styles.table_grid),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in range(2, len(data), 2):
        cmds.append(("BACKGROUND", (0, r), (-1, r), styles.table_zebra))
    tbl.setStyle(TableStyle(cmds))
    return tbl


def to_flowables(
    lines: list[str],
    styles: MdStyles,
    *,
    width: float,
    glyphs: set[int] | None,
) -> list[Flowable]:
    """Render the Markdown ``lines`` as flowables, in order."""
    out: list[Flowable] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            out.append(Paragraph(inline(" ".join(para), styles, glyphs), styles.body))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        if stripped == "---":
            flush_para()
            i += 1
            continue
        m = _HEADING.match(stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            if level >= 2:
                style = styles.h2 if level == 2 else styles.h3
                out.append(Paragraph(inline(m.group(2), styles, glyphs), style))
            i += 1
            continue
        if (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEP.match(lines[i + 1].strip())
        ):
            flush_para()
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_table(rows, styles, glyphs, width))
            out.append(Spacer(1, 6))
            continue
        b = _BULLET.match(line)
        if b:
            flush_para()
            level = min(len(b.group(1)) // 2, len(styles.bullets) - 1)
            out.append(
                Paragraph(
                    inline(b.group(2), styles, glyphs),
                    styles.bullets[level],
                    bulletText="•" if level == 0 else "–",
                )
            )
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return out
