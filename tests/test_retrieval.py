from tunnel_qa.core.enums import ReviewStatus, Role, SecurityLevel, SourceType
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import SourceDocument
from tunnel_qa.data.schemas import DataSourceCreate, DocumentMetadata
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.knowledge.schemas import ReviewRequest
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.retrieval.schemas import RetrievalFilters
from tunnel_qa.retrieval.service import HybridRetrievalService


def ctx(level=SecurityLevel.CONFIDENTIAL):
    return AccessContext("test-user", Role.SYSTEM_ADMIN, level)


def setup_published_corpus(database, settings):
    from tunnel_qa.data import models as data_models  # noqa: F401
    from tunnel_qa.knowledge import models as knowledge_models  # noqa: F401

    database.create_schema()
    session = database.session_factory()
    ingestion = DataIngestionService(session, settings)
    knowledge = KnowledgeService(session)
    knowledge.bootstrap_ontology(ctx())
    contents = [
        (
            "SRC-HEAT",
            "高地温隧道通风处置规范",
            SecurityLevel.INTERNAL,
            "A隧道",
            "高地温影响作业人员。传感器监测掌子面温度，应采用通风降温措施并由现场负责人复核。",
        ),
        (
            "SRC-WATER",
            "地下水施工案例",
            SecurityLevel.INTERNAL,
            "B隧道",
            "地下水突涌风险需要超前地质预报，并准备排水设备和应急预案。",
        ),
        (
            "SRC-SECRET",
            "受限岩爆处置资料",
            SecurityLevel.RESTRICTED,
            "A隧道",
            "岩爆风险应通过应力监测设备持续监测，此资料只允许受限用户查看。",
        ),
    ]
    document_ids = []
    for index, (source_id, title, level, site, content) in enumerate(contents):
        source = ingestion.create_data_source(
            DataSourceCreate(
                name=f"source-{index}",
                source_type=SourceType.STANDARD,
                owner="测试",
                security_level=level,
            ),
            ctx(),
        )
        result = ingestion.import_document(
            data_source_id=source.id,
            filename=f"document-{index}.txt",
            content=content.encode(),
            metadata=DocumentMetadata(
                source_id=source_id,
                title=title,
                source_type=SourceType.STANDARD,
                project_site=site,
                version="V1",
                effective_date="2026-01-01",
                security_level=level,
                owner_org="测试单位",
                content_location="正文",
            ),
            idempotency_key=f"retrieval-{index}",
            access=ctx(),
        )
        assert result.accepted
        extraction = knowledge.extract_candidates(result.document_id, ctx())
        for entity_id in extraction.entity_ids:
            knowledge.review(
                ReviewRequest(
                    target_type="entity",
                    target_id=entity_id,
                    decision=ReviewStatus.APPROVED,
                ),
                ctx(),
            )
        for relation_id in extraction.relation_ids:
            knowledge.review(
                ReviewRequest(
                    target_type="relation",
                    target_id=relation_id,
                    decision=ReviewStatus.APPROVED,
                ),
                ctx(),
            )
        if extraction.entity_ids:
            knowledge.publish(ctx())
        else:
            document = session.get(SourceDocument, result.document_id)
            document.status = "published"
            session.commit()
        document_ids.append(result.document_id)
    return session, document_ids


def test_hybrid_retrieval_ranks_relevant_graph_evidence_first(database, settings):
    session, _ = setup_published_corpus(database, settings)
    retrieval = HybridRetrievalService(session)

    result = retrieval.search("高地温时如何通风降温？", ctx(SecurityLevel.INTERNAL))

    assert result.hits
    assert result.hits[0].source_id == "SRC-HEAT"
    assert result.hits[0].graph_score > 0
    assert result.hits[0].graph_path


def test_security_filter_runs_before_results_are_returned(database, settings):
    session, _ = setup_published_corpus(database, settings)
    retrieval = HybridRetrievalService(session)

    internal_result = retrieval.search("岩爆应力监测", ctx(SecurityLevel.INTERNAL))
    restricted_result = retrieval.search("岩爆应力监测", ctx(SecurityLevel.RESTRICTED))

    assert all(hit.source_id != "SRC-SECRET" for hit in internal_result.hits)
    assert any(hit.source_id == "SRC-SECRET" for hit in restricted_result.hits)


def test_context_filters_limit_project_and_source_version(database, settings):
    session, _ = setup_published_corpus(database, settings)
    retrieval = HybridRetrievalService(session)

    result = retrieval.search(
        "隧道风险处置",
        ctx(),
        filters=RetrievalFilters(project_site="B隧道", versions=["V1"]),
    )

    assert result.hits
    assert {hit.source_id for hit in result.hits} == {"SRC-WATER"}
