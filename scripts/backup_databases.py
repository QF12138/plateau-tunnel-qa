"""Consistent SQLite snapshots and non-destructive recovery to a NEW directory."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

from tunnel_qa.core.config import Settings

NAMES = ("tunnel_qa.db", "original_documents.db", "document_chunks.db")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def database_paths(settings: Settings) -> list[Path]:
    paths = []
    for url in (settings.database_url, settings.original_content_database_url, settings.chunk_content_database_url):
        if not url.startswith("sqlite:///") or url.endswith(":memory:"):
            raise ValueError("Backup requires three file-based SQLite databases")
        path = Path(url.removeprefix("sqlite:///")).resolve()
        if not path.is_file():
            raise ValueError("A configured database does not exist")
        paths.append(path)
    if len(set(paths)) != 3:
        raise ValueError("The three database paths must be distinct")
    return paths


def check_databases(folder: Path) -> dict[str, int]:
    with ExitStack() as stack:
        connections = [stack.enter_context(closing(sqlite3.connect((folder / name).as_uri() + "?mode=ro", uri=True))) for name in NAMES]
        for connection in connections:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("SQLite foreign key check failed")
        main, originals, chunks = connections
        expected_docs = {row[0] for row in main.execute("SELECT id FROM source_documents WHERE source_id != 'SYS-GRAPH-MANUAL'")}
        stored_docs = {row[0] for row in originals.execute("SELECT document_id FROM original_documents")}
        expected_chunks = set(main.execute("SELECT c.id, c.document_id FROM chunks c JOIN source_documents d ON c.document_id=d.id WHERE d.source_id != 'SYS-GRAPH-MANUAL'"))
        stored_chunks = set(chunks.execute("SELECT chunk_id, document_id FROM document_chunks"))
        # A committed deletion may still be awaiting its durable cleanup task.
        has_outbox = main.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_cleanup_jobs'").fetchone()
        pending = main.execute("SELECT target_kind, target_id FROM content_cleanup_jobs WHERE status='pending'").fetchall() if has_outbox else []
        pending_docs = {row[1] for row in pending if row[0] == "document"}
        pending_chunks = {row[1] for row in pending if row[0] == "chunk"}
        if expected_docs != stored_docs - pending_docs:
            raise ValueError("Original-document mirror does not match metadata")
        remaining_chunks = {(cid, did) for cid, did in stored_chunks if cid not in pending_chunks and did not in pending_docs}
        if expected_chunks != remaining_chunks:
            raise ValueError("Chunk mirror does not match metadata")
        return {"documents": len(expected_docs), "chunks": len(expected_chunks), "pending_cleanup": len(pending)}


def backup(settings: Settings, destination: Path) -> dict:
    paths = database_paths(settings)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    # Lock in write order (primary -> originals -> chunks). Read-only connections
    # do the backup: sqlite backup on the lock-owning writer would wait on itself.
    with ExitStack() as stack:
        for path in paths:
            lock = stack.enter_context(closing(sqlite3.connect(path, timeout=15)))
            lock.execute("BEGIN IMMEDIATE")
        for path, name in zip(paths, NAMES):
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(destination / name)) as target:
                source.backup(target)
    counts = check_databases(destination)
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "databases": {name: digest(destination / name) for name in NAMES}, "counts": counts}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify(folder: Path) -> dict:
    folder = folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest.get("databases", {})) != set(NAMES):
        raise ValueError("Invalid backup manifest")
    for name in NAMES:
        if (folder / name).is_symlink() or digest(folder / name) != manifest["databases"][name]:
            raise ValueError("Backup checksum mismatch")
    return check_databases(folder)


def restore(folder: Path, destination: Path) -> dict:
    counts = verify(folder)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in NAMES:
        shutil.copy2(folder / name, destination / name)
    storage = destination / "storage"
    storage.mkdir()
    with closing(sqlite3.connect(destination / NAMES[0])) as main, closing(sqlite3.connect(destination / NAMES[1])) as originals:
        active = {row[0] for row in main.execute("SELECT id FROM source_documents WHERE source_id != 'SYS-GRAPH-MANUAL'")}
        for doc_id, checksum, filename, blob in originals.execute("SELECT document_id, checksum, original_filename, original_blob FROM original_documents"):
            if doc_id not in active:
                continue
            if hashlib.sha256(blob).hexdigest() != checksum:
                raise ValueError("Original file checksum mismatch")
            suffix = Path(filename).suffix.lower()
            if len(suffix) > 15 or not suffix.replace('.', '').isalnum():
                suffix = ".bin"
            target = storage / (checksum + suffix)
            target.write_bytes(blob)
            main.execute("UPDATE source_documents SET storage_uri=? WHERE id=?", (str(target), doc_id))
            originals.execute("UPDATE original_documents SET storage_uri=? WHERE document_id=?", (str(target), doc_id))
        # Never allow restored pending cleanup jobs to touch old/live storage.
        if main.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_cleanup_jobs'").fetchone():
            main.execute("UPDATE content_cleanup_jobs SET storage_uri=NULL WHERE status='pending'")
        if main.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_sessions'").fetchone():
            main.execute("DELETE FROM login_sessions")
        main.commit()
        originals.commit()
    check_databases(destination)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup-to", type=Path)
    group.add_argument("--verify", type=Path)
    group.add_argument("--restore-from", type=Path)
    parser.add_argument("--restore-to", type=Path)
    args = parser.parse_args()
    if args.backup_to:
        result = backup(Settings(), args.backup_to)
    elif args.verify:
        result = verify(args.verify)
    else:
        if not args.restore_to:
            parser.error("--restore-to must be a new, empty destination")
        result = restore(args.restore_from.resolve(), args.restore_to)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
