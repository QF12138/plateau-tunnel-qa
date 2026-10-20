"""Conservative reading order for native columns and explicitly marked OCR.

Source glyphs are not corrected or synthesized. A legacy scan format's private
line/column separators are decoded before geometry can misorder its OCR. Other
ambiguous/single-column pages keep PyMuPDF's sorted extraction. This is not a
table, formula or image understanding engine; retain the source for verification.
"""

from dataclasses import dataclass, replace
import re
from statistics import median
import unicodedata


@dataclass(frozen=True)
class _TextLine:
    text: str
    x0: float
    x1: float
    baseline: float
    size: float


def _legacy_ocr_text(page) -> str | None:
    """Decode an identifiable scanned-PDF OCR stream, never repair its glyphs.

    These documents encode spacing with private-use characters whose font sizes
    can be hundreds of times larger than their visible neighbours. PyMuPDF's
    geometric text sorter consequently separates decimals and interleaves rows.
    The paired line terminators and explicit column/space tokens already carry
    the intended order. Require both that signature and a page-sized scan so a
    native PDF using private-use mathematical glyphs is not treated as OCR.
    """
    import pymupdf

    blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
    stream = "".join(
        span["text"] for block in blocks for line in block.get("lines", [])
        for span in line.get("spans", [])
    )
    if stream.count("\ue5d2\ue5cf") < 4:
        return None
    page_area = page.rect.get_area()
    if not page_area or not any(
        (pymupdf.Rect(info["bbox"]) & page.rect).get_area() >= page_area * .75
        for info in page.get_image_info()
    ):
        return None
    # Only decode the four verified structural tokens. Other private-use
    # glyphs, question marks and visually suspect numbers remain unchanged.
    text = stream.replace("\ue5d2\ue5cf", "\n").replace("\ue5ce", "\t").replace("\ue5e5", " ")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def page_text_warnings(page) -> list[str]:
    """Page-level signals for a caller's review/quarantine policy.

    The signals do not claim the visual table or figure was understood. In
    particular, numeric OCR values must not be silently corrected from context.
    No source content is discarded here; callers retain the original page.
    """
    text = _legacy_ocr_text(page)
    if text is None:
        return []
    normalized = unicodedata.normalize("NFKC", text)
    rows = normalized.splitlines()
    warnings = ["legacy_ocr_text_layer"]
    has_table_caption = any(re.match(r"^(?:表\s*\d|Tab(?:le)?\.?\s*\d)", row, re.I) for row in rows)
    numbered_table_rows = sum(bool(re.match(r"^\d{1,3}\s*\t\s*\S.*\t\s*\S", row)) for row in rows)
    if has_table_caption and numbered_table_rows >= 5:
        warnings.append("legacy_ocr_numeric_table_requires_review")
    has_figure_caption = any(re.match(r"^(?:图\s*\d|Fig(?:ure)?\.?\s*\d)", row, re.I) for row in rows)
    prose = [row for row in rows if (
        (len(re.findall(r"[\u4e00-\u9fff]", row)) >= 25
         or len(re.findall(r"\b[A-Za-z]{2,}\b", row)) >= 10)
        and not re.match(r"^(?:图\s*\d|表\s*\d|Fig(?:ure)?\.?\s*\d|Tab(?:le)?\.?\s*\d|[（(][a-zA-Z][)）])", row, re.I)
    )]
    if has_figure_caption and not prose:
        warnings.append("legacy_ocr_figure_page_requires_review")
    return warnings


def _lines(page) -> list[_TextLine]:
    import pymupdf

    # TEXTFLAGS_TEXT avoids materializing embedded images in the dict response.
    blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
    result = []
    for block in blocks:
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span["text"] for span in spans).strip()
            if not text:
                continue
            main_size = max(span["size"] for span in spans)
            main_spans = [span for span in spans if span["size"] >= main_size * .9]
            result.append(_TextLine(
                text, line["bbox"][0], line["bbox"][2],
                median(span["origin"][1] for span in main_spans),
                main_size,
            ))
    return result


def _column_bounds(lines: list[_TextLine], width: float) -> tuple[float, float] | None:
    center = width / 2
    tolerance = max(2, width * .005)
    # Short table cells, figures, headings and sparse page decorations are not
    # sufficient evidence of two prose columns.
    prose = [line for line in lines if len(line.text) >= 16 and line.x1 - line.x0 >= width * .27]
    left = [line for line in prose if line.x1 <= center + tolerance]
    right = [line for line in prose if line.x0 >= center - tolerance]
    if min(len(left), len(right)) < 4:
        return None
    left_edge = max(line.x1 for line in left)
    right_edge = min(line.x0 for line in right)
    gap = right_edge - left_edge
    if not width * .008 <= gap <= width * .20:
        return None
    overlap = min(max(line.baseline for line in left), max(line.baseline for line in right)) - max(
        min(line.baseline for line in left), min(line.baseline for line in right)
    )
    if overlap < median(line.size for line in left + right) * 3:
        return None
    paired = sum(any(abs(a.baseline - b.baseline) <= max(a.size, b.size) for b in right) for a in left)
    return (left_edge, right_edge) if paired >= 4 else None


def _column_split(lines: list[_TextLine], width: float) -> float | None:
    bounds = _column_bounds(lines, width)
    return sum(bounds) / 2 if bounds else None


def _rows(lines: list[_TextLine]) -> list[str]:
    """Join same-baseline fragments, e.g. a sentence and its citation marker."""
    anchored = []
    for line in lines:
        # A separately encoded small exponent must not sort ahead of its base
        # merely because it is raised on the page. Anchor only adjacent numeric
        # scripts; retain their original glyphs, without guessing a formula.
        candidates = [parent for parent in lines if (
            line.size < parent.size * .8
            and -parent.size * .2 <= line.x0 - parent.x1 <= parent.size * .7
            and parent.size * .2 <= abs(line.baseline - parent.baseline) <= parent.size * .8
        )] if re.fullmatch(r"[+\-−]?\d+", line.text) else []
        if candidates:
            parent = min(candidates, key=lambda item: abs(item.x1 - line.x0))
            line = replace(line, baseline=parent.baseline)
        anchored.append(line)
    rows: list[list[_TextLine]] = []
    for line in sorted(anchored, key=lambda item: (item.baseline, item.x0)):
        if rows and abs(line.baseline - rows[-1][0].baseline) <= min(line.size, rows[-1][0].size) * .25:
            rows[-1].append(line)
        else:
            rows.append([line])
    result = []
    for row in rows:
        row.sort(key=lambda item: item.x0)
        text = row[0].text
        for previous, current in zip(row, row[1:]):
            gap = current.x0 - previous.x1
            text += (" " if gap > min(previous.size, current.size) * .3 else "") + current.text
        result.append(text)
    return result


def _has_spanning_table_grid(page, split: float) -> bool:
    """Do not read two long table cells as unrelated prose columns."""
    horizontal, vertical = [], []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < 1 and min(a.x, b.x) < split - 10 < split + 10 < max(a.x, b.x):
                    horizontal.append((min(a.x, b.x), a.y, max(a.x, b.x)))
                elif abs(a.x - b.x) < 1:
                    vertical.append((a.x, min(a.y, b.y), max(a.y, b.y)))
            elif item[0] == "re":
                rect = item[1]
                if rect.x0 < split - 10 < split + 10 < rect.x1:
                    horizontal.extend([(rect.x0, rect.y0, rect.x1), (rect.x0, rect.y1, rect.x1)])
                    vertical.extend([(rect.x0, rect.y0, rect.y1), (rect.x1, rect.y0, rect.y1)])
    # A grid has several shared horizontal/vertical intersections. Page rules,
    # underlines and a lone column separator are not a table.
    horizontal = set(tuple(round(value, 1) for value in line) for line in horizontal)
    vertical = set(tuple(round(value, 1) for value in line) for line in vertical)
    spanning_verticals = sum(
        sum(x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1 for x0, y, x1 in horizontal) >= 3
        for x, y0, y1 in vertical
    )
    return spanning_verticals >= 2


def extract_page_text(page) -> str:
    marked_ocr = _legacy_ocr_text(page)
    if marked_ocr is not None:
        return marked_ocr
    lines = _lines(page)
    bounds = _column_bounds(lines, page.rect.width)
    if bounds is None:
        return page.get_text("text", sort=True)
    split = sum(bounds) / 2
    if _has_spanning_table_grid(page, split):
        return page.get_text("text", sort=True)

    # A full-width title, abstract or figure divides the page into horizontal
    # bands. Inside each band read the complete left column, then the right.
    # Never crop at the gutter: cropping may silently lose characters there.
    def spans_both_columns(line: _TextLine) -> bool:
        # An overhanging left-column title may cross the gutter midpoint while
        # never entering the right column. It must not split the reading band.
        return line.x0 < bounds[0] - 2 and line.x1 > bounds[1] + 2

    wide = sorted((line for line in lines if spans_both_columns(line)), key=lambda line: line.baseline)
    narrow = [line for line in lines if not spans_both_columns(line)]
    output = []

    def append_columns(band: list[_TextLine]) -> None:
        output.extend(_rows([line for line in band if (line.x0 + line.x1) / 2 <= split]))
        output.extend(_rows([line for line in band if (line.x0 + line.x1) / 2 > split]))

    for separator in wide:
        before = [line for line in narrow if line.baseline < separator.baseline]
        narrow = [line for line in narrow if line.baseline >= separator.baseline]
        append_columns(before)
        output.append(separator.text)
    append_columns(narrow)
    return "\n".join(output)
