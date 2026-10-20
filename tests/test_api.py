from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tunnel_qa.app import create_app
from tunnel_qa.core.enums import ProcessingStatus
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.qa.model_gateway import ModelOutput
from tunnel_qa.qa.models import QATrace
from tunnel_qa.retrieval.service import HybridRetrievalService


class APIFakeGateway:
    provider = "api-test"
    model = "api-test-v1"

    def generate(self, *, question, evidence, scenario, response_mode="detailed"):
        return ModelOutput(answer="应持续监测温度并检查通风降温措施。", follow_up=[])


def headers(role="system_admin", level="confidential", user="api-user"):
    return {"X-User-Id": user, "X-Role": role, "X-Security-Level": level}


def build_client(database, settings):
    app = create_app(settings, database, APIFakeGateway())
    return TestClient(app), app


def test_workbench_liveness_and_trace_header(database, settings):
    client, _ = build_client(database, settings)

    page = client.get("/")
    health = client.get("/healthz", headers={"X-Trace-Id": "TRC-api-test"})

    assert page.status_code == 200
    assert "知识问答" in page.text
    assert "资料治理" in page.text
    assert "运行审计" in page.text
    assert "查询已发布知识" in page.text
    assert '<fieldset class="knowledge-scope" id="block-scope"' in page.text
    assert 'id="block-scope-all" type="checkbox"' in page.text
    assert page.text.count('name="block-source-type" value=') == 5
    assert '<select id="block-scope"' not in page.text
    assert 'data-view="graph"' in page.text
    assert 'id="knowledge-map"' in page.text
    assert 'id="governance-time"' in page.text
    assert "入库日期" in page.text
    assert "graphMarkup" in page.text
    assert "selectGraphNode" in page.text
    assert "checkGraphUpdates" in page.text
    assert 'id="graph-add-entity"' in page.text
    assert 'id="graph-add-relation"' in page.text
    assert 'id="graph-entity-form"' in page.text
    assert 'id="graph-relation-form"' in page.text
    assert 'data-graph-action="layout"' in page.text
    assert 'data-graph-action="cluster"' in page.text
    assert "graphLayoutMode='cluster'" in page.text
    assert "graphDepth=2" in page.text
    assert "graphDisplayData" in page.text
    assert 'id="graph-hop-depth"' in page.text
    assert "prepareLayeredGraphPositions" in page.text
    assert "graphVisualEdges" in page.text
    assert "graphEdgeLabelWidth" in page.text
    assert "graph-edge-label-bg" in page.text
    assert "graphNodeLevels" in page.text
    assert "该实体对全部关系" in page.text
    assert "`${edge.relationTypes.size} 类关系`" not in page.text
    assert "bindGraphPhysicsInteractions" in page.text
    assert "MAGNETIC REPULSION" in page.text
    assert "prepareGraphPositions" in page.text
    assert "/api/v1/graph/workspace" in page.text
    assert "placeNewGraphNode" in page.text
    assert "graphEntityIndexMarkup" in page.text
    assert 'id="graph-index-search"' in page.text
    assert "filterGraphEntityIndex" in page.text
    assert "filterGraphRelationDirectory" in page.text
    assert "/api/v1/data/chunks/" in page.text
    assert 'class="graph-entity-index"' in page.text
    assert "syncGraphEntityIndex" in page.text
    assert "graphNodeBounds" in page.text
    assert "openGraphRelationBuilder" in page.text
    assert 'id="graph-delete-selected-entity"' in page.text
    assert 'id="graph-delete-selected-relation"' in page.text
    assert 'id="graph-quality"' in page.text
    assert 'id="graph-review-list"' in page.text
    assert 'id="graph-publish-approved"' in page.text
    assert 'id="graph-batch-approve"' in page.text
    assert "批量加入图谱" in page.text
    assert "graphEvidencePanelMarkup" in page.text
    assert "完整切块正文" in page.text
    assert 'id="graph-batch-reject"' in page.text
    assert "/api/v1/knowledge/candidates" in page.text
    assert "/api/v1/knowledge/review/batch" in page.text
    assert "loadGraphGovernance" in page.text
    assert 'class="audit-detail-toggle"' in page.text
    assert "/api/v1/workbench/traces/" in page.text
    assert "renderTraceDetail" in page.text
    assert "answerEvidenceMarkup" in page.text
    assert "回答如何连接到来源" in page.text
    assert "data-citation-index" in page.text
    assert 'data-view-panel="data"' in page.text
    assert 'data-view="ingest"' in page.text
    assert 'data-view-panel="ingest"' in page.text
    assert "<h2>文档入库</h2>" in page.text
    assert "添加新资料，自动完成" not in page.text
    assert 'id="ingest-form"' in page.text
    assert "/api/v1/data/documents/upload" in page.text
    assert 'data-view-panel="evaluation"' not in page.text
    assert 'data-view="evaluation"' not in page.text
    assert 'aria-label="运行审计"><span class="nav-index">05</span>' in page.text
    assert 'data-view-panel="audit"' in page.text
    assert 'data-view="settings"' in page.text
    assert 'class="settings-entry"' in page.text
    assert 'data-view-panel="settings"' in page.text
    assert 'id="api-key"' in page.text
    assert 'id="settings-form"' in page.text
    assert "/api/v1/settings" in page.text
    assert 'id="report-enabled"' in page.text
    assert 'id="report-format"' in page.text
    assert "/api/v1/qa/reports/" in page.text
    assert health.json()["status"] == "ok"
    assert health.headers["X-Trace-Id"] == "TRC-api-test"


def test_workbench_overview_filters_documents_by_security_level(database, settings):
    client, _ = build_client(database, settings)
    source_response = client.post(
        "/api/v1/data/sources",
        headers=headers(),
        json={
            "name": "工作台资料源",
            "source_type": "research",
            "owner": "资料管理员",
            "security_level": "confidential",
        },
    )
    source_id = source_response.json()["id"]

    import json

    for index, level in enumerate(("internal", "confidential"), start=1):
        metadata = {
            "source_id": f"SRC-OVERVIEW-{index}",
            "title": f"工作台资料-{level}",
            "source_type": "research",
            "version": "V1",
            "effective_date": "2026-01-01",
            "security_level": level,
            "owner_org": "测试单位",
            "content_location": "正文",
        }
        imported = client.post(
            "/api/v1/data/import",
            headers={**headers(), "Idempotency-Key": f"overview-{index}"},
            data={"data_source_id": source_id, "metadata_json": json.dumps(metadata)},
            files={
                "file": (
                    f"overview-{index}.txt",
                    f"第{index}份工作台测试资料，包含隧道施工与监测内容。".encode(),
                    "text/plain",
                )
            },
        )
        assert imported.status_code == 202

    overview = client.get(
        "/api/v1/workbench/overview",
        headers=headers("field_engineer", "internal", "web-demo"),
    )

    assert overview.status_code == 200
    body = overview.json()
    assert body["counts"]["data_sources"] == 1
    assert body["counts"]["documents"] == 1
    assert body["counts"]["chunks"] >= 1
    assert body["counts"]["knowledge_version"] == 0
    assert [item["source_id"] for item in body["recent_sources"]] == ["SRC-OVERVIEW-1"]

    documents = client.get(
        "/api/v1/data/documents",
        headers=headers("data_admin", "internal", "data-user"),
    )
    assert documents.status_code == 200
    listing = documents.json()
    assert listing["filtered_documents"] == 1
    assert listing["total_documents"] == 1
    assert listing["content_databases"]["original_documents"] == 1
    assert [item["source_id"] for item in listing["documents"]] == ["SRC-OVERVIEW-1"]


def test_missing_identity_uses_unified_authentication_error(database, settings):
    client, _ = build_client(database, settings)

    response = client.post("/api/v1/qa/ask", json={"question": "测试"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTH_REQUIRED"
    assert response.json()["trace_id"].startswith("TRC-")


def test_data_import_job_and_qa_end_to_end(database, settings):
    client, app = build_client(database, settings)
    source_response = client.post(
        "/api/v1/data/sources",
        headers=headers(),
        json={
            "name": "API规范库",
            "source_type": "standard",
            "owner": "API管理员",
            "security_level": "internal",
        },
    )
    assert source_response.status_code == 201
    source_id = source_response.json()["id"]
    metadata = {
        "source_id": "SRC-API-001",
        "title": "高地温施工技术规范",
        "source_type": "standard",
        "project_site": "API测试隧道",
        "version": "V1",
        "effective_date": "2026-01-01",
        "security_level": "internal",
        "owner_org": "测试单位",
        "content_location": "第2章",
    }
    import json

    imported = client.post(
        "/api/v1/data/import",
        headers={**headers(), "Idempotency-Key": "api-import-1"},
        data={"data_source_id": source_id, "metadata_json": json.dumps(metadata)},
        files={
            "file": (
                "guide.txt",
                "高地温影响作业安全，应持续监测掌子面温度并检查通风降温措施。".encode(),
                "text/plain",
            )
        },
    )
    assert imported.status_code == 202
    assert imported.json()["accepted"] is True
    job = client.get(f"/api/v1/jobs/{imported.json()['job_id']}", headers=headers())
    assert job.json()["progress"] == 100

    session = app.state.database.session_factory()
    document = session.get(SourceDocument, imported.json()["document_id"])
    document.status = ProcessingStatus.PUBLISHED
    session.commit()
    session.close()

    answer = client.post(
        "/api/v1/qa/ask",
        headers=headers("field_engineer", "internal"),
        json={"question": "高地温施工如何通风降温？"},
    )
    body = answer.json()

    assert answer.status_code == 200
    assert body["model_provider"] == "api-test"
    assert body["citations"][0]["source_id"] == "SRC-API-001"
    assert "graph_path" in body["citations"][0]
    assert "retrieval_score" in body["citations"][0]
    assert answer.headers["X-Trace-Id"] == body["trace_id"]

    overview = client.get(
        "/api/v1/workbench/overview",
        headers=headers("field_engineer", "internal"),
    )
    trace_created_at = overview.json()["recent_traces"][0]["created_at"]
    assert trace_created_at.endswith("Z") or trace_created_at.endswith("+00:00")

    detail = client.get(
        f"/api/v1/workbench/traces/{body['trace_id']}",
        headers=headers("field_engineer", "internal"),
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["question"] == "高地温施工如何通风降温？"
    assert detail_body["answer"] == "应持续监测温度并检查通风降温措施。"
    assert detail_body["answer_type"] == "explanation"
    assert detail_body["sources"][0]["source_id"] == "SRC-API-001"
    assert detail_body["sources"][0]["chunk_id"] == body["citations"][0]["chunk_id"]
    assert "高地温影响作业安全" in detail_body["sources"][0]["excerpt"]
    assert detail_body["retrieval_parameters"]["top_k"] == settings.retrieval_top_k
    assert detail_body["model_provider"] == "api-test"
    assert detail_body["created_at"].endswith("Z") or detail_body["created_at"].endswith(
        "+00:00"
    )

    hidden = client.get(
        f"/api/v1/workbench/traces/{body['trace_id']}",
        headers=headers("field_engineer", "internal", "another-user"),
    )
    assert hidden.status_code == 404

    insufficient_level = client.get(
        f"/api/v1/workbench/traces/{body['trace_id']}",
        headers=headers("field_engineer", "public"),
    )
    assert insufficient_level.status_code == 403


def test_role_permission_error_is_structured(database, settings):
    client, _ = build_client(database, settings)

    response = client.post(
        "/api/v1/data/sources",
        headers=headers("field_engineer", "internal"),
        json={
            "name": "无权创建",
            "source_type": "standard",
            "owner": "测试",
            "security_level": "internal",
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"


def test_managed_document_upload_chunks_publishes_and_lists(database, settings):
    client, app = build_client(database, settings)
    response = client.post(
        "/api/v1/data/documents/upload",
        headers=headers(),
        data={
            "title": "新增加的高地温施工资料",
            "source_type": "research",
            "version": "V2026.1",
            "effective_date": "2026-08-22",
            "security_level": "internal",
            "owner_org": "测试单位",
            "chunk_size": "160",
            "chunk_overlap": "30",
            "chunk_model": "same_as_answer",
        },
        files={
            "file": (
                "高原铁路隧道科研资料.md",
                ("# 高地温施工\n隧道掌子面应持续监测温度并检查通风系统。" * 8).encode(),
                "text/markdown",
            )
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["quality"]["chunk_count"] >= 1
    assert body["document_status"] == "published"
    assert body["chunking"] == {
        "chunk_size": 160,
        "chunk_overlap": 30,
        "chunk_model": "structural-v1",
    }

    session = app.state.database.session_factory()
    document = session.get(SourceDocument, body["document_id"])
    assert document.status == ProcessingStatus.PUBLISHED
    session.close()

    listing = client.get("/api/v1/data/documents", headers=headers())
    assert listing.status_code == 200
    assert listing.json()["documents"][0]["title"] == "新增加的高地温施工资料"
    assert listing.json()["documents"][0]["chunk_count"] >= 1
    assert ".pdf" in listing.json()["supported_extensions"]
    assert listing.json()["content_databases"]["original_documents"] == 1
    assert listing.json()["content_databases"]["chunks"] == body["quality"]["chunk_count"]
    assert listing.json()["chunking_defaults"]["answer_model"] == "api-test-v1"


def test_document_upload_auto_extracts_and_publishes_graph_knowledge(
    database, settings
):
    client, _ = build_client(database, settings)
    response = client.post(
        "/api/v1/data/documents/upload",
        headers=headers(),
        data={
            "title": "图谱自动识别联动测试",
            "source_type": "research",
            "version": "V1",
            "effective_date": "2026-09-01",
            "security_level": "internal",
            "owner_org": "联动测试单位",
            "chunk_size": "180",
            "chunk_overlap": "20",
            "chunk_model": "same_as_answer",
        },
        files={
            "file": (
                "graph-coupling.txt",
                ("高地温影响作业人员。" * 12).encode(),
                "text/plain",
            )
        },
    )

    assert response.status_code == 202
    uploaded = response.json()
    assert uploaded["accepted"] is True
    assert uploaded["quality"]["chunk_count"] >= 1
    assert uploaded["graph"]["published"] is True
    assert uploaded["graph"]["entities"] == 2
    assert uploaded["graph"]["relations"] == 1
    assert uploaded["graph"]["pending_entities"] == 0
    assert uploaded["graph"]["pending_relations"] == 0

    workspace = client.get("/api/v1/graph/workspace", headers=headers())
    assert workspace.status_code == 200
    graph = workspace.json()
    nodes_by_name = {node["name"]: node for node in graph["nodes"]}
    assert {"高地温", "作业人员"}.issubset(nodes_by_name)
    relation = next(
        edge for edge in graph["edges"] if edge["relation_type"] == "影响"
    )
    assert {
        relation["source"],
        relation["target"],
    } == {
        nodes_by_name["高地温"]["id"],
        nodes_by_name["作业人员"]["id"],
    }

    evidence = client.get(
        f"/api/v1/knowledge/evidence/entity/{nodes_by_name['高地温']['id']}",
        headers=headers(),
    )
    assert evidence.status_code == 200
    assert any(
        item["source_id"] == uploaded["source_id"]
        and "高地温影响作业人员" in item["chunk_text"]
        for item in evidence.json()
    )


def test_managed_upload_rejects_overlap_not_smaller_than_chunk(database, settings):
    client, _ = build_client(database, settings)
    response = client.post(
        "/api/v1/data/documents/upload",
        headers=headers(),
        data={
            "title": "无效切块参数",
            "source_type": "research",
            "effective_date": "2026-08-23",
            "chunk_size": "200",
            "chunk_overlap": "200",
        },
        files={"file": ("invalid.txt", "足够长的测试正文内容。" * 10, "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_CHUNKING_CONFIG"


def test_chunk_listing_and_safe_delete_endpoint(database, settings):
    client, app = build_client(database, settings)
    uploaded = client.post(
        "/api/v1/data/documents/upload",
        headers=headers(),
        data={
            "title": "分块证据删除测试",
            "source_type": "standard",
            "effective_date": "2026-09-01",
            "security_level": "internal",
            "chunk_size": "100",
            "chunk_overlap": "0",
        },
        files={
            "file": (
                "chunk-delete.md",
                "# 第一处\n高地温影响作业人员。\n# 第二处\n高地温影响作业人员。",
                "text/markdown",
            )
        },
    )
    assert uploaded.status_code == 202
    document_id = uploaded.json()["document_id"]

    listing = client.get(
        f"/api/v1/data/documents/{document_id}/chunks", headers=headers()
    )
    assert listing.status_code == 200
    assert listing.json()["total_chunks"] == 2
    assert all(item["evidence_count"] > 0 for item in listing.json()["chunks"])

    chunk_id = listing.json()["chunks"][0]["chunk_id"]
    deleted = client.delete(f"/api/v1/data/chunks/{chunk_id}", headers=headers())
    assert deleted.status_code == 200
    assert deleted.json()["remaining_chunks"] == 1
    assert deleted.json()["preserved_entities"] >= 2
    assert app.state.content_store.get_chunk_texts([chunk_id]) == {}


def test_governance_search_type_filter_and_delete_sync_all_stores(database, settings):
    client, app = build_client(database, settings)

    def upload(title, source_type, filename, text):
        response = client.post(
            "/api/v1/data/documents/upload",
            headers=headers(),
            data={
                "title": title,
                "source_type": source_type,
                "effective_date": "2026-08-23",
                "security_level": "internal",
            },
            files={"file": (filename, text.encode(), "text/plain")},
        )
        assert response.status_code == 202
        return response.json()

    standard = upload(
        "独特标准检索词：高地温作业规范",
        "standard",
        "标准检索样本.txt",
        "高地温作业人员应检查通风设备并持续监测掌子面温度。" * 8,
    )
    research = upload(
        "独特科研检索词：岩爆监测研究",
        "research",
        "科研检索样本.txt",
        "高地应力影响岩爆风险，监测设备采集数据后应由专家复核。" * 8,
    )

    by_text = client.get(
        "/api/v1/data/documents",
        headers=headers(),
        params={"query": "独特科研检索词"},
    )
    assert by_text.status_code == 200
    assert by_text.json()["filtered_documents"] == 1
    assert by_text.json()["documents"][0]["document_id"] == research["document_id"]

    by_type = client.get(
        "/api/v1/data/documents",
        headers=headers(),
        params={"source_type": "standard"},
    )
    assert by_type.status_code == 200
    assert by_type.json()["filtered_documents"] == 1
    assert by_type.json()["documents"][0]["document_id"] == standard["document_id"]

    session = app.state.database.session_factory()
    deleted_document = session.get(SourceDocument, research["document_id"])
    deleted_storage_path = deleted_document.storage_uri
    deleted_chunk_ids = list(
        session.scalars(
            select(Chunk.id).where(Chunk.document_id == research["document_id"])
        ).all()
    )
    session.close()

    deletion = client.delete(
        f"/api/v1/data/documents/{research['document_id']}", headers=headers()
    )
    assert deletion.status_code == 200
    assert deletion.json()["deleted_chunks"] == len(deleted_chunk_ids)

    session = app.state.database.session_factory()
    assert session.get(SourceDocument, research["document_id"]) is None
    assert session.get(SourceDocument, standard["document_id"]) is not None
    session.close()
    assert not Path(deleted_storage_path).exists()
    assert app.state.content_store.get_chunk_texts(deleted_chunk_ids) == {}
    stats = app.state.content_store.stats()
    assert stats["original_documents"] == 1
    assert stats["chunks"] == standard["quality"]["chunk_count"]

    after = client.get(
        "/api/v1/data/documents",
        headers=headers(),
        params={"source_type": "research"},
    )
    assert after.status_code == 200
    assert after.json()["filtered_documents"] == 0


def test_governance_can_filter_documents_by_created_period(database, settings):
    client, app = build_client(database, settings)

    def upload(title):
        response = client.post(
            "/api/v1/data/documents/upload",
            headers=headers(),
            data={
                "title": title,
                "source_type": "research",
                "effective_date": "2026-08-23",
                "security_level": "internal",
            },
            files={
                "file": (
                    f"{title}.txt",
                    (f"{title}：高原隧道时间筛选测试资料。" * 8).encode(),
                    "text/plain",
                )
            },
        )
        assert response.status_code == 202
        return response.json()["document_id"]

    today_id = upload("今日入库资料")
    recent_id = upload("三日前入库资料")
    old_id = upload("半年前入库资料")

    session = app.state.database.session_factory()
    now = datetime.now(timezone.utc)
    session.get(SourceDocument, recent_id).created_at = now - timedelta(days=3)
    session.get(SourceDocument, old_id).created_at = now - timedelta(days=200)
    session.commit()
    session.close()

    expected_ids = {
        "today": {today_id},
        "within_week": {today_id, recent_id},
        "within_month": {today_id, recent_id},
        "within_half_year": {today_id, recent_id},
        "before_half_year": {old_id},
    }
    for period, expected in expected_ids.items():
        response = client.get(
            "/api/v1/data/documents",
            headers=headers(),
            params={"created_period": period},
        )
        assert response.status_code == 200
        assert {item["document_id"] for item in response.json()["documents"]} == expected

    invalid = client.get(
        "/api/v1/data/documents",
        headers=headers(),
        params={"created_period": "unknown"},
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("selected_types", "expected_types"),
    [
        (["research"], {"research"}),
        (["research", "standard"], {"research", "standard"}),
        ([], {"research", "standard", "monitoring"}),
    ],
    ids=["single-type", "multiple-types", "all-types"],
)
def test_qa_can_limit_retrieval_to_selected_knowledge_block_type(
    database, settings, monkeypatch, selected_types, expected_types
):
    client, app = build_client(database, settings)

    for source_type, title, marker in (
        ("standard", "共享主题标准规范", "标准规范要求"),
        ("research", "共享主题科研成果", "科研试验表明"),
        ("monitoring", "共享主题监测资料", "现场监测记录"),
    ):
        uploaded = client.post(
            "/api/v1/data/documents/upload",
            headers=headers(),
            data={
                "title": title,
                "source_type": source_type,
                "effective_date": "2026-08-23",
                "security_level": "internal",
                "chunk_size": "1000",
                "chunk_overlap": "0",
            },
            files={
                "file": (
                    f"{source_type}.txt",
                    (f"共享检索词：{marker}，高地温隧道应持续监测温度并检查通风设备。" * 8).encode(),
                    "text/plain",
                )
            },
        )
        assert uploaded.status_code == 202

    with app.state.database.session_factory() as session:
        chunk_types = dict(
            session.execute(
                select(Chunk.id, SourceDocument.source_type).join(
                    SourceDocument, Chunk.document_id == SourceDocument.id
                )
            ).all()
        )
    assert set(chunk_types.values()) == {"research", "standard", "monitoring"}
    assert len(chunk_types) == 3

    # Graph expansion can match every type, but it must never reintroduce blocks
    # excluded by the user's scope into citations or the model's evidence.
    monkeypatch.setattr(
        HybridRetrievalService,
        "_graph_signals",
        lambda self, query, access: (
            {chunk_id: 1.0 for chunk_id in chunk_types},
            {
                chunk_id: [f"graph-evidence:{source_type}"]
                for chunk_id, source_type in chunk_types.items()
            },
        ),
    )
    model_evidence = []

    def generate_with_recorded_evidence(self, *, question, evidence, scenario, response_mode="detailed"):
        model_evidence.extend(evidence)
        return ModelOutput(answer="应持续监测温度并检查通风降温措施。", follow_up=[])

    monkeypatch.setattr(APIFakeGateway, "generate", generate_with_recorded_evidence)

    answer = client.post(
        "/api/v1/qa/ask",
        headers=headers("field_engineer", "internal", "scope-user"),
        json={
            "question": "共享检索词对高地温监测有什么要求？",
            "context_filters": {"source_types": selected_types},
        },
    )

    assert answer.status_code == 200
    body = answer.json()
    assert body["citations"]
    assert body["model_provider"] == "api-test"
    assert {citation["source_type"] for citation in body["citations"]} == expected_types
    assert set(body["graph_path"]) == {
        f"graph-evidence:{source_type}" for source_type in expected_types
    }
    expected_chunk_ids = {
        chunk_id
        for chunk_id, source_type in chunk_types.items()
        if source_type in expected_types
    }
    assert {citation["chunk_id"] for citation in body["citations"]} == expected_chunk_ids
    assert {item["chunk_id"] for item in model_evidence} == expected_chunk_ids
    with app.state.database.session_factory() as session:
        trace = session.scalar(select(QATrace).where(QATrace.trace_id == body["trace_id"]))
        assert trace is not None
        assert trace.retrieval_parameters["filters"]["source_types"] == selected_types
        assert set(trace.retrieved_chunk_ids) == expected_chunk_ids


def test_graph_manual_entity_and_relation_create_update_are_immediately_visible(
    database, settings
):
    client, app = build_client(database, settings)

    left = client.post(
        "/api/v1/graph/entities",
        headers=headers(),
        json={
            "standard_name": "人工实体甲",
            "domain": "environment",
            "aliases": ["甲别名"],
            "properties": {"位置": "进口工区"},
            "confidence": 0.96,
            "security_level": "internal",
        },
    )
    right = client.post(
        "/api/v1/graph/entities",
        headers=headers(),
        json={
            "standard_name": "人工实体乙",
            "domain": "method",
            "properties": {"措施": "动态验证"},
        },
    )
    assert left.status_code == 201
    assert right.status_code == 201
    left_id, right_id = left.json()["id"], right.json()["id"]

    catalog = client.get("/api/v1/graph/entities", headers=headers())
    assert catalog.status_code == 200
    assert {item["id"] for item in catalog.json()} == {left_id, right_id}

    relation = client.post(
        "/api/v1/graph/relations",
        headers=headers(),
        json={
            "source_entity_id": left_id,
            "target_entity_id": right_id,
            "relation_type": "采用",
            "applicable_conditions": {"温度": ">28℃"},
            "confidence": 0.93,
        },
    )
    assert relation.status_code == 201
    relation_id = relation.json()["id"]

    subgraph = client.get(
        "/api/v1/graph/subgraph",
        headers=headers(),
        params={"entity": "人工实体甲", "depth": 2},
    )
    assert subgraph.status_code == 200
    assert {item["id"] for item in subgraph.json()["nodes"]} == {left_id, right_id}
    assert [item["id"] for item in subgraph.json()["edges"]] == [relation_id]

    updated_entity = client.put(
        f"/api/v1/graph/entities/{left_id}",
        headers=headers(),
        json={
            "standard_name": "人工实体甲（已修改）",
            "aliases": ["甲别名", "甲新别名"],
            "properties": {"位置": "出口工区", "状态": "已复核"},
            "confidence": 0.99,
        },
    )
    assert updated_entity.status_code == 200
    assert updated_entity.json()["name"] == "人工实体甲（已修改）"
    assert updated_entity.json()["properties"]["状态"] == "已复核"

    updated_relation = client.put(
        f"/api/v1/graph/relations/{relation_id}",
        headers=headers(),
        json={
            "relation_type": "指导",
            "applicable_conditions": {"阶段": "施工期"},
            "confidence": 0.98,
        },
    )
    assert updated_relation.status_code == 200
    assert updated_relation.json()["relation_type"] == "指导"
    assert updated_relation.json()["applicable_conditions"] == {"阶段": "施工期"}

    updated_graph = client.get(
        "/api/v1/graph/subgraph",
        headers=headers(),
        params={"entity": "人工实体甲（已修改）", "depth": 2},
    ).json()
    assert {item["name"] for item in updated_graph["nodes"]} == {
        "人工实体甲（已修改）",
        "人工实体乙",
    }
    assert updated_graph["edges"][0]["relation_type"] == "指导"

    workspace = client.get("/api/v1/graph/workspace", headers=headers())
    assert workspace.status_code == 200
    assert {item["id"] for item in workspace.json()["nodes"]} == {left_id, right_id}
    assert [item["id"] for item in workspace.json()["edges"]] == [relation_id]

    overview = client.get("/api/v1/workbench/overview", headers=headers()).json()
    assert overview["counts"]["documents"] == 0
    assert overview["counts"]["data_sources"] == 0
    assert overview["counts"]["published_entities"] == 2
    assert overview["counts"]["published_relations"] == 1
    assert overview["counts"]["knowledge_version"] == 5

    documents = client.get("/api/v1/data/documents", headers=headers()).json()
    assert documents["total_documents"] == 0
    assert documents["total_chunks"] == 0
    assert documents["content_databases"]["original_documents"] == 0
    assert documents["content_databases"]["chunks"] == 0

    forbidden = client.post(
        "/api/v1/graph/entities",
        headers=headers("field_engineer", "internal", "field-user"),
        json={"standard_name": "无权新增", "domain": "human"},
    )
    assert forbidden.status_code == 403

    forbidden_delete = client.delete(
        f"/api/v1/graph/entities/{left_id}",
        headers=headers("field_engineer", "internal", "field-user"),
    )
    assert forbidden_delete.status_code == 403

    deleted_relation = client.delete(
        f"/api/v1/graph/relations/{relation_id}", headers=headers()
    )
    assert deleted_relation.status_code == 200
    assert deleted_relation.json()["relation_id"] == relation_id
    assert client.get("/api/v1/graph/workspace", headers=headers()).json()["edges"] == []

    replacement_relation = client.post(
        "/api/v1/graph/relations",
        headers=headers(),
        json={
            "source_entity_id": left_id,
            "target_entity_id": right_id,
            "relation_type": "支撑",
        },
    )
    assert replacement_relation.status_code == 201

    deleted_entity = client.delete(
        f"/api/v1/graph/entities/{left_id}", headers=headers()
    )
    assert deleted_entity.status_code == 200
    assert deleted_entity.json()["deleted_relations"] == 1
    after_delete = client.get("/api/v1/graph/workspace", headers=headers()).json()
    assert [item["id"] for item in after_delete["nodes"]] == [right_id]
    assert after_delete["edges"] == []
    final_overview = client.get("/api/v1/workbench/overview", headers=headers()).json()
    assert final_overview["counts"]["published_entities"] == 1
    assert final_overview["counts"]["published_relations"] == 0
    assert final_overview["counts"]["knowledge_version"] == 8

    session = app.state.database.session_factory()
    assert session.scalar(
        select(SourceDocument).where(SourceDocument.source_id == "SYS-GRAPH-MANUAL")
    ) is not None
    session.close()


def test_graph_v2_candidate_review_publish_statistics_and_evidence_are_coupled(
    database, settings
):
    client, _ = build_client(database, settings)
    uploaded = client.post(
        "/api/v1/data/documents/upload",
        headers=headers(),
        data={
            "title": "智能判识知识抽取资料",
            "source_type": "research",
            "effective_date": "2026-09-01",
            "security_level": "internal",
        },
        files={
            "file": (
                "knowledge-v2.txt",
                ("提出围岩稳定性智能判识算法。高地温影响施工人员。" * 8).encode(),
                "text/plain",
            )
        },
    )
    assert uploaded.status_code == 202
    assert uploaded.json()["graph"]["pending_entities"] >= 1

    ontology = client.get("/api/v1/knowledge/ontology", headers=headers()).json()
    assert len([item for item in ontology if item["active"]]) == 5
    assert {item["version"] for item in ontology if item["active"]} == {2}

    candidates = client.get("/api/v1/knowledge/candidates", headers=headers()).json()
    candidate = next(
        item for item in candidates if item["label"] == "围岩稳定性智能判识算法"
    )
    assert candidate["extraction_method"] == "pattern"
    assert candidate["source_title"] == "智能判识知识抽取资料"

    reviewed = client.post(
        "/api/v1/knowledge/review/batch",
        headers=headers("reviewer", "confidential", "reviewer-user"),
        json={
            "items": [
                {
                    "target_type": "entity",
                    "target_id": candidate["target_id"],
                    "decision": "approved",
                    "comment": "证据核对通过",
                }
            ]
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["approved"] == 1
    published = client.post(
        "/api/v1/knowledge/publish",
        headers=headers("reviewer", "confidential", "reviewer-user"),
    )
    assert published.status_code == 200
    assert published.json()["entity_count"] == 1

    evidence = client.get(
        f"/api/v1/knowledge/evidence/entity/{candidate['target_id']}",
        headers=headers(),
    ).json()
    assert evidence[0]["source_id"] == uploaded.json()["source_id"]
    assert evidence[0]["review_status"] == "published"
    assert evidence[0]["chunk_id"]
    assert evidence[0]["chunk_ordinal"] >= 1
    assert evidence[0]["chunk_char_count"] > 0
    assert "围岩稳定性智能判识算法" in evidence[0]["chunk_text"]
    statistics = client.get("/api/v1/graph/statistics", headers=headers()).json()
    assert statistics["active_ontology_version"] == 2
    assert statistics["target_nodes"] == 500
    assert statistics["nodes_by_domain"]["method"] >= 1
    releases = client.get("/api/v1/knowledge/releases", headers=headers()).json()
    assert releases[0]["version"] == published.json()["version"]
