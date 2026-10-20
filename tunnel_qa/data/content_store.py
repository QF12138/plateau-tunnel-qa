"""Physical content stores for immutable originals and searchable chunks.

The operational SQLAlchemy database keeps workflow metadata and relationships.
Document payloads are additionally persisted into two independent SQLite files:
one for immutable original files and parsed text, and one for chunk payloads.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from tunnel_qa.core.config import Settings
from tunnel_qa.core.errors import AppError
from tunnel_qa.data.models import Chunk, SourceDocument, SYSTEM_MANAGED_SOURCE_IDS
from tunnel_qa.data.schemas import ChunkingConfig
from tunnel_qa.data.processing_metadata import saved_chunking


def _sqlite_path(database_url: str, label: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        raise RuntimeError(f"{label} 必须配置为文件型 SQLite 数据库")
    path = Path(database_url.removeprefix(prefix)).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class DocumentContentStore:
    """Owns the two physical databases used for document content."""

    def __init__(self, settings: Settings) -> None:
        self.original_path = _sqlite_path(
            settings.original_content_database_url, "原文内容库"
        )
        self.chunk_path = _sqlite_path(settings.chunk_content_database_url, "切片内容库")
        if self.original_path == self.chunk_path:
            raise RuntimeError("原文内容库与切片内容库必须使用不同文件")
        self.initialize()

    @contextmanager
    def _connect(self, path: Path) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(path, timeout=60)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 60000")
            with connection:
                yield connection
        finally:
            # sqlite3's transaction context does not close the connection.
            connection.close()

    def initialize(self) -> None:
        with self._connect(self.original_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS original_documents (
                    document_id TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL UNIQUE,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    storage_uri TEXT NOT NULL,
                    original_blob BLOB NOT NULL,
                    original_size INTEGER NOT NULL,
                    parsed_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        with self._connect(self.chunk_path) as connection:
            # The first mirror schema keyed ordinals without a processing
            # version. Upgrade in a transaction before retaining old revisions.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(document_chunks)")}
            if columns and "processing_version" not in columns:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""CREATE TABLE document_chunks_v2 (
                    chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    processing_version INTEGER NOT NULL DEFAULT 1,
                    ordinal INTEGER NOT NULL, text TEXT NOT NULL, char_count INTEGER NOT NULL,
                    chunk_size INTEGER NOT NULL, chunk_overlap INTEGER NOT NULL,
                    chunk_model TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(document_id, processing_version, ordinal))""")
                connection.execute("""INSERT INTO document_chunks_v2
                    (chunk_id, document_id, processing_version, ordinal, text, char_count,
                     chunk_size, chunk_overlap, chunk_model, created_at)
                    SELECT chunk_id, document_id, 1, ordinal, text, char_count,
                           chunk_size, chunk_overlap, chunk_model, created_at FROM document_chunks""")
                connection.execute("DROP TABLE document_chunks")
                connection.execute("ALTER TABLE document_chunks_v2 RENAME TO document_chunks")
                connection.commit()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    processing_version INTEGER NOT NULL DEFAULT 1,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    chunk_size INTEGER NOT NULL,
                    chunk_overlap INTEGER NOT NULL,
                    chunk_model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(document_id, processing_version, ordinal)
                );
                CREATE INDEX IF NOT EXISTS ix_document_chunks_document
                    ON document_chunks(document_id);
                """
            )

    def save_document(
        self,
        document: SourceDocument,
        chunks: Iterable[Chunk],
        *,
        original_content: bytes,
        chunking: ChunkingConfig,
    ) -> None:
        """Fill missing payloads without replacing or deleting existing records.

        Both files share one attached transaction. A conflict rolls back only
        this attempt, including a newly inserted original, not earlier data.
        """
        try:
            with self._connect(self.original_path) as connection:
                connection.execute("ATTACH DATABASE ? AS chunk_store", (str(self.chunk_path),))
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT checksum, original_blob, parsed_text FROM original_documents WHERE document_id = ?",
                    (document.id,),
                ).fetchone()
                if existing and (existing["checksum"] != document.checksum
                                 or existing["original_blob"] != original_content
                                 or existing["parsed_text"] != document.parsed_text):
                    raise AppError("CONTENT_STORE_CONFLICT", "原文镜像与待保存内容不一致，已保留原有记录")
                if not existing:
                    connection.execute(
                        """
                        INSERT INTO original_documents (
                            document_id, checksum, original_filename, mime_type, storage_uri,
                            original_blob, original_size, parsed_text, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document.id,
                            document.checksum,
                            document.original_filename,
                            document.mime_type,
                            document.storage_uri,
                            sqlite3.Binary(original_content),
                            len(original_content),
                            document.parsed_text,
                            json.dumps(document.metadata_extra, ensure_ascii=False, default=str),
                        ),
                    )
                rows = list(chunks)
                for version in sorted({chunk.processing_version for chunk in rows}):
                    config = saved_chunking(document.metadata_extra, version) or chunking
                    self._save_chunks(connection, document.id,
                        [chunk for chunk in rows if chunk.processing_version == version],
                        config, "chunk_store.document_chunks")
        except sqlite3.Error as exc:
            raise AppError(
                "CONTENT_STORE_FAILED",
                "原文或切片内容库写入失败",
                details={"reason": str(exc)},
            ) from exc

    @staticmethod
    def _save_chunks(connection, document_id, chunks, chunking, table="document_chunks") -> int:
        added = 0
        for chunk in chunks:
            existing = connection.execute(
                f"SELECT document_id, ordinal, text, char_count, processing_version FROM {table} WHERE chunk_id = ?",
                (chunk.id,),
            ).fetchone()
            if existing:
                if tuple(existing) != (document_id, chunk.ordinal, chunk.text, chunk.char_count, chunk.processing_version):
                    raise AppError("CONTENT_STORE_CONFLICT", "分块镜像与待保存内容不一致，已保留原有记录")
                continue
            connection.execute(
                f"INSERT INTO {table} (chunk_id, document_id, ordinal, text, char_count, "
                "chunk_size, chunk_overlap, chunk_model, processing_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk.id, document_id, chunk.ordinal, chunk.text, chunk.char_count,
                 chunking.chunk_size, chunking.chunk_overlap, chunking.chunk_model, chunk.processing_version),
            )
            added += 1
        return added

    def save_revision(self, document: SourceDocument, chunks: Iterable[Chunk], chunking: ChunkingConfig) -> int:
        """Append a revision without changing the immutable original snapshot."""
        with self._connect(self.chunk_path) as connection:
            return self._save_chunks(connection, document.id, chunks, chunking)

    def backfill(self, session: Session) -> dict[str, int]:
        """Idempotently mirror legacy operational rows into both content stores."""
        source_added = 0
        chunk_added = 0
        documents = session.scalars(
            select(SourceDocument)
            .where(SourceDocument.source_id.not_in(SYSTEM_MANAGED_SOURCE_IDS))
            .order_by(SourceDocument.created_at)
        ).all()
        valid_document_ids = [document.id for document in documents]
        # The operational database is authoritative. Remove rows left by an older
        # database instance or an interrupted test before reconciling checksums.
        for path, table in (
            (self.original_path, "original_documents"),
            (self.chunk_path, "document_chunks"),
        ):
            with self._connect(path) as connection:
                if valid_document_ids:
                    placeholders = ",".join("?" for _ in valid_document_ids)
                    connection.execute(
                        f"DELETE FROM {table} WHERE document_id NOT IN ({placeholders})",
                        valid_document_ids,
                    )
                else:
                    connection.execute(f"DELETE FROM {table}")
        for document in documents:
            chunking = saved_chunking(document.metadata_extra) or ChunkingConfig(chunk_model="legacy-structural-v1")
            chunks = session.scalars(
                select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
            ).all()
            with self._connect(self.chunk_path) as connection:
                existing_ids = {row[0] for row in connection.execute(
                    "SELECT chunk_id FROM document_chunks WHERE document_id = ?", (document.id,)
                )}
            with self._connect(self.original_path) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM original_documents WHERE document_id = ?", (document.id,)
                ).fetchone()
            if not exists:
                storage_path = Path(document.storage_uri)
                if not storage_path.is_file():
                    continue
                original_content = storage_path.read_bytes()
                # Preserve each historical revision's own strategy metadata.
                self.save_document(document, chunks, original_content=original_content, chunking=chunking)
                source_added += 1
                chunk_added += sum(chunk.id not in existing_ids for chunk in chunks)
                continue

            with self._connect(self.chunk_path) as connection:
                missing = [chunk for chunk in chunks if chunk.id not in existing_ids]
                for version in sorted({chunk.processing_version for chunk in missing}):
                    config = saved_chunking(document.metadata_extra, version) or ChunkingConfig(chunk_model="legacy-structural-v1")
                    chunk_added += self._save_chunks(connection, document.id,
                        [chunk for chunk in missing if chunk.processing_version == version], config)
        return {"original_documents_added": source_added, "chunks_added": chunk_added}

    def get_chunk_texts(self, chunk_ids: Iterable[str]) -> dict[str, str]:
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect(self.chunk_path) as connection:
            rows = connection.execute(
                f"SELECT chunk_id, text FROM document_chunks WHERE chunk_id IN ({placeholders})",
                ids,
            ).fetchall()
        return {row["chunk_id"]: row["text"] for row in rows}

    def delete_document(self, document_id: str, *, strict: bool = False) -> None:
        errors: list[str] = []
        for path, table in (
            (self.original_path, "original_documents"),
            (self.chunk_path, "document_chunks"),
        ):
            try:
                with self._connect(path) as connection:
                    connection.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
            except sqlite3.Error as exc:
                # Best-effort compensation; the primary error remains more useful to the caller.
                errors.append(f"{path.name}: {exc}")
        if strict and errors:
            raise AppError(
                "CONTENT_DELETE_FAILED",
                "资料元数据已删除，但内容库清理失败",
                details={"errors": errors},
            )

    def delete_chunk(self, chunk_id: str, *, strict: bool = False) -> None:
        """Delete one chunk payload without touching the immutable original."""
        try:
            with self._connect(self.chunk_path) as connection:
                connection.execute(
                    "DELETE FROM document_chunks WHERE chunk_id = ?", (chunk_id,)
                )
        except sqlite3.Error as exc:
            if strict:
                raise AppError(
                    "CONTENT_DELETE_FAILED",
                    "分块元数据已删除，但切片内容库清理失败",
                    details={"errors": [f"{self.chunk_path.name}: {exc}"]},
                ) from exc

    def stats(self) -> dict[str, int | str]:
        with self._connect(self.original_path) as connection:
            original = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(original_size), 0) AS bytes FROM original_documents"
            ).fetchone()
        with self._connect(self.chunk_path) as connection:
            chunk_count = connection.execute(
                "SELECT COUNT(*) FROM document_chunks"
            ).fetchone()[0]
        return {
            "original_documents": int(original["count"]),
            "original_bytes": int(original["bytes"]),
            "chunks": int(chunk_count),
            "original_database": str(self.original_path),
            "chunk_database": str(self.chunk_path),
        }
