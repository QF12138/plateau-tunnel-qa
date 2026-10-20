from pydantic import BaseModel, Field

from tunnel_qa.qa.schemas import QAResponse


class MonitoringSnapshot(BaseModel):
    metric_code: str
    value: float
    unit: str
    location: dict = Field(default_factory=dict)


class RiskAnalysisRequest(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    construction_stage: str
    monitoring: list[MonitoringSnapshot] = Field(default_factory=list)
    resource_conditions: list[str] = Field(default_factory=list)
    scenario: str = "risk_analysis"


class RiskFactor(BaseModel):
    risk_type: str
    severity: str
    trigger: str
    rule_id: str


class RiskAnalysisResponse(BaseModel):
    decision_id: str
    risk_level: str
    risk_factors: list[RiskFactor]
    check_items: list[str]
    similar_evidence_count: int
    qa: QAResponse
    review_required: bool


class FeedbackCreate(BaseModel):
    message_id: str
    rating: int = Field(ge=1, le=5)
    issue_type: str | None = None
    comment: str = Field(default="", max_length=2000)


class EvaluationCaseCreate(BaseModel):
    name: str
    question: str
    expected_key_points: list[str] = Field(default_factory=list)
    required_source_ids: list[str] = Field(default_factory=list)
    should_refuse: bool = False
    scenario: str | None = None
    tags: list[str] = Field(default_factory=list)


class EvaluationRunResponse(BaseModel):
    run_id: str
    status: str
    case_count: int
    metrics: dict[str, float]


class HealthReport(BaseModel):
    status: str
    database: str
    quarantined_jobs: int
    open_feedback: int
    pending_high_risk_reviews: int
    published_nodes: int
    alerts: list[str]

