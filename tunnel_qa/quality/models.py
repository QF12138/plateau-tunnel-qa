from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tunnel_qa.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RiskRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "risk_rules"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    metric_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    operator: Mapped[str] = mapped_column(String(5), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    risk_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    check_items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class DecisionRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "decision_records"

    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    qa_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), nullable=False)
    scenario: Mapped[str] = mapped_column(String(50), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_factors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    monitoring_snapshot: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    check_items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    review_required: Mapped[bool] = mapped_column(default=True, nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(100))


class Feedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "feedback"

    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_type: Mapped[str | None] = mapped_column(String(50))
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)


class EvaluationCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_cases"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_key_points: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    required_source_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    should_refuse: Mapped[bool] = mapped_column(default=False, nullable=False)
    scenario: Mapped[str | None] = mapped_column(String(50))
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class EvaluationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_runs"

    run_by: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    knowledge_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class EvaluationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), nullable=False)
    response_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), nullable=False)
    key_point_score: Mapped[float] = mapped_column(Float, nullable=False)
    retrieval_recall: Mapped[float] = mapped_column(Float, nullable=False)
    citation_valid: Mapped[bool] = mapped_column(nullable=False)
    refusal_correct: Mapped[bool] = mapped_column(nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)


class AuditEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_events"

    trace_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

