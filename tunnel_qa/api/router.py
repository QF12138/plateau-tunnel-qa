import hashlib
import json

from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tunnel_qa.api.dependencies import get_access_context, get_session
from tunnel_qa.core.enums import (
    ProcessingStatus,
    SECURITY_RANK,
    SecurityLevel,
    SourceType,
)
from tunnel_qa.core.errors import AppError, NotFoundError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.tracing import get_trace_id
from tunnel_qa.data.models import (
    Chunk,
    DataSource,
    ImportJob,
    SourceDocument,
    SYSTEM_MANAGED_SOURCE_IDS,
)
from tunnel_qa.data.schemas import (
    DataSourceCreate,
    DataSourceRead,
    ChunkingConfig,
    DocumentMetadata,
    ImportResult,
    MonitoringInput,
)
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.data.processing_metadata import metadata_object
from tunnel_qa.data.cleanup import retry_cleanup
from tunnel_qa.data.models import ContentCleanupJob
from tunnel_qa.knowledge.schemas import (
    BatchReviewRequest,
    ExtractionResult,
    GraphEdge,
    GraphNode,
    ManualEntityCreate,
    ManualEntityUpdate,
    ManualRelationCreate,
    ManualRelationUpdate,
    PublishResult,
    ReviewRequest,
    Subgraph,
)
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.knowledge.synchronization import DocumentGraphSynchronizer
from tunnel_qa.qa.schemas import QARequest, QAResponse
from tunnel_qa.qa.model_gateway import build_model_gateway
from tunnel_qa.qa.reporting import QAReportService, ReportFormat
from tunnel_qa.qa.service import QAService
from tunnel_qa.quality.schemas import (
    EvaluationCaseCreate,
    EvaluationRunResponse,
    FeedbackCreate,
    HealthReport,
    RiskAnalysisRequest,
    RiskAnalysisResponse,
)
from tunnel_qa.quality.models import RiskRule
from tunnel_qa.quality.service import (
    EvaluationService,
    FeedbackService,
    MonitoringService,
    RiskService,
)
from tunnel_qa.retrieval.service import HybridRetrievalService
from tunnel_qa.workbench.schemas import TraceDetail, WorkbenchOverview
from tunnel_qa.workbench.service import WorkbenchService
from tunnel_qa.settings.schemas import SystemSettingsRead, SystemSettingsUpdate
from tunnel_qa.settings.service import SystemSettingsService


router = APIRouter(prefix="/api/v1")


@router.get("/auth/me")
def current_identity(request: Request, access: AccessContext = Depends(get_access_context)):
    return {
        "user_id": access.user_id, "role": access.role, "security_level": access.security_level,
        "can_manage": access.can("settings:write"),
        "username": getattr(request.state, "account_username", access.user_id),
        "csrf_token": getattr(request.state, "csrf_token", None),
    }

DocumentCreatedPeriod = Literal[
    "today", "within_week", "within_month", "within_half_year", "before_half_year"
]


def _document_created_cutoff(period: DocumentCreatedPeriod) -> datetime:
    now = datetime.now(timezone.utc)
    if period == "today":
        shanghai_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        return shanghai_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        )
    days = {
        "within_week": 7,
        "within_month": 30,
        "within_half_year": 183,
        "before_half_year": 183,
    }[period]
    return now - timedelta(days=days)


def ingestion_service(request: Request, session: Session) -> DataIngestionService:
    return DataIngestionService(
        session, request.app.state.settings, request.app.state.content_store
    )


def qa_service(request: Request, session: Session) -> QAService:
    return QAService(
        session,
        request.app.state.settings,
        HybridRetrievalService(session, request.app.state.content_store),
        request.app.state.model_gateway,
    )


@router.get("/workbench/overview", response_model=WorkbenchOverview)
def workbench_overview(
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return WorkbenchService(session).overview(access)


@router.get("/workbench/traces/{trace_id}", response_model=TraceDetail)
def workbench_trace_detail(
    trace_id: str,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return WorkbenchService(session).trace_detail(trace_id, access)


@router.get("/settings", response_model=SystemSettingsRead)
def get_system_settings(
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return SystemSettingsService(session).get(
        access, request.app.state.settings, request.app.state.model_gateway
    )


@router.put("/settings", response_model=SystemSettingsRead)
def update_system_settings(
    payload: SystemSettingsUpdate,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    service = SystemSettingsService(session)
    service.update(payload, access, request.app.state.settings)
    request.app.state.model_gateway = build_model_gateway(request.app.state.settings)
    return service.get(access, request.app.state.settings, request.app.state.model_gateway)


@router.post("/data/sources", response_model=DataSourceRead, status_code=201)
def create_data_source(
    payload: DataSourceCreate,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return ingestion_service(request, session).create_data_source(payload, access)


@router.get("/data/documents")
def list_documents(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    query: str | None = Query(default=None, max_length=200),
    source_type: SourceType | None = Query(default=None),
    status: ProcessingStatus | None = Query(default=None),
    created_period: DocumentCreatedPeriod | None = Query(default=None),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    access.require("data:manage")
    visible_security_levels = [
        level
        for level, rank in SECURITY_RANK.items()
        if rank <= SECURITY_RANK[access.security_level]
    ]
    base_conditions = (
        SourceDocument.source_id.not_in(SYSTEM_MANAGED_SOURCE_IDS),
        SourceDocument.security_level.in_(visible_security_levels),
    )
    statement = (
        select(SourceDocument, func.count(Chunk.id))
        .outerjoin(Chunk, (Chunk.document_id == SourceDocument.id)
                   & (Chunk.processing_version == SourceDocument.processing_version))
        .where(*base_conditions)
        .group_by(SourceDocument.id)
        .order_by(SourceDocument.created_at.desc())
    )
    if query and query.strip():
        term = query.strip()
        statement = statement.where(
            or_(
                SourceDocument.title.contains(term),
                SourceDocument.source_id.contains(term),
                SourceDocument.original_filename.contains(term),
                SourceDocument.owner_org.contains(term),
                SourceDocument.project_site.contains(term),
            )
        )
    if source_type:
        statement = statement.where(SourceDocument.source_type == source_type)
    if status:
        statement = statement.where(SourceDocument.status == status)
    if created_period:
        cutoff = _document_created_cutoff(created_period)
        if created_period == "before_half_year":
            statement = statement.where(SourceDocument.created_at < cutoff)
        else:
            statement = statement.where(SourceDocument.created_at >= cutoff)
    filtered_documents = session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ) or 0
    visible_rows = session.execute(statement.limit(limit)).all()
    documents = []
    for document, chunk_count in visible_rows:
        documents.append(
            {
                "document_id": document.id,
                "source_id": document.source_id,
                "title": document.title,
                "source_type": document.source_type,
                "version": document.version,
                "effective_date": document.effective_date,
                "status": document.status,
                "security_level": document.security_level,
                "owner_org": document.owner_org,
                "project_site": document.project_site,
                "content_location": document.content_location,
                "original_filename": document.original_filename,
                "chunk_count": chunk_count,
                "ocr_applied": bool(metadata_object(document.metadata_extra).get("ocr_applied")),
                "created_at": document.created_at,
            }
        )
    total_documents = session.scalar(
        select(func.count(SourceDocument.id)).where(*base_conditions)
    ) or 0
    total_chunks = session.scalar(
        select(func.count(Chunk.id))
        .join(SourceDocument, Chunk.document_id == SourceDocument.id)
        .where(*base_conditions, Chunk.processing_version == SourceDocument.processing_version)
    ) or 0
    return {
        "documents": documents,
        "filtered_documents": filtered_documents,
        "total_documents": total_documents,
        "total_chunks": total_chunks,
        "max_upload_bytes": request.app.state.settings.max_upload_bytes,
        "supported_extensions": [".pdf", ".docx", ".txt", ".md", ".html", ".json", ".csv", ".xlsx"],
        "chunking_defaults": {
            "chunk_size": request.app.state.settings.chunk_size,
            "chunk_overlap": request.app.state.settings.chunk_overlap,
            "chunk_model": "key-v2",
            "answer_model": request.app.state.model_gateway.model,
        },
        "content_databases": {
            "original_documents": total_documents,
            "chunks": total_chunks,
        },
    }


@router.get("/data/cleanup-jobs")
def list_cleanup_jobs(session: Session = Depends(get_session),
                      access: AccessContext = Depends(get_access_context)):
    access.require("settings:write")
    jobs = session.scalars(select(ContentCleanupJob).where(
        ContentCleanupJob.status == "pending").order_by(ContentCleanupJob.created_at).limit(100)).all()
    return {"pending": session.scalar(select(func.count(ContentCleanupJob.id)).where(
        ContentCleanupJob.status == "pending")) or 0,
        "jobs": [{"id": j.id, "target_kind": j.target_kind, "target_id": j.target_id,
                  "attempts": j.attempts, "last_error": j.last_error} for j in jobs]}


@router.post("/data/cleanup-jobs/retry")
def retry_cleanup_jobs(request: Request, session: Session = Depends(get_session),
                       access: AccessContext = Depends(get_access_context)):
    access.require("settings:write")
    return retry_cleanup(session, request.app.state.content_store, request.app.state.settings)


@router.delete("/data/documents/{document_id}")
def delete_document(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return ingestion_service(request, session).delete_document(document_id, access)


@router.get("/data/documents/{document_id}/chunks")
def list_document_chunks(
    document_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return ingestion_service(request, session).list_document_chunks(
        document_id, access, limit=limit
    )


@router.delete("/data/chunks/{chunk_id}")
def delete_document_chunk(
    chunk_id: str,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return ingestion_service(request, session).delete_chunk(chunk_id, access)


def _upload_chunking(request, chunk_size, chunk_overlap, chunk_model):
    try:
        return ChunkingConfig(
            chunk_size=request.app.state.settings.chunk_size if chunk_size is None else chunk_size,
            chunk_overlap=request.app.state.settings.chunk_overlap if chunk_overlap is None else chunk_overlap,
            chunk_model=chunk_model,
        )
    except ValidationError as exc:
        raise AppError("INVALID_CHUNKING_CONFIG", "切块参数无效：overlap 必须小于切块大小", field="chunk_overlap") from exc


@router.post("/data/documents/preview")
async def preview_document_chunks(
    request: Request,
    source_type: SourceType = Form(SourceType.RESEARCH),
    chunk_size: int | None = Form(None, ge=100, le=5000),
    chunk_overlap: int | None = Form(None, ge=0, le=1000),
    chunk_model: str = Form("key-v2"),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    access.require("data:import")
    config = _upload_chunking(request, chunk_size, chunk_overlap, chunk_model)
    content = await file.read(request.app.state.settings.max_upload_bytes + 1)
    return ingestion_service(request, session).preview_document(
        filename=file.filename or "upload.bin", content=content, source_type=source_type,
        access=access, chunking=config,
    )


@router.post("/data/documents/upload", status_code=202)
async def upload_and_publish_document(
    request: Request,
    title: str = Form(...),
    source_type: SourceType = Form(...),
    version: str = Form("V1"),
    effective_date: str = Form(...),
    security_level: SecurityLevel = Form(SecurityLevel.INTERNAL),
    owner_org: str = Form("国铁川藏科创中心"),
    project_site: str | None = Form(None),
    chunk_size: int | None = Form(None, ge=100, le=5000),
    chunk_overlap: int | None = Form(None, ge=0, le=1000),
    chunk_model: str = Form("key-v2"),
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    access.require("data:import")
    service = ingestion_service(request, session)
    chunking = service.resolve_chunking(_upload_chunking(request, chunk_size, chunk_overlap, chunk_model))
    content = await file.read(request.app.state.settings.max_upload_bytes + 1)
    checksum = hashlib.sha256(content).hexdigest()
    type_code = {
        SourceType.STANDARD: "STD",
        SourceType.RESEARCH: "RES",
        SourceType.EXPERT_EXPERIENCE: "EXP",
        SourceType.ENGINEERING_CASE: "CASE",
        SourceType.MONITORING: "MON",
    }[source_type]
    source_name = f"手动文档入库·{source_type.value}"
    source = session.scalar(select(DataSource).where(DataSource.name == source_name))
    if source is None:
        source = ingestion_service(request, session).create_data_source(
            DataSourceCreate(
                name=source_name,
                source_type=source_type,
                owner=owner_org,
                security_level=security_level,
                sync_policy={"mode": "manual_upload"},
            ),
            access,
        )
    metadata = DocumentMetadata(
        source_id=f"SRC-{type_code}-{checksum[:12].upper()}",
        title=title.strip(),
        source_type=source_type,
        project_site=project_site or None,
        version=version.strip(),
        effective_date=effective_date,
        security_level=security_level,
        owner_org=owner_org.strip(),
        content_location="用户上传（自动切片）",
        extra={
            "upload_method": "workbench",
            "ocr_applied": False,
            "requested_chunk_model": chunk_model,
        },
    )
    result = ingestion_service(request, session).import_document(
        data_source_id=source.id,
        filename=file.filename or "upload.bin",
        content=content,
        metadata=metadata,
        idempotency_key=idempotency_key or f"manual-upload-{checksum[:24]}",
        access=access,
        chunking=chunking,
    )
    graph = {
        "entities": 0,
        "relations": 0,
        "published": False,
        "pending_entities": 0,
        "pending_relations": 0,
    }
    document_status = None
    if result.accepted and result.document_id:
        document = session.get(SourceDocument, result.document_id)
        if document is None:
            raise NotFoundError("文档", result.document_id)
        document_status = document.status
        if not result.idempotent_replay:
            document.status = ProcessingStatus.PUBLISHED
            document_status = document.status
            session.commit()
        if document.status != ProcessingStatus.ARCHIVED:
            graph = DocumentGraphSynchronizer(session, request.app.state.content_store).synchronize(
                document.id, access
            )
        # A replay keeps original document metadata and chunk boundaries. Only an
        # unfinished graph stage is retried; completed sync returns its saved result.
        return {**result.model_dump(mode="json"), "source_id": document.source_id,
                "title": document.title, "document_status": document_status, "graph": graph}
    return {
        **result.model_dump(mode="json"),
        "source_id": metadata.source_id,
        "title": metadata.title,
        "document_status": document_status,
        "graph": graph,
    }


@router.post("/data/documents/{document_id}/sync-graph")
def synchronize_document_graph(
    document_id: str,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return DocumentGraphSynchronizer(session, request.app.state.content_store).synchronize(
        document_id, access
    )


@router.post("/data/import", response_model=ImportResult, status_code=202)
async def import_document(
    request: Request,
    data_source_id: str = Form(...),
    metadata_json: str = Form(...),
    file: UploadFile = File(...),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    try:
        metadata = DocumentMetadata.model_validate_json(metadata_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AppError("INVALID_METADATA", "metadata_json 格式或字段无效", field="metadata_json") from exc
    content = await file.read(request.app.state.settings.max_upload_bytes + 1)
    return ingestion_service(request, session).import_document(
        data_source_id=data_source_id,
        filename=file.filename or "upload.bin",
        content=content,
        metadata=metadata,
        idempotency_key=idempotency_key,
        access=access,
    )


@router.post("/data/monitoring", status_code=201)
def ingest_monitoring(
    payload: MonitoringInput,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    record = ingestion_service(request, session).ingest_monitoring(payload, access)
    return {"record_id": record.id, "quality_flag": record.quality_flag}


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    access.require("data:manage")
    job = session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("导入任务", job_id)
    return {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "quality": job.quality_score,
        "error_report": job.error_report,
        "document_id": job.document_id,
    }


@router.post("/knowledge/ontology/bootstrap")
def bootstrap_ontology(
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    result = KnowledgeService(session).bootstrap_ontology(access)
    return {"count": len(result), "ontology_type_ids": [item.id for item in result]}


@router.get("/knowledge/ontology")
def list_ontology_types(
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).list_ontology_types(access)


@router.post("/knowledge/extract/{document_id}", response_model=ExtractionResult)
def extract_knowledge(
    document_id: str,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).extract_candidates(document_id, access)


@router.post("/knowledge/review")
def review_knowledge(
    payload: ReviewRequest,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    result = KnowledgeService(session).review(payload, access)
    return {"audit_id": result.id, "decision": result.decision, "target_id": result.target_id}


@router.get("/knowledge/candidates")
def list_knowledge_candidates(
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).list_review_candidates(limit, access)


@router.post("/knowledge/review/batch")
def batch_review_knowledge(
    payload: BatchReviewRequest,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).review_batch(payload, access)


@router.get("/knowledge/evidence/{target_type}/{target_id}")
def list_knowledge_evidence(
    target_type: str,
    target_id: str,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(
        session, request.app.state.content_store
    ).list_fact_evidence(target_type, target_id, access)


@router.get("/knowledge/releases")
def list_knowledge_releases(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).list_releases(limit, access)


@router.post("/knowledge/publish", response_model=PublishResult)
def publish_knowledge(
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).publish(access)


@router.get("/graph/subgraph", response_model=Subgraph)
def get_subgraph(
    entity: str = Query(..., min_length=1),
    depth: int = Query(2, ge=0, le=5),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).get_subgraph(entity, depth, access)


@router.get("/graph/entities", response_model=list[GraphNode])
def list_graph_entities(
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=300, ge=1, le=500),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).list_entities(query, limit, access)


@router.get("/graph/workspace", response_model=Subgraph)
def get_graph_workspace(
    limit: int = Query(default=500, ge=1, le=500),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).get_workspace(limit, access)


@router.post("/graph/entities", response_model=GraphNode, status_code=201)
def create_graph_entity(
    payload: ManualEntityCreate,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).create_manual_entity(payload, access)


@router.put("/graph/entities/{entity_id}", response_model=GraphNode)
def update_graph_entity(
    entity_id: str,
    payload: ManualEntityUpdate,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).update_manual_entity(entity_id, payload, access)


@router.delete("/graph/entities/{entity_id}")
def delete_graph_entity(
    entity_id: str,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).delete_manual_entity(entity_id, access)


@router.post("/graph/relations", response_model=GraphEdge, status_code=201)
def create_graph_relation(
    payload: ManualRelationCreate,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).create_manual_relation(payload, access)


@router.put("/graph/relations/{relation_id}", response_model=GraphEdge)
def update_graph_relation(
    relation_id: str,
    payload: ManualRelationUpdate,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).update_manual_relation(relation_id, payload, access)


@router.delete("/graph/relations/{relation_id}")
def delete_graph_relation(
    relation_id: str,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).delete_manual_relation(relation_id, access)


@router.get("/graph/statistics")
def graph_statistics(
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return KnowledgeService(session).graph_statistics(access)


@router.post("/qa/ask", response_model=QAResponse)
def ask(
    payload: QARequest,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return qa_service(request, session).ask(payload, access, trace_id=get_trace_id())


@router.get("/qa/reports/{message_id}")
def export_qa_report(
    message_id: str,
    format: ReportFormat = Query("markdown"),
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    report = QAReportService(session).generate(message_id, format, access)
    localized_name = f"隧道智能问答报告_{report.trace_id}.{report.extension}"
    disposition = (
        f'attachment; filename="qa-report-{report.trace_id}.{report.extension}"; '
        f"filename*=UTF-8''{quote(localized_name)}"
    )
    return Response(
        content=report.content,
        media_type=report.media_type,
        headers={
            "Content-Disposition": disposition,
            "Content-Language": "zh-CN",
        },
    )


@router.post("/decision/risk", response_model=RiskAnalysisResponse)
def analyze_risk(
    payload: RiskAnalysisRequest,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    service = RiskService(session, qa_service(request, session))
    if not session.query(RiskRule).first():
        if access.role.value == "system_admin":
            service.bootstrap_rules(access)
    return service.analyze(payload, access)


@router.post("/feedback", status_code=201)
def submit_feedback(
    payload: FeedbackCreate,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    result = FeedbackService(session).submit(payload, access)
    return {"feedback_id": result.id, "status": result.status}


@router.post("/evaluation/cases", status_code=201)
def create_evaluation_case(
    payload: EvaluationCaseCreate,
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    result = EvaluationService(session, qa_service(request, session)).create_case(payload, access)
    return {"case_id": result.id}


@router.post("/evaluation/runs", response_model=EvaluationRunResponse)
def run_evaluation(
    request: Request,
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return EvaluationService(session, qa_service(request, session)).run(access)


@router.get("/ops/health", response_model=HealthReport)
def ops_health(
    session: Session = Depends(get_session),
    access: AccessContext = Depends(get_access_context),
):
    return MonitoringService(session).health(access)
