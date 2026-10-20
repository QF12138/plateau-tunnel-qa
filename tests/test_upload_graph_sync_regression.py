"""Upload/graph coupling regressions; all data lives in pytest temporary stores."""

import pytest
from sqlalchemy import func, select

from test_api import build_client, headers
from test_knowledge import access, prepare_document
from tunnel_qa.core.enums import ReviewStatus
from tunnel_qa.core.errors import AppError
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.knowledge.models import KnowledgeEntity, KnowledgeEvidenceLink, KnowledgeRelation, KnowledgeReview, OntologyType
from tunnel_qa.knowledge.schemas import ReviewRequest
from tunnel_qa.knowledge.service import KnowledgeService


def upload(client, *, role="system_admin", content="高地温影响作业人员。" * 12):
    return client.post(
        "/api/v1/data/documents/upload",
        headers=headers(role=role),
        data={
            "title": "上传图谱同步回归",
            "source_type": "research",
            "version": "V1",
            "effective_date": "2026-10-18",
            "security_level": "internal",
            "owner_org": "回归测试",
            "chunk_size": "180",
            "chunk_overlap": "20",
            "chunk_model": "key-v2",
        },
        files={"file": ("graph-sync.txt", content.encode(), "text/plain")},
    )


def counts(database):
    with database.session_factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (SourceDocument, Chunk, KnowledgeEntity, KnowledgeRelation, KnowledgeEvidenceLink)
        )


def test_completed_upload_replay_preserves_graph_outcome_and_evidence(database, settings):
    client, _ = build_client(database, settings)
    first = upload(client)
    assert first.status_code == 202
    assert first.json()["graph"]["published"] is True
    before = counts(database)

    replay = upload(client)

    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["graph"]["entities"] == first.json()["graph"]["entities"]
    assert replay.json()["graph"]["relations"] == first.json()["graph"]["relations"]
    assert replay.json()["graph"]["published"] is True
    assert counts(database) == before


@pytest.mark.parametrize("failed_method", ["extract_candidates", "auto_review_high_confidence", "publish"])
def test_graph_sync_failure_can_be_recovered_by_upload_replay(
    database, settings, monkeypatch, failed_method
):
    client, _ = build_client(database, settings)

    def temporary_failure(*args, **kwargs):
        raise AppError("TEST_GRAPH_TEMPORARY_FAILURE", "测试：图谱暂时不可用")

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeService, failed_method, temporary_failure)
        upload(client)
    before = counts(database)
    assert before[0] == 1
    assert before[1] >= 1

    recovered = upload(client)

    assert recovered.status_code == 202
    assert recovered.json()["accepted"] is True
    assert recovered.json()["idempotent_replay"] is True
    graph = client.get("/api/v1/graph/workspace", headers=headers()).json()
    assert {"高地温", "作业人员"}.issubset({node["name"] for node in graph["nodes"]})
    assert any(edge["relation_type"] == "影响" for edge in graph["edges"])
    assert counts(database)[:2] == before[:2]
    after = counts(database)
    assert upload(client).status_code == 202
    assert counts(database) == after


def test_upload_does_not_publish_unrelated_approved_knowledge(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    extracted = service.extract_candidates(document_id, access())
    approved_entities, _ = service.auto_review_high_confidence(
        extracted, access(), comment="待人工确认发布"
    )
    assert approved_entities
    session.close()
    client, _ = build_client(database, settings)

    response = upload(client, content="凿岩台车适用于钻爆法。" * 12)

    assert response.status_code == 202
    assert response.json()["graph"]["published"] is True
    with database.session_factory() as session:
        assert all(
            session.get(KnowledgeEntity, entity_id).status == ReviewStatus.APPROVED
            for entity_id in approved_entities
        )
        document = session.get(SourceDocument, response.json()["document_id"])
        document.metadata_extra = {
            **document.metadata_extra,
            "graph_sync": {"status": "failed", "processing_version": document.processing_version,
                           "entity_ids": approved_entities, "relation_ids": []},
        }
        session.commit()
    # Legacy metadata cannot expand the authority of a document-scoped retry.
    retry = client.post(
        f"/api/v1/data/documents/{response.json()['document_id']}/sync-graph", headers=headers()
    )
    assert retry.status_code == 200
    with database.session_factory() as session:
        assert all(session.get(KnowledgeEntity, item).status == ReviewStatus.APPROVED for item in approved_entities)


def test_data_admin_upload_succeeds_without_bypassing_review_permissions(database, settings):
    client, _ = build_client(database, settings)

    response = upload(client, role="data_admin")

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["graph"]["published"] is False
    assert response.json()["graph"]["pending_entities"] >= 2
    with database.session_factory() as session:
        assert all(
            entity.status == ReviewStatus.CANDIDATE
            for entity in session.scalars(select(KnowledgeEntity)).all()
        )


def test_new_upload_does_not_resurrect_rejected_entity(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    service.extract_candidates(document_id, access())
    rejected = session.scalar(select(KnowledgeEntity).where(KnowledgeEntity.standard_name == "高地温"))
    service.review(
        ReviewRequest(target_type="entity", target_id=rejected.id, decision=ReviewStatus.REJECTED),
        access(),
    )
    rejected_id = rejected.id
    session.close()
    client, _ = build_client(database, settings)

    response = upload(client)

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    with database.session_factory() as session:
        matches = session.scalars(select(KnowledgeEntity).where(KnowledgeEntity.standard_name == "高地温")).all()
        assert len(matches) == 1
        assert matches[0].id == rejected_id
        assert matches[0].status == ReviewStatus.REJECTED


def test_normal_user_cannot_upload_or_mutate_graph(database, settings):
    client, _ = build_client(database, settings)
    before = counts(database)

    response = upload(client, role="field_engineer")

    assert response.status_code == 403
    assert counts(database) == before


def test_partial_review_commit_retries_only_remaining_work(database, settings, monkeypatch):
    client, _ = build_client(database, settings)
    original = KnowledgeService.review
    calls = 0

    def fail_after_first_review(self, payload, access_context):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("secret-provider-value-must-not-be-returned")
        return original(self, payload, access_context)

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeService, "review", fail_after_first_review)
        interrupted = upload(client)
    assert interrupted.status_code == 202
    assert interrupted.json()["accepted"] is True
    assert interrupted.json()["graph"]["status"] == "failed"
    assert interrupted.json()["graph"]["retryable"] is True
    assert "secret-provider" not in interrupted.text
    before = counts(database)

    recovered = client.post(
        f"/api/v1/data/documents/{interrupted.json()['document_id']}/sync-graph",
        headers=headers(),
    )

    assert recovered.status_code == 200
    assert recovered.json()["status"] == "completed"
    assert recovered.json()["pending_entities"] == 0
    assert recovered.json()["published"] is True
    assert counts(database) == before


def test_graph_retry_enforces_role_and_document_clearance(database, settings):
    client, _ = build_client(database, settings)
    imported = upload(client).json()
    route = f"/api/v1/data/documents/{imported['document_id']}/sync-graph"
    before = counts(database)
    assert client.post(route, headers=headers(role="field_engineer")).status_code == 403
    assert client.post(route, headers=headers(level="public")).status_code == 403
    assert counts(database) == before


def test_shared_facts_add_new_document_evidence_without_duplicate_nodes(database, settings):
    client, _ = build_client(database, settings)
    first = upload(client).json()
    before = counts(database)

    second = upload(client, content="施工前，高地温影响作业人员。" * 12)

    assert second.status_code == 202
    body = second.json()
    assert body["document_id"] != first["document_id"]
    assert body["graph"]["status"] == "completed"
    assert body["graph"]["published"] is True
    assert body["graph"]["matched_entities"] == 2
    assert body["graph"]["matched_relations"] == 1
    assert body["graph"]["evidence_links"] > 0
    after = counts(database)
    assert after[2:4] == before[2:4]
    assert after[4] > before[4]
    graph = client.get("/api/v1/graph/workspace", headers=headers()).json()
    heat = next(node for node in graph["nodes"] if node["name"] == "高地温")
    evidence = client.get(f"/api/v1/knowledge/evidence/entity/{heat['id']}", headers=headers()).json()
    assert {first["source_id"], body["source_id"]}.issubset({item["source_id"] for item in evidence})


def test_upload_preserves_existing_ontology_customization(database, settings):
    client, _ = build_client(database, settings)
    with database.session_factory() as session:
        KnowledgeService(session).bootstrap_ontology(access())
        ontology = session.scalar(select(OntologyType).where(OntologyType.domain == "environment"))
        ontology.allowed_relations = ["人工配置关系"]
        ontology.properties_schema = {"人工配置属性": "string"}
        ontology.aliases = ["人工别名"]
        ontology_id = ontology.id
        session.commit()

    assert upload(client).status_code == 202

    with database.session_factory() as session:
        ontology = session.get(OntologyType, ontology_id)
        assert ontology.allowed_relations == ["人工配置关系"]
        assert ontology.properties_schema == {"人工配置属性": "string"}
        assert ontology.aliases == ["人工别名"]


def test_scoped_publish_requires_unpublished_endpoints_in_scope(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    extraction = service.extract_candidates(document_id, access())
    _, relation_ids = service.auto_review_high_confidence(extraction, access(), comment="先审核")
    assert relation_ids
    with pytest.raises(AppError, match="不在本次发布范围"):
        service.publish(access(), relation_ids=[relation_ids[0]])
    assert session.get(KnowledgeRelation, relation_ids[0]).status == ReviewStatus.APPROVED
    assert all(entity.status == ReviewStatus.APPROVED for entity in session.scalars(select(KnowledgeEntity)).all())
    session.close()


@pytest.mark.parametrize("target_type", ["entity", "relation"])
def test_new_upload_keeps_manually_deleted_facts_deleted(database, settings, target_type):
    client, _ = build_client(database, settings)
    assert upload(client).status_code == 202
    graph = client.get("/api/v1/graph/workspace", headers=headers()).json()
    target = graph["nodes"][0] if target_type == "entity" else graph["edges"][0]
    path = "entities" if target_type == "entity" else "relations"
    assert client.delete(f"/api/v1/graph/{path}/{target['id']}", headers=headers()).status_code == 200

    response = upload(client, content="施工期间，高地温影响作业人员。" * 12)

    assert response.status_code == 202
    assert response.json()["graph"]["status"] == "completed"
    updated = client.get("/api/v1/graph/workspace", headers=headers()).json()
    if target_type == "entity":
        assert target["name"] not in {item["name"] for item in updated["nodes"]}
    else:
        assert not any(
            (edge["source"], edge["relation_type"], edge["target"])
            == (target["source"], target["relation_type"], target["target"])
            for edge in updated["edges"]
        )


@pytest.mark.parametrize("retired_scope", ["entities_and_relations", "relations_only", "unpublished_candidates"])
def test_migration_retired_facts_can_be_proposed_again_with_new_evidence(
    database, settings, retired_scope
):
    if retired_scope == "unpublished_candidates":
        session, document_id = prepare_document(database, settings)
        service = KnowledgeService(session)
        service.bootstrap_ontology(access())
        service.extract_candidates(document_id, access())
        session.close()
        client, _ = build_client(database, settings)
    else:
        client, _ = build_client(database, settings)
        assert upload(client).status_code == 202
    retired = []
    with database.session_factory() as session:
        for target_type, model in (("entity", KnowledgeEntity), ("relation", KnowledgeRelation)):
            if target_type == "entity" and retired_scope == "relations_only":
                continue
            for fact in session.scalars(select(model)).all():
                fact.status = ReviewStatus.ARCHIVED
                retired.append((model, fact.id))
                session.add(KnowledgeReview(
                    target_type=target_type, target_id=fact.id, decision=ReviewStatus.ARCHIVED,
                    reviewer_id="rechunk-migration", comment="重切后无当前版本证据，自动退役；新证据可重新提出候选",
                ))
        session.commit()

    response = upload(client, content="新工点施工期间，高地温影响作业人员。" * 12)

    assert response.status_code == 202
    assert response.json()["graph"]["status"] == "completed"
    assert response.json()["graph"]["published"] is True
    graph = client.get("/api/v1/graph/workspace", headers=headers()).json()
    assert {"高地温", "作业人员"}.issubset({node["name"] for node in graph["nodes"]})
    assert any(edge["relation_type"] == "影响" for edge in graph["edges"])
    with database.session_factory() as session:
        assert all(session.get(model, identifier).status == ReviewStatus.ARCHIVED for model, identifier in retired)
