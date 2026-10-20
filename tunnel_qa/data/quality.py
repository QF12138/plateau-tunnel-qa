"""Shared gates for preview, interactive upload and batch corpus ingestion."""

from tunnel_qa.data.schemas import DocumentMetadata, QualityReport


def content_quality_checks(parsed_text: str, texts: list[str]) -> dict[str, bool]:
    return {
        "text_extracted": len(parsed_text.strip()) >= 20,
        "chunks_created": bool(texts),
        "bounded_knowledge_blocks": all(len(text) <= 10000 for text in texts),
    }


def document_quality_report(metadata: DocumentMetadata, parsed_text: str,
                            chunks: list[tuple[str, str, int | None]]) -> QualityReport:
    checks = {
        "metadata_complete": all([metadata.source_id, metadata.title, metadata.version, metadata.owner_org]),
        **content_quality_checks(parsed_text, [text for _, text, _ in chunks]),
        "location_present": bool(metadata.content_location),
    }
    return QualityReport(score=sum(checks.values()) / len(checks), passed=all(checks.values()),
                         checks=checks, issues=[name for name, passed in checks.items() if not passed],
                         chunk_count=len(chunks))
