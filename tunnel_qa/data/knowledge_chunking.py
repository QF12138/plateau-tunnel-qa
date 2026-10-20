"""Conservative, source-preserving preparation of retrieval knowledge blocks.

This is deliberately not a summarizer: essential conditions, numbers and negations
remain verbatim after mechanical text cleanup.  Original files must be archived by
the caller.  ``key`` removes only confidently identified non-body sections; the
report makes that bounded policy and each sampled exclusion inspectable.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import unicodedata


STRATEGY = "knowledge-structural-v2"
_CJK = r"\u3400-\u9fff"
_END = re.compile(r"[。！？!?；;.][\"'”’」』）)]*$")
_CLAUSE = re.compile(r"^\d+(?:\.\d+){1,5}(?![\d.])\s*\S")
_LIST = re.compile(r"^(?:\d+(?:[、)）]|\.\s)|[（(]\d+[)）]|[一二三四五六七八九十]+[、)）]|[-*+]\s)\s*\S")
_UNIT = re.compile(r"^\d+(?:\.\d+)?\s*(?:V|kV|MPa|Pa|kPa|m|mm|cm|km|kg|kN|N|℃|°C|%|Hz|s|min|h|μm)(?:\b|[;；。，,])", re.I)
# A short numeric measurement is evidence, not a numbered section. Keep the
# unit grammar generic: a finite whitelist misses kJ, kWh and compound units.
# Long words such as "Introduction" still qualify as real section titles;
# ambiguous short Latin labels are conservatively kept as body text.
_UNIT_ATOM = r"[A-Za-zµμΩω℃°%‰]{1,5}(?:[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+|\^?[+\-−]?\d+)?"
_QUANTITY = re.compile(
    rf"[+\-−±∓]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+\-−]?\d+)?\s*{_UNIT_ATOM}"
    rf"(?:\s*[/·⋅*×]\s*{_UNIT_ATOM}|\s+{_UNIT_ATOM})*[。;；,.，]?"
)
_STANDALONE_SIGN = re.compile(r"[+\-−﹣±∓–—]")
_EXCLUDED_HEADINGS = {
    "目录": "contents", "目次": "contents", "contents": "contents",
    "tableofcontents": "contents", "参考文献": "references",
    "references": "references", "bibliography": "references",
    "致谢": "acknowledgements", "acknowledgements": "acknowledgements",
    "acknowledgments": "acknowledgements", "出版声明": "publication_notice",
    "版权声明": "publication_notice", "网络首发声明": "publication_notice",
}
_NAMED_HEADINGS = {"摘要", "abstract", "关键词", "引言", "绪论", "结论", "结语", "附录"}


@dataclass
class PreparedChunk:
    heading: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    kind: str = "paragraph"
    warnings: list[str] = field(default_factory=list)


@dataclass
class PreparedKnowledgeResult:
    chunks: list[PreparedChunk]
    report: dict


def chunk_source_location(prefix: str, chunk: PreparedChunk, ordinal: int) -> str:
    """Keep existing single-page fields compatible, with the true range in citations."""
    page = ""
    if chunk.page_start is not None:
        page = f"/第{chunk.page_start}页"
        if chunk.page_end is not None and chunk.page_end != chunk.page_start:
            page = f"/第{chunk.page_start}–{chunk.page_end}页"
    return f"{prefix[:300]}{page}/{chunk.heading[:500]}/片段{ordinal}"


@dataclass
class _Line:
    text: str
    page: int | None


@dataclass
class _Block:
    text: str
    heading: str
    kind: str
    # Character ranges in cleaned text, with their original PDF page.
    spans: list[tuple[int, int, int | None]]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Unit:
    text: str
    page_start: int | None
    page_end: int | None
    kind: str
    warnings: list[str] = field(default_factory=list)


def _clean_line(value: str, pdf: bool) -> str:
    # Do not use NFKC: it changes scientific notation such as m² and ℃.
    value = "".join(
        chr(ord(char) - 0xFEE0) if 0xFF01 <= ord(char) <= 0xFF5E and char not in "，；：！？（）"
        else " " if char == "\u3000" else char
        for char in value
        if unicodedata.category(char) not in {"Co", "Cs"}
        and (unicodedata.category(char) not in {"Cc", "Cf"} or char == "\t")
    )
    value = re.sub(r"[ \t]+", " ", value).strip()
    if pdf:
        chapter = re.match(r"^(第[一二三四五六七八九十百零〇\d]+[章节篇]) +(.+)$", value)
        if chapter:
            value = chapter.group(1) + " " + re.sub(rf"(?<=[{_CJK}]) +(?=[{_CJK}])", "", chapter.group(2))
        else:
            value = re.sub(rf"(?<=[{_CJK}]) +(?=[{_CJK}])", "", value)
        value = re.sub(r"(?<=\d) *\. *(?=\d)", ".", value)
    return value


def _heading(line: str) -> tuple[str, int, str | None] | None:
    """Recognize actual headings, never a voltage, list item or full clause."""
    if not line or "|" in line or len(line) > 100 or re.search(r"\.{3,}|…{2,}|·{3,}", line):
        return None
    markdown = re.match(r"^(#{1,6})\s+(.+)$", line)
    title = markdown.group(2).strip() if markdown else line
    plain = re.sub(r"^(?:第[一二三四五六七八九十百零〇\d]+[章节篇]|\d+(?:\.\d+)*[.、]?)\s*", "", title)
    key = re.sub(r"\s+", "", plain).lower()
    if key in _EXCLUDED_HEADINGS:
        return title, len(markdown.group(1)) if markdown else 1, _EXCLUDED_HEADINGS[key]
    if markdown:
        return title, len(markdown.group(1)), None
    if key in _NAMED_HEADINGS:
        return title, 1, None
    if _END.search(title) or _UNIT.match(title) or _QUANTITY.fullmatch(title) or _LIST.match(title):
        return None
    chapter = re.match(r"^第[一二三四五六七八九十百零〇\d]+([章节篇])\s*\S", title)
    if chapter and len(title) <= 60:
        return title, 2 if chapter.group(1) == "节" else 1, None
    numbered = re.match(r"^(\d{1,2}(?:\.\d{1,3}){0,5})(?:\s+|(?=[\u3400-\u9fff]))(.+)$", title)
    if numbered and len(numbered.group(2)) <= 32:
        body = numbered.group(2)
        if (not body[0].isdigit() and len(re.findall(rf"[{_CJK}A-Za-z]", body)) >= 2
                and not re.search(r"\d$|应当|必须|不得|严禁|不应|需要|应|可采用|宜|；|;|，|,|：|:|[=≤≥]", body)):
            return title, numbered.group(1).count(".") + 1, None
    sheet = re.match(r"^\[工作表:\s*(.+)\]$", title)
    if sheet:
        return title, 1, None
    return None


def _page_numbers(spans: list[tuple[int, int, int | None]], start: int, end: int) -> tuple[int | None, int | None]:
    pages = [page for left, right, page in spans if page is not None and left < end and right > start]
    return (min(pages), max(pages)) if pages else (None, None)


def _joiner(previous: str, current: str, pdf: bool) -> str:
    if pdf:
        if re.search(rf"[{_CJK}，、：；（(]$", previous) or re.match(rf"^[{_CJK}，。！？；）)]", current):
            return ""
        return " "
    return "\n"


def _confirmed_continuation(previous: str, current: str) -> bool:
    if not previous or not current or _END.search(previous):
        return False
    if _heading(current) or _CLAUSE.match(current) or _LIST.match(current) or "|" in current:
        return False
    return bool(
        re.search(r"[，,、（(]$|(?:应当|不得|应|须|与|和|及|或|对|在|将|为|按|的|进行|采用|采取|包括|通过|根据|按照|需要|实施)$", previous)
        or re.match(r"^(?:并|且|以及|或者|持续|进行|采取|实施)", current)
        or (re.search(r"[A-Za-z]-$", previous) and re.match(r"^[a-z]", current))
    )


def _is_page_number(line: str, footer: bool, previous: str = "") -> bool:
    if re.fullmatch(r"(?:[·•—–-]\s*)+\d{1,5}(?:\s*[·•—–-])+", line):
        return True
    if re.fullmatch(r"第\s*\d{1,5}\s*页", line):
        return True
    # A bare 38 followed by ℃ is a value, not a page number. Without layout
    # coordinates only a final numeral after a complete prose line is safe.
    return footer and len(previous) > 12 and bool(_END.search(previous)) and bool(re.fullmatch(r"\d{1,5}", line))


def _header_candidate(line: str) -> bool:
    if not (3 <= len(line) <= 70) or _END.search(line) or "|" in line:
        return False
    if _UNIT.match(line) or _QUANTITY.fullmatch(line):
        return False
    if _CLAUSE.match(line) or _LIST.match(line) or re.search(r"应|必须|不得|严禁|不应|至少|不大于|不小于|≤|≥|=", line):
        return False
    heading = _heading(line)
    if heading and heading[2]:
        return False
    return bool(re.search(rf"[{_CJK}A-Za-z]", line))


def _sentences(block: _Block, chunk_size: int) -> list[_Unit]:
    units: list[_Unit] = []
    # Preserve decimals. English full stops need whitespace/end, not a digit.
    boundary = re.compile(r"[。！？!?；;]+[\"'”’」』]*|(?<!\d)\.(?=\s|$)|\n+")
    start = 0
    ranges = []
    for match in boundary.finditer(block.text):
        ranges.append((start, match.end()))
        start = match.end()
    if start < len(block.text):
        ranges.append((start, len(block.text)))
    for left, right in ranges:
        hard_split = right - left > chunk_size
        while left < right:
            end = min(left + chunk_size, right)
            body = block.text[left:end].strip()
            warnings = list(block.warnings)
            if hard_split:
                warnings.append("hard_split_long_sentence")
            if body:
                page_start, page_end = _page_numbers(block.spans, left, end)
                kind = "heading" if body == block.heading.split(" / ")[-1] and _heading(body) else block.kind
                units.append(_Unit(body, page_start, page_end, kind, list(dict.fromkeys(warnings))))
            left = end
    return units


def _make_chunk(heading: str, units: list[_Unit], kind: str | None = None) -> PreparedChunk:
    starts = [unit.page_start for unit in units if unit.page_start is not None]
    ends = [unit.page_end for unit in units if unit.page_end is not None]
    kinds = {unit.kind for unit in units}
    return PreparedChunk(
        heading=heading,
        text="\n".join(unit.text for unit in units),
        page_start=min(starts) if starts else None,
        page_end=max(ends) if ends else None,
        kind=kind or (next(iter(kinds)) if len(kinds) == 1 else "mixed"),
        warnings=list(dict.fromkeys(warning for unit in units for warning in unit.warnings)),
    )


def _pack_table(block: _Block, chunk_size: int) -> list[PreparedChunk]:
    rows = []
    offset = 0
    for row in block.text.splitlines():
        start, end = _page_numbers(block.spans, offset, offset + len(row))
        rows.append(_Unit(row, start, end, "table", list(block.warnings)))
        offset += len(row) + 1
    if not rows:
        return []
    # The heading (if any) precedes the actual table header and stays with it.
    header_count = 1
    while header_count < len(rows) and "|" not in rows[header_count - 1].text:
        header_count += 1
    if header_count < len(rows) and re.fullmatch(r"[ |:\-]+", rows[header_count].text):
        header_count += 1
    header = rows[:header_count]
    current = list(header)
    chunks = []
    for row in rows[header_count:]:
        if len("\n".join(unit.text for unit in current + [row])) > chunk_size and len(current) > len(header):
            chunks.append(_make_chunk(block.heading, current, "table"))
            current = list(header)
        if len("\n".join(unit.text for unit in current + [row])) > chunk_size:
            row.warnings.append("oversize_table_row")
        current.append(row)
    if current:
        chunk = _make_chunk(block.heading, current, "table")
        if len(chunk.text) > chunk_size and "oversize_table_row" not in chunk.warnings:
            chunk.warnings.append("oversize_table_row")
        chunks.append(chunk)
    return chunks


def _pack(blocks: list[_Block], chunk_size: int, overlap: int) -> list[PreparedChunk]:
    chunks: list[PreparedChunk] = []
    current: list[_Unit] = []
    heading = "正文"

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(_make_chunk(heading, current))
            current = []

    for block in blocks:
        block_start, _ = _page_numbers(block.spans, 0, len(block.text))
        # Complete paragraphs from separate PDF pages are not arbitrarily joined.
        if current and (block.heading != heading or current[-1].page_end != block_start):
            flush()
        heading = block.heading
        if block.kind == "table":
            flush()
            chunks.extend(_pack_table(block, chunk_size))
            continue
        for unit in _sentences(block, chunk_size):
            if current and len("\n".join(item.text for item in current + [unit])) > chunk_size:
                old = list(current)
                flush()
                carried: list[_Unit] = []
                for candidate in reversed(old):
                    proposed = [candidate] + carried
                    if len("\n".join(item.text for item in proposed)) > overlap:
                        break
                    # Never overlap a fragment that had to be cut mid-sentence.
                    if "hard_split_long_sentence" in candidate.warnings:
                        break
                    carried = proposed
                while carried and len("\n".join(item.text for item in carried + [unit])) > chunk_size:
                    carried.pop(0)
                current = carried
            current.append(unit)
    flush()
    return chunks


def prepare_knowledge_chunks(
    filename: str,
    text: str,
    chunk_size: int,
    overlap: int,
    *,
    source_type: str = "research",
    mode: str = "key",
) -> PreparedKnowledgeResult:
    """Clean, conservatively select and split source text without external calls.

    ``key`` excludes explicit non-body sections. ``clean`` and ``full`` both
    retain substantive sections, while still removing mechanical extraction noise.
    The size is a target/hard limit for prose, not for indivisible table rows.
    """
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size 必须大于 0，chunk_overlap 必须非负且小于 chunk_size")
    if mode not in {"key", "clean", "full"}:
        raise ValueError("mode 必须是 key、clean 或 full")
    pdf = Path(filename).suffix.lower() == ".pdf"
    reasons: Counter[str] = Counter()
    samples: list[dict] = []
    report_warnings = ["rule_based_selection_not_semantic_summarization"] if mode == "key" else []
    if pdf and sum(unicodedata.category(char) == "Co" for char in text) / max(len(text), 1) > 0.01:
        report_warnings.append("extracted_text_requires_visual_review")

    def exclude(reason: str, value: str, page: int | None) -> None:
        if not value.strip():
            return
        reasons[reason] += 1
        if len(samples) < 20:
            samples.append({"reason": reason, "page": page, "text": value[:180]})

    pages: list[tuple[int | None, list[_Line]]] = []
    page_number: int | None = None
    lines: list[_Line] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        marker = re.fullmatch(r"\s*\[第(\d+)页\]\s*", raw) if pdf else None
        if marker:
            if lines:
                pages.append((page_number, lines))
            page_number, lines = int(marker.group(1)), []
            continue
        cleaned = _clean_line(raw, pdf)
        if raw.strip() and not cleaned:
            exclude("extraction_noise", raw, page_number)
        lines.append(_Line(cleaned, page_number))
    if lines:
        pages.append((page_number, lines))

    edge_counts: Counter[str] = Counter()
    if pdf:
        for _, page_lines in pages:
            nonempty = [line.text for line in page_lines if line.text]
            edges = nonempty[:2] + nonempty[-2:]
            edge_counts.update(set(line for line in edges if _header_candidate(line)))
    repeated = {line for line, count in edge_counts.items() if count >= 3}

    clean_lines: list[_Line] = []
    bibliography_pages = set()
    preserved_running_headings: set[str] = set()
    for _, page_lines in pages:
        indexes = [i for i, line in enumerate(page_lines) if line.text]
        edges = set(indexes[:2] + indexes[-2:])
        page_text = "\n".join(line.text for line in page_lines)
        # Whole-page exclusion is safe only when every substantive line is an
        # independently recognizable reference. A conclusion or appendix can
        # share a dense bibliography page: leave mixed/uncertain pages to the
        # section filter below instead of discarding their engineering content.
        bibliography = pdf and mode == "key" and (
            len(re.findall(r"(?m)^\[\s*\d+\s*\]", page_text)) >= 3
            and len(re.findall(r"\[\s*(?:J|D|N|C|M|R|S|EB/OL)\s*\]", page_text)) >= 3
            and len(re.findall(r"\b(?:19|20)\d{2}\b", page_text)) >= 3
            and all(
                (
                    re.match(r"^\[\s*\d+\s*\]", page_lines[index].text)
                    and re.search(r"\[\s*(?:J|D|N|C|M|R|S|EB/OL)\s*\]", page_lines[index].text)
                    and re.search(r"\b(?:19|20)\d{2}\b", page_lines[index].text)
                )
                or (index in edges and page_lines[index].text in repeated)
                or _is_page_number(
                    page_lines[index].text, position == len(indexes) - 1,
                    page_lines[indexes[position - 1]].text if position else "",
                )
                for position, index in enumerate(indexes)
            )
        )
        if bibliography:
            bibliography_pages.add(page_lines[0].page)
            for line in page_lines:
                exclude("bibliography_page", line.text, line.page)
            continue
        previous_nonempty = ""
        for index, line in enumerate(page_lines):
            previous = previous_nonempty
            if line.text:
                previous_nonempty = line.text
            if pdf and _is_page_number(line.text, bool(indexes) and index == indexes[-1], previous):
                exclude("page_number", line.text, line.page)
            elif pdf and index in edges and line.text in repeated:
                if _heading(line.text) and line.text not in preserved_running_headings:
                    # A running chapter title also establishes the initial path.
                    clean_lines.append(line)
                    preserved_running_headings.add(line.text)
                else:
                    exclude("repeated_header_footer", line.text, line.page)
            elif re.fullmatch(r"[_＿\-—–·•]+", line.text) and not _STANDALONE_SIGN.fullmatch(line.text):
                # A single dash may be the sign of a value on the next line.
                # Preserve it verbatim rather than turning a negative into a
                # positive; repeated decorative separators can still be removed.
                exclude("extraction_noise", line.text, line.page)
            else:
                clean_lines.append(line)

    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []
    heading = "正文"
    pending_heading: _Line | None = None
    pending: list[_Line] = []
    kind = "paragraph"
    skipped: tuple[int, str] | None = None
    skipped_page: int | None = None

    # Some scanned theses encode the reference heading as "料文献". Do not
    # interpret that OCR typo in isolation: require several numbered citations,
    # publication-type markers and years in the following local text as well.
    corroborated_reference_headings = set()
    if pdf and mode == "key":
        for index, line in enumerate(clean_lines):
            if re.sub(r"\s+", "", line.text) not in {"料文献", "##文献"}:
                continue
            following = "\n".join(item.text for item in clean_lines[index + 1:index + 81])
            if (len(re.findall(r"(?m)^\[\s*\d[\d\s]*\]", following)) >= 3
                    and len(re.findall(r"\[\s*(?:J|D|N|C|M|R|S|EB/OL)\s*\]", following)) >= 3
                    and len(re.findall(r"\b(?:19|20)\d{2}\b", following)) >= 2):
                corroborated_reference_headings.add(index)

    def flush() -> None:
        nonlocal pending, pending_heading
        if not pending:
            return
        body = ""
        spans: list[tuple[int, int, int | None]] = []
        included = ([pending_heading] if pending_heading else []) + pending
        for index, line in enumerate(included):
            separator = ""
            if index:
                separator = "\n" if kind == "table" or (pending_heading and index == 1) else _joiner(body, line.text, pdf)
            body += separator
            start = len(body)
            body += line.text
            spans.append((start, len(body), line.page))
        span_pages = {page for _, _, page in spans if page is not None}
        warnings = ["cross_page_continuation"] if len(span_pages) > 1 and kind != "table" else []
        blocks.append(_Block(body, heading, kind, spans, warnings))
        pending, pending_heading = [], None

    for line_index, line in enumerate(clean_lines):
        if not line.text:
            # PDF extraction often inserts a blank line at a page boundary; defer
            # that boundary decision to the next actual line's page provenance.
            if not pdf:
                flush()
            continue
        found_heading = ((line.text, 1, "references") if line_index in corroborated_reference_headings
                         else _heading(line.text))
        if found_heading:
            title, level, excluded_reason = found_heading
            if mode == "key" and excluded_reason:
                flush()
                skipped = (level, excluded_reason)
                skipped_page = line.page
                pending_heading = None
                exclude(excluded_reason, line.text, line.page)
                continue
            # A damaged chapter title must not make a PDF's contents section
            # swallow the first body chapter. A real heading on a later page
            # ends TOC filtering even if its hierarchical level is deeper.
            resumes_pdf_body = (pdf and skipped and skipped[1] == "contents"
                                and line.page != skipped_page)
            if skipped and level > skipped[0] and not resumes_pdf_body:
                exclude(skipped[1], line.text, line.page)
                continue
            skipped = None
            skipped_page = None
            flush()
            if pending_heading:
                # Retain consecutive headings instead of silently discarding text.
                blocks.append(_Block(pending_heading.text, heading, "heading", [(0, len(pending_heading.text), pending_heading.page)]))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading = " / ".join(item[1] for item in stack)
            pending_heading = _Line(title, line.page)
            continue
        if skipped:
            exclude(skipped[1], line.text, line.page)
            continue
        if mode == "key" and (
            re.match(r"^网络首发(?:时间|地址)\s*[:：]", line.text)
            or ("中国学术期刊（网络版）" in line.text and "出版" in line.text and "确认" in line.text)
            or ("中国学术期刊(网络版)" in line.text and "出版" in line.text and "确认" in line.text)
        ):
            flush()
            exclude("publication_notice", line.text, line.page)
            continue
        line_kind = "table" if "|" in line.text else "clause" if _CLAUSE.match(line.text) else "list" if _LIST.match(line.text) else "paragraph"
        if pending:
            previous = pending[-1]
            new_page = line.page != previous.page
            if (
                line_kind == "table" and kind != "table"
                or kind == "table" and line_kind != "table"
                or line_kind in {"clause", "list"}
                or (new_page and (not _confirmed_continuation(previous.text, line.text)
                    or (line.page is not None and previous.page is not None and line.page != previous.page + 1)))
                or (kind != "table" and _END.search(previous.text))
            ):
                flush()
        if not pending:
            kind = line_kind
        pending.append(line)
    flush()
    if pending_heading:
        blocks.append(_Block(pending_heading.text, heading, "heading", [(0, len(pending_heading.text), pending_heading.page)], ["heading_without_body"]))

    chunks = _pack(blocks, chunk_size, overlap)
    if mode == "key":
        body_chunks = []
        for chunk in chunks:
            if chunk.kind == "heading":
                # The heading remains in structure_path of its section. It is
                # not an independent fact when no body fits beside it.
                exclude("standalone_heading", chunk.text, chunk.page_start)
            else:
                body_chunks.append(chunk)
        chunks = body_chunks
    report_warnings.extend(warning for chunk in chunks for warning in chunk.warnings)
    empty_pages = [page for page, page_lines in pages if page is not None and not any(line.text for line in page_lines)]
    if empty_pages:
        report_warnings.append("empty_text_pages_require_ocr_review")
    # Headers alone are not a successful extraction of an image/diagram page.
    clean_page_chars: Counter[int] = Counter()
    for line in clean_lines:
        if line.page is not None:
            clean_page_chars[line.page] += len(line.text)
    low_text_pages = [page for page, _ in pages if page is not None and page not in bibliography_pages and clean_page_chars[page] < 40]
    if low_text_pages:
        report_warnings.append("pages_with_little_text_require_visual_review")
    if mode == "key" and text.strip() and not chunks:
        report_warnings.append("no_knowledge_content_after_selection")
    report = {
        "strategy": STRATEGY,
        "mode": mode,
        "source_type": getattr(source_type, "value", source_type),
        "input_chars": len(text),
        "retained_chars": sum(len(block.text) for block in blocks),
        "excluded_count": sum(reasons.values()),
        "excluded_by_reason": dict(reasons),
        "warnings": list(dict.fromkeys(report_warnings)),
        "excluded_samples": samples,
        "chunk_count": len(chunks),
        "empty_text_pages": empty_pages,
        "low_text_pages": low_text_pages,
        "needs_review": bool(empty_pages or low_text_pages or "extracted_text_requires_visual_review" in report_warnings or (text.strip() and not chunks) or any(
            warning in {"hard_split_long_sentence", "oversize_table_row", "heading_without_body"}
            for warning in report_warnings
        )),
    }
    return PreparedKnowledgeResult(chunks, report)
