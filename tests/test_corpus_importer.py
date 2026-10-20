import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from tunnel_qa.app import register_models
from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import ProcessingStatus
from tunnel_qa.corpus.importer import CorpusDatabaseImporter
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.core.errors import AppError
from tunnel_qa.core.enums import Role, SecurityLevel, SourceType
from tunnel_qa.core.security import AccessContext
from tunnel_qa.db.session import Database


def test_corpus_importer_loads_page_chunks_and_is_idempotent(tmp_path):
    corpus = tmp_path / "corpus"
    processed = tmp_path / "processed"
    source_dir = corpus / "标准"
    source_dir.mkdir(parents=True)
    (processed / "documents").mkdir(parents=True)
    raw = b"test-pdf-content"
    original = source_dir / "TB 99999-2026 测试规范.pdf"
    original.write_bytes(raw)
    checksum = hashlib.sha256(raw).hexdigest()
    source_id = f"SRC-STD-{checksum[:12].upper()}"
    manifest = {
        "source_id": source_id,
        "path": str(original),
        "relative_path": "标准/TB 99999-2026 测试规范.pdf",
        "category": "标准",
        "source_type": "standard",
        "title": "测试规范",
        "version": "TB 99999-2026",
        "effective_date": "2026-01-01",
        "owner_org": "测试单位",
        "security_level": "internal",
        "size_bytes": len(raw),
        "checksum_sha256": checksum,
        "media_type": "application/pdf",
        "page_count": 2,
        "encrypted": False,
        "sample_page_chars": [20, 20],
        "extraction_status": "ready",
        "quality_score": 1.0,
        "issues": [],
        "sensitive_flags": [],
    }
    (processed / "manifest.jsonl").write_text(
        json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (processed / "documents" / f"{source_id}.txt").write_text(
        "[第1页]\n隧道施工安全要求和通风监测内容足够用于切片。\n\n"
        "[第2页]\n掌子面风险识别与应急处置要求足够用于切片。",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'corpus.db').as_posix()}",
        original_content_database_url=f"sqlite:///{tmp_path / 'originals.db'}",
        chunk_content_database_url=f"sqlite:///{tmp_path / 'chunks.db'}",
        storage_path=tmp_path / "storage",
        chunk_size=120,
        chunk_overlap=20,
    )
    first = CorpusDatabaseImporter(corpus, processed, settings).run(publish_knowledge=False)
    second = CorpusDatabaseImporter(corpus, processed, settings).run(publish_knowledge=False)

    assert first["imported_files"] == 1
    assert first["missing_files"] == 0
    assert first["imported_chunks"] == 2
    assert second["imported_files"] == 0
    assert second["skipped_files"] == 1

    register_models()
    database = Database(settings)
    session = database.session_factory()
    document = session.query(SourceDocument).one()
    assert document.status == ProcessingStatus.PUBLISHED
    assert session.query(Chunk).count() == 2
    assert {chunk.page_number for chunk in session.query(Chunk).all()} == {1, 2}
    assert Path(document.storage_uri).exists()
    store = DocumentContentStore(settings)
    assert store.stats()["original_documents"] == 1
    assert store.stats()["chunks"] == 2
    with store._connect(store.original_path) as connection:
        assert connection.execute("SELECT original_blob FROM original_documents").fetchone()[0] == raw
    assert store.get_chunk_texts(chunk.id for chunk in session.query(Chunk)) == {
        chunk.id: chunk.text for chunk in session.query(Chunk)
    }
    # The archive must not share an inode with the mutable input file.
    original.write_bytes(b"edited-input")
    assert Path(document.storage_uri).read_bytes() == raw
    session.close()
    database.engine.dispose()


def batch_input(tmp_path, settings, text):
    corpus, processed = tmp_path / "input", tmp_path / "processed"
    corpus.mkdir()
    (processed / "documents").mkdir(parents=True)
    raw = text.encode()
    original = corpus / "test.md"
    original.write_bytes(raw)
    checksum = hashlib.sha256(raw).hexdigest()
    item = {
        "source_id": "SRC-BATCH-TEST", "path": str(original), "relative_path": "test.md",
        "category": "论文", "source_type": "research", "title": "批量入库测试",
        "version": "V1", "effective_date": "2026-10-17", "owner_org": "测试单位",
        "size_bytes": len(raw), "checksum_sha256": checksum, "media_type": "text/markdown",
        "extraction_status": "ready", "quality_score": 1.0,
    }
    (processed / "manifest.jsonl").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    (processed / "documents" / "SRC-BATCH-TEST.txt").write_text(text, encoding="utf-8")
    file_settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'main.db'}"})
    return CorpusDatabaseImporter(corpus, processed, file_settings), original


@pytest.mark.parametrize("text", ["参数 | 说明\n阈值 | " + "测试" * 6000, "# 参考文献\n[1] 某某. 参考文献内容. 2026.", "很短"],
                         ids=["oversized-table", "references-only", "too-short"])
def test_batch_and_preview_reject_the_same_invalid_content(tmp_path, settings, text):
    importer, _ = batch_input(tmp_path, settings, text)
    result = importer.run(publish_knowledge=False)
    assert result["imported_files"] == 0
    assert result["missing"][0]["reason"] == "data_quality_failed"
    database = Database(importer.settings)
    with database.session_factory() as session:
        service = DataIngestionService(session, importer.settings)
        preview = service.preview_document(filename="test.md", content=text.encode(), source_type=SourceType.RESEARCH,
            access=AccessContext("test", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL))
        assert not preview["accepted"]
        assert session.scalars(select(SourceDocument)).all() == []
        assert session.scalars(select(Chunk)).all() == []
        assert service.content_store.stats()["original_documents"] == 0
        assert service.content_store.stats()["chunks"] == 0
    database.engine.dispose()


def test_batch_mirror_failure_does_not_publish_incomplete_document(tmp_path, settings, monkeypatch):
    importer, _ = batch_input(tmp_path, settings, "高地温影响作业人员，隧道施工中应及时检测环境温度并进行通风。")
    def fail_save(*args, **kwargs):
        raise AppError("CONTENT_STORE_FAILED", "模拟镜像写入失败")
    monkeypatch.setattr(DocumentContentStore, "save_document", fail_save)
    report = importer.run(publish_knowledge=False)
    assert report["imported_files"] == 0
    assert report["missing"][0]["reason"] == "CONTENT_STORE_FAILED"
    database = Database(importer.settings)
    with database.session_factory() as session:
        assert session.scalars(select(SourceDocument)).all() == []
        assert session.scalars(select(Chunk)).all() == []
    database.engine.dispose()


def test_batch_rejects_original_changed_after_manifest(tmp_path, settings):
    importer, original = batch_input(tmp_path, settings, "高地温影响作业人员，隧道施工中应及时检测环境温度并进行通风。")
    original.write_bytes(b"changed")
    report = importer.run(publish_knowledge=False)
    assert report["imported_files"] == 0
    assert report["missing"][0]["reason"] == "ORIGINAL_CHECKSUM_MISMATCH"
    assert DocumentContentStore(importer.settings).stats()["original_documents"] == 0
