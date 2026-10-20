from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tunnel_qa.core.enums import SecurityLevel
from tunnel_qa.core.errors import NotFoundError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import (
    Chunk,
    DataSource,
    ImportJob,
    MonitoringRecord,
    SourceDocument,
    SYSTEM_MANAGED_SOURCE_IDS,
    SYSTEM_MANAGED_SOURCE_NAMES,
)
from tunnel_qa.knowledge.models import KnowledgeEntity, KnowledgeRelation, KnowledgeRelease
from tunnel_qa.qa.models import Conversation, Message, QATrace
from tunnel_qa.quality.models import DecisionRecord, EvaluationCase, EvaluationRun
from tunnel_qa.workbench.schemas import (
    EvaluationSnapshot,
    RecentSource,
    RecentTrace,
    TraceDetail,
    TraceSource,
    WorkbenchCounts,
    WorkbenchOverview,
)


class WorkbenchService:
    """Build a security-filtered, read-only snapshot for the web workbench."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """SQLite returns timezone-aware columns as naive values; restore UTC explicitly."""
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def overview(self, access: AccessContext) -> WorkbenchOverview:
        access.require("knowledge:read")
        documents = [
            item
            for item in self.session.scalars(
                select(SourceDocument)
                .where(SourceDocument.source_id.not_in(SYSTEM_MANAGED_SOURCE_IDS))
                .order_by(SourceDocument.created_at.desc())
            ).all()
            if access.can_access(item.security_level)
        ]
        document_ids = [item.id for item in documents]
        visible_entities = [
            item
            for item in self.session.scalars(
                select(KnowledgeEntity).where(KnowledgeEntity.status == "published")
            ).all()
            if access.can_access(item.security_level)
        ]
        visible_relations = [
            item
            for item in self.session.scalars(
                select(KnowledgeRelation).where(KnowledgeRelation.status == "published")
            ).all()
            if access.can_access(item.security_level)
        ]
        chunks = (
            self.session.scalar(
                select(func.count()).select_from(Chunk)
                .join(SourceDocument, Chunk.document_id == SourceDocument.id)
                .where(Chunk.document_id.in_(document_ids),
                       Chunk.processing_version == SourceDocument.processing_version)
            )
            if document_ids
            else 0
        ) or 0
        latest_run = self.session.scalar(
            select(EvaluationRun).order_by(EvaluationRun.created_at.desc()).limit(1)
        )
        visible_conversation_ids = [
            item.id
            for item in self.session.scalars(
                select(Conversation).where(Conversation.owner_user_id == access.user_id)
            ).all()
            if access.can_access(item.security_level)
        ]
        recent_traces = (
            self.session.scalars(
                select(QATrace)
                .where(
                    QATrace.user_id == access.user_id,
                    QATrace.conversation_id.in_(visible_conversation_ids),
                )
                .order_by(QATrace.created_at.desc())
                .limit(6)
            ).all()
            if visible_conversation_ids
            else []
        )
        counts = WorkbenchCounts(
            data_sources=self.session.scalar(
                select(func.count())
                .select_from(DataSource)
                .where(DataSource.name.not_in(SYSTEM_MANAGED_SOURCE_NAMES))
            )
            or 0,
            documents=len(documents),
            published_documents=sum(item.status == "published" for item in documents),
            chunks=chunks,
            monitoring_records=self.session.scalar(
                select(func.count()).select_from(MonitoringRecord)
            )
            or 0,
            published_entities=len(visible_entities),
            published_relations=len(visible_relations),
            knowledge_version=self.session.scalar(select(func.max(KnowledgeRelease.version))) or 0,
            evaluation_cases=self.session.scalar(
                select(func.count()).select_from(EvaluationCase).where(EvaluationCase.active.is_(True))
            )
            or 0,
            evaluation_runs=self.session.scalar(select(func.count()).select_from(EvaluationRun))
            or 0,
            quarantined_jobs=self.session.scalar(
                select(func.count()).select_from(ImportJob).where(ImportJob.status == "quarantined")
            )
            or 0,
            pending_risk_reviews=self.session.scalar(
                select(func.count()).select_from(DecisionRecord).where(
                    DecisionRecord.review_required.is_(True),
                    DecisionRecord.review_status == "pending",
                )
            )
            or 0,
        )
        alerts_present = counts.quarantined_jobs > 0 or counts.pending_risk_reviews > 0
        return WorkbenchOverview(
            status="attention" if alerts_present else "healthy",
            counts=counts,
            document_statuses=dict(Counter(str(item.status) for item in documents)),
            recent_sources=[
                RecentSource(
                    source_id=item.source_id,
                    title=item.title,
                    source_type=str(item.source_type),
                    version=item.version,
                    status=str(item.status),
                    content_location=item.content_location,
                )
                for item in documents[:6]
            ],
            latest_evaluation=(
                EvaluationSnapshot(
                    status=latest_run.status,
                    case_count=latest_run.case_count,
                    model_version=latest_run.model_version,
                    knowledge_version=latest_run.knowledge_version,
                    metrics=latest_run.metrics,
                    created_at=self._as_utc(latest_run.created_at),
                )
                if latest_run
                else EvaluationSnapshot()
            ),
            recent_traces=[
                RecentTrace(
                    trace_id=item.trace_id,
                    question=item.question,
                    model_provider=item.model_provider,
                    model_version=item.model_version,
                    knowledge_version=item.knowledge_version,
                    created_at=self._as_utc(item.created_at),
                )
                for item in recent_traces
            ],
        )

    def trace_detail(self, trace_id: str, access: AccessContext) -> TraceDetail:
        """Return the persisted answer and its currently authorized evidence blocks."""
        access.require("knowledge:read")
        trace = self.session.scalar(select(QATrace).where(QATrace.trace_id == trace_id))
        if trace is None or trace.user_id != access.user_id:
            raise NotFoundError("问答追踪", trace_id)
        message = self.session.scalar(
            select(Message)
            .where(Message.trace_id == trace_id, Message.role == "assistant")
            .order_by(Message.sequence.desc())
            .limit(1)
        )
        if message is None:
            raise NotFoundError("追踪回答", trace_id)
        access.require_access(SecurityLevel(message.conversation.security_level))

        chunk_ids = list(dict.fromkeys(trace.retrieved_chunk_ids or []))
        chunks = (
            self.session.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))).all()
            if chunk_ids
            else []
        )
        chunk_by_id = {
            item.id: item for item in chunks if access.can_access(item.security_level)
            and access.can_access(item.document.security_level)
        }
        citation_by_chunk = {
            item.get("chunk_id"): item
            for item in (message.citations or [])
            if item.get("chunk_id") in chunk_by_id
        }
        sources: list[TraceSource] = []
        for chunk_id in chunk_ids:
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            document = chunk.document
            citation = citation_by_chunk.get(chunk_id, {})
            sources.append(
                TraceSource(
                    chunk_id=chunk.id,
                    chunk_ordinal=chunk.ordinal,
                    source_id=document.source_id,
                    title=document.title,
                    source_type=str(document.source_type),
                    version=document.version,
                    effective_date=document.effective_date,
                    content_location=citation.get("content_location")
                    or chunk.content_location,
                    excerpt=citation.get("excerpt") or chunk.text,
                    retrieval_score=float(citation.get("retrieval_score") or 0.0),
                )
            )
        return TraceDetail(
            trace_id=trace.trace_id,
            question=trace.question,
            rewritten_question=trace.rewritten_question,
            answer=message.content,
            answer_type=str(message.answer_type) if message.answer_type else None,
            sources=sources,
            graph_path=message.graph_path or [],
            warnings=message.warnings or [],
            retrieval_parameters=trace.retrieval_parameters or {},
            model_provider=trace.model_provider,
            model_version=trace.model_version,
            prompt_version=trace.prompt_version,
            knowledge_version=trace.knowledge_version,
            refusal_reason=trace.refusal_reason,
            created_at=self._as_utc(trace.created_at),
        )
