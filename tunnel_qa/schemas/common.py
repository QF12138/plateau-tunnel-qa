from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    field: str | None = None
    retryable: bool = False
    trace_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class TraceableResponse(BaseModel):
    trace_id: str


class TimestampedResponse(ORMModel):
    id: str
    created_at: datetime
    updated_at: datetime

