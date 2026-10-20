from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from tunnel_qa.core.enums import JobStatus, QualityFlag, SecurityLevel, SourceType
from tunnel_qa.schemas.common import ORMModel


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: SourceType
    connection_type: str = "upload"
    owner: str = Field(min_length=1, max_length=200)
    security_level: SecurityLevel
    sync_policy: dict[str, Any] = Field(default_factory=dict)


class DataSourceRead(ORMModel):
    id: str
    name: str
    source_type: SourceType
    connection_type: str
    owner: str
    security_level: SecurityLevel
    sync_policy: dict[str, Any]
    enabled: bool


class DocumentMetadata(BaseModel):
    source_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    source_type: SourceType
    project_site: str | None = None
    version: str = Field(min_length=1, max_length=100)
    effective_date: str = Field(min_length=1, max_length=30)
    security_level: SecurityLevel
    owner_org: str = Field(min_length=1, max_length=300)
    content_location: str = Field(min_length=1, max_length=500)
    extra: dict[str, Any] = Field(default_factory=dict)


class QualityReport(BaseModel):
    score: float = Field(ge=0, le=1)
    passed: bool
    checks: dict[str, bool]
    issues: list[str] = Field(default_factory=list)
    chunk_count: int = 0


class ChunkingConfig(BaseModel):
    chunk_size: int = Field(default=800, ge=100, le=5000)
    chunk_overlap: int = Field(default=120, ge=0, le=1000)
    chunk_model: str = Field(default="key-v2", min_length=1, max_length=200)

    @model_validator(mode="after")
    def overlap_must_be_smaller_than_chunk(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("overlap 必须小于切块大小")
        return self


class ImportResult(BaseModel):
    job_id: str
    document_id: str | None = None
    status: JobStatus
    accepted: bool
    idempotent_replay: bool = False
    quality: QualityReport | None = None
    chunking: ChunkingConfig | None = None
    processing: dict[str, Any] | None = None
    validation_errors: list[str] = Field(default_factory=list)


class MonitoringInput(BaseModel):
    sensor_id: str = Field(min_length=1)
    timestamp: datetime
    metric_code: str = Field(min_length=1)
    value: float | None
    unit: str = Field(min_length=1)
    quality_flag: QualityFlag = QualityFlag.NORMAL
    location: dict[str, Any]
    source_system: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp 必须包含时区")
        return value
