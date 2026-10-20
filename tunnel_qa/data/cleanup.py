"""Retryable, idempotent content cleanup after the metadata transaction commits."""
import sqlite3
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tunnel_qa.core.config import Settings
from tunnel_qa.core.errors import AppError
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.models import ContentCleanupJob, SourceDocument


def process_cleanup(session: Session, store: DocumentContentStore, settings: Settings,
                    job: ContentCleanupJob) -> bool:
    if job.status == "complete":
        return True
    # Import also flushes its job before writing the checksum-based file. The
    # primary write lock prevents a retry from removing a re-imported file.
    job.attempts += 1
    session.flush()
    try:
        if job.target_kind == "document":
            store.delete_document(job.target_id, strict=True)
            if job.storage_uri:
                path = Path(job.storage_uri).resolve()
                root = settings.storage_path.resolve()
                reused = session.scalar(select(SourceDocument.id).where(
                    SourceDocument.storage_uri == job.storage_uri).limit(1))
                if not reused:
                    if not path.is_relative_to(root) or path == root:
                        raise AppError("UNSAFE_CLEANUP_PATH", "原文路径不在受管目录内")
                    path.unlink(missing_ok=True)
        elif job.target_kind == "chunk":
            store.delete_chunk(job.target_id, strict=True)
        else:
            raise AppError("INVALID_CLEANUP_TARGET", "未知清理任务类型")
    except (AppError, sqlite3.Error, OSError) as exc:
        job.last_error = getattr(exc, "error_code", type(exc).__name__)
        job.status = "pending"
        session.commit()
        return False
    job.status = "complete"
    job.last_error = None
    session.commit()
    return True


def retry_cleanup(session: Session, store: DocumentContentStore, settings: Settings,
                  limit: int = 100) -> dict[str, int]:
    jobs = session.scalars(select(ContentCleanupJob).where(
        ContentCleanupJob.status == "pending").order_by(ContentCleanupJob.updated_at).limit(limit)).all()
    completed = sum(process_cleanup(session, store, settings, job) for job in jobs)
    return {"attempted": len(jobs), "completed": completed, "still_pending": len(jobs) - completed}
