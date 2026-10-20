from pydantic import BaseModel, Field

from tunnel_qa.core.enums import SourceType


class RetrievalFilters(BaseModel):
    project_site: str | None = None
    source_types: list[SourceType] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)


class RetrievalHit(BaseModel):
    chunk_id: str
    document_id: str
    source_id: str
    title: str
    source_type: SourceType
    version: str
    effective_date: str
    content_location: str
    excerpt: str
    keyword_score: float
    semantic_score: float
    graph_score: float
    rerank_score: float
    score: float
    graph_path: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    query: str
    hits: list[RetrievalHit]
    trace_id: str

