from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tunnel_qa.core.enums import (
    JobStatus,
    ProcessingStatus,
    QualityFlag,
    SecurityLevel,
    SourceType,
)
from tunnel_qa.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


SYSTEM_MANAGED_SOURCE_IDS = frozenset({"SYS-GRAPH-MANUAL"})


class ContentCleanupJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Transactional outbox; survives deletion of its source document/chunk."""
    __tablename__ = "content_cleanup_jobs"

    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    storage_uri: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(100))
SYSTEM_MANAGED_SOURCE_NAMES = frozenset({"系统·图谱人工维护"})


class DataSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "data_sources"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    source_type: Mapped[SourceType] = mapped_column(String(40), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(30), default="upload", nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(String(20), nullable=False)
    sync_policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)


class ImportJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_import_job_idempotency"),)

    data_source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[JobStatus] = mapped_column(String(20), default=JobStatus.ACCEPTED, nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_score: Mapped[float | None] = mapped_column(Float)
    error_report: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.id"))

    data_source: Mapped[DataSource] = relationship()
    document: Mapped["SourceDocument | None"] = relationship(foreign_keys=[document_id])


class SourceDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "source_documents"

    source_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    data_source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(String(40), nullable=False)
    project_site: Mapped[str | None] = mapped_column(String(300))
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    effective_date: Mapped[str] = mapped_column(String(30), nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(String(20), nullable=False)
    owner_org: Mapped[str] = mapped_column(String(300), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    content_location: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    parsed_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[ProcessingStatus] = mapped_column(
        String(30), default=ProcessingStatus.RECEIVED, nullable=False
    )
    processing_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    metadata_extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Chunk.ordinal"
    )


class Chunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "processing_version", "ordinal", name="uq_chunk_version_order"),
    )

    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), nullable=False)
    processing_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    structure_path: Mapped[str] = mapped_column(String(1000), default="正文", nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    content_location: Mapped[str] = mapped_column(String(1000), nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(String(20), nullable=False)
    quality_status: Mapped[str] = mapped_column(String(30), default="qualified", nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    document: Mapped[SourceDocument] = relationship(back_populates="chunks")


class MonitoringRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "monitoring_records"
    __table_args__ = (
        UniqueConstraint(
            "sensor_id", "measured_at", "metric_code", name="uq_monitoring_sensor_time_metric"
        ),
    )

    sensor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(100), nullable=False)
    original_value: Mapped[float | None] = mapped_column(Float)
    cleaned_value: Mapped[float | None] = mapped_column(Float)
    original_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    canonical_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    conversion_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    quality_flag: Mapped[QualityFlag] = mapped_column(String(20), nullable=False)
    location: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_system: Mapped[str] = mapped_column(String(200), nullable=False)
    processing_details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
