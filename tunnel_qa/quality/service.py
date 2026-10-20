import operator
from typing import TypedDict

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from tunnel_qa.core.enums import AnswerType
from tunnel_qa.core.errors import AppError, NotFoundError, PermissionDeniedError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.tracing import get_trace_id
from tunnel_qa.data.models import Chunk, ImportJob
from tunnel_qa.knowledge.models import KnowledgeEntity
from tunnel_qa.qa.models import Message
from tunnel_qa.qa.schemas import QARequest
from tunnel_qa.qa.service import QAService
from tunnel_qa.quality.models import (
    AuditEvent,
    DecisionRecord,
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    Feedback,
    RiskRule,
)
from tunnel_qa.quality.schemas import (
    EvaluationCaseCreate,
    EvaluationRunResponse,
    FeedbackCreate,
    HealthReport,
    RiskAnalysisRequest,
    RiskAnalysisResponse,
    RiskFactor,
)


class RiskRuleDefinition(TypedDict):
    name: str
    metric_code: str
    operator: str
    threshold: float
    unit: str
    risk_type: str
    severity: str
    check_items: list[str]


DEFAULT_RISK_RULES: tuple[RiskRuleDefinition, ...] = (
    {
        "name": "掌子面高温预警",
        "metric_code": "temperature",
        "operator": ">=",
        "threshold": 35.0,
        "unit": "℃",
        "risk_type": "高地温作业风险",
        "severity": "high",
        "check_items": ["复核温度传感器", "检查通风降温设备", "核查作业人员轮换与防护"],
    },
    {
        "name": "收敛位移预警",
        "metric_code": "displacement",
        "operator": ">=",
        "threshold": 10.0,
        "unit": "mm",
        "risk_type": "围岩变形风险",
        "severity": "high",
        "check_items": ["复测位移测点", "核查支护状态", "组织围岩稳定性复核"],
    },
    {
        "name": "一氧化碳预警",
        "metric_code": "co",
        "operator": ">=",
        "threshold": 24.0,
        "unit": "ppm",
        "risk_type": "有害气体风险",
        "severity": "critical",
        "check_items": ["立即复核气体监测", "检查通风系统", "按已审批预案组织人员防护"],
    },
)

OPERATORS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


class AuditService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log(
        self,
        access: AccessContext,
        action: str,
        resource_type: str,
        resource_id: str | None,
        *,
        outcome: str = "success",
        details: dict | None = None,
        trace_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            trace_id=trace_id or get_trace_id(),
            user_id=access.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details or {},
        )
        self.session.add(event)
        self.session.flush()
        return event


class RiskService:
    def __init__(self, session: Session, qa_service: QAService) -> None:
        self.session = session
        self.qa_service = qa_service
        self.audit = AuditService(session)

    def bootstrap_rules(self, access: AccessContext) -> list[RiskRule]:
        access.require("*")
        existing = {rule.name: rule for rule in self.session.scalars(select(RiskRule)).all()}
        rules = []
        for payload in DEFAULT_RISK_RULES:
            if payload["name"] in existing:
                rules.append(existing[payload["name"]])
            else:
                rule = RiskRule(**payload)
                self.session.add(rule)
                rules.append(rule)
        self.session.commit()
        return rules

    def analyze(
        self, request: RiskAnalysisRequest, access: AccessContext
    ) -> RiskAnalysisResponse:
        access.require("qa:ask")
        rules = self.session.scalars(select(RiskRule).where(RiskRule.active.is_(True))).all()
        factors: list[RiskFactor] = []
        check_items: list[str] = []
        for reading in request.monitoring:
            for rule in rules:
                if reading.metric_code != rule.metric_code or reading.unit != rule.unit:
                    continue
                compare = OPERATORS.get(rule.operator)
                if compare and compare(reading.value, rule.threshold):
                    factors.append(
                        RiskFactor(
                            risk_type=rule.risk_type,
                            severity=rule.severity,
                            trigger=f"{reading.metric_code}={reading.value}{reading.unit} {rule.operator} {rule.threshold}{rule.unit}",
                            rule_id=rule.id,
                        )
                    )
                    check_items.extend(rule.check_items)
        highest = max(
            (factor.severity for factor in factors),
            key=lambda item: SEVERITY_RANK[item],
            default="low",
        )
        monitoring_text = "；".join(
            f"{item.metric_code}={item.value}{item.unit}" for item in request.monitoring
        ) or "无实时监测摘要"
        question = (
            f"施工阶段：{request.construction_stage}。场景：{request.description}。"
            f"监测摘要：{monitoring_text}。请给出风险因素、依据和检查项。"
        )
        qa_response = self.qa_service.ask(
            QARequest(question=question, scenario=request.scenario), access
        )
        review_required = highest in {"high", "critical"} or request.scenario == "emergency_support"
        record = DecisionRecord(
            user_id=access.user_id,
            qa_message_id=qa_response.message_id,
            scenario=request.scenario,
            risk_level=highest,
            risk_factors=[factor.model_dump() for factor in factors],
            monitoring_snapshot=[item.model_dump() for item in request.monitoring],
            check_items=list(dict.fromkeys(check_items)),
            review_required=review_required,
        )
        self.session.add(record)
        self.session.flush()
        self.audit.log(
            access,
            "risk.analyze",
            "decision_record",
            record.id,
            details={"risk_level": highest, "review_required": review_required},
            trace_id=qa_response.trace_id,
        )
        self.session.commit()
        return RiskAnalysisResponse(
            decision_id=record.id,
            risk_level=highest,
            risk_factors=factors,
            check_items=record.check_items,
            similar_evidence_count=len(qa_response.citations),
            qa=qa_response,
            review_required=review_required,
        )


class FeedbackService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit = AuditService(session)

    def submit(self, payload: FeedbackCreate, access: AccessContext) -> Feedback:
        access.require("feedback:write")
        message = self.session.get(Message, payload.message_id)
        if message is None:
            raise NotFoundError("问答消息", payload.message_id)
        if (
            message.conversation.owner_user_id != access.user_id
            and access.role.value != "system_admin"
        ):
            raise PermissionDeniedError("不能评价其他用户的问答")
        feedback = Feedback(user_id=access.user_id, **payload.model_dump())
        self.session.add(feedback)
        self.session.flush()
        self.audit.log(access, "feedback.submit", "feedback", feedback.id)
        self.session.commit()
        return feedback


class EvaluationService:
    def __init__(self, session: Session, qa_service: QAService) -> None:
        self.session = session
        self.qa_service = qa_service
        self.audit = AuditService(session)

    def create_case(
        self, payload: EvaluationCaseCreate, access: AccessContext
    ) -> EvaluationCase:
        access.require("evaluation:run")
        case = EvaluationCase(**payload.model_dump())
        self.session.add(case)
        self.session.commit()
        return case

    def run(self, access: AccessContext) -> EvaluationRunResponse:
        access.require("evaluation:run")
        cases = self.session.scalars(
            select(EvaluationCase).where(EvaluationCase.active.is_(True))
        ).all()
        if not cases:
            raise AppError("EVALUATION_SET_EMPTY", "没有可运行的评测用例")
        run = EvaluationRun(
            run_by=access.user_id,
            model_version=self.qa_service.model_gateway.model,
            knowledge_version=self.qa_service._knowledge_version(),
            case_count=len(cases),
        )
        self.session.add(run)
        self.session.flush()
        result_rows = []
        for case in cases:
            response = self.qa_service.ask(
                QARequest(question=case.question, scenario=case.scenario), access
            )
            points = case.expected_key_points
            point_score = (
                sum(point in response.answer for point in points) / len(points) if points else 1.0
            )
            actual_sources = {item.source_id for item in response.citations}
            required_sources = set(case.required_source_ids)
            recall = (
                len(required_sources & actual_sources) / len(required_sources)
                if required_sources
                else 1.0
            )
            citation_valid = all(
                self.session.get(Chunk, citation.chunk_id) is not None
                for citation in response.citations
            )
            refused = response.answer_type == AnswerType.REFUSAL
            refusal_correct = refused == case.should_refuse
            passed = (
                point_score >= 0.5 and recall >= 1.0 and citation_valid and refusal_correct
            )
            result = EvaluationResult(
                run_id=run.id,
                case_id=case.id,
                response_message_id=response.message_id,
                key_point_score=point_score,
                retrieval_recall=recall,
                citation_valid=citation_valid,
                refusal_correct=refusal_correct,
                passed=passed,
            )
            self.session.add(result)
            result_rows.append(result)
        count = len(result_rows)
        run.metrics = {
            "pass_rate": sum(row.passed for row in result_rows) / count,
            "key_point_score": sum(row.key_point_score for row in result_rows) / count,
            "retrieval_recall": sum(row.retrieval_recall for row in result_rows) / count,
            "citation_valid_rate": sum(row.citation_valid for row in result_rows) / count,
            "refusal_accuracy": sum(row.refusal_correct for row in result_rows) / count,
        }
        run.status = "completed"
        self.audit.log(
            access, "evaluation.run", "evaluation_run", run.id, details=run.metrics
        )
        self.session.commit()
        return EvaluationRunResponse(
            run_id=run.id,
            status=run.status,
            case_count=run.case_count,
            metrics=run.metrics,
        )


class MonitoringService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def health(self, access: AccessContext) -> HealthReport:
        if not access.can("audit:read") and access.role.value != "system_admin":
            raise PermissionDeniedError("缺少运维监控权限")
        self.session.execute(text("SELECT 1"))
        quarantined = self.session.scalar(
            select(func.count()).select_from(ImportJob).where(ImportJob.status == "quarantined")
        ) or 0
        open_feedback = self.session.scalar(
            select(func.count()).select_from(Feedback).where(Feedback.status == "open")
        ) or 0
        pending_risk = self.session.scalar(
            select(func.count()).select_from(DecisionRecord).where(
                DecisionRecord.review_required.is_(True),
                DecisionRecord.review_status == "pending",
            )
        ) or 0
        published_nodes = self.session.scalar(
            select(func.count()).select_from(KnowledgeEntity).where(
                KnowledgeEntity.status == "published"
            )
        ) or 0
        alerts = []
        if quarantined:
            alerts.append(f"有 {quarantined} 个导入任务处于隔离状态")
        if pending_risk:
            alerts.append(f"有 {pending_risk} 个高风险辅助结果等待复核")
        if published_nodes < 500:
            alerts.append(f"已发布关键知识节点为 {published_nodes}，尚未达到 500 节点目标")
        return HealthReport(
            status="degraded" if alerts else "healthy",
            database="healthy",
            quarantined_jobs=quarantined,
            open_feedback=open_feedback,
            pending_high_risk_reviews=pending_risk,
            published_nodes=published_nodes,
            alerts=alerts,
        )
