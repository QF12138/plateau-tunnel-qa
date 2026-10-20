import hashlib
from pathlib import Path

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import JobStatus, ProcessingStatus, QualityFlag, ReviewStatus, SecurityLevel
from tunnel_qa.core.errors import AppError, ConflictError, NotFoundError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.cleanup import process_cleanup
from tunnel_qa.data.models import ContentCleanupJob, SYSTEM_MANAGED_SOURCE_IDS
from tunnel_qa.data.models import Chunk, DataSource, ImportJob, MonitoringRecord, SourceDocument
from tunnel_qa.data.parsers import parse_document
from tunnel_qa.data.knowledge_chunking import chunk_source_location
from tunnel_qa.data.preparation import prepare_source_knowledge
from tunnel_qa.data.processing_metadata import processing_report, processing_chunk, saved_chunking, saved_quality
from tunnel_qa.data.quality import content_quality_checks, document_quality_report
from tunnel_qa.knowledge.models import (
    Evidence,
    KnowledgeEntity,
    KnowledgeEvidenceLink,
    KnowledgeRelation,
    KnowledgeReview,
)
from tunnel_qa.data.schemas import (
    DataSourceCreate,
    ChunkingConfig,
    DocumentMetadata,
    ImportResult,
    MonitoringInput,
    QualityReport,
)


class DataIngestionService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        content_store: DocumentContentStore | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.content_store = content_store or DocumentContentStore(settings)

    def resolve_chunking(self, chunking: ChunkingConfig | None) -> ChunkingConfig:
        chunking = chunking or ChunkingConfig(
            chunk_size=self.settings.chunk_size, chunk_overlap=self.settings.chunk_overlap
        )
        if chunking.chunk_model not in {"key-v2", "same_as_answer", "structural-v1"}:
            raise AppError("UNSUPPORTED_CHUNK_STRATEGY", "请选择关键正文切块或完整正文切块；不调用大模型改写原文", field="chunk_model")
        return chunking.model_copy(update={
            "chunk_model": "structural-v1" if chunking.chunk_model == "same_as_answer" else chunking.chunk_model
        })

    def _prepare_content(self, filename, content, chunking, source_type):
        if not content:
            raise AppError("EMPTY_FILE", "文件内容为空", field="file")
        if len(content) > self.settings.max_upload_bytes:
            raise AppError("FILE_TOO_LARGE", "文件超过大小限制", field="file")
        try:
            parsed_text, mime_type = parse_document(filename, content)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "DOCUMENT_PARSE_FAILED", "文档解析失败，请检查文件是否损坏且扩展名是否正确",
                field="file", details={"parser_error": type(exc).__name__},
            ) from exc
        prepared, _ = prepare_source_knowledge(
            filename, parsed_text, chunking.chunk_size, chunking.chunk_overlap,
            source_type=source_type,
            mode="key" if chunking.chunk_model == "key-v2" else "full",
            content=content,
        )
        return parsed_text, mime_type, prepared

    def preview_document(self, *, filename, content, source_type, access, chunking=None):
        """Parse in memory only. No file archive, job, chunk or graph is created."""
        access.require("data:import")
        chunking = self.resolve_chunking(chunking)
        parsed_text, _, prepared = self._prepare_content(filename, content, chunking, source_type)
        return {
            "accepted": all(content_quality_checks(parsed_text, [chunk.text for chunk in prepared.chunks]).values()),
            "chunking": chunking.model_dump(mode="json"),
            "processing": prepared.report,
            "total_chunks": len(prepared.chunks),
            "returned_chunks": min(20, len(prepared.chunks)),
            "chunks": [
                {"ordinal": ordinal, "text": chunk.text, "structure_path": chunk.heading,
                 "page_number": chunk.page_start, "page_end": chunk.page_end,
                 "kind": chunk.kind, "warnings": chunk.warnings, "char_count": len(chunk.text)}
                for ordinal, chunk in enumerate(prepared.chunks[:20], 1)
            ],
        }

    def create_data_source(self, payload: DataSourceCreate, access: AccessContext) -> DataSource:
        access.require("data:manage")
        source = DataSource(**payload.model_dump())
        self.session.add(source)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("数据源名称已存在", field="name") from exc
        return source

    def import_document(
        self,
        *,
        data_source_id: str,
        filename: str,
        content: bytes,
        metadata: DocumentMetadata,
        idempotency_key: str,
        access: AccessContext,
        chunking: ChunkingConfig | None = None,
    ) -> ImportResult:
        access.require("data:import")
        source = self.session.get(DataSource, data_source_id)
        if source is None:
            raise NotFoundError("数据源", data_source_id)
        access.require_access(metadata.security_level)

        existing_job = self.session.scalar(
            select(ImportJob).where(ImportJob.idempotency_key == idempotency_key)
        )
        if existing_job:
            existing_chunking = None
            existing_processing = None
            existing_quality = None
            if existing_job.document_id:
                existing_document = self.session.get(SourceDocument, existing_job.document_id)
                if existing_document:
                    access.require_access(SecurityLevel(existing_document.security_level))
                    existing_processing = processing_report(existing_document.metadata_extra)
                    existing_quality = saved_quality(existing_document.metadata_extra)
                    existing_chunking = saved_chunking(existing_document.metadata_extra)
            return ImportResult(
                job_id=existing_job.id,
                document_id=existing_job.document_id,
                status=existing_job.status,
                accepted=existing_job.status == JobStatus.SUCCEEDED,
                idempotent_replay=True,
                chunking=existing_chunking,
                processing=existing_processing,
                quality=existing_quality,
                validation_errors=existing_job.error_report,
            )

        chunking = self.resolve_chunking(chunking)

        job = ImportJob(
            data_source_id=data_source_id,
            idempotency_key=idempotency_key,
            status=JobStatus.RUNNING,
            progress=5,
        )
        self.session.add(job)
        self.session.flush()

        archived_document_id: str | None = None
        processing = None
        try:
            self._validate_file(content, metadata, source)
            checksum = hashlib.sha256(content).hexdigest()
            duplicate = self.session.scalar(
                select(SourceDocument).where(SourceDocument.checksum == checksum)
            )
            if duplicate:
                raise ConflictError(
                    f"文件内容与现有资料 {duplicate.source_id} 重复", field="file"
                )
            parsed_text, mime_type, prepared = self._prepare_content(
                filename, content, chunking, metadata.source_type
            )
            processing = prepared.report
            job.progress = 35
            chunks = [(chunk.heading, chunk.text, chunk.page_start) for chunk in prepared.chunks]
            quality = self._quality_report(metadata, parsed_text, chunks)
            if not quality.passed:
                raise AppError(
                    "DATA_QUALITY_FAILED",
                    "未识别到可入库的有效正文，请检查原文或先预览处理结果" if not chunks else "资料质量检查未通过",
                    details={"issues": quality.issues},
                )

            storage_uri = self._store_immutable(checksum, filename, content)
            metadata_extra = dict(metadata.extra)
            # Processing/synchronization state is server-owned, never an
            # instruction supplied through the generic import metadata API.
            for reserved in ("graph_sync", "processing_history", "processing_version", "rechunked_at", "rechunk_text_source"):
                metadata_extra.pop(reserved, None)
            metadata_extra["chunking"] = chunking.model_dump(mode="json")
            metadata_extra["ingestion_quality"] = quality.model_dump(mode="json")
            metadata_extra["chunk_processing"] = {
                "report": processing,
                "chunks": {str(ordinal): {
                    "page_start": chunk.page_start, "page_end": chunk.page_end,
                    "kind": chunk.kind, "warnings": chunk.warnings,
                } for ordinal, chunk in enumerate(prepared.chunks, 1)},
            }
            document = SourceDocument(
                source_id=metadata.source_id,
                data_source_id=data_source_id,
                title=metadata.title,
                source_type=metadata.source_type,
                project_site=metadata.project_site,
                version=metadata.version,
                effective_date=metadata.effective_date,
                security_level=metadata.security_level,
                owner_org=metadata.owner_org,
                checksum=checksum,
                content_location=metadata.content_location,
                original_filename=filename,
                mime_type=mime_type,
                storage_uri=storage_uri,
                parsed_text=parsed_text,
                status=ProcessingStatus.PENDING_REVIEW,
                metadata_extra=metadata_extra,
            )
            self.session.add(document)
            self.session.flush()
            chunk_rows: list[Chunk] = []
            for ordinal, prepared_chunk in enumerate(prepared.chunks, 1):
                heading, text, page_number = prepared_chunk.heading, prepared_chunk.text, prepared_chunk.page_start
                chunk = Chunk(
                        document_id=document.id,
                        processing_version=1,
                        ordinal=ordinal,
                        text=text,
                        structure_path=heading,
                        page_number=page_number,
                        content_location=chunk_source_location(metadata.content_location, prepared_chunk, ordinal),
                        security_level=metadata.security_level,
                        char_count=len(text),
                    )
                self.session.add(chunk)
                chunk_rows.append(chunk)
            self.session.flush()
            self.content_store.save_document(
                document,
                chunk_rows,
                original_content=content,
                chunking=chunking,
            )
            archived_document_id = document.id
            job.document_id = document.id
            job.status = JobStatus.SUCCEEDED
            job.progress = 100
            job.quality_score = quality.score
            self.session.commit()
            return ImportResult(
                job_id=job.id,
                document_id=document.id,
                status=job.status,
                accepted=True,
                quality=quality,
                chunking=chunking,
                processing=processing,
            )
        except (AppError, IntegrityError) as exc:
            self.session.rollback()
            if archived_document_id:
                self.content_store.delete_document(archived_document_id)
            persisted_job = self.session.scalar(
                select(ImportJob).where(ImportJob.idempotency_key == idempotency_key)
            )
            if persisted_job is None:
                persisted_job = ImportJob(
                    data_source_id=data_source_id,
                    idempotency_key=idempotency_key,
                )
                self.session.add(persisted_job)
            message = exc.message if isinstance(exc, AppError) else "source_id 或文件摘要重复"
            persisted_job.status = JobStatus.QUARANTINED
            persisted_job.progress = 100
            persisted_job.error_report = [message]
            self.session.commit()
            return ImportResult(
                job_id=persisted_job.id,
                status=persisted_job.status,
                accepted=False,
                validation_errors=[message],
                processing=processing,
            )

    def ingest_monitoring(
        self, payload: MonitoringInput, access: AccessContext
    ) -> MonitoringRecord:
        access.require("data:import")
        cleaned_value, canonical_unit, rule = self._convert_unit(payload.value, payload.unit)
        quality_flag = payload.quality_flag
        if payload.value is None:
            quality_flag = QualityFlag.MISSING
        record = MonitoringRecord(
            sensor_id=payload.sensor_id,
            measured_at=payload.timestamp,
            metric_code=payload.metric_code,
            original_value=payload.value,
            cleaned_value=cleaned_value,
            original_unit=payload.unit,
            canonical_unit=canonical_unit,
            conversion_rule=rule,
            quality_flag=quality_flag,
            location=payload.location,
            source_system=payload.source_system,
            processing_details={"operator": access.user_id, "algorithm": "unit-map-v1"},
        )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("同一测点、时间和指标的记录已存在") from exc
        return record

    def delete_document(self, document_id: str, access: AccessContext) -> dict[str, int | str]:
        """Delete one source document and all content/knowledge rows derived from it."""
        access.require("data:manage")
        document = self.session.get(SourceDocument, document_id)
        if document is None:
            raise NotFoundError("源文档", document_id)
        access.require_access(document.security_level)

        chunk_ids = list(
            self.session.scalars(select(Chunk.id).where(Chunk.document_id == document_id)).all()
        )
        evidence_ids = (
            list(
                self.session.scalars(
                    select(Evidence.id).where(Evidence.chunk_id.in_(chunk_ids))
                ).all()
            )
            if chunk_ids
            else []
        )
        cleanup = self._delete_knowledge_for_evidence(evidence_ids)
        self.session.execute(delete(ImportJob).where(ImportJob.document_id == document_id))

        storage_path = Path(document.storage_uri)
        source_id = document.source_id
        cleanup_job = ContentCleanupJob(target_kind="document", target_id=document_id,
                                        storage_uri=str(storage_path))
        self.session.add(cleanup_job)
        self.session.delete(document)
        self.session.commit()

        cleaned = process_cleanup(self.session, self.content_store, self.settings, cleanup_job)
        return {
            "cleanup_pending": not cleaned,
            "cleanup_job_id": cleanup_job.id,
            "document_id": document_id,
            "source_id": source_id,
            "deleted_chunks": len(chunk_ids),
            **cleanup,
        }

    def list_document_chunks(
        self, document_id: str, access: AccessContext, *, limit: int = 200
    ) -> dict:
        """List traceable chunks and the graph facts each chunk supports."""
        access.require("data:manage")
        document = self.session.get(SourceDocument, document_id)
        if document is None:
            raise NotFoundError("源文档", document_id)
        access.require_access(document.security_level)
        chunks = self.session.scalars(
            select(Chunk)
            .where(Chunk.document_id == document_id,
                   Chunk.processing_version == document.processing_version)
            .order_by(Chunk.ordinal)
            .limit(limit)
        ).all()
        texts = self.content_store.get_chunk_texts(chunk.id for chunk in chunks)
        rows = []
        for chunk in chunks:
            evidence_ids = list(
                self.session.scalars(
                    select(Evidence.id).where(Evidence.chunk_id == chunk.id)
                ).all()
            )
            entity_ids: set[str] = set()
            relation_ids: set[str] = set()
            if evidence_ids:
                links = self.session.scalars(
                    select(KnowledgeEvidenceLink).where(
                        KnowledgeEvidenceLink.evidence_id.in_(evidence_ids)
                    )
                ).all()
                entity_ids = {
                    link.target_id for link in links if link.target_type == "entity"
                }
                relation_ids = {
                    link.target_id for link in links if link.target_type == "relation"
                }
            text = texts.get(chunk.id, chunk.text)
            processed = processing_chunk(document.metadata_extra, chunk.ordinal)
            rows.append(
                {
                    "chunk_id": chunk.id,
                    "ordinal": chunk.ordinal,
                    "page_number": chunk.page_number,
                    "page_end": processed.get("page_end", chunk.page_number),
                    "kind": processed.get("kind", "legacy"),
                    "warnings": processed.get("warnings", []),
                    "structure_path": chunk.structure_path,
                    "content_location": chunk.content_location,
                    "char_count": chunk.char_count,
                    "excerpt": text[:260],
                    "text": text,
                    "evidence_count": len(evidence_ids),
                    "entity_count": len(entity_ids),
                    "relation_count": len(relation_ids),
                }
            )
        total = len(
            self.session.scalars(
                select(Chunk.id).where(Chunk.document_id == document_id,
                                       Chunk.processing_version == document.processing_version)
            ).all()
        )
        return {
            "document_id": document_id,
            "title": document.title,
            "chunks": rows,
            "returned_chunks": len(rows),
            "total_chunks": total,
            "processing": processing_report(document.metadata_extra),
        }

    def _remaining_current_evidence(
        self, target_type: str, target_id: str, primary_id: str, removed_ids: list[str],
        *, manual_only: bool = False,
    ) -> Evidence | None:
        """Historical provenance is retained, but cannot keep a live fact alive."""
        current = (
            select(Evidence)
            .join(Chunk, Evidence.chunk_id == Chunk.id)
            .join(SourceDocument, Chunk.document_id == SourceDocument.id)
            .where(Chunk.processing_version == SourceDocument.processing_version,
                   Evidence.id.not_in(removed_ids))
        )
        if manual_only:
            current = current.where(SourceDocument.source_id.in_(SYSTEM_MANAGED_SOURCE_IDS))
        primary = self.session.scalar(current.where(Evidence.id == primary_id))
        if primary is not None:
            return primary
        return self.session.scalar(
            current.join(KnowledgeEvidenceLink, KnowledgeEvidenceLink.evidence_id == Evidence.id)
            .where(KnowledgeEvidenceLink.target_type == target_type,
                   KnowledgeEvidenceLink.target_id == target_id)
            .order_by(KnowledgeEvidenceLink.created_at)
        )

    def _delete_knowledge_for_evidence(self, evidence_ids: list[str]) -> dict[str, int]:
        """Detach evidence and remove only facts left without valid support.

        A canonical entity/relation survives when another chunk still supports
        it. If the removed evidence was primary, current-version evidence is
        promoted. Published manual relations preserve their endpoint entities
        using the manual record, while unsupported automatic facts are removed.
        """
        if not evidence_ids:
            return {
                "deleted_entities": 0,
                "deleted_relations": 0,
                "preserved_entities": 0,
                "preserved_relations": 0,
            }

        affected_entity_ids = set(
            self.session.scalars(
                select(KnowledgeEvidenceLink.target_id).where(
                    KnowledgeEvidenceLink.target_type == "entity",
                    KnowledgeEvidenceLink.evidence_id.in_(evidence_ids),
                )
            ).all()
        )
        affected_relation_ids = set(
            self.session.scalars(
                select(KnowledgeEvidenceLink.target_id).where(
                    KnowledgeEvidenceLink.target_type == "relation",
                    KnowledgeEvidenceLink.evidence_id.in_(evidence_ids),
                )
            ).all()
        )

        primary_entity_ids = set(
            self.session.scalars(
                select(KnowledgeEntity.id).where(KnowledgeEntity.evidence_id.in_(evidence_ids))
            ).all()
        )
        entity_ids: set[str] = set()
        # A primary may still point at a retained historical block. Recheck
        # all affected facts, including those whose current support was secondary.
        for entity_id in primary_entity_ids | affected_entity_ids:
            entity = self.session.get(KnowledgeEntity, entity_id)
            alternative = self._remaining_current_evidence(
                "entity", entity_id, entity.evidence_id, evidence_ids
            ) if entity is not None else None
            if entity is not None and alternative is not None:
                entity.evidence_id = alternative.id
            else:
                entity_ids.add(entity_id)

        # A user-authored relation is independent of an uploaded document.
        # Retain its endpoints under the manual audit record when their final
        # automatic support is removed, instead of cascading into manual work.
        if entity_ids:
            manual_relations = self.session.scalars(
                select(KnowledgeRelation).where(
                    KnowledgeRelation.status == ReviewStatus.PUBLISHED,
                    or_(KnowledgeRelation.source_entity_id.in_(entity_ids),
                        KnowledgeRelation.target_entity_id.in_(entity_ids)),
                )
            ).all()
            for relation in manual_relations:
                manual_evidence = self._remaining_current_evidence(
                    "relation", relation.id, relation.evidence_id, evidence_ids,
                    manual_only=True,
                )
                if manual_evidence is None:
                    continue
                for endpoint_id in (relation.source_entity_id, relation.target_entity_id):
                    if endpoint_id not in entity_ids:
                        continue
                    endpoint = self.session.get(KnowledgeEntity, endpoint_id)
                    endpoint.evidence_id = manual_evidence.id
                    entity_ids.remove(endpoint_id)

        primary_relation_ids = set(
            self.session.scalars(
                select(KnowledgeRelation.id).where(
                    KnowledgeRelation.evidence_id.in_(evidence_ids)
                )
            ).all()
        )
        relation_ids = set(
            self.session.scalars(
                select(KnowledgeRelation.id).where(
                    or_(
                        KnowledgeRelation.source_entity_id.in_(entity_ids),
                        KnowledgeRelation.target_entity_id.in_(entity_ids),
                    )
                )
            ).all()
        ) if entity_ids else set()
        for relation_id in primary_relation_ids | affected_relation_ids:
            if relation_id in relation_ids:
                continue
            relation = self.session.get(KnowledgeRelation, relation_id)
            alternative = self._remaining_current_evidence(
                "relation", relation_id, relation.evidence_id, evidence_ids
            ) if relation is not None else None
            if relation is not None and alternative is not None:
                relation.evidence_id = alternative.id
            else:
                relation_ids.add(relation_id)

        orphan_candidates = set(evidence_ids)
        if entity_ids:
            orphan_candidates.update(
                self.session.scalars(
                    select(KnowledgeEvidenceLink.evidence_id).where(
                        KnowledgeEvidenceLink.target_type == "entity",
                        KnowledgeEvidenceLink.target_id.in_(entity_ids),
                    )
                ).all()
            )
        if relation_ids:
            orphan_candidates.update(
                self.session.scalars(
                    select(KnowledgeEvidenceLink.evidence_id).where(
                        KnowledgeEvidenceLink.target_type == "relation",
                        KnowledgeEvidenceLink.target_id.in_(relation_ids),
                    )
                ).all()
            )

        if relation_ids:
            self.session.execute(
                delete(KnowledgeReview).where(
                    KnowledgeReview.target_type == "relation",
                    KnowledgeReview.target_id.in_(relation_ids),
                )
            )
            self.session.execute(
                delete(KnowledgeEvidenceLink).where(
                    KnowledgeEvidenceLink.target_type == "relation",
                    KnowledgeEvidenceLink.target_id.in_(relation_ids),
                )
            )
            self.session.execute(
                delete(KnowledgeRelation).where(KnowledgeRelation.id.in_(relation_ids))
            )
        if entity_ids:
            self.session.execute(
                delete(KnowledgeReview).where(
                    KnowledgeReview.target_type == "entity",
                    KnowledgeReview.target_id.in_(entity_ids),
                )
            )
            self.session.execute(
                delete(KnowledgeEvidenceLink).where(
                    KnowledgeEvidenceLink.target_type == "entity",
                    KnowledgeEvidenceLink.target_id.in_(entity_ids),
                )
            )
            self.session.execute(
                delete(KnowledgeEntity).where(KnowledgeEntity.id.in_(entity_ids))
            )
        self.session.execute(
            delete(KnowledgeEvidenceLink).where(
                KnowledgeEvidenceLink.evidence_id.in_(evidence_ids)
            )
        )
        self.session.flush()
        for evidence_id in orphan_candidates:
            still_primary = self.session.scalar(
                select(KnowledgeEntity.id).where(KnowledgeEntity.evidence_id == evidence_id)
            ) or self.session.scalar(
                select(KnowledgeRelation.id).where(KnowledgeRelation.evidence_id == evidence_id)
            )
            still_linked = self.session.scalar(
                select(KnowledgeEvidenceLink.id).where(
                    KnowledgeEvidenceLink.evidence_id == evidence_id
                )
            )
            if not still_primary and not still_linked:
                evidence = self.session.get(Evidence, evidence_id)
                if evidence is not None:
                    self.session.delete(evidence)

        return {
            "deleted_entities": len(entity_ids),
            "deleted_relations": len(relation_ids),
            "preserved_entities": len(affected_entity_ids - entity_ids),
            "preserved_relations": len(affected_relation_ids - relation_ids),
        }

    def delete_chunk(self, chunk_id: str, access: AccessContext) -> dict[str, int | str]:
        """Delete one chunk and safely update all graph provenance."""
        access.require("data:manage")
        chunk = self.session.get(Chunk, chunk_id)
        if chunk is None:
            raise NotFoundError("文档分块", chunk_id)
        document = self.session.get(SourceDocument, chunk.document_id)
        if document is None:
            raise NotFoundError("源文档", chunk.document_id)
        access.require_access(document.security_level)

        if chunk.processing_version != document.processing_version:
            raise AppError("HISTORICAL_CHUNK_READ_ONLY", "历史版本分块用于问答追溯，不能单独删除")

        evidence_ids = list(
            self.session.scalars(select(Evidence.id).where(Evidence.chunk_id == chunk_id)).all()
        )
        cleanup = self._delete_knowledge_for_evidence(evidence_ids)
        document_id = document.id
        ordinal = chunk.ordinal
        self.session.delete(chunk)
        self.session.flush()
        remaining_count = self.session.scalar(
            select(func.count(Chunk.id)).where(
                Chunk.document_id == document_id,
                Chunk.processing_version == document.processing_version,
            )
        ) or 0
        if remaining_count == 0:
            document.status = ProcessingStatus.ARCHIVED
        cleanup_job = ContentCleanupJob(target_kind="chunk", target_id=chunk_id)
        self.session.add(cleanup_job)
        self.session.commit()
        cleaned = process_cleanup(self.session, self.content_store, self.settings, cleanup_job)
        return {
            "cleanup_pending": not cleaned,
            "cleanup_job_id": cleanup_job.id,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "chunk_ordinal": ordinal,
            "remaining_chunks": remaining_count,
            **cleanup,
        }

    def _validate_file(
        self, content: bytes, metadata: DocumentMetadata, source: DataSource
    ) -> None:
        if not content:
            raise AppError("EMPTY_FILE", "文件内容为空", field="file")
        if len(content) > self.settings.max_upload_bytes:
            raise AppError("FILE_TOO_LARGE", "文件超过大小限制", field="file")
        if metadata.source_type != source.source_type:
            raise AppError("SOURCE_TYPE_MISMATCH", "资料类型与数据源类型不一致", field="source_type")
        if metadata.source_type.value == "engineering_case" and not metadata.project_site:
            raise AppError("MISSING_PROJECT_SITE", "工程案例必须填写项目或工点", field="project_site")

    def _quality_report(
        self,
        metadata: DocumentMetadata,
        parsed_text: str,
        chunks: list[tuple[str, str, int | None]],
    ) -> QualityReport:
        return document_quality_report(metadata, parsed_text, chunks)

    def _store_immutable(self, checksum: str, filename: str, content: bytes) -> str:
        safe_suffix = Path(filename).suffix.lower()
        destination = self.settings.storage_path / checksum[:2] / f"{checksum}{safe_suffix}"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
                    raise ConflictError("对象存储校验冲突")
            else:
                destination.write_bytes(content)
        except OSError as exc:
            raise AppError(
                "OBJECT_STORAGE_FAILED",
                "原始文档保存失败，请检查存储目录是否可用",
                details={"storage_error": type(exc).__name__},
            ) from exc
        return str(destination.resolve())

    @staticmethod
    def _convert_unit(value: float | None, unit: str) -> tuple[float | None, str, str]:
        conversions = {
            "℃": (lambda x: x, "℃", "identity"),
            "°C": (lambda x: x, "℃", "celsius-alias"),
            "K": (lambda x: x - 273.15, "℃", "kelvin-to-celsius"),
            "mm": (lambda x: x, "mm", "identity"),
            "cm": (lambda x: x * 10, "mm", "cm-to-mm"),
            "m": (lambda x: x * 1000, "mm", "m-to-mm"),
            "MPa": (lambda x: x, "MPa", "identity"),
            "kPa": (lambda x: x / 1000, "MPa", "kpa-to-mpa"),
        }
        if unit not in conversions:
            return value, unit, "unmapped"
        converter, canonical, rule = conversions[unit]
        return (converter(value) if value is not None else None), canonical, rule
