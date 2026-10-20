from tunnel_qa.core.enums import ReviewStatus, Role, SecurityLevel, SourceType
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import DataSource
from tunnel_qa.data.schemas import ChunkingConfig, DataSourceCreate, DocumentMetadata
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.knowledge.models import (
    Evidence,
    KnowledgeEntity,
    KnowledgeEvidenceLink,
    KnowledgeRelation,
    OntologyType,
)
from tunnel_qa.knowledge.schemas import BatchReviewRequest, ReviewRequest
from tunnel_qa.knowledge.service import KnowledgeService


def access(role=Role.SYSTEM_ADMIN, level=SecurityLevel.CONFIDENTIAL):
    return AccessContext(user_id=f"user-{role.value}", role=role, security_level=level)


def prepare_document(database, settings):
    from tunnel_qa.data import models as data_models  # noqa: F401
    from tunnel_qa.knowledge import models as knowledge_models  # noqa: F401

    database.create_schema()
    session = database.session_factory()
    ingestion = DataIngestionService(session, settings)
    source = ingestion.create_data_source(
        DataSourceCreate(
            name="规范库",
            source_type=SourceType.STANDARD,
            owner="数据管理员",
            security_level=SecurityLevel.INTERNAL,
        ),
        access(),
    )
    result = ingestion.import_document(
        data_source_id=source.id,
        filename="guide.md",
        content=(
            "# 高地温处置\n高地温影响作业人员，应采用通风降温措施。"
            "传感器监测掌子面温度，现场负责人应复核异常数据。"
        ).encode(),
        metadata=DocumentMetadata(
            source_id="SRC-KG-001",
            title="高地温处置指南",
            source_type=SourceType.STANDARD,
            version="V1",
            effective_date="2026-01-01",
            security_level=SecurityLevel.INTERNAL,
            owner_org="测试单位",
            content_location="第3章",
        ),
        idempotency_key="kg-import",
        access=access(),
    )
    assert result.accepted
    return session, result.document_id


def test_extraction_creates_evidence_bound_candidates(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())

    result = service.extract_candidates(document_id, access())

    assert len(result.entity_ids) >= 5
    assert len(result.relation_ids) >= 2
    entities = session.query(KnowledgeEntity).all()
    assert all(entity.status == ReviewStatus.CANDIDATE for entity in entities)
    assert all(session.get(Evidence, entity.evidence_id) is not None for entity in entities)


def test_bootstrap_upgrades_to_complete_v2_ontology_without_deleting_v1(database, settings):
    session, _ = prepare_document(database, settings)
    session.add(
        OntologyType(
            name="人员与组织",
            domain="human",
            aliases=["人员"],
            properties_schema={},
            allowed_relations=[],
            version=1,
            active=True,
        )
    )
    session.commit()

    service = KnowledgeService(session)
    active = service.bootstrap_ontology(access())
    catalog = service.list_ontology_types(access())

    assert len(active) == 5
    assert {item.version for item in active} == {2}
    assert all(item.properties_schema for item in active)
    assert all(item.allowed_relations for item in active)
    assert any(item.version == 1 and not item.active for item in catalog)
    assert {item.domain for item in catalog if item.active} == {
        "human", "machine", "material", "method", "environment"
    }


def test_extraction_uses_v2_terms_sentence_relations_and_multi_source_evidence(
    database, settings
):
    session, first_document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    first = service.extract_candidates(first_document_id, access())
    assert first.entity_ids

    source = session.query(DataSource).one()
    ingestion = DataIngestionService(session, settings)
    second = ingestion.import_document(
        data_source_id=source.id,
        filename="construction.md",
        content=(
            "# 钻爆施工\n凿岩台车适用于钻爆法。"
            "掌子面温度达到28℃时，监测设备监测掌子面温度。"
            "高地温影响施工人员。隧道掘进机（TBM）应定期检查。"
            "提出围岩稳定性智能判识算法。"
        ).encode(),
        metadata=DocumentMetadata(
            source_id="SRC-KG-002",
            title="钻爆施工知识",
            source_type=SourceType.STANDARD,
            version="V1",
            effective_date="2026-02-01",
            security_level=SecurityLevel.INTERNAL,
            owner_org="测试单位",
            content_location="第4章",
        ),
        idempotency_key="kg-import-2",
        access=access(),
    )
    extracted = service.extract_candidates(second.document_id, access())

    names = {item.standard_name for item in session.query(KnowledgeEntity).all()}
    assert {"凿岩台车", "钻爆法", "施工人员", "隧道掘进机"}.issubset(names)
    pattern_entity = (
        session.query(KnowledgeEntity)
        .filter_by(standard_name="围岩稳定性智能判识算法")
        .one()
    )
    approved_entities, _ = service.auto_review_high_confidence(
        extracted, access(), comment="自动审核测试"
    )
    assert approved_entities
    assert pattern_entity.id not in approved_entities
    assert pattern_entity.status == ReviewStatus.CANDIDATE
    tbm = session.query(KnowledgeEntity).filter_by(standard_name="隧道掘进机").one()
    assert "TBM" in tbm.aliases
    applies = (
        session.query(KnowledgeRelation)
        .join(KnowledgeEntity, KnowledgeRelation.source_entity_id == KnowledgeEntity.id)
        .filter(KnowledgeEntity.standard_name == "凿岩台车")
        .filter(KnowledgeRelation.relation_type == "适用于")
        .one()
    )
    assert applies.target_entity.standard_name == "钻爆法"
    monitored = (
        session.query(KnowledgeRelation)
        .join(KnowledgeEntity, KnowledgeRelation.source_entity_id == KnowledgeEntity.id)
        .filter(KnowledgeEntity.standard_name == "监测设备")
        .filter(KnowledgeRelation.relation_type == "监测")
        .one()
    )
    assert monitored.applicable_conditions == {"measurements": ["28℃"]}

    high_temperature = (
        session.query(KnowledgeEntity).filter_by(standard_name="高地温").one()
    )
    links = (
        session.query(KnowledgeEvidenceLink)
        .filter_by(target_type="entity", target_id=high_temperature.id)
        .all()
    )
    assert {item.document_id for item in links} == {first_document_id, second.document_id}
    assert extracted.evidence_count >= 4

    deletion = ingestion.delete_document(first_document_id, access())
    session.expire_all()
    preserved = session.query(KnowledgeEntity).filter_by(standard_name="高地温").one()
    remaining_links = (
        session.query(KnowledgeEvidenceLink)
        .filter_by(target_type="entity", target_id=preserved.id)
        .all()
    )
    assert deletion["deleted_entities"] >= 1
    assert {item.document_id for item in remaining_links} == {second.document_id}
    assert preserved.evidence_id == remaining_links[0].evidence_id


def test_same_fact_keeps_one_node_with_per_chunk_evidence_and_safe_chunk_deletion(
    database, settings
):
    session, _ = prepare_document(database, settings)
    source = session.query(DataSource).one()
    ingestion = DataIngestionService(session, settings)
    imported = ingestion.import_document(
        data_source_id=source.id,
        filename="repeated.md",
        content=(
            "# 第一处记录\n高地温影响作业人员。\n"
            "# 第二处记录\n高地温影响作业人员。"
        ).encode(),
        metadata=DocumentMetadata(
            source_id="SRC-KG-REPEATED",
            title="重复事实证据",
            source_type=SourceType.STANDARD,
            version="V1",
            effective_date="2026-03-01",
            security_level=SecurityLevel.INTERNAL,
            owner_org="测试单位",
            content_location="全文",
        ),
        idempotency_key="kg-repeated-fact",
        access=access(),
        chunking=ChunkingConfig(chunk_size=100, chunk_overlap=0),
    )
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    service.extract_candidates(imported.document_id, access())

    entity = session.query(KnowledgeEntity).filter_by(standard_name="高地温").one()
    person = session.query(KnowledgeEntity).filter_by(standard_name="作业人员").one()
    relation = (
        session.query(KnowledgeRelation)
        .filter_by(
            source_entity_id=entity.id,
            target_entity_id=person.id,
            relation_type="影响",
        )
        .one()
    )
    entity_links = (
        session.query(KnowledgeEvidenceLink)
        .filter_by(target_type="entity", target_id=entity.id)
        .all()
    )
    relation_links = (
        session.query(KnowledgeEvidenceLink)
        .filter_by(target_type="relation", target_id=relation.id)
        .all()
    )
    assert len({link.evidence.chunk_id for link in entity_links}) == 2
    assert len({link.evidence.chunk_id for link in relation_links}) == 2

    listing = ingestion.list_document_chunks(imported.document_id, access())
    assert listing["total_chunks"] == 2
    assert all(row["entity_count"] >= 2 for row in listing["chunks"])
    first_chunk_id, second_chunk_id = [row["chunk_id"] for row in listing["chunks"]]

    first_delete = ingestion.delete_chunk(first_chunk_id, access())
    session.expire_all()
    assert first_delete["remaining_chunks"] == 1
    assert first_delete["preserved_entities"] >= 2
    assert first_delete["preserved_relations"] >= 1
    assert session.get(KnowledgeEntity, entity.id) is not None
    assert session.get(KnowledgeRelation, relation.id) is not None
    assert ingestion.content_store.get_chunk_texts([first_chunk_id]) == {}

    second_delete = ingestion.delete_chunk(second_chunk_id, access())
    session.expire_all()
    assert second_delete["remaining_chunks"] == 0
    assert second_delete["deleted_entities"] >= 2
    assert second_delete["deleted_relations"] >= 1
    assert session.get(KnowledgeEntity, entity.id) is None
    assert session.get(KnowledgeRelation, relation.id) is None


def test_review_publish_and_subgraph_only_expose_published_knowledge(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    extraction = service.extract_candidates(document_id, access())

    assert service.get_subgraph("高地温", 2, access()).nodes == []
    candidates = service.list_review_candidates(100, access(Role.REVIEWER))
    assert {item["target_id"] for item in candidates} == {
        *extraction.entity_ids,
        *extraction.relation_ids,
    }
    assert all(item["evidence_quote"] for item in candidates)
    assert all(item["source_id"] == "SRC-KG-001" for item in candidates)
    for entity_id in extraction.entity_ids:
        service.review(
            ReviewRequest(
                target_type="entity",
                target_id=entity_id,
                decision=ReviewStatus.APPROVED,
                comment="证据与类型正确",
            ),
            access(Role.REVIEWER),
        )
    for relation_id in extraction.relation_ids:
        service.review(
            ReviewRequest(
                target_type="relation",
                target_id=relation_id,
                decision=ReviewStatus.APPROVED,
            ),
            access(Role.REVIEWER),
        )

    release = service.publish(access(Role.REVIEWER), notes="首次发布")
    subgraph = service.get_subgraph("高地温", 2, access(Role.FIELD_ENGINEER))

    assert release.version == 1
    assert release.entity_count == len(extraction.entity_ids)
    assert any(node.name == "高地温" for node in subgraph.nodes)
    assert len(subgraph.edges) >= 1
    statistics = service.graph_statistics(access())
    assert statistics["published_nodes"] >= 5
    assert statistics["active_ontology_version"] == 2
    assert statistics["pending_entities"] == 0
    assert statistics["target_nodes"] == 500
    assert statistics["nodes_by_domain"]["environment"] >= 1
    evidence = service.list_fact_evidence("entity", extraction.entity_ids[0], access())
    assert evidence[0]["source_id"] == "SRC-KG-001"
    assert service.list_releases(10, access())[0]["version"] == release.version


def test_publish_rejects_unreviewed_candidates(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    service.extract_candidates(document_id, access())

    try:
        service.publish(access())
    except AppError as exc:
        assert exc.error_code == "NOTHING_TO_PUBLISH"
    else:
        raise AssertionError("未审核知识不得发布")


def test_batch_approving_relation_automatically_includes_candidate_endpoints(
    database, settings
):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    extraction = service.extract_candidates(document_id, access())
    relation_id = extraction.relation_ids[0]

    reviewed = service.review_batch(
        BatchReviewRequest(
            items=[
                ReviewRequest(
                    target_type="relation",
                    target_id=relation_id,
                    decision=ReviewStatus.APPROVED,
                    comment="批量加入关系",
                )
            ]
        ),
        access(Role.REVIEWER),
    )

    assert reviewed["processed"] == 3
    assert reviewed["approved"] == 3
    assert reviewed["auto_approved_endpoints"] == 2
    release = service.publish(access(Role.REVIEWER))
    assert release.entity_count == 2
    assert release.relation_count == 1


def test_legacy_low_confidence_relation_without_v2_evidence_returns_to_review(
    database, settings
):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    extraction = service.extract_candidates(document_id, access())
    for entity_id in extraction.entity_ids:
        service.review(
            ReviewRequest(
                target_type="entity",
                target_id=entity_id,
                decision=ReviewStatus.APPROVED,
            ),
            access(Role.REVIEWER),
        )
    service.publish(access(Role.REVIEWER))
    entities = session.query(KnowledgeEntity).limit(2).all()
    evidence = Evidence(
        chunk_id=entities[0].evidence.chunk_id,
        quote="旧版相邻词关系证据",
        source_location="旧版抽取",
        review_status=ReviewStatus.PUBLISHED,
    )
    session.add(evidence)
    session.flush()
    legacy = KnowledgeRelation(
        source_entity_id=entities[0].id,
        target_entity_id=entities[1].id,
        relation_type="旧版关联",
        confidence=0.7,
        status=ReviewStatus.PUBLISHED,
        knowledge_version=1,
        security_level=SecurityLevel.INTERNAL,
        evidence_id=evidence.id,
    )
    session.add(legacy)
    session.commit()

    result = service.requeue_unsupported_legacy_relations(access())

    assert result["requeued_relations"] == 1
    assert legacy.status == ReviewStatus.CANDIDATE
    assert evidence.review_status == ReviewStatus.CANDIDATE
    assert any(
        item["target_id"] == legacy.id
        for item in service.list_review_candidates(100, access(Role.REVIEWER))
    )


def test_security_level_filters_subgraph(database, settings):
    session, document_id = prepare_document(database, settings)
    service = KnowledgeService(session)
    service.bootstrap_ontology(access())
    extraction = service.extract_candidates(document_id, access())
    entity = session.get(KnowledgeEntity, extraction.entity_ids[0])
    entity.security_level = SecurityLevel.RESTRICTED
    entity.status = ReviewStatus.APPROVED
    session.commit()
    service.publish(access())

    public_user = access(Role.FIELD_ENGINEER, SecurityLevel.PUBLIC)
    assert service.get_subgraph(entity.standard_name, 0, public_user).nodes == []
