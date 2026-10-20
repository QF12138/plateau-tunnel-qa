import re

import pymupdf

from tunnel_qa.data.pdf_layout import extract_page_text, page_text_warnings, _legacy_ocr_text
from tunnel_qa.data.parsers import parse_document
from tunnel_qa.data.knowledge_chunking import prepare_knowledge_chunks


def insert_column(page, x, start_y, prefix, count=6):
    labels = []
    for i in range(count):
        text = f"{prefix}{i}: retain the complete source condition."
        page.insert_text((x, start_y + i * 18), text, fontsize=10)
        labels.append(text)
    return labels


def compact(text):
    return re.sub(r"\s+", "", text)


def test_two_columns_are_not_interleaved_and_every_character_is_retained():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        left = insert_column(page, 40, 100, "LEFT")
        right = insert_column(page, 320, 100, "RIGHT")
        text = extract_page_text(page)
        assert all(text.index(a) < text.index(b) for a, b in zip(left + right, (left + right)[1:]))
        assert sorted(compact(text)) == sorted(compact(page.get_text("text")))


def test_full_width_title_and_midpage_heading_split_column_bands():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((140, 60), "FULL WIDTH TITLE: RECORD ALL CONDITIONS", fontsize=14)
        first = insert_column(page, 40, 100, "UPLEFT") + insert_column(page, 320, 100, "UPRIGHT")
        page.insert_text((140, 300), "FULL WIDTH SECOND SECTION HEADING", fontsize=14)
        second = insert_column(page, 40, 340, "LOWLEFT") + insert_column(page, 320, 340, "LOWRIGHT")
        text = extract_page_text(page)
        expected = ["FULL WIDTH TITLE"] + first + ["FULL WIDTH SECOND SECTION"] + second
        assert all(text.index(a) < text.index(b) for a, b in zip(expected, expected[1:]))
        assert sorted(compact(text)) == sorted(compact(page.get_text("text")))


def test_single_column_keeps_default_extraction_order():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        for i in range(10):
            page.insert_text((40, 60 + i * 20), f"Row {i}: this is one wide paragraph spanning the page with conditions and exceptions.", fontsize=11)
        assert extract_page_text(page) == page.get_text("text", sort=True)


def test_sparse_numeric_table_is_not_misdetected_as_two_prose_columns():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        for i in range(10):
            page.insert_text((50, 100 + i * 20), f"{i} kJ", fontsize=10)
            page.insert_text((320, 100 + i * 20), f"{i + 1} kPa", fontsize=10)
        assert extract_page_text(page) == page.get_text("text", sort=True)


def test_ruled_table_with_long_text_cells_keeps_rowwise_order():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        insert_column(page, 40, 100, "TABLELEFT")
        insert_column(page, 320, 100, "TABLERIGHT")
        for y in range(86, 213, 18):
            page.draw_line((35, y), (570, y))
        for x in (35, 300, 570):
            page.draw_line((x, 86), (x, 212))
        assert extract_page_text(page) == page.get_text("text", sort=True)


def test_column_order_keeps_conclusion_before_references_in_chunk_pipeline():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        left = insert_column(page, 40, 100, "METHOD")
        # The left column continues below the right-column reference heading.
        left += insert_column(page, 40, 240, "CONDITION")
        right = insert_column(page, 320, 100, "RESULT")
        page.insert_text((320, 240), "References", fontsize=12)
        page.insert_text((320, 260), "[1] Example citation. Journal, 2026.", fontsize=10)
        raw = doc.tobytes()
    parsed, _ = parse_document("two-column.pdf", raw)
    prepared = prepare_knowledge_chunks("two-column.pdf", parsed, 800, 120)
    body = "\n".join(chunk.text for chunk in prepared.chunks)
    assert all(text in body for text in left + right)
    assert "Example citation" not in body
    assert all(chunk.page_start == chunk.page_end == 1 for chunk in prepared.chunks)


def test_empty_scan_page_remains_empty_instead_of_inventing_text():
    with pymupdf.open() as doc:
        page = doc.new_page()
        assert extract_page_text(page) == ""


def test_raised_exponent_stays_after_its_value_before_the_unit():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        insert_column(page, 40, 100, "LEFT")
        insert_column(page, 320, 100, "RIGHT")
        phrase = "The condition energy is 10"
        exponent_x = 40 + pymupdf.get_text_length(phrase, fontsize=10)
        page.insert_text((40, 250), phrase, fontsize=10)
        page.insert_text((exponent_x, 245), "-3", fontsize=6)
        page.insert_text((exponent_x + 8, 250), " kJ.", fontsize=10)
        text = extract_page_text(page)
        assert sorted(compact(text)) == sorted(compact(page.get_text("text")))
        assert text.index(phrase) < text.index("-3") < text.index("kJ.")


def test_left_column_title_overhanging_gutter_is_not_a_full_width_band():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        left, right = [], []
        for x, prefix, labels in [(40, "LEFT", left), (320, "RIGHT", right)]:
            for i in range(8):
                label = f"{prefix}{i}: retain the complete source condition."
                page.insert_text((x, 100 + i * 25), label, fontsize=10)
                labels.append(label)
        # This title ends at x305.6: it crosses the inferred gutter midpoint,
        # but not the start of the right column at x320.
        title = "LEFT ONLY SECTION HEADING WITH EXTENDED TITLE"
        page.insert_text((40, 187), title, fontsize=10)
        text = extract_page_text(page)
        assert sorted(compact(text)) == sorted(compact(page.get_text("text")))
        expected = left[:4] + [title] + left[4:] + right
        assert all(text.index(a) < text.index(b) for a, b in zip(expected, expected[1:]))


class LegacyOCRPage:
    """A saved OCR stream is text, not a claim that image pixels were read."""
    rect = pymupdf.Rect(0, 0, 600, 800)

    def __init__(self, rows, *, scan=True):
        self.scan = scan
        self.blocks = [{"lines": [
            {"spans": [{"text": part} for part in row] + [{"text": "\ue5d2"}, {"text": "\ue5cf"}]}
            for row in rows
        ]}]

    def get_text(self, mode, **kwargs):
        assert mode == "dict"
        return {"blocks": self.blocks}

    def get_image_info(self):
        return [{"bbox": (0, 0, 600, 800)}] if self.scan else []


def test_marked_ocr_rejoins_fragments_and_preserves_uncertain_source_digits():
    page = LegacyOCRPage([
        ["表３－４", " 数值"],
        ["１\ue5ce", "８？７\ue5ce", "６４！〇"],
        ["２\ue5ce", "９", "．７\ue5ce６４", "．６"],
        ["必须", "\ue5e5", "保留\ue123"],
    ])
    text = extract_page_text(page)
    assert "２\t９．７\t６４．６" in text
    assert "１\t８？７\t６４！〇" in text  # No guessed decimal point.
    assert "必须 保留\ue123" in text  # Unknown private-use glyph not discarded.
    assert text.count("\n") == 3


def test_private_glyphs_without_page_scan_are_not_decoded_as_legacy_ocr():
    page = LegacyOCRPage([["x\ue5cey"] for _ in range(5)], scan=False)
    assert _legacy_ocr_text(page) is None
    assert page_text_warnings(page) == []


def test_sparse_private_markers_are_insufficient_for_ocr_reordering():
    page = LegacyOCRPage([["x\ue5cey"] for _ in range(3)])
    assert _legacy_ocr_text(page) is None


def test_legacy_numeric_table_is_flagged_not_silently_claimed_repaired():
    page = LegacyOCRPage([["表３－４ 压力试验"]] + [
        [str(i), "\ue5ce", "８？７\ue5ce６４！〇"] for i in range(1, 7)
    ])
    assert page_text_warnings(page) == ["legacy_ocr_text_layer", "legacy_ocr_numeric_table_requires_review"]
    assert "８？７" in extract_page_text(page)


def test_legacy_table_caption_without_repeated_numeric_rows_is_not_quarantined():
    page = LegacyOCRPage([["表３－４ 参数说明"], ["参数", "\ue5ce", "含义"], ["A\ue5ce长度"], ["B\ue5ce压力"]])
    assert page_text_warnings(page) == ["legacy_ocr_text_layer"]


def test_legacy_figure_only_page_is_flagged_but_mixed_prose_page_is_not():
    rows = [["Ｘ\ue5ceＹ"], ["０\ue5ce１"], ["（ａ） 预报结果"], ["图３－４９ 插值图"]]
    assert "legacy_ocr_figure_page_requires_review" in page_text_warnings(LegacyOCRPage(rows))
    chinese_body = "当围岩压力发生变化时必须结合测量条件进行综合判断，不能仅根据单幅图像得到施工结论。"
    assert page_text_warnings(LegacyOCRPage(rows + [[chinese_body]])) == ["legacy_ocr_text_layer"]
    english_body = "The measured results must be assessed together with the source conditions and independent observations."
    assert page_text_warnings(LegacyOCRPage(rows + [[english_body]])) == ["legacy_ocr_text_layer"]
