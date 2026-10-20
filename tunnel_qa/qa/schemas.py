from typing import Any, Literal

from pydantic import BaseModel, Field

from tunnel_qa.core.enums import AnswerType, ConfidenceLevel
from tunnel_qa.retrieval.schemas import RetrievalFilters


class QARequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    context_filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    scenario: str | None = None
    response_mode: Literal["detailed", "brief"] | None = None
    trace_required: bool = True


class Citation(BaseModel):
    chunk_id: str
    source_id: str
    title: str
    version: str
    effective_date: str
    content_location: str
    excerpt: str
    source_type: str | None = None
    retrieval_score: float = 0.0
    graph_path: list[str] = Field(default_factory=list)


class QAResponse(BaseModel):
    answer: str
    answer_type: AnswerType
    confidence_level: ConfidenceLevel
    citations: list[Citation]
    graph_path: list[str]
    warnings: list[str]
    follow_up: list[str]
    trace_id: str
    session_id: str
    message_id: str
    model_provider: str
    model_version: str
    knowledge_version: int
    trace: dict[str, Any] | None = None
