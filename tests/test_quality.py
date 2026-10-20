from tunnel_qa.core.enums import ProcessingStatus, Role, SecurityLevel, SourceType
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import SourceDocument
from tunnel_qa.data.schemas import DataSourceCreate, DocumentMetadata
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.qa.model_gateway import ModelOutput
from tunnel_qa.qa.schemas import QARequest
from tunnel_qa.qa.service import QAService
from tunnel_qa.quality.models import AuditEvent, DecisionRecord, EvaluationResult, Feedback
from tunnel_qa.quality.schemas import (
    EvaluationCaseCreate,
    FeedbackCreate,
    MonitoringSnapshot,
    RiskAnalysisRequest,
)
from tunnel_qa.quality.service import (
    EvaluationService,
    FeedbackService,
    MonitoringService,
    RiskService,
)
from tunnel_qa.retrieval.service import HybridRetrievalService


class StableGateway:
    provider = "test"
    model = "stable-v1"

    def generate(self, *, question, evidence, scenario, response_mode="detailed"):
        return ModelOutput(
            answer="应监测掌子面温度，采用通风降温措施，并由现场负责人复核。",
            follow_up=[],
        )


def admin():
    return AccessContext("admin", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL)


def researcher():
    return AccessContext("researcher", Role.RESEARCHER, SecurityLevel.INTERNAL)


def prepare(database, settings):
    from tunnel_qa.data import models as data_models  # noqa: F401
    from tunnel_qa.knowledge import models as knowledge_models  # noqa: F401
    from tunnel_qa.qa import models as qa_models  # noqa: F401
    from tunnel_qa.quality import models as quality_models  # noqa: F401

    database.create_schema()
    session = database.session_factory()
    ingestion = DataIngestionService(session, settings)
    source = ingestion.create_data_source(
        DataSourceCreate(
            name="质量测试库",
            source_type=SourceType.STANDARD,
            owner="测试",
            security_level=SecurityLevel.INTERNAL,
        ),
        admin(),
    )
    result = ingestion.import_document(
        data_source_id=source.id,
        filename="quality.txt",
        content=(
            "高地温影响作业人员，传感器应监测掌子面温度，"
            "应采用通风降温措施，异常情况由现场负责人复核。"
        ).encode(),
        metadata=DocumentMetadata(
            source_id="SRC-QUALITY",
            title="高地温风险规范",
            source_type=SourceType.STANDARD,
            version="V1",
            effective_date="2026-01-01",
            security_level=SecurityLevel.INTERNAL,
            owner_org="测试单位",
            content_location="第1节",
        ),
        idempotency_key="quality-import",
        access=admin(),
    )
    document = session.get(SourceDocument, result.document_id)
    document.status = ProcessingStatus.PUBLISHED
    session.commit()
    qa = QAService(
        session, settings, HybridRetrievalService(session), StableGateway()
    )
    return session, qa


def test_high_risk_analysis_triggers_rules_review_and_audit(database, settings):
    session, qa = prepare(database, settings)
    risk = RiskService(session, qa)
    risk.bootstrap_rules(admin())

    response = risk.analyze(
        RiskAnalysisRequest(
            description="高地温掌子面持续作业",
            construction_stage="开挖",
            monitoring=[
                MonitoringSnapshot(
                    metric_code="temperature", value=39, unit="℃", location={"site": "A"}
                )
            ],
        ),
        admin(),
    )

    assert response.risk_level == "high"
    assert response.review_required
    assert any("通风" in item for item in response.check_items)
    assert response.qa.warnings and "不替代" in response.qa.warnings[0]
    assert session.get(DecisionRecord, response.decision_id)
    assert session.query(AuditEvent).filter_by(action="risk.analyze").one()


def test_feedback_is_bound_to_owned_message_and_audited(database, settings):
    session, qa = prepare(database, settings)
    response = qa.ask(QARequest(question="高地温如何处置？"), researcher())

    feedback = FeedbackService(session).submit(
        FeedbackCreate(
            message_id=response.message_id,
            rating=4,
            issue_type="citation",
            comment="引用位置清晰",
        ),
        researcher(),
    )

    assert feedback.status == "open"
    assert session.query(Feedback).count() == 1
    assert session.query(AuditEvent).filter_by(action="feedback.submit").one()


def test_evaluation_scores_answer_citation_and_refusal(database, settings):
    session, qa = prepare(database, settings)
    evaluation = EvaluationService(session, qa)
    evaluation.create_case(
        EvaluationCaseCreate(
            name="高地温规范",
            question="高地温如何通风降温？",
            expected_key_points=["监测", "通风降温"],
            required_source_ids=["SRC-QUALITY"],
        ),
        researcher(),
    )
    evaluation.create_case(
        EvaluationCaseCreate(
            name="开放域拒答",
            question="食堂今天有什么菜？",
            should_refuse=True,
        ),
        researcher(),
    )

    run = evaluation.run(researcher())

    assert run.status == "completed"
    assert run.case_count == 2
    assert run.metrics["citation_valid_rate"] == 1.0
    assert run.metrics["refusal_accuracy"] == 1.0
    assert session.query(EvaluationResult).count() == 2


def test_health_reports_actionable_alerts(database, settings):
    session, _ = prepare(database, settings)

    report = MonitoringService(session).health(admin())

    assert report.database == "healthy"
    assert report.published_nodes == 0
    assert any("500" in alert for alert in report.alerts)
