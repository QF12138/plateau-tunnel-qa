"""One source-preserving preparation policy for preview, import and re-chunking."""
from pathlib import Path
import re

from tunnel_qa.data.knowledge_chunking import prepare_knowledge_chunks


def isolate_review_pages(parsed_text: str, page_warnings: dict[int, list[str]]) -> tuple[str, list[dict]]:
    unsafe = {"legacy_ocr_numeric_table_requires_review", "legacy_ocr_figure_page_requires_review"}
    isolated = []

    def replace_page(match):
        page = int(match.group(1))
        reasons = sorted(unsafe.intersection(page_warnings.get(page, [])))
        if not reasons:
            return match.group(0)
        isolated.append({"page": page, "reasons": reasons, "extracted_text": match.group(2)})
        return f"[第{page}页]\n\n"

    text = re.sub(r"\[第(\d+)页\]\n(.*?)(?=\[第\d+页\]\n|\Z)", replace_page, parsed_text, flags=re.S)
    return text, isolated


def prepare_source_knowledge(filename, parsed_text, chunk_size, overlap, *, source_type="research",
                             mode="key", content=None, reviewed_ocr=False):
    """Isolate strong bad-OCR signals, without mutating the archived parsed text.

    Entire suspect pages are held for review, including any prose sharing their
    table: no inferred correction of numbers or hidden deletion of source bytes.
    """
    page_warnings = {}
    inspection_unavailable = False
    if content and not reviewed_ocr and Path(filename).suffix.lower() == ".pdf":
        try:
            import pymupdf
        except ImportError:
            pymupdf = None
        if pymupdf is not None:
            from tunnel_qa.data.pdf_layout import page_text_warnings
            try:
                with pymupdf.open(stream=content, filetype="pdf") as pdf:
                    page_warnings = {i: warnings for i, page in enumerate(pdf, 1)
                                     if (warnings := page_text_warnings(page))}
            except (RuntimeError, ValueError):
                # Cached, reviewed corpus text can outlive a PDF decoder's
                # compatibility. Make this explicit; interactive parse failures
                # are rejected by the caller before reaching preparation.
                inspection_unavailable = True
        else:
            inspection_unavailable = True
    selection_text, isolated = isolate_review_pages(parsed_text, page_warnings)
    prepared = prepare_knowledge_chunks(filename, selection_text, chunk_size, overlap,
                                         source_type=source_type, mode=mode)
    prepared.report["quarantined_pages"] = [{"page": item["page"], "reasons": item["reasons"]} for item in isolated]
    prepared.report["legacy_ocr_pages"] = sorted(page_warnings)
    if isolated or page_warnings:
        prepared.report["needs_review"] = True
        prepared.report["warnings"].append("legacy_ocr_requires_source_verification")
    if isolated:
        prepared.report["warnings"].append("unreliable_scanned_tables_figures_excluded_from_retrieval")
    if inspection_unavailable:
        prepared.report["needs_review"] = True
        prepared.report["warnings"].append("pdf_page_quality_inspection_unavailable")
    return prepared, isolated
