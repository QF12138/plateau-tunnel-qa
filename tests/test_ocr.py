from tunnel_qa.corpus.ocr import completed_page_count, ordered_ocr_text


def test_completed_page_count_reads_latest_checkpoint():
    text = "[第1页]\n正文一\n\n[第2页]\n正文二\n"
    assert completed_page_count(text) == 2
    assert completed_page_count("") == 0


def test_ordered_ocr_text_filters_confidence_and_sorts_lines():
    result = [
        ([[10, 40], [60, 40], [60, 55], [10, 55]], "第二行", 0.98),
        ([[10, 10], [60, 10], [60, 25], [10, 25]], "第一行", 0.95),
        ([[70, 10], [90, 10], [90, 25], [70, 25]], "低置信", 0.2),
    ]
    assert ordered_ocr_text(result) == "第一行\n第二行"
