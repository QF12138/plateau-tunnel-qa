"""Rechunking keeps old evidence readable without retrieving obsolete blocks."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tunnel_qa.app import create_app
from tunnel_qa.core.enums import Role, SecurityLevel
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import Chunk, DataSource, SourceDocument
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.knowledge.models import Evidence, KnowledgeEntity, KnowledgeEvidenceLink, KnowledgeRelation
from tunnel_qa.knowledge.schemas import ManualRelationCreate
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.qa.models import Conversation, Message, QATrace
from tunnel_qa.retrieval.service import HybridRetrievalService
from tunnel_qa.workbench.service import WorkbenchService


ACCESS = AccessContext("version-test", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL)
HEADERS = {"X-User-Id": "version-test", "X-Role": "system_admin",
           "X-Security-Level": "confidential"}


def seed_versions(session, settings, *, key=0):
    source = DataSource(name=f"version-test-{key}", source_type="research", owner="测试",
                        security_level="internal")
    session.add(source)
    session.flush()
    document = SourceDocument(
        source_id=f"SRC-VERSION-TEST-{key}", data_source_id=source.id, title="版本验收",
        source_type="research", version="V1", effective_date="2026-10-17",
        security_level="internal", owner_org="测试", checksum=f"{key + 1:064x}",
        content_location="正文", original_filename="test.txt", mime_type="text/plain",
        storage_uri=str(settings.storage_path / f"version-test-{key}.txt"),
        parsed_text="原始解析文本保持不变", status="published", processing_version=2,
        metadata_extra={"chunking": {"chunk_model": "key-v2"}},
    )
    session.add(document)
    session.flush()
    chunks = []
    for version, text in [(1, "岩爆影响作业人员，这是旧版证据。"),
                          (2, "高地温影响作业人员，这是新版证据。")]:
        chunk = Chunk(document_id=document.id, processing_version=version, ordinal=1,
                      text=text, structure_path="正文", page_number=version,
                      content_location=f"第{version}页", security_level="internal",
                      char_count=len(text))
        session.add(chunk)
        chunks.append(chunk)
    session.commit()
    return document, *chunks


def seed_fact(session, chunks, *, primary=0, name="高地温"):
    service = KnowledgeService(session)
    ontology = service.bootstrap_ontology(ACCESS)[0]
    evidence = [Evidence(chunk_id=chunk.id, quote=chunk.text,
                         source_location=chunk.content_location, review_status="published")
                for chunk in chunks]
    session.add_all(evidence)
    session.flush()
    fact = KnowledgeEntity(ontology_type_id=ontology.id, standard_name=name,
                           status="published", security_level="internal",
                           evidence_id=evidence[primary].id)
    session.add(fact)
    session.flush()
    for chunk, item in zip(chunks, evidence):
        session.add(KnowledgeEvidenceLink(target_type="entity", target_id=fact.id,
                                         evidence_id=item.id, document_id=chunk.document_id,
                                         extraction_method="test", confidence=0.8))
    session.commit()
    return fact, evidence


def test_document_and_workbench_counts_only_include_current_version(database, settings):
    app = create_app(settings, database)
    with database.session_factory() as session:
        document, old, current = seed_versions(session, settings)
        document_id, current_id = document.id, current.id
        overview = WorkbenchService(session).overview(ACCESS)
        assert overview.counts.chunks == 1
    with TestClient(app) as client:
        result = client.get("/api/v1/data/documents", headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["total_chunks"] == 1
        assert result.json()["documents"][0]["chunk_count"] == 1
        chunks = client.get(f"/api/v1/data/documents/{document_id}/chunks", headers=HEADERS)
        assert chunks.status_code == 200
        assert chunks.json()["total_chunks"] == 1
        assert [item["chunk_id"] for item in chunks.json()["chunks"]] == [current_id]


def test_retrieval_and_graph_signals_ignore_old_blocks(database, settings):
    with database.session_factory() as session:
        _, old, current = seed_versions(session, settings)
        seed_fact(session, [old, current])
        retrieval = HybridRetrievalService(session)
        hits = retrieval.search("高地温作业人员", ACCESS).hits
        assert {hit.chunk_id for hit in hits} == {current.id}
        scores, paths = retrieval._graph_signals("高地温", ACCESS)
        assert set(scores) == {current.id}
        assert set(paths) == {current.id}


def test_extraction_uses_only_active_version(database, settings):
    with database.session_factory() as session:
        document, old, current = seed_versions(session, settings)
        service = KnowledgeService(session)
        service.bootstrap_ontology(ACCESS)
        result = service.extract_candidates(document.id, ACCESS)
        assert result.entity_ids
        assert set(session.scalars(select(Evidence.chunk_id)).all()) == {current.id}
        assert "岩爆" not in session.scalars(select(KnowledgeEntity.standard_name)).all()


def test_fact_drawer_excludes_old_evidence_but_keeps_current(database, settings):
    with database.session_factory() as session:
        _, old, current = seed_versions(session, settings)
        fact, _ = seed_fact(session, [old, current])
        evidence = KnowledgeService(session).list_fact_evidence("entity", fact.id, ACCESS)
        assert [item["chunk_id"] for item in evidence] == [current.id]
        assert evidence[0]["chunk_text"] == current.text


def test_trace_still_reads_historical_chunk_and_checks_document_clearance(database, settings):
    with database.session_factory() as session:
        document, old, _ = seed_versions(session, settings)
        conversation = Conversation(owner_user_id=ACCESS.user_id, title="历史问答",
                                    security_level="internal")
        session.add(conversation)
        session.flush()
        trace = QATrace(trace_id="TRACE-VERSION", user_id=ACCESS.user_id,
                        conversation_id=conversation.id, question="历史问题", rewritten_question="历史问题",
                        retrieval_parameters={}, retrieved_chunk_ids=[old.id], model_provider="test",
                        model_version="test", prompt_version="test")
        message = Message(conversation_id=conversation.id, sequence=1, role="assistant",
                          content="历史回答", trace_id=trace.trace_id,
                          citations=[{"chunk_id": old.id, "excerpt": old.text}])
        session.add_all([trace, message])
        session.commit()
        access = AccessContext(ACCESS.user_id, Role.SYSTEM_ADMIN, SecurityLevel.INTERNAL)
        service = WorkbenchService(session)
        detail = service.trace_detail(trace.trace_id, access)
        assert [item.chunk_id for item in detail.sources] == [old.id]
        assert detail.sources[0].excerpt == old.text
        document.security_level = "confidential"
        session.commit()
        assert service.trace_detail(trace.trace_id, access).sources == []


def test_historical_chunks_are_read_only_and_last_active_deletion_archives_document(database, settings):
    with database.session_factory() as session:
        document, old, current = seed_versions(session, settings)
        service = DataIngestionService(session, settings)
        with pytest.raises(AppError) as caught:
            service.delete_chunk(old.id, ACCESS)
        assert caught.value.error_code == "HISTORICAL_CHUNK_READ_ONLY"
        result = service.delete_chunk(current.id, ACCESS)
        assert result["remaining_chunks"] == 0
        assert document.status == "archived"
        assert session.get(Chunk, old.id) is not None


@pytest.mark.parametrize("primary", [0, 1])
def test_deleting_last_current_support_does_not_promote_historical_evidence(database, settings, primary):
    with database.session_factory() as session:
        _, old, current = seed_versions(session, settings)
        fact, _ = seed_fact(session, [old, current], primary=primary)
        fact_id = fact.id
        result = DataIngestionService(session, settings).delete_chunk(current.id, ACCESS)
        assert result["deleted_entities"] == 1
        assert session.get(KnowledgeEntity, fact_id) is None
        assert session.get(Chunk, old.id) is not None


def test_current_alternative_evidence_is_preserved(database, settings):
    with database.session_factory() as session:
        document, old, current = seed_versions(session, settings)
        other = Chunk(document_id=document.id, processing_version=2, ordinal=2,
                      text="另一条当前证据", structure_path="正文", content_location="第3页",
                      security_level="internal", char_count=8)
        session.add(other)
        session.commit()
        fact, evidence = seed_fact(session, [old, current, other])
        result = DataIngestionService(session, settings).delete_chunk(current.id, ACCESS)
        assert result["deleted_entities"] == 0
        assert result["remaining_chunks"] == 1
        assert fact.evidence_id == evidence[2].id


def test_document_deletion_still_removes_all_chunk_versions(database, settings):
    with database.session_factory() as session:
        document, old, current = seed_versions(session, settings)
        ids = [old.id, current.id]
        result = DataIngestionService(session, settings).delete_document(document.id, ACCESS)
        assert result["deleted_chunks"] == 2
        assert session.scalars(select(Chunk).where(Chunk.id.in_(ids))).all() == []


def seed_relation(session, source, target, chunks, evidence, *, primary=0):
    relation = KnowledgeRelation(
        source_entity_id=source.id, target_entity_id=target.id, relation_type="影响",
        status="published", security_level="internal", evidence_id=evidence[primary].id,
    )
    session.add(relation)
    session.flush()
    session.add_all([
        KnowledgeEvidenceLink(target_type="relation", target_id=relation.id,
                              evidence_id=item.id, document_id=chunk.document_id,
                              extraction_method="test", confidence=0.8)
        for chunk, item in zip(chunks, evidence)
    ])
    session.commit()
    return relation


@pytest.mark.parametrize("primary", [0, 1])
def test_document_deletion_does_not_promote_other_documents_historical_support(
    database, settings, primary,
):
    with database.session_factory() as session:
        removed, _, current = seed_versions(session, settings)
        retained, historical, other_current = seed_versions(session, settings, key=1)
        fact, evidence = seed_fact(session, [current, historical], primary=primary)
        left, _ = seed_fact(session, [other_current], name="隧道")
        right, _ = seed_fact(session, [other_current], name="作业人员")
        relation = seed_relation(session, left, right, [current, historical], evidence, primary=primary)
        removed_id, fact_id, relation_id = removed.id, fact.id, relation.id

        result = DataIngestionService(session, settings).delete_document(removed_id, ACCESS)
        assert result["deleted_chunks"] == 2
        assert result["deleted_entities"] == 1
        assert result["deleted_relations"] == 1
        assert session.get(KnowledgeEntity, fact_id) is None
        assert session.get(KnowledgeRelation, relation_id) is None
        assert session.get(KnowledgeEntity, left.id) is not None
        assert session.get(KnowledgeEntity, right.id) is not None
        assert session.get(SourceDocument, retained.id) is not None
        assert session.get(Chunk, historical.id) is not None
        assert session.get(Chunk, other_current.id) is not None
        assert not session.scalars(select(Chunk).where(Chunk.document_id == removed_id)).all()


def test_document_deletion_promotes_only_other_documents_current_support(database, settings):
    with database.session_factory() as session:
        removed, _, current = seed_versions(session, settings)
        _, historical, other_current = seed_versions(session, settings, key=1)
        chunks = [current, historical, other_current]
        fact, evidence = seed_fact(session, chunks)
        target, _ = seed_fact(session, [other_current], name="作业人员")
        relation = seed_relation(session, fact, target, chunks, evidence)

        result = DataIngestionService(session, settings).delete_document(removed.id, ACCESS)
        assert result["deleted_entities"] == result["deleted_relations"] == 0
        assert fact.evidence_id == relation.evidence_id == evidence[2].id
        assert session.get(Chunk, historical.id) is not None


@pytest.mark.parametrize("delete_kind", ["document", "chunk"])
def test_content_deletion_preserves_manual_relation_and_its_endpoints(database, settings, delete_kind):
    with database.session_factory() as session:
        document, _, current = seed_versions(session, settings)
        left, _ = seed_fact(session, [current], name="高地温")
        right, _ = seed_fact(session, [current], name="作业人员")
        edge = KnowledgeService(session).create_manual_relation(
            ManualRelationCreate(source_entity_id=left.id, target_entity_id=right.id,
                                 relation_type="人工确认影响", applicable_conditions={"温度": "28℃"}),
            ACCESS,
        )
        relation = session.get(KnowledgeRelation, edge.id)
        manual_evidence_id = relation.evidence_id
        service = DataIngestionService(session, settings)
        if delete_kind == "document":
            result = service.delete_document(document.id, ACCESS)
        else:
            result = service.delete_chunk(current.id, ACCESS)

        assert result["deleted_entities"] == result["deleted_relations"] == 0
        assert left.evidence_id == right.evidence_id == manual_evidence_id
        session.expire_all()
        assert session.get(KnowledgeRelation, relation.id).relation_type == "人工确认影响"
        assert session.get(KnowledgeRelation, relation.id).applicable_conditions == {"温度": "28℃"}
        assert session.get(KnowledgeEntity, left.id).status == "published"
        assert session.get(KnowledgeEntity, right.id).status == "published"
