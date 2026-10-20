from pathlib import Path

import pytest
from sqlalchemy import select

from test_data_ingestion import admin_access, build_service, create_source, metadata
from test_knowledge_ingestion import build_client, headers, post_upload, row_counts, SOURCE
from tunnel_qa.core.errors import AppError
from tunnel_qa.data.models import Chunk, SourceDocument
from tunnel_qa.data.processing_metadata import saved_chunking, saved_quality
from tunnel_qa.data.schemas import ChunkingConfig


def prepared_document(database, settings):
    session, service = build_service(database, settings)
    source = create_source(service)
    imported = service.import_document(
        data_source_id=source.id, filename="recovery.txt",
        content=("隧道施工应持续监测围岩变化，并检查通风设备。" * 10).encode(),
        metadata=metadata(), idempotency_key="recovery", access=admin_access(),
    )
    assert imported.accepted
    document = session.get(SourceDocument, imported.document_id)
    chunks = session.scalars(select(Chunk).where(Chunk.document_id == document.id)).all()
    return session, service.content_store, document, chunks


def test_backfill_recovers_missing_original_without_deleting_existing_chunks(database, settings):
    session, store, document, chunks = prepared_document(database, settings)
    before = store.get_chunk_texts(chunk.id for chunk in chunks)
    with store._connect(store.original_path) as connection:
        connection.execute("DELETE FROM original_documents WHERE document_id = ?", (document.id,))
    assert store.backfill(session) == {"original_documents_added": 1, "chunks_added": 0}
    assert store.get_chunk_texts(before) == before
    assert store.backfill(session) == {"original_documents_added": 0, "chunks_added": 0}
    session.close()


def test_conflicting_chunk_rolls_back_new_original_and_keeps_existing_payload(database, settings):
    session, store, document, chunks = prepared_document(database, settings)
    with store._connect(store.original_path) as connection:
        connection.execute("DELETE FROM original_documents WHERE document_id = ?", (document.id,))
    with store._connect(store.chunk_path) as connection:
        connection.execute("UPDATE document_chunks SET text = 'existing payload' WHERE chunk_id = ?", (chunks[0].id,))
    before = store.get_chunk_texts(chunk.id for chunk in chunks)
    with pytest.raises(AppError, match="分块镜像"):
        store.backfill(session)
    assert store.stats()["original_documents"] == 0
    assert store.get_chunk_texts(before) == before
    session.close()


def test_duplicate_save_is_safe_and_conflicting_original_is_not_overwritten(database, settings):
    session, store, document, chunks = prepared_document(database, settings)
    before = store.stats()
    content = Path(document.storage_uri).read_bytes()
    store.save_document(document, chunks, original_content=content,
                        chunking=ChunkingConfig(chunk_size=200, chunk_overlap=20))
    assert store.stats() == before
    with pytest.raises(AppError, match="原文镜像"):
        store.save_document(document, chunks, original_content=b"conflicting bytes", chunking=ChunkingConfig())
    assert store.stats() == before
    assert store.get_chunk_texts(chunk.id for chunk in chunks) == {chunk.id: chunk.text for chunk in chunks}
    session.close()


def test_sqlite_failure_rolls_back_only_new_rows_in_both_stores(database, settings):
    session, store, document, chunks = prepared_document(database, settings)
    assert len(chunks) > 1
    with store._connect(store.original_path) as connection:
        connection.execute("DELETE FROM original_documents WHERE document_id = ?", (document.id,))
    with store._connect(store.chunk_path) as connection:
        connection.execute("DELETE FROM document_chunks WHERE chunk_id != ?", (chunks[0].id,))
        connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON document_chunks BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
    before = store.get_chunk_texts(chunk.id for chunk in chunks)
    with pytest.raises(AppError) as caught:
        store.backfill(session)
    assert caught.value.error_code == "CONTENT_STORE_FAILED"
    assert store.stats()["original_documents"] == 0
    assert store.get_chunk_texts(chunk.id for chunk in chunks) == before
    session.close()


LEGACY_METADATA = [None, "legacy", ["legacy"], 1, {},
    {"chunking": None}, {"chunking": "legacy"}, {"chunking": [1]},
    {"chunking": {"chunk_size": 0}}, {"chunking": {"chunk_overlap": 900}},
    {"chunking": {"chunk_model": None}}, {"ingestion_quality": {"score": "unknown"}}]


@pytest.mark.parametrize("legacy", LEGACY_METADATA)
@pytest.mark.parametrize("missing_original", [False, True])
def test_legacy_metadata_can_backfill_without_rechunking(database, settings, legacy, missing_original):
    session, store, document, chunks = prepared_document(database, settings)
    document.metadata_extra = legacy
    session.commit()
    with store._connect(store.chunk_path) as connection:
        connection.execute("DELETE FROM document_chunks WHERE document_id = ?", (document.id,))
    if missing_original:
        with store._connect(store.original_path) as connection:
            connection.execute("DELETE FROM original_documents WHERE document_id = ?", (document.id,))
    report = store.backfill(session)
    assert report["chunks_added"] == len(chunks)
    assert store.get_chunk_texts(chunk.id for chunk in chunks) == {chunk.id: chunk.text for chunk in chunks}
    with store._connect(store.chunk_path) as connection:
        assert connection.execute("SELECT DISTINCT chunk_model FROM document_chunks").fetchone()[0] == "legacy-structural-v1"
    assert document.metadata_extra == legacy
    session.close()


@pytest.mark.parametrize("legacy", LEGACY_METADATA)
def test_legacy_metadata_does_not_break_listing_or_idempotent_replay(database, settings, legacy):
    client, _, _ = build_client(database, settings)
    identity = headers(**{"Idempotency-Key": "legacy-replay"})
    first = post_upload(client, SOURCE, identity=identity)
    assert first.json()["accepted"]
    with database.session_factory() as session:
        document = session.get(SourceDocument, first.json()["document_id"])
        document.metadata_extra = legacy
        session.commit()
    before = row_counts(database)
    listing = client.get("/api/v1/data/documents", headers=headers())
    assert listing.status_code == 200
    replay = post_upload(client, SOURCE, identity=identity)
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"]
    assert replay.json()["chunking"] is None
    assert replay.json()["quality"] is None
    assert row_counts(database) == before


def test_partial_historical_config_is_never_mislabeled_as_key_v2():
    assert saved_chunking({"chunking": {"chunk_size": 300}}).chunk_model == "legacy-structural-v1"
    assert saved_chunking({"chunking": {"chunk_model": "key-v2"}}).chunk_model == "key-v2"
    assert saved_quality({"ingestion_quality": {"score": 1, "passed": True, "checks": {}}}).passed
