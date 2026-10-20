"""Resumable, document-scoped synchronization after immutable document import."""

import logging
from threading import RLock

from sqlalchemy import select
from sqlalchemy.orm import Session

from tunnel_qa.core.enums import ProcessingStatus, ReviewStatus
from tunnel_qa.core.errors import AppError, NotFoundError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.models import Chunk, SourceDocument, SYSTEM_MANAGED_SOURCE_IDS
from tunnel_qa.data.processing_metadata import metadata_object
from tunnel_qa.db.base import utc_now
from tunnel_qa.knowledge.models import Evidence, KnowledgeEntity, KnowledgeEvidenceLink, KnowledgeRelation, KnowledgeReview, OntologyType
from tunnel_qa.knowledge.schemas import ExtractionResult
from tunnel_qa.knowledge.service import KnowledgeService


logger = logging.getLogger(__name__)
# The deployed service is a single process. Serialize its sync requests so upload
# retries cannot duplicate work while another request is between durable stages.
_SYNC_LOCK = RLock()


class DocumentGraphSynchronizer:
    def __init__(self, session: Session, content_store: DocumentContentStore | None = None):
        self.session = session
        self.knowledge = KnowledgeService(session, content_store)

    def _save(self, document: SourceDocument, state: dict) -> None:
        state["updated_at"] = utc_now().isoformat()
        document.metadata_extra = {**metadata_object(document.metadata_extra), "graph_sync": dict(state)}
        self.session.commit()

    def _owned_ids(self, document: SourceDocument, model) -> list[str]:
        # Primary evidence identifies facts first extracted for this document.
        # Secondary evidence must not authorize publishing somebody else's facts.
        return list(self.session.scalars(
            select(model.id)
            .join(Evidence, model.evidence_id == Evidence.id)
            .join(Chunk, Evidence.chunk_id == Chunk.id)
            .where(
                Chunk.document_id == document.id,
                Chunk.processing_version == document.processing_version,
                model.status.in_([ReviewStatus.CANDIDATE, ReviewStatus.APPROVED, ReviewStatus.PUBLISHED]),
            )
        ).all())

    def _exclusions(self) -> tuple[set[tuple[str, str]], set[tuple[str, str, str]]]:
        latest_archive: dict[tuple[str, str], str] = {}
        for review in self.session.scalars(select(KnowledgeReview).where(
            KnowledgeReview.decision == ReviewStatus.ARCHIVED
        ).order_by(KnowledgeReview.created_at.desc(), KnowledgeReview.id.desc())).all():
            latest_archive.setdefault((review.target_type, review.target_id), review.reviewer_id)

        def blocked_by_user(fact, target_type: str) -> bool:
            # Only the explicit migration marker permits new evidence to open a
            # fresh candidate. Unknown legacy archives are preserved conservatively.
            return fact.status == ReviewStatus.REJECTED or latest_archive.get(
                (target_type, fact.id)
            ) != "rechunk-migration"

        active_ontology = self.session.scalars(select(OntologyType).where(OntologyType.active.is_(True))).all()
        blocked = self.session.scalars(select(KnowledgeEntity).where(
            KnowledgeEntity.status.in_([ReviewStatus.ARCHIVED, ReviewStatus.REJECTED])
        )).all()
        entities = {
            (fact.standard_name, ontology.id)
            for fact in blocked for ontology in active_ontology
            if blocked_by_user(fact, "entity") and fact.ontology_type.domain == ontology.domain
        }
        relations = {
            (fact.source_entity.standard_name, fact.relation_type, fact.target_entity.standard_name)
            for fact in self.session.scalars(select(KnowledgeRelation).where(
                KnowledgeRelation.status.in_([ReviewStatus.ARCHIVED, ReviewStatus.REJECTED])
            )).all()
            if blocked_by_user(fact, "relation")
        }
        return entities, relations

    def synchronize(self, document_id: str, access: AccessContext) -> dict:
        access.require("data:import")
        access.require("knowledge:propose")
        with _SYNC_LOCK:
            # Another request may have completed while this one waited for lock.
            self.session.expire_all()
            document = self.session.get(SourceDocument, document_id)
            if document is None:
                raise NotFoundError("源文档", document_id)
            access.require_access(document.security_level)
            if document.source_id in SYSTEM_MANAGED_SOURCE_IDS or document.status == ProcessingStatus.ARCHIVED:
                raise AppError("GRAPH_SYNC_DOCUMENT_UNAVAILABLE", "该资料当前不允许同步图谱")
            if self.session.scalar(select(Chunk.id).where(
                Chunk.document_id == document_id,
                Chunk.processing_version == document.processing_version,
            ).limit(1)) is None:
                raise AppError("GRAPH_SYNC_NO_CHUNKS", "没有可用于图谱同步的当前版本知识块")
            old_state = metadata_object(metadata_object(document.metadata_extra).get("graph_sync"))
            state = old_state if old_state.get("processing_version") == document.processing_version else {}
            if state.get("status") == "completed" and isinstance(state.get("result"), dict):
                return dict(state["result"])
            state = {**state, "processing_version": document.processing_version, "status": "running"}
            state.pop("error_code", None)
            self._save(document, state)
            try:
                self.knowledge.bootstrap_ontology(access, preserve_existing=True)
                excluded_entities, excluded_relations = self._exclusions()
                extraction = self.knowledge.extract_candidates(
                    document_id, access, excluded_entities=excluded_entities,
                    excluded_relations=excluded_relations,
                )
                # Extraction commits before returning. If the previous attempt
                # died before saving IDs, primary evidence reconstructs its scope.
                # Never use metadata IDs as authority: legacy import metadata is
                # user supplied, and primary evidence may have been edited since
                # the last attempt. Recompute ownership from current rows.
                entity_ids = sorted(set(extraction.entity_ids) | set(self._owned_ids(document, KnowledgeEntity)))
                relation_ids = sorted(set(extraction.relation_ids) | set(self._owned_ids(document, KnowledgeRelation)))
                state.update(entity_ids=entity_ids, relation_ids=relation_ids, stage="extracted")
                self._save(document, state)
                if access.can("knowledge:review"):
                    self.knowledge.auto_review_high_confidence(
                        ExtractionResult(document_id=document_id, entity_ids=entity_ids,
                                         relation_ids=relation_ids, evidence_count=extraction.evidence_count),
                        access, comment="工作台文档入库高置信知识自动审核",
                        endpoint_entity_ids=set(entity_ids),
                    )
                entities = self.session.scalars(select(KnowledgeEntity).where(KnowledgeEntity.id.in_(entity_ids))).all()
                relations = self.session.scalars(select(KnowledgeRelation).where(KnowledgeRelation.id.in_(relation_ids))).all()
                approved_entities = [item.id for item in entities if item.status == ReviewStatus.APPROVED]
                approved_entity_set = set(approved_entities)
                approved_relations = [item.id for item in relations if item.status == ReviewStatus.APPROVED and all(
                    endpoint.status == ReviewStatus.PUBLISHED or endpoint.id in approved_entity_set
                    for endpoint in (item.source_entity, item.target_entity)
                )]
                if access.can("knowledge:publish") and (approved_entities or approved_relations):
                    self.knowledge.publish(
                        access, notes=f"文档入库自动发布：{document.title}",
                        entity_ids=approved_entities, relation_ids=approved_relations,
                    )
                links = self.session.scalars(
                    select(KnowledgeEvidenceLink)
                    .join(Evidence, KnowledgeEvidenceLink.evidence_id == Evidence.id)
                    .join(Chunk, Evidence.chunk_id == Chunk.id)
                    .where(Chunk.document_id == document_id,
                           Chunk.processing_version == document.processing_version)
                ).all()
                linked_entities = {item.target_id for item in links if item.target_type == "entity"}
                linked_relations = {item.target_id for item in links if item.target_type == "relation"}
                has_published_evidence = any(item.evidence.review_status == ReviewStatus.PUBLISHED for item in links)
                result = {
                    "status": "completed", "retryable": False,
                    "processing_version": document.processing_version,
                    "entities": len(entity_ids), "relations": len(relation_ids),
                    "published": has_published_evidence,
                    "matched_entities": len(linked_entities), "matched_relations": len(linked_relations),
                    "evidence_links": len(links),
                    "pending_entities": sum(item.status in (ReviewStatus.CANDIDATE, ReviewStatus.APPROVED) for item in entities),
                    "pending_relations": sum(item.status in (ReviewStatus.CANDIDATE, ReviewStatus.APPROVED) for item in relations),
                }
                result["message"] = "知识块已同步图谱；待审核知识保留在审核队列。"
                state.update(status="completed", stage="completed", result=result)
                self._save(document, state)
                return result
            except Exception as exc:
                self.session.rollback()
                # Only a stable public error is returned; parser/provider/database
                # exception strings can contain connection details or document text.
                logger.warning("Document graph synchronization failed for %s (%s)", document_id, type(exc).__name__)
                document = self.session.get(SourceDocument, document_id)
                result = {
                    "status": "failed", "retryable": True, "error_code": "GRAPH_SYNC_FAILED",
                    "message": "文档及知识块已保存，图谱同步暂未完成；可重试图谱同步，无需重复切分。",
                    "processing_version": state["processing_version"],
                    "entities": len(state.get("entity_ids", [])), "relations": len(state.get("relation_ids", [])),
                    "published": False, "pending_entities": 0, "pending_relations": 0,
                }
                state.update(status="failed", result=result, error_code="GRAPH_SYNC_FAILED")
                if document is not None:
                    self._save(document, state)
                return result
