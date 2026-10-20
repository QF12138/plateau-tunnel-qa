from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import select

from scripts.rechunk_corpus import make_plan, apply_plan, fingerprint, isolate_review_pages
from test_knowledge import prepare_document, access
from tunnel_qa.core.enums import OntologyDomain, ProcessingStatus, ReviewStatus
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.data.schemas import ChunkingConfig
from tunnel_qa.db.session import Database
from tunnel_qa.knowledge.models import KnowledgeEntity, KnowledgeEvidenceLink
from tunnel_qa.knowledge.schemas import ManualEntityCreate
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.retrieval.service import HybridRetrievalService
from tunnel_qa.data.knowledge_chunking import prepare_knowledge_chunks


def test_quarantined_scanned_page_is_retained_for_review_but_not_retrieval():
    raw = "[第1页]\n支护前必须进行\n[第2页]\n表1\n错误数值8?7\n[第3页]\n围岩检查。"
    text, isolated = isolate_review_pages(raw, {2: ["legacy_ocr_numeric_table_requires_review"]})
    assert isolated[0]["page"] == 2 and "8?7" in isolated[0]["extracted_text"]
    assert "8?7" not in text and "[第3页]" in text
    chunks = prepare_knowledge_chunks("test.pdf", text, 800, 120).chunks
    assert chunks and all(not (c.page_start <= 2 <= c.page_end) for c in chunks)
    assert "支护前必须进行" in chunks[0].text


def migration_fixture(settings, tmp_path):
    file_settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'main.db'}"})
    database = Database(file_settings)
    session, document_id = prepare_document(database, file_settings)
    document = session.get(SourceDocument, document_id)
    document.status = ProcessingStatus.PUBLISHED
    knowledge = KnowledgeService(session)
    knowledge.bootstrap_ontology(access())
    knowledge.extract_candidates(document_id, access())
    manual = knowledge.create_manual_entity(ManualEntityCreate(
        standard_name="人工维护节点", domain=OntologyDomain.ENVIRONMENT,
        properties={"说明": "必须原样保留"}, aliases=["手工别名"],
    ), access())
    session.commit()
    session.close()
    return file_settings, database, document_id, manual.id


def test_rechunk_retains_original_old_chunks_manual_facts_and_switches_retrieval(settings, tmp_path):
    config, database, document_id, manual_id = migration_fixture(settings, tmp_path)
    with database.session_factory() as session:
        document = session.get(SourceDocument, document_id)
        parsed = document.parsed_text
        old = session.scalars(select(Chunk).where(Chunk.document_id == document_id)).all()
        old_hash, old_ids = fingerprint(old), {chunk.id for chunk in old}
    store = DocumentContentStore(config)
    with store._connect(store.original_path) as connection:
        original_before = tuple(connection.execute("SELECT * FROM original_documents").fetchone())
    plan = make_plan(config, ChunkingConfig(chunk_size=150, chunk_overlap=0))
    result = apply_plan(config, plan)
    assert result["documents_rechunked"] == 1
    with database.session_factory() as session:
        document = session.get(SourceDocument, document_id)
        assert document.processing_version == 2
        assert document.parsed_text == parsed
        old = session.scalars(select(Chunk).where(Chunk.id.in_(old_ids))).all()
        assert fingerprint(old) == old_hash
        current = session.scalars(select(Chunk).where(Chunk.document_id == document_id, Chunk.processing_version == 2)).all()
        assert current and not old_ids.intersection(chunk.id for chunk in current)
        assert {hit.chunk_id for hit in HybridRetrievalService(session, store).search("高地温", access()).hits}.isdisjoint(old_ids)
        manual = session.get(KnowledgeEntity, manual_id)
        assert manual.standard_name == "人工维护节点" and manual.properties == {"说明": "必须原样保留"}
        assert manual.status == ReviewStatus.PUBLISHED and manual.aliases == ["手工别名"]
        assert document.metadata_extra["processing_history"]["1"]["chunking"]["chunk_model"] == "key-v2"
        assert store.get_chunk_texts(chunk.id for chunk in [*old, *current]) == {chunk.id: chunk.text for chunk in [*old, *current]}
    with store._connect(store.original_path) as connection:
        assert tuple(connection.execute("SELECT * FROM original_documents").fetchone()) == original_before
    with pytest.raises(ValueError, match="version changed"):
        apply_plan(config, plan)
    database.engine.dispose()


def test_rechunk_rejects_stale_plan_before_writing(settings, tmp_path):
    config, database, document_id, _ = migration_fixture(settings, tmp_path)
    plan = make_plan(config, ChunkingConfig())
    with database.session_factory() as session:
        chunk = session.scalar(select(Chunk).where(Chunk.document_id == document_id))
        chunk.text += "后续修订"
        session.commit()
    with pytest.raises(ValueError, match="chunks changed"):
        apply_plan(config, plan)
    with database.session_factory() as session:
        assert session.get(SourceDocument, document_id).processing_version == 1
        assert session.scalars(select(Chunk).where(Chunk.processing_version == 2)).all() == []
    database.engine.dispose()


def test_old_mirror_schema_upgrades_and_supports_repeated_ordinals(settings):
    path = Path(settings.chunk_content_database_url.removeprefix("sqlite:///"))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""CREATE TABLE document_chunks (
            chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            text TEXT NOT NULL, char_count INTEGER NOT NULL, chunk_size INTEGER NOT NULL,
            chunk_overlap INTEGER NOT NULL, chunk_model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(document_id, ordinal))""")
        connection.execute("INSERT INTO document_chunks(chunk_id,document_id,ordinal,text,char_count,chunk_size,chunk_overlap,chunk_model) VALUES ('old','doc',1,'旧块',2,800,120,'legacy')")
        connection.commit()
    store = DocumentContentStore(settings)
    with store._connect(path) as connection:
        assert connection.execute("SELECT processing_version,text FROM document_chunks WHERE chunk_id='old'").fetchone()[0] == 1
        connection.execute("INSERT INTO document_chunks(chunk_id,document_id,processing_version,ordinal,text,char_count,chunk_size,chunk_overlap,chunk_model) VALUES ('new','doc',2,1,'新块',2,800,120,'key-v2')")
    DocumentContentStore(settings)
    assert store.get_chunk_texts(["old", "new"]) == {"old": "旧块", "new": "新块"}


def test_rechunk_rejects_changed_published_document_set(settings, tmp_path):
    config, database, document_id, _ = migration_fixture(settings, tmp_path)
    plan = make_plan(config, ChunkingConfig())
    with database.session_factory() as session:
        session.get(SourceDocument, document_id).status = ProcessingStatus.ARCHIVED
        session.commit()
    with pytest.raises(ValueError, match="document set changed"):
        apply_plan(config, plan)
    with database.session_factory() as session:
        assert session.get(SourceDocument, document_id).processing_version == 1
    database.engine.dispose()


def test_rechunk_does_not_revive_rejected_facts(settings, tmp_path):
    config, database, document_id, _ = migration_fixture(settings, tmp_path)
    with database.session_factory() as session:
        rejected = session.scalar(select(KnowledgeEntity).where(KnowledgeEntity.standard_name == "高地温"))
        rejected.status = ReviewStatus.REJECTED
        session.commit()
    apply_plan(config, make_plan(config, ChunkingConfig()))
    with database.session_factory() as session:
        rows = session.scalars(select(KnowledgeEntity).where(KnowledgeEntity.standard_name == "高地温")).all()
        assert rows and all(row.status in {ReviewStatus.REJECTED, ReviewStatus.ARCHIVED} for row in rows)
    database.engine.dispose()
