import pytest
from sqlalchemy import select

from test_knowledge import access, prepare_document
from tunnel_qa.core.enums import ProcessingStatus, ReviewStatus, SecurityLevel, SourceType
from tunnel_qa.core.errors import PermissionDeniedError
from tunnel_qa.data.models import Chunk, DataSource, SourceDocument
from tunnel_qa.data.schemas import DocumentMetadata
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.knowledge.models import KnowledgeEntity, KnowledgeRelation
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.retrieval.schemas import RetrievalFilters
from tunnel_qa.retrieval.service import HybridRetrievalService


def shared_fact(database, settings, second_level=SecurityLevel.INTERNAL):
    session, first_id = prepare_document(database, settings)
    ingestion = DataIngestionService(session, settings)
    knowledge = KnowledgeService(session, ingestion.content_store)
    knowledge.bootstrap_ontology(access())
    knowledge.extract_candidates(first_id, access())
    second = ingestion.import_document(
        data_source_id=session.scalar(select(DataSource.id)), filename="second.md",
        content="高地温影响作业人员。第二处独立试验的原始观测结果应当保留。".encode(),
        metadata=DocumentMetadata(source_id="SRC-SHARED", title="独立证据",
            source_type=SourceType.STANDARD, version="V2", effective_date="2026-10-17",
            security_level=second_level, owner_org="测试单位", content_location="正文"),
        idempotency_key="shared-evidence", access=access(),
    )
    assert second.accepted
    knowledge.extract_candidates(second.document_id, access())
    for model in (KnowledgeEntity, KnowledgeRelation):
        for row in session.scalars(select(model)).all():
            row.status = ReviewStatus.PUBLISHED
    for document in session.scalars(select(SourceDocument)).all():
        document.status = ProcessingStatus.PUBLISHED
    session.commit()
    entity = session.scalar(select(KnowledgeEntity).where(KnowledgeEntity.standard_name == "高地温"))
    relation = session.scalar(select(KnowledgeRelation).where(
        KnowledgeRelation.source_entity_id == entity.id, KnowledgeRelation.relation_type == "影响"))
    return session, ingestion, knowledge, first_id, second.document_id, entity, relation


@pytest.mark.parametrize("target_type", ["entity", "relation"])
@pytest.mark.parametrize("restricted_field", ["document", "chunk", "both"])
def test_shared_fact_checks_each_source_before_and_after_primary_deletion(
    database, settings, target_type, restricted_field,
):
    session, ingestion, knowledge, first_id, second_id, entity, relation = shared_fact(database, settings)
    document = session.get(SourceDocument, second_id)
    chunk = session.scalar(select(Chunk).where(Chunk.document_id == second_id))
    if restricted_field in {"document", "both"}:
        document.security_level = SecurityLevel.RESTRICTED
    if restricted_field in {"chunk", "both"}:
        chunk.security_level = SecurityLevel.RESTRICTED
    session.commit()
    target_id = entity.id if target_type == "entity" else relation.id
    visible = knowledge.list_fact_evidence(target_type, target_id, access(level=SecurityLevel.INTERNAL))
    assert {item["document_id"] for item in visible} == {first_id}
    assert {item["document_id"] for item in knowledge.list_fact_evidence(target_type, target_id, access())} == {first_id, second_id}
    ingestion.delete_document(first_id, access())
    session.expire_all()
    assert knowledge.list_fact_evidence(target_type, target_id, access(level=SecurityLevel.INTERNAL)) == []
    assert {item["document_id"] for item in knowledge.list_fact_evidence(target_type, target_id, access())} == {second_id}
    session.close()


def test_all_visible_sources_receive_graph_weight_with_document_filter(database, settings):
    session, _, _, first_id, second_id, _, _ = shared_fact(database, settings)
    retrieval = HybridRetrievalService(session)
    hits = retrieval.search("高地温", access()).hits
    assert {hit.document_id for hit in hits if hit.graph_score == 1} == {first_id, second_id}
    filtered = retrieval.search("高地温", access(), filters=RetrievalFilters(versions=["V2"]))
    assert {hit.document_id for hit in filtered.hits} == {second_id}
    assert filtered.hits[0].graph_score == 1
    session.close()


def test_graph_signals_hide_private_endpoint_and_unpublished_sources(database, settings):
    session, _, _, first_id, second_id, _, relation = shared_fact(database, settings)
    relation.target_entity.security_level = SecurityLevel.RESTRICTED
    session.get(SourceDocument, second_id).status = ProcessingStatus.ARCHIVED
    session.commit()
    scores, paths = HybridRetrievalService(session)._graph_signals("高地温", access(level=SecurityLevel.INTERNAL))
    first_chunks = {chunk.id for chunk in session.scalars(select(Chunk).where(Chunk.document_id == first_id))}
    assert set(scores) <= first_chunks
    assert set(scores.values()) == {0.8}
    assert all("作业人员" not in path for entries in paths.values() for path in entries)
    session.close()


def test_candidate_review_uses_visible_evidence_after_primary_promotion(database, settings):
    session, ingestion, knowledge, first_id, second_id, entity, relation = shared_fact(database, settings)
    session.get(SourceDocument, second_id).security_level = SecurityLevel.RESTRICTED
    entity.status = relation.status = ReviewStatus.CANDIDATE
    session.commit()
    internal = access(level=SecurityLevel.INTERNAL)
    targets = {entity.id, relation.id}
    visible = [row for row in knowledge.list_review_candidates(100, internal) if row["target_id"] in targets]
    assert len(visible) == 2
    assert all(row["source_count"] == 1 and row["source_id"] != "SRC-SHARED" for row in visible)
    ingestion.delete_document(first_id, access())
    session.expire_all()
    assert not any(row["target_id"] in targets for row in knowledge.list_review_candidates(100, internal))
    assert {row["target_id"] for row in knowledge.list_review_candidates(100, access())} >= targets
    session.close()


@pytest.mark.parametrize("level", ["restricted", "unknown", None, [], {}])
def test_stored_or_invalid_clearance_denies_without_server_error(level):
    identity = access(level=SecurityLevel.INTERNAL)
    assert not identity.can_access(level)
    with pytest.raises(PermissionDeniedError) as caught:
        identity.require_access(level)
    assert caught.value.status_code == 403
    assert identity.can_access("public")
