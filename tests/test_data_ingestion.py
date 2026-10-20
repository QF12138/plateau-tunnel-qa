from datetime import datetime, timezone
from pathlib import Path
from io import BytesIO
import sqlite3

from openpyxl import Workbook

from tunnel_qa.core.enums import JobStatus, Role, SecurityLevel, SourceType
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.data.schemas import DataSourceCreate, DocumentMetadata, MonitoringInput
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.data.parsers import parse_document, split_parsed_document


def admin_access():
    return AccessContext(
        user_id="admin-1",
        role=Role.SYSTEM_ADMIN,
        security_level=SecurityLevel.CONFIDENTIAL,
    )


def test_import_metadata_cannot_forge_processing_or_graph_sync_state(database, settings):
    session, service = build_service(database, settings)
    source = create_source(service)
    supplied = metadata().model_copy(update={"extra": {
        "graph_sync": {"status": "completed", "processing_version": 1},
        "processing_history": {"1": {"forged": True}},
        "processing_version": 900,
        "rechunked_at": "forged",
        "rechunk_text_source": "forged",
        "user_note": "保留业务备注",
    }})
    result = service.import_document(
        data_source_id=source.id, filename="metadata.txt",
        content=("高地温隧道施工应监测温度并持续通风降温。" * 10).encode(),
        metadata=supplied, idempotency_key="reserved-metadata", access=admin_access(),
    )
    assert result.accepted
    document = session.get(SourceDocument, result.document_id)
    assert document.processing_version == 1
    assert document.metadata_extra["user_note"] == "保留业务备注"
    assert not {"graph_sync", "processing_history", "processing_version", "rechunked_at", "rechunk_text_source"}.intersection(document.metadata_extra)
    assert document.metadata_extra["chunk_processing"]
    session.close()


def test_deleted_chunk_cleanup_survives_failure_and_retry(database, settings, monkeypatch):
    from tunnel_qa.data.cleanup import retry_cleanup
    from tunnel_qa.data.models import ContentCleanupJob
    session, service = build_service(database, settings)
    source = create_source(service)
    result = service.import_document(data_source_id=source.id, filename="cleanup.txt",
        content=("高地温隧道施工应监测温度并持续通风降温。" * 20).encode(),
        metadata=metadata(), idempotency_key="cleanup-chunk", access=admin_access())
    chunk = session.query(Chunk).filter_by(document_id=result.document_id).first()
    chunk_id = chunk.id
    original = service.content_store.delete_chunk
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated locked store")
    monkeypatch.setattr(service.content_store, "delete_chunk", fail)
    deleted = service.delete_chunk(chunk_id, admin_access())
    assert deleted["cleanup_pending"]
    assert session.get(Chunk, chunk_id) is None
    assert service.content_store.get_chunk_texts([chunk_id])
    job_id = deleted["cleanup_job_id"]
    session.close()
    # A fresh session simulates process interruption after the primary commit.
    with database.session_factory() as recovered:
        monkeypatch.setattr(service.content_store, "delete_chunk", original)
        result = retry_cleanup(recovered, service.content_store, settings)
        assert result == {"attempted": 1, "completed": 1, "still_pending": 0}
        assert not service.content_store.get_chunk_texts([chunk_id])
        assert recovered.get(ContentCleanupJob, job_id).attempts == 2
        assert retry_cleanup(recovered, service.content_store, settings)["attempted"] == 0


def test_deleted_document_cleanup_is_durable(database, settings, monkeypatch):
    from tunnel_qa.data.cleanup import retry_cleanup
    session, service = build_service(database, settings)
    source = create_source(service)
    imported = service.import_document(data_source_id=source.id, filename="cleanup.txt",
        content=("隧道施工必须监测围岩变化并检查现场安全。" * 10).encode(),
        metadata=metadata(), idempotency_key="cleanup-document", access=admin_access())
    path = Path(session.get(SourceDocument, imported.document_id).storage_uri)
    original = service.content_store.delete_document
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(service.content_store, "delete_document", fail)
    result = service.delete_document(imported.document_id, admin_access())
    assert result["cleanup_pending"] and path.exists()
    assert session.get(SourceDocument, imported.document_id) is None
    monkeypatch.setattr(service.content_store, "delete_document", original)
    assert retry_cleanup(session, service.content_store, settings)["completed"] == 1
    assert not path.exists()
    assert service.content_store.stats()["original_documents"] == 0


def test_cleanup_cannot_unlink_outside_storage(database, settings, tmp_path):
    from tunnel_qa.data.cleanup import process_cleanup
    from tunnel_qa.data.models import ContentCleanupJob
    session, service = build_service(database, settings)
    outside = tmp_path / "unrelated.txt"
    outside.write_text("keep", encoding="utf-8")
    job = ContentCleanupJob(target_kind="document", target_id="missing", storage_uri=str(outside))
    session.add(job)
    session.commit()
    assert not process_cleanup(session, service.content_store, settings, job)
    assert outside.read_text() == "keep"
    assert job.last_error == "UNSAFE_CLEANUP_PATH"


def test_three_database_backup_restore_and_corruption(settings, tmp_path):
    from scripts.backup_databases import backup, restore, verify
    from tunnel_qa.app import register_models
    from tunnel_qa.db.session import Database
    import pytest
    register_models()
    file_settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'primary.db'}"})
    database = Database(file_settings)
    session, service = build_service(database, file_settings)
    source = create_source(service)
    imported = service.import_document(data_source_id=source.id, filename="backup.txt",
        content=("隧道备份演练仅使用测试资料，需要核对原文及分块。" * 10).encode(),
        metadata=metadata(), idempotency_key="backup-restore", access=admin_access())
    source_path = Path(session.get(SourceDocument, imported.document_id).storage_uri)
    session.close()
    bundle = tmp_path / "backup"
    result = backup(file_settings, bundle)
    assert result["counts"]["documents"] == 1
    assert verify(bundle)["chunks"] > 0
    restored = tmp_path / "recovered"
    assert restore(bundle, restored)["documents"] == 1
    with sqlite3.connect(restored / "tunnel_qa.db") as connection:
        restored_path = Path(connection.execute("SELECT storage_uri FROM source_documents").fetchone()[0])
    assert restored_path.is_relative_to(restored)
    assert restored_path.read_bytes() == source_path.read_bytes()
    with pytest.raises(FileExistsError):
        restore(bundle, restored)
    with sqlite3.connect(bundle / "document_chunks.db") as connection:
        connection.execute("UPDATE document_chunks SET text='corrupt'")
    with pytest.raises(ValueError, match="checksum"):
        verify(bundle)
    database.engine.dispose()


def build_service(database, settings):
    from tunnel_qa.data import models  # noqa: F401 - register ORM metadata

    database.create_schema()
    session = database.session_factory()
    return session, DataIngestionService(session, settings)


def create_source(service, source_type=SourceType.STANDARD):
    return service.create_data_source(
        DataSourceCreate(
            name=f"source-{source_type.value}",
            source_type=source_type,
            owner="数据管理员",
            security_level=SecurityLevel.INTERNAL,
        ),
        admin_access(),
    )


def metadata(source_type=SourceType.STANDARD):
    return DocumentMetadata(
        source_id="SRC-2026-000123",
        title="高地温隧道施工规范",
        source_type=source_type,
        project_site="某隧道进口工区" if source_type == SourceType.ENGINEERING_CASE else None,
        version="V2.1",
        effective_date="2026-05-01",
        security_level=SecurityLevel.INTERNAL,
        owner_org="国铁川藏科创中心",
        content_location="第1章",
    )


def test_document_import_is_traceable_chunked_and_immutable(database, settings):
    session, service = build_service(database, settings)
    source = create_source(service)
    content = (
        "# 总则\n高地温隧道施工应持续监测掌子面温度，并建立通风降温措施。"
        "当监测数据异常时，应复核传感器状态并由现场负责人确认。" * 4
    ).encode()

    result = service.import_document(
        data_source_id=source.id,
        filename="standard.md",
        content=content,
        metadata=metadata(),
        idempotency_key="idem-001",
        access=admin_access(),
    )

    assert result.accepted
    assert result.status == JobStatus.SUCCEEDED
    assert result.quality and result.quality.chunk_count >= 2
    document = session.get(SourceDocument, result.document_id)
    assert document is not None
    assert Path(document.storage_uri).read_bytes() == content
    chunks = session.query(Chunk).filter_by(document_id=document.id).all()
    assert len(chunks) >= 2

    stores = service.content_store.stats()
    assert stores["original_documents"] == 1
    assert stores["chunks"] == len(chunks)
    with sqlite3.connect(service.content_store.original_path) as connection:
        original = connection.execute(
            "SELECT original_blob, parsed_text FROM original_documents WHERE document_id = ?",
            (document.id,),
        ).fetchone()
    assert original[0] == content
    assert "高地温隧道施工" in original[1]
    stored_chunks = service.content_store.get_chunk_texts(chunk.id for chunk in chunks)
    assert stored_chunks[chunks[0].id] == chunks[0].text


def test_idempotency_returns_original_result_without_duplicate(database, settings):
    session, service = build_service(database, settings)
    source = create_source(service)
    kwargs = dict(
        data_source_id=source.id,
        filename="standard.txt",
        content="隧道施工监测资料，内容足以通过最小质量校验。".encode(),
        metadata=metadata(),
        idempotency_key="idem-replay",
        access=admin_access(),
    )
    first = service.import_document(**kwargs)
    second = service.import_document(**kwargs)

    assert first.document_id == second.document_id
    assert second.idempotent_replay
    assert session.query(SourceDocument).count() == 1


def test_invalid_case_is_quarantined_with_repairable_error(database, settings):
    _, service = build_service(database, settings)
    source = create_source(service, SourceType.ENGINEERING_CASE)
    bad_metadata = metadata(SourceType.ENGINEERING_CASE).model_copy(update={"project_site": None})

    result = service.import_document(
        data_source_id=source.id,
        filename="case.txt",
        content="工程案例的有效正文内容应当足够长以进入解析流程。".encode(),
        metadata=bad_metadata,
        idempotency_key="idem-bad",
        access=admin_access(),
    )

    assert not result.accepted
    assert result.status == JobStatus.QUARANTINED
    assert "必须填写项目或工点" in result.validation_errors[0]


def test_damaged_document_is_quarantined_instead_of_raising_server_error(
    database, settings
):
    session, service = build_service(database, settings)
    source = create_source(service)

    result = service.import_document(
        data_source_id=source.id,
        filename="damaged.json",
        content=b'{"broken": }',
        metadata=metadata(),
        idempotency_key="idem-damaged-json",
        access=admin_access(),
    )

    assert result.accepted is False
    assert result.status == JobStatus.QUARANTINED
    assert result.validation_errors == ["文档解析失败，请检查文件是否损坏且扩展名是否正确"]
    assert session.query(SourceDocument).count() == 0


def test_monitoring_unit_conversion_keeps_original_and_cleaned_values(database, settings):
    _, service = build_service(database, settings)
    record = service.ingest_monitoring(
        MonitoringInput(
            sensor_id="TEMP-01",
            timestamp=datetime(2026, 8, 22, 10, tzinfo=timezone.utc),
            metric_code="temperature",
            value=303.15,
            unit="K",
            location={"tunnel": "测试隧道", "mileage": "DK1+200"},
            source_system="monitor-v1",
        ),
        admin_access(),
    )

    assert record.original_value == 303.15
    assert round(record.cleaned_value, 2) == 30.00
    assert record.canonical_unit == "℃"
    assert record.conversion_rule == "kelvin-to-celsius"


def test_xlsx_parser_preserves_sheet_and_row_content():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "监测数据"
    sheet.append(["测点", "温度"])
    sheet.append(["TEMP-01", 39.5])
    buffer = BytesIO()
    workbook.save(buffer)

    text, mime_type = parse_document("monitoring.xlsx", buffer.getvalue())

    assert "[工作表: 监测数据]" in text
    assert "TEMP-01 | 39.5" in text
    assert "spreadsheetml.sheet" in mime_type


def test_pdf_chunking_preserves_page_boundaries_and_numbers():
    parsed = (
        "[第1页]\n# 总则\n第一页的高地温施工内容足够用于切片测试。\n\n"
        "[第2页]\n# 监测\n第二页的传感器监测内容足够用于切片测试。"
    )

    chunks = split_parsed_document("standard.pdf", parsed, 120, 20)

    assert [chunk[2] for chunk in chunks] == [1, 2]
    assert "第一页" in chunks[0][1]
    assert "第二页" in chunks[1][1]
