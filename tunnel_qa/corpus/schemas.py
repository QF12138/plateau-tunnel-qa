from pydantic import BaseModel, Field

from tunnel_qa.core.enums import SecurityLevel, SourceType


class CorpusItem(BaseModel):
    source_id: str
    path: str
    relative_path: str
    category: str
    source_type: SourceType
    title: str
    author: str | None = None
    version: str
    effective_date: str
    owner_org: str
    security_level: SecurityLevel = SecurityLevel.INTERNAL
    size_bytes: int
    checksum_sha256: str
    media_type: str
    page_count: int | None = None
    encrypted: bool = False
    sample_page_chars: list[int] = Field(default_factory=list)
    extraction_status: str
    quality_score: float = Field(ge=0, le=1)
    issues: list[str] = Field(default_factory=list)
    sensitive_flags: list[str] = Field(default_factory=list)


class ProcessedDocument(BaseModel):
    source_id: str
    status: str
    text_path: str | None = None
    extracted_pages: int = 0
    empty_pages: int = 0
    text_chars: int = 0
    chunk_count: int = 0
    errors: list[str] = Field(default_factory=list)


class CorpusSummary(BaseModel):
    input_root: str
    output_root: str
    total_files: int
    ready_files: int
    processed_files: int
    requires_ocr_files: int
    failed_files: int
    total_pages: int
    text_chars: int
    chunk_count: int
    status_counts: dict[str, int]
    category_counts: dict[str, int]
