"""Acceptance contracts for prepare-before-chunk ingestion.

All clients use the temporary databases supplied by conftest.  No API model,
production database, or corpus file is needed by these integration tests.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tunnel_qa.app import create_app
from tunnel_qa.data.models import Chunk, DataSource, ImportJob, SourceDocument
from tunnel_qa.data.processing_metadata import processing_chunk, processing_report
from tunnel_qa.knowledge.models import (
    Evidence,
    KnowledgeEntity,
    KnowledgeEvidenceLink,
    KnowledgeRelation,
    KnowledgeRelease,
)
from tunnel_qa.qa.model_gateway import ModelOutput


class RecordingGateway:
    provider = "knowledge-ingestion-test"
    model = "recording-test-v1"

    def __init__(self):
        self.evidence = []

    def generate(self, *, question, evidence, scenario, response_mode="detailed"):
        self.evidence = evidence
        return ModelOutput(answer="依据完整条款核对适用条件。", follow_up=[])


def headers(role="system_admin", level="confidential", **extra):
    return {
        "X-User-Id": "knowledge-ingestion-test",
        "X-Role": role,
        "X-Security-Level": level,
        **extra,
    }


def build_client(database, settings):
    gateway = RecordingGateway()
    app = create_app(settings, database, gateway)
    return TestClient(app), app, gateway


def post_preview(client, text, *, filename="knowledge.md", extra=None, identity=None):
    data = {"source_type": "research", **(extra or {})}
    return client.post(
        "/api/v1/data/documents/preview",
        headers=identity or headers(),
        data=data,
        files={"file": (filename, text.encode("utf-8"), "application/octet-stream")},
    )


def post_upload(client, text, *, filename="knowledge.md", extra=None, identity=None):
    data = {
        "title": "隧道施工知识块集成验收",
        "source_type": "research",
        "version": "V1",
        "effective_date": "2026-10-14",
        "security_level": "internal",
        "owner_org": "测试单位",
        **(extra or {}),
    }
    return client.post(
        "/api/v1/data/documents/upload",
        headers=identity or headers(),
        data=data,
        files={"file": (filename, text.encode("utf-8"), "application/octet-stream")},
    )


def row_counts(database):
    models = (
        DataSource, ImportJob, SourceDocument, Chunk, Evidence,
        KnowledgeEntity, KnowledgeRelation, KnowledgeEvidenceLink, KnowledgeRelease,
    )
    with database.session_factory() as session:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in models
        }


def stored_files(settings):
    return set(settings.storage_path.rglob("*")) if settings.storage_path.exists() else set()


SOURCE = (
    "# 目录\n第一章 试验方法........1\n第二章 试验结果........3\n"
    "# 第一章 试验方法\n高地温影响作业人员。试验温度为38℃，应持续监测温度变化。\n"
    "# 第二章 试验结果\n通风措施实施后温度降低5℃，但不得省略现场复核。\n"
    "# 参考文献\n[1] 仅作参考的目录外文献，2024。\n"
    "# 致谢\n感谢专用于测试的支持单位。"
)


def test_preview_is_read_only_and_returns_inspectable_knowledge_blocks(database, settings):
    client, app, _ = build_client(database, settings)
    before = row_counts(database)
    stores_before = app.state.content_store.stats()
    files_before = stored_files(settings)

    response = post_preview(client, SOURCE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["chunking"]["chunk_model"] == "key-v2"
    assert body["returned_chunks"] == len(body["chunks"])
    assert body["total_chunks"] >= body["returned_chunks"] > 0
    assert body["processing"]["chunk_count"] == body["total_chunks"]
    for chunk in body["chunks"]:
        assert set(("ordinal", "text", "structure_path", "page_number", "page_end",
                    "kind", "warnings", "char_count")).issubset(chunk)
        assert chunk["text"] and chunk["char_count"] == len(chunk["text"])
        assert isinstance(chunk["warnings"], list)
    assert row_counts(database) == before
    assert app.state.content_store.stats() == stores_before
    assert stored_files(settings) == files_before


def test_upload_preserves_original_but_only_publishes_selected_blocks(database, settings):
    client, app, _ = build_client(database, settings)
    preview = post_preview(client, SOURCE).json()
    response = post_upload(client, SOURCE)

    assert response.status_code == 202, response.text
    result = response.json()
    assert result["accepted"] is True
    assert result["chunking"]["chunk_model"] == "key-v2"
    assert result["processing"] == preview["processing"]
    document_id = result["document_id"]
    listing = client.get(f"/api/v1/data/documents/{document_id}/chunks", headers=headers())
    assert listing.status_code == 200, listing.text
    listed = listing.json()
    assert listed["processing"] == result["processing"]
    combined = "\n".join(chunk["text"] for chunk in listed["chunks"])
    assert "38℃" in combined and "降低5℃" in combined
    assert "不得省略现场复核" in combined
    for excluded in ("........1", "仅作参考的目录外文献", "感谢专用于测试的支持单位"):
        assert excluded not in combined
    assert [row["text"] for row in listed["chunks"]] == [
        row["text"] for row in preview["chunks"]
    ]

    with database.session_factory() as session:
        document = session.get(SourceDocument, document_id)
        assert "仅作参考的目录外文献" in document.parsed_text
        metadata = document.metadata_extra["chunk_processing"]
        assert metadata["report"] == result["processing"]
        chunks = session.scalars(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)
        ).all()
        mirrored = app.state.content_store.get_chunk_texts(chunk.id for chunk in chunks)
        assert len(chunks) == listed["total_chunks"]
        for chunk in chunks:
            assert mirrored[chunk.id] == chunk.text
            assert chunk.char_count == len(chunk.text)
            stored = metadata["chunks"][str(chunk.ordinal)]
            assert set(("page_start", "page_end", "kind", "warnings")).issubset(stored)
        assert all("仅作参考" not in evidence.quote for evidence in session.scalars(select(Evidence)))
    with sqlite3.connect(app.state.content_store.original_path) as connection:
        original, parsed = connection.execute(
            "SELECT original_blob, parsed_text FROM original_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    assert original == SOURCE.encode("utf-8")
    assert "感谢专用于测试的支持单位" in parsed


def test_new_pdf_preview_and_import_share_unsafe_page_isolation(database, settings, monkeypatch):
    import pymupdf
    from tunnel_qa.data import pdf_layout
    with pymupdf.open() as pdf:
        for value in ("高地温影响作业人员，必须保持通风并记录检测条件。", "不可靠表格数值", "隧道采用超前支护，施工过程中不得省略现场复核。"):
            page = pdf.new_page()
            page.insert_text((60, 100), value, fontname="china-s", fontsize=12)
        raw = pdf.tobytes()
    monkeypatch.setattr(pdf_layout, "page_text_warnings", lambda page:
        ["legacy_ocr_numeric_table_requires_review"] if page.number == 1 else [])
    client, app, _ = build_client(database, settings)
    preview = client.post("/api/v1/data/documents/preview", headers=headers(),
                          data={"source_type": "research"}, files={"file": ("scan.pdf", raw, "application/pdf")})
    assert preview.status_code == 200 and preview.json()["accepted"]
    response = client.post("/api/v1/data/documents/upload", headers=headers(), data={
        "title": "隔离测试", "source_type": "research", "effective_date": "2026-10-18",
    }, files={"file": ("scan.pdf", raw, "application/pdf")})
    assert response.status_code == 202 and response.json()["accepted"]
    assert response.json()["processing"] == preview.json()["processing"]
    assert response.json()["processing"]["quarantined_pages"][0]["page"] == 2
    with database.session_factory() as session:
        document = session.get(SourceDocument, response.json()["document_id"])
        assert "不可靠表格数值" in document.parsed_text
        chunks = session.scalars(select(Chunk).where(Chunk.document_id == document.id)).all()
        assert all(c.page_number != 2 and "不可靠" not in c.text for c in chunks)
        assert all("不可靠" not in e.quote for e in session.scalars(select(Evidence)))
    with sqlite3.connect(app.state.content_store.original_path) as connection:
        assert connection.execute("SELECT original_blob FROM original_documents").fetchone()[0] == raw


@pytest.mark.parametrize("role", ["field_engineer", "researcher", "external_system"])
def test_preview_requires_ingestion_permission(database, settings, role):
    client, app, _ = build_client(database, settings)
    before = row_counts(database)
    response = post_preview(client, SOURCE, identity=headers(role=role))
    assert response.status_code == 403
    assert row_counts(database) == before
    assert app.state.content_store.stats()["original_documents"] == 0


def test_all_nonknowledge_content_is_rejected_without_publishing(database, settings):
    client, app, _ = build_client(database, settings)
    text = "# 参考文献\n" + "\n".join(f"[{i}] 仅供出处核查的参考资料。" for i in range(60))
    preview = post_preview(client, text)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["accepted"] is False
    assert body["total_chunks"] == 0
    assert body["chunks"] == []
    assert body["processing"]["needs_review"] is True
    assert len(body["processing"]["excluded_samples"]) <= 20
    assert all(len(item["text"]) <= 180 for item in body["processing"]["excluded_samples"])

    uploaded = post_upload(client, text)
    assert uploaded.status_code == 202, uploaded.text
    assert uploaded.json()["accepted"] is False
    assert uploaded.json()["status"] == "quarantined"
    counts = row_counts(database)
    assert counts["source_documents"] == counts["chunks"] == counts["knowledge_entities"] == 0
    assert app.state.content_store.stats()["original_documents"] == 0
    assert app.state.content_store.stats()["chunks"] == 0


def test_replay_returns_original_processing_report_without_graph_increment(database, settings):
    client, app, _ = build_client(database, settings)
    identity = headers(**{"Idempotency-Key": "key-v2-stable-replay"})
    first = post_upload(client, SOURCE, identity=identity)
    assert first.status_code == 202, first.text
    before = row_counts(database)
    stores_before = app.state.content_store.stats()
    second = post_upload(
        client, SOURCE, identity=identity,
        extra={"chunk_size": "500", "chunk_overlap": "0"},
    )
    assert second.status_code == 202, second.text
    result = second.json()
    assert result["idempotent_replay"] is True
    assert result["document_id"] == first.json()["document_id"]
    assert result["chunking"] == first.json()["chunking"]
    assert result["processing"] == first.json()["processing"]
    assert row_counts(database) == before
    assert app.state.content_store.stats() == stores_before


def test_legacy_same_as_answer_still_uses_legacy_structure_strategy(database, settings):
    client, _, _ = build_client(database, settings)
    response = post_upload(client, SOURCE, extra={"chunk_model": "same_as_answer"})
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["accepted"] is True
    assert result["chunking"]["chunk_model"] == "structural-v1"
    with database.session_factory() as session:
        chunks = session.scalars(select(Chunk).where(Chunk.document_id == result["document_id"])).all()
        assert "仅作参考的目录外文献" in "\n".join(chunk.text for chunk in chunks)


def test_plaintext_safety_clauses_keep_conditions_negations_and_units(database, settings):
    client, _, _ = build_client(database, settings)
    text = (
        "# 供电及作业要求\n"
        "3.1.1 潮湿场所作业时，照明电压不得超过36 V。\n"
        "3.1.2 温度超过38℃时，应持续通风；未经现场负责人确认不得恢复作业。\n"
        "3.1.3 接地电阻不得大于4 Ω。\n220 V;\n仅用于规定的非潮湿设备回路。"
    )
    response = post_preview(client, text, filename="safety.txt", extra={"source_type": "standard"})
    assert response.status_code == 200, response.text
    combined = "\n".join(row["text"] for row in response.json()["chunks"])
    for essential in (
        "3.1.1 潮湿场所作业时，照明电压不得超过36 V。",
        "温度超过38℃时", "未经现场负责人确认不得恢复作业", "接地电阻不得大于4 Ω",
        "220 V;", "仅用于规定的非潮湿设备回路",
    ):
        assert essential in combined
    assert all("220 V" not in row["structure_path"] for row in response.json()["chunks"])


def test_qa_receives_late_conditions_in_knowledge_block_longer_than_1000_chars(database, settings):
    client, _, gateway = build_client(database, settings)
    tail = "特别限制：仅当备用通风设备和监测装置均经检查合格后才可恢复作业，否则不得恢复。"
    text = "# 隧道复工条件\n" + "\n".join(
        f"第{i}项检查记录应核对隧道作业条件、通风状态及现场监测数据并由负责人签认。"
        for i in range(45)
    ) + "\n" + tail
    uploaded = post_upload(client, text, extra={"chunk_size": "3000", "chunk_overlap": "0"})
    assert uploaded.status_code == 202 and uploaded.json()["accepted"], uploaded.text
    with database.session_factory() as session:
        chunks = session.scalars(select(Chunk).where(Chunk.document_id == uploaded.json()["document_id"])).all()
        assert len(chunks) == 1
        assert len(chunks[0].text) > 1000
        assert chunks[0].text.index(tail) > 1000
    answer = client.post(
        "/api/v1/qa/ask", headers=headers(),
        json={"question": "隧道复工有哪些特别限制和恢复作业条件？"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["citations"]
    assert any(tail in item["excerpt"] for item in answer.json()["citations"])
    assert any(tail in item["excerpt"] for item in gateway.evidence)


@pytest.mark.parametrize("legacy_metadata", [
    pytest.param({"chunking": None}, id="chunking-null"),
    pytest.param({"chunking": "legacy"}, id="chunking-string"),
    pytest.param({"chunking": []}, id="chunking-list"),
    pytest.param({"chunking": 1}, id="chunking-number"),
    pytest.param(None, id="metadata-json-null"),
    pytest.param("legacy", id="metadata-string"),
    pytest.param(["legacy"], id="metadata-list"),
    pytest.param(1, id="metadata-number"),
    pytest.param({}, id="metadata-empty"),
])
def test_qa_tolerates_legacy_chunking_metadata_and_uses_bounded_excerpt(
    database, settings, legacy_metadata,
):
    client, _, gateway = build_client(database, settings)
    tail = "旧资料末尾标记：应继续核对其余复工条件。"
    text = "# 隧道复工条件\n" + "\n".join(
        f"第{i}项检查记录应核对隧道作业条件、通风状态及现场监测数据并由负责人签认。"
        for i in range(45)
    ) + "\n" + tail
    uploaded = post_upload(client, text, extra={"chunk_size": "3000", "chunk_overlap": "0"})
    assert uploaded.status_code == 202 and uploaded.json()["accepted"], uploaded.text
    document_id = uploaded.json()["document_id"]
    with database.session_factory() as session:
        document = session.get(SourceDocument, document_id)
        chunks = session.scalars(select(Chunk).where(Chunk.document_id == document_id)).all()
        assert len(chunks) == 1
        chunk_id = chunks[0].id
        assert chunks[0].text.index(tail) > 1000
        expected_excerpt = chunks[0].text[:1000]
        # Older JSON metadata was unconstrained. Simulate persisted legacy
        # shapes without altering either the original file or stored chunks.
        document.metadata_extra = legacy_metadata
        session.commit()

    answer = client.post(
        "/api/v1/qa/ask", headers=headers(),
        json={"question": "隧道复工应核对哪些作业条件与通风监测数据？"},
    )
    assert answer.status_code == 200, answer.text
    citations = answer.json()["citations"]
    assert len(citations) == 1
    assert citations[0]["chunk_id"] == chunk_id
    assert citations[0]["excerpt"] == expected_excerpt
    assert citations[0]["content_location"]
    assert citations[0]["source_id"] == uploaded.json()["source_id"]
    assert len(gateway.evidence) == 1
    assert gateway.evidence[0]["excerpt"] == expected_excerpt
    assert tail not in gateway.evidence[0]["excerpt"]


def test_cross_page_range_survives_preview_storage_and_graph_evidence(database, settings, monkeypatch):
    parsed = (
        "[第1页]\n# 温度控制\n高地温影响作业人员，当作业面温度超过38℃时，应\n· 1 ·\n\n"
        "[第2页]\n持续通风并撤离非必要人员。\n· 2 ·"
    )
    # The parser itself is covered by parser tests; isolate the integration
    # contract here so the fixture does not depend on PDF fonts or OCR.
    monkeypatch.setattr("tunnel_qa.data.service.parse_document", lambda *_: (parsed, "application/pdf"))
    client, _, _ = build_client(database, settings)
    preview = post_preview(client, "synthetic-pdf-cross-page", filename="cross-page.pdf")
    assert preview.status_code == 200, preview.text
    first = preview.json()["chunks"][0]
    assert (first["page_number"], first["page_end"]) == (1, 2)
    uploaded = post_upload(client, "synthetic-pdf-cross-page", filename="cross-page.pdf")
    assert uploaded.status_code == 202 and uploaded.json()["accepted"], uploaded.text
    listing = client.get(
        f"/api/v1/data/documents/{uploaded.json()['document_id']}/chunks", headers=headers(),
    )
    assert listing.status_code == 200, listing.text
    row = listing.json()["chunks"][0]
    assert (row["page_number"], row["page_end"]) == (1, 2)
    assert "cross_page_continuation" in row["warnings"]
    assert "1" in row["content_location"] and "2" in row["content_location"]
    with database.session_factory() as session:
        evidence = session.scalars(select(Evidence)).all()
        assert evidence
        assert all(item.source_location == row["content_location"] for item in evidence)


def test_real_two_column_pdf_preview_and_saved_chunks_agree(database, settings):
    import pymupdf

    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        for side, x in (("LEFT", 40), ("RIGHT", 320)):
            for i in range(10):
                page.insert_text((x, 100 + i * 18), f"{side}{i}: preserve the original testing condition.", fontsize=10)
        page.insert_text((320, 300), "References", fontsize=12)
        page.insert_text((320, 320), "[1] Synthetic reference excluded from knowledge.", fontsize=10)
        raw = document.tobytes()
    client, _, _ = build_client(database, settings)
    files = {"file": ("two-columns.pdf", raw, "application/pdf")}
    before = row_counts(database)
    preview = client.post("/api/v1/data/documents/preview", headers=headers(), files=files,
                          data={"source_type": "research", "chunk_size": "800", "chunk_overlap": "0"})
    assert preview.status_code == 200 and preview.json()["accepted"], preview.text
    assert row_counts(database) == before
    preview_text = "\n".join(chunk["text"] for chunk in preview.json()["chunks"])
    assert preview_text.index("LEFT9") < preview_text.index("RIGHT0")
    assert "Synthetic reference" not in preview_text
    uploaded = client.post("/api/v1/data/documents/upload", headers=headers(), files=files, data={
        "title": "双栏PDF切块集成测试", "source_type": "research", "version": "V1",
        "effective_date": "2026-10-16", "security_level": "internal", "owner_org": "测试单位",
        "chunk_size": "800", "chunk_overlap": "0",
    })
    assert uploaded.status_code == 202 and uploaded.json()["accepted"], uploaded.text
    listing = client.get(f"/api/v1/data/documents/{uploaded.json()['document_id']}/chunks", headers=headers())
    assert listing.status_code == 200, listing.text
    chunks = listing.json()["chunks"]
    assert [chunk["text"] for chunk in chunks] == [chunk["text"] for chunk in preview.json()["chunks"]]
    assert all(chunk["page_number"] == chunk["page_end"] == 1 for chunk in chunks)


def test_replay_checks_stored_document_security_before_returning_report(database, settings):
    client, _, _ = build_client(database, settings)
    first = post_upload(
        client, SOURCE, extra={"security_level": "confidential"},
        identity=headers(**{"Idempotency-Key": "private-processing-report"}),
    )
    assert first.status_code == 202 and first.json()["accepted"], first.text
    replay = post_upload(
        client, SOURCE, extra={"security_level": "internal"},
        identity=headers(role="data_admin", level="internal", **{"Idempotency-Key": "private-processing-report"}),
    )
    assert replay.status_code == 403
    assert "excluded_samples" not in replay.text
    assert "仅作参考的目录外文献" not in replay.text


def test_import_cannot_accept_forged_processing_metadata(database, settings):
    client, _, _ = build_client(database, settings)
    source = client.post(
        "/api/v1/data/sources", headers=headers(),
        json={"name": "processing-metadata-test", "source_type": "research",
              "owner": "测试单位", "security_level": "internal"},
    )
    assert source.status_code == 201
    metadata = {
        "source_id": "SRC-PROCESSING-META", "title": "服务端处理报告", "source_type": "research",
        "version": "V1", "effective_date": "2026-10-14", "security_level": "internal",
        "owner_org": "测试单位", "content_location": "原文",
        "extra": {"chunk_processing": {"report": {"forged": True}, "chunks": {"1": {"page_end": 999999}}}},
    }
    response = client.post(
        "/api/v1/data/import", headers=headers(**{"Idempotency-Key": "forged-report"}),
        data={"data_source_id": source.json()["id"], "metadata_json": json.dumps(metadata, ensure_ascii=False)},
        files={"file": ("source.md", SOURCE.encode(), "text/markdown")},
    )
    assert response.status_code == 202 and response.json()["accepted"], response.text
    result = response.json()
    assert result["chunking"]["chunk_model"] == "key-v2"
    assert "forged" not in result["processing"]
    with database.session_factory() as session:
        document = session.get(SourceDocument, result["document_id"])
        stored = document.metadata_extra["chunk_processing"]
        assert stored["report"] == result["processing"]
        assert stored["chunks"]["1"]["page_end"] is None


@pytest.mark.parametrize("metadata", [
    None, [], "legacy", 1, {},
    {"chunk_processing": None},
    {"chunk_processing": "legacy"},
    {"chunk_processing": []},
    {"chunk_processing": {"report": None, "chunks": None}},
    {"chunk_processing": {"report": [], "chunks": []}},
    {"chunk_processing": {"report": "legacy", "chunks": {"1": "legacy"}}},
    {"chunk_processing": {"report": 1, "chunks": {"1": None}}},
])
def test_processing_metadata_readers_tolerate_legacy_shapes(metadata):
    assert processing_report(metadata) is None
    assert processing_chunk(metadata, 1) == {}


def test_processing_metadata_readers_preserve_reports_and_return_detached_containers():
    report = {"strategy": "knowledge-structural-v2", "chunk_count": 1}
    chunk = {"page_start": 1, "page_end": 2, "kind": "clause", "warnings": []}
    metadata = {"chunk_processing": {"report": report, "chunks": {"1": chunk}}}
    loaded_report = processing_report(metadata)
    loaded_chunk = processing_chunk(metadata, 1)
    assert loaded_report == report and loaded_report is not report
    assert loaded_chunk == chunk and loaded_chunk is not chunk
    assert processing_chunk(metadata, 2) == {}
    assert processing_report({"chunk_processing": {"report": {}}}) == {}


@pytest.mark.parametrize("legacy_processing", [
    None, "legacy", [],
    {"report": None, "chunks": None},
    {"report": "legacy", "chunks": {"1": "legacy"}},
])
def test_legacy_processing_metadata_does_not_break_list_replay_or_evidence(
    database, settings, legacy_processing,
):
    client, _, _ = build_client(database, settings)
    identity = headers(**{"Idempotency-Key": "legacy-processing-shape"})
    first = post_upload(client, SOURCE, identity=identity)
    assert first.status_code == 202 and first.json()["accepted"], first.text
    document_id = first.json()["document_id"]
    with database.session_factory() as session:
        document = session.get(SourceDocument, document_id)
        document.metadata_extra = {"chunk_processing": legacy_processing}
        entity_id = session.scalar(select(KnowledgeEntity.id))
        assert entity_id is not None
        session.commit()
    before = row_counts(database)

    listed = client.get(f"/api/v1/data/documents/{document_id}/chunks", headers=headers())
    assert listed.status_code == 200, listed.text
    assert listed.json()["processing"] is None
    assert listed.json()["chunks"]
    assert all(row["kind"] == "legacy" for row in listed.json()["chunks"])

    replay = post_upload(client, SOURCE, identity=identity)
    assert replay.status_code == 202, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["document_id"] == document_id
    assert replay.json()["processing"] is None

    evidence = client.get(f"/api/v1/knowledge/evidence/entity/{entity_id}", headers=headers())
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()
    assert all(row["page_end"] is None for row in evidence.json())
    assert row_counts(database) == before
