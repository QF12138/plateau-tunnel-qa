import json
import mimetypes
import hashlib
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from tunnel_qa.app import register_models
from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import JobStatus, ProcessingStatus, ReviewStatus, Role, SecurityLevel, SourceType
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.errors import AppError
from tunnel_qa.corpus.schemas import CorpusItem
from tunnel_qa.data.models import Chunk, DataSource, ImportJob, SourceDocument
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.schemas import ChunkingConfig, DocumentMetadata
from tunnel_qa.data.quality import document_quality_report
from tunnel_qa.data.knowledge_chunking import chunk_source_location
from tunnel_qa.data.preparation import prepare_source_knowledge
from tunnel_qa.data.parsers import parse_document
from tunnel_qa.db.session import Database
from tunnel_qa.knowledge.schemas import ReviewRequest
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.settings.service import SystemSettingsService


SOURCE_NAMES = {
    SourceType.STANDARD: "全量语料库·标准规范",
    SourceType.RESEARCH: "全量语料库·科研论文",
    SourceType.EXPERT_EXPERIENCE: "全量语料库·专家经验",
    SourceType.ENGINEERING_CASE: "全量语料库·工程案例",
    SourceType.MONITORING: "全量语料库·监测资料",
}


class CorpusDatabaseImporter:
    """Idempotently load processed corpus text and page-aware chunks into the app database."""

    def __init__(
        self,
        corpus_root: Path,
        processed_root: Path,
        settings: Settings,
    ) -> None:
        self.corpus_root = corpus_root.resolve()
        self.processed_root = processed_root.resolve()
        self.settings = settings
        self.access = AccessContext(
            "corpus-importer", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL
        )

    def run(self, *, publish_knowledge: bool = True) -> dict:
        register_models()
        database = Database(self.settings)
        database.create_schema()
        session = database.session_factory()
        try:
            SystemSettingsService(session).initialize(self.settings)
            content_store = DocumentContentStore(self.settings)
            chunking = ChunkingConfig(chunk_size=self.settings.chunk_size,
                                      chunk_overlap=self.settings.chunk_overlap, chunk_model="key-v2")
            items = self._load_manifest()
            sources = self._ensure_sources(session, items)
            imported: list[dict] = []
            skipped: list[dict] = []
            missing: list[dict] = []
            document_ids: list[str] = []
            total_chunks = 0

            for item in items:
                existing = session.scalar(
                    select(SourceDocument).where(SourceDocument.checksum == item.checksum_sha256)
                )
                if existing is not None:
                    skipped.append({"source_id": item.source_id, "reason": "checksum_exists"})
                    document_ids.append(existing.id)
                    continue
                text_path, is_ocr = self._processed_text_path(item)
                if text_path is None:
                    missing.append(
                        {
                            "source_id": item.source_id,
                            "relative_path": item.relative_path,
                            "reason": "processed_text_missing",
                        }
                    )
                    continue
                parsed_text = text_path.read_text(encoding="utf-8")
                original = (self.corpus_root / item.relative_path).resolve()
                # Check the original against the manifest before archiving any
                # payload; never let changed files inherit a stale checksum.
                try:
                    if not original.is_relative_to(self.corpus_root):
                        raise AppError("UNSAFE_CORPUS_PATH", "原文路径超出语料目录")
                    original_content = original.read_bytes()
                    if not original_content or len(original_content) > self.settings.max_upload_bytes:
                        raise AppError("INVALID_ORIGINAL_SIZE", "原文为空或超过文件大小限制")
                    if hashlib.sha256(original_content).hexdigest() != item.checksum_sha256:
                        raise AppError("ORIGINAL_CHECKSUM_MISMATCH", "原文已变化，请重新生成语料清单")
                except (AppError, OSError) as exc:
                    missing.append({"source_id": item.source_id, "relative_path": item.relative_path,
                                    "reason": exc.error_code if isinstance(exc, AppError) else "original_read_failed"})
                    continue
                parse_warning = None
                if not is_ocr and original.suffix.lower() == ".pdf" and original.is_file():
                    try:
                        fresh_text, _ = parse_document(original.name, original_content)
                        if fresh_text.strip():
                            parsed_text = fresh_text
                    except Exception:
                        # A cached, reviewed extraction can still be imported;
                        # expose the fallback rather than silently implying that
                        # the new layout-aware PDF parser was used.
                        parse_warning = "cached_parse_used_after_pdf_parse_failure"
                prepared, _ = prepare_source_knowledge(
                    Path(item.relative_path).name,
                    parsed_text,
                    self.settings.chunk_size,
                    self.settings.chunk_overlap,
                    source_type=item.source_type,
                    mode="key",
                    content=original_content, reviewed_ocr=is_ocr,
                )
                chunks = prepared.chunks
                if parse_warning:
                    prepared.report["warnings"].append(parse_warning)
                    prepared.report["needs_review"] = True
                location_prefix = "PDF全文（OCR按页定位）" if is_ocr else "PDF全文（按页定位）"
                try:
                    metadata = DocumentMetadata(
                        source_id=item.source_id, title=item.title, source_type=item.source_type,
                        version=item.version, effective_date=item.effective_date,
                        security_level=item.security_level, owner_org=item.owner_org,
                        content_location=location_prefix,
                    )
                    quality = document_quality_report(metadata, parsed_text,
                        [(chunk.heading, chunk.text, chunk.page_start) for chunk in chunks])
                    if not quality.passed:
                        missing.append({"source_id": item.source_id, "relative_path": item.relative_path,
                                        "reason": "data_quality_failed", "issues": quality.issues})
                        continue
                    storage_uri = self._store_original(item, original, original_content)
                except (ValidationError, AppError, OSError) as exc:
                    missing.append({"source_id": item.source_id, "relative_path": item.relative_path,
                                    "reason": exc.error_code if isinstance(exc, AppError) else type(exc).__name__})
                    continue
                document = SourceDocument(
                    source_id=item.source_id,
                    data_source_id=sources[item.source_type].id,
                    title=item.title,
                    source_type=item.source_type,
                    project_site=None,
                    version=item.version,
                    effective_date=item.effective_date,
                    security_level=item.security_level,
                    owner_org=item.owner_org,
                    checksum=item.checksum_sha256,
                    content_location=location_prefix,
                    original_filename=original.name,
                    mime_type=item.media_type or mimetypes.guess_type(original.name)[0] or "application/octet-stream",
                    storage_uri=str(storage_uri),
                    parsed_text=parsed_text,
                    status=ProcessingStatus.PUBLISHED,
                    metadata_extra={
                        "original_path": str(original),
                        "relative_path": item.relative_path,
                        "author": item.author,
                        "ocr_applied": is_ocr,
                        "corpus_quality_score": item.quality_score,
                        "corpus_issues": item.issues,
                        "chunking": chunking.model_dump(mode="json"),
                        "ingestion_quality": quality.model_dump(mode="json"),
                        "chunk_processing": {"report": prepared.report, "chunks": {
                            str(ordinal): {"page_start": chunk.page_start, "page_end": chunk.page_end,
                                           "kind": chunk.kind, "warnings": chunk.warnings}
                            for ordinal, chunk in enumerate(chunks, 1)
                        }},
                    },
                )
                mirrored_id = None
                try:
                    session.add(document)
                    session.flush()
                    chunk_rows = [Chunk(
                            document_id=document.id,
                            processing_version=1,
                            ordinal=ordinal,
                            text=chunk.text,
                            structure_path=chunk.heading,
                            page_number=chunk.page_start,
                            content_location=chunk_source_location(location_prefix, chunk, ordinal),
                            security_level=item.security_level,
                            char_count=len(chunk.text),
                        ) for ordinal, chunk in enumerate(chunks, 1)]
                    session.add_all(chunk_rows)
                    session.flush()
                    content_store.save_document(document, chunk_rows,
                                                original_content=original_content, chunking=chunking)
                    mirrored_id = document.id
                    session.add(ImportJob(
                        data_source_id=sources[item.source_type].id,
                        idempotency_key=f"full-corpus-{item.checksum_sha256[:24]}",
                        status=JobStatus.SUCCEEDED, progress=100,
                        quality_score=quality.score, document_id=document.id,
                    ))
                    session.commit()
                except (AppError, SQLAlchemyError) as exc:
                    session.rollback()
                    if mirrored_id:
                        content_store.delete_document(mirrored_id)
                    missing.append({"source_id": item.source_id, "relative_path": item.relative_path,
                                    "reason": exc.error_code if isinstance(exc, AppError) else "database_write_failed"})
                    continue
                document_ids.append(document.id)
                total_chunks += len(chunks)
                imported.append(
                    {
                        "source_id": item.source_id,
                        "title": item.title,
                        "document_id": document.id,
                        "chunk_count": len(chunks),
                        "ocr_applied": is_ocr,
                    }
                )

            knowledge = self._publish_knowledge(session, document_ids) if publish_knowledge else None
            status_counts = Counter(item.extraction_status for item in items)
            report = {
                "database": self.settings.database_url,
                "corpus_root": str(self.corpus_root),
                "total_files": len(items),
                "imported_files": len(imported),
                "skipped_files": len(skipped),
                "missing_files": len(missing),
                "imported_chunks": total_chunks,
                "manifest_statuses": dict(status_counts),
                "imported": imported,
                "skipped": skipped,
                "missing": missing,
                "knowledge": knowledge,
            }
            report_path = self.processed_root / "full_import_report.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return report
        finally:
            session.close()
            database.engine.dispose()

    def _load_manifest(self) -> list[CorpusItem]:
        manifest = self.processed_root / "manifest.jsonl"
        if not manifest.exists():
            raise FileNotFoundError(f"语料清单不存在: {manifest}")
        return [
            CorpusItem.model_validate_json(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _ensure_sources(self, session, items: list[CorpusItem]) -> dict[SourceType, DataSource]:
        result: dict[SourceType, DataSource] = {}
        for source_type in dict.fromkeys(item.source_type for item in items):
            name = SOURCE_NAMES[source_type]
            source = session.scalar(select(DataSource).where(DataSource.name == name))
            if source is None:
                source = DataSource(
                    name=name,
                    source_type=source_type,
                    connection_type="corpus_batch",
                    owner="国铁川藏科创中心",
                    security_level=SecurityLevel.INTERNAL,
                    sync_policy={"mode": "manual", "source": str(self.corpus_root)},
                )
                session.add(source)
                session.flush()
            result[source_type] = source
        session.commit()
        return result

    def _processed_text_path(self, item: CorpusItem) -> tuple[Path | None, bool]:
        standard = self.processed_root / "documents" / f"{item.source_id}.txt"
        if standard.exists():
            return standard, False
        ocr = self.processed_root / "ocr" / f"{item.source_id}.txt"
        if ocr.exists():
            return ocr, True
        return None, item.extraction_status == "requires_ocr"

    def _store_original(self, item: CorpusItem, source: Path, content: bytes) -> Path:
        suffix = source.suffix.lower()
        destination = self.settings.storage_path / item.checksum_sha256[:2] / f"{item.checksum_sha256}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            # A hard link lets later edits of the input alter the archive.
            destination.write_bytes(content)
        elif hashlib.sha256(destination.read_bytes()).hexdigest() != item.checksum_sha256:
            raise AppError("ORIGINAL_ARCHIVE_CONFLICT", "归档原文摘要不一致，已保留原有文件")
        return destination.resolve()

    def _publish_knowledge(self, session, document_ids: list[str]) -> dict | None:
        service = KnowledgeService(session)
        service.bootstrap_ontology(self.access)
        entity_ids: list[str] = []
        relation_ids: list[str] = []
        for document_id in document_ids:
            extraction = service.extract_candidates(document_id, self.access)
            entity_ids.extend(extraction.entity_ids)
            relation_ids.extend(extraction.relation_ids)
        for entity_id in dict.fromkeys(entity_ids):
            service.review(
                ReviewRequest(
                    target_type="entity",
                    target_id=entity_id,
                    decision=ReviewStatus.APPROVED,
                    comment="全量语料初始化自动审核",
                ),
                self.access,
            )
        for relation_id in dict.fromkeys(relation_ids):
            service.review(
                ReviewRequest(
                    target_type="relation",
                    target_id=relation_id,
                    decision=ReviewStatus.APPROVED,
                    comment="全量语料初始化自动审核",
                ),
                self.access,
            )
        if not entity_ids and not relation_ids:
            return None
        release = service.publish(self.access, notes="全量语料初始化发布")
        return release.model_dump(mode="json")
