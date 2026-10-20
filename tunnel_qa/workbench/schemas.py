from datetime import datetime

from typing import Any

from pydantic import BaseModel, Field


class WorkbenchCounts(BaseModel):
    data_sources: int = 0
    documents: int = 0
    published_documents: int = 0
    chunks: int = 0
    monitoring_records: int = 0
    published_entities: int = 0
    published_relations: int = 0
    knowledge_version: int = 0
    evaluation_cases: int = 0
    evaluation_runs: int = 0
    quarantined_jobs: int = 0
    pending_risk_reviews: int = 0


class RecentSource(BaseModel):
    source_id: str
    title: str
    source_type: str
    version: str
    status: str
    content_location: str


class EvaluationSnapshot(BaseModel):
    status: str = "not_run"
    case_count: int = 0
    model_version: str | None = None
    knowledge_version: int = 0
    metrics: dict[str, float] = Field(default_factory=dict)
    created_at: datetime | None = None


class RecentTrace(BaseModel):
    trace_id: str
    question: str
    model_provider: str
    model_version: str
    knowledge_version: int
    created_at: datetime


class TraceSource(BaseModel):
    chunk_id: str
    chunk_ordinal: int
    source_id: str
    title: str
    source_type: str
    version: str
    effective_date: str
    content_location: str
    excerpt: str
    retrieval_score: float = 0.0


class TraceDetail(BaseModel):
    trace_id: str
    question: str
    rewritten_question: str
    answer: str
    answer_type: str | None = None
    sources: list[TraceSource] = Field(default_factory=list)
    graph_path: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    retrieval_parameters: dict[str, Any] = Field(default_factory=dict)
    model_provider: str
    model_version: str
    prompt_version: str
    knowledge_version: int
    refusal_reason: str | None = None
    created_at: datetime


class WorkbenchOverview(BaseModel):
    status: str
    counts: WorkbenchCounts
    document_statuses: dict[str, int] = Field(default_factory=dict)
    recent_sources: list[RecentSource] = Field(default_factory=list)
    latest_evaluation: EvaluationSnapshot = Field(default_factory=EvaluationSnapshot)
    recent_traces: list[RecentTrace] = Field(default_factory=list)
