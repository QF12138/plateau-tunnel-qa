import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import AnswerType, ConfidenceLevel
from tunnel_qa.core.errors import AppError, NotFoundError, PermissionDeniedError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.core.tracing import new_trace_id, set_trace_id
from tunnel_qa.knowledge.models import KnowledgeRelease
from tunnel_qa.qa.model_gateway import ExtractiveFallbackGateway, ModelGateway
from tunnel_qa.qa.models import Conversation, Message, QATrace
from tunnel_qa.qa.schemas import Citation, QARequest, QAResponse
from tunnel_qa.retrieval.service import HybridRetrievalService


INJECTION_PATTERNS = (
    r"忽略.{0,10}(之前|上述|系统).{0,10}(指令|提示)",
    r"(输出|泄露|显示).{0,10}(系统提示|密钥|api.?key)",
    r"ignore.{0,20}(previous|system).{0,20}instructions?",
    r"reveal.{0,20}(system prompt|api.?key|secret)",
)

HIGH_RISK_TERMS = ("应急", "事故", "岩爆", "坍塌", "突水", "施工方案", "风险", "处置")


class QAService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        retrieval: HybridRetrievalService,
        model_gateway: ModelGateway,
    ) -> None:
        self.session = session
        self.settings = settings
        self.retrieval = retrieval
        self.model_gateway = model_gateway

    def ask(
        self, request: QARequest, access: AccessContext, *, trace_id: str | None = None
    ) -> QAResponse:
        access.require("qa:ask")
        response_mode = request.response_mode or self.settings.default_response_mode
        trace_id = set_trace_id(trace_id or new_trace_id())
        conversation = self._get_or_create_conversation(request, access)
        rewritten = self._rewrite_question(request.question, conversation)
        self._append_message(
            conversation, "user", request.question, trace_id=trace_id
        )

        if self._is_injection(request.question):
            return self._refuse(
                request=request,
                access=access,
                conversation=conversation,
                rewritten=rewritten,
                trace_id=trace_id,
                reason="检测到提示注入或敏感配置探测",
                warning="请求包含试图绕过系统约束的内容，已拒绝处理。",
            )

        retrieval_result = self.retrieval.search(
            rewritten,
            access,
            filters=request.context_filters,
            top_k=self.settings.retrieval_top_k,
        )
        hits = [
            hit for hit in retrieval_result.hits if hit.score >= self.settings.minimum_evidence_score
        ]
        if not hits:
            return self._refuse(
                request=request,
                access=access,
                conversation=conversation,
                rewritten=rewritten,
                trace_id=trace_id,
                reason="没有达到质量阈值的已授权证据",
                warning="当前知识库中没有足够且可定位的证据，系统未生成推测性答案。",
            )

        citations = [
            Citation(
                chunk_id=hit.chunk_id,
                source_id=hit.source_id,
                title=hit.title,
                version=hit.version,
                effective_date=hit.effective_date,
                content_location=hit.content_location,
                excerpt=hit.excerpt,
                source_type=hit.source_type.value,
                retrieval_score=hit.score,
                graph_path=hit.graph_path,
            )
            for hit in hits
        ]
        evidence_payload = [citation.model_dump() for citation in citations]
        warnings: list[str] = []
        high_risk = request.scenario in {"risk_analysis", "emergency_support"} or any(
            term in request.question for term in HIGH_RISK_TERMS
        )
        if high_risk:
            warnings.append(
                "本回答仅用于辅助分析，不替代现场负责人、设计单位或领域专家的审批与签认。"
            )
        try:
            output = self.model_gateway.generate(
                question=rewritten,
                evidence=evidence_payload,
                scenario=request.scenario,
                response_mode=response_mode,
            )
            provider = self.model_gateway.provider
            model_version = self.model_gateway.model
        except AppError as exc:
            fallback = ExtractiveFallbackGateway()
            output = fallback.generate(
                question=rewritten, evidence=evidence_payload, scenario=request.scenario,
                response_mode=response_mode,
            )
            provider = fallback.provider
            model_version = fallback.model
            warnings.append(f"外部模型暂不可用，已使用证据摘录降级回答（{exc.error_code}）。")

        confidence = self._confidence(hits)
        graph_path = list(dict.fromkeys(path for hit in hits for path in hit.graph_path))
        answer_type = (
            AnswerType.DECISION_SUPPORT if high_risk else AnswerType.EXPLANATION
        )
        assistant_message = self._append_message(
            conversation,
            "assistant",
            output.answer,
            trace_id=trace_id,
            answer_type=answer_type,
            citations=[citation.model_dump() for citation in citations],
            graph_path=graph_path,
            warnings=warnings,
        )
        knowledge_version = self._knowledge_version()
        trace = QATrace(
            trace_id=trace_id,
            user_id=access.user_id,
            conversation_id=conversation.id,
            question=request.question,
            rewritten_question=rewritten,
            retrieval_parameters={
                "top_k": self.settings.retrieval_top_k,
                "filters": request.context_filters.model_dump(mode="json"),
                "response_mode": response_mode,
            },
            retrieved_chunk_ids=[hit.chunk_id for hit in hits],
            model_provider=provider,
            model_version=model_version,
            prompt_version=self.settings.prompt_version,
            knowledge_version=knowledge_version,
        )
        self.session.add(trace)
        self.session.commit()
        trace_details = (
            {
                "rewritten_question": rewritten,
                "retrieved_chunk_ids": trace.retrieved_chunk_ids,
                "prompt_version": self.settings.prompt_version,
                "retrieval_scores": [hit.score for hit in hits],
            }
            if request.trace_required
            else None
        )
        return QAResponse(
            answer=output.answer,
            answer_type=answer_type,
            confidence_level=confidence,
            citations=citations,
            graph_path=graph_path,
            warnings=warnings,
            follow_up=output.follow_up,
            trace_id=trace_id,
            session_id=conversation.id,
            message_id=assistant_message.id,
            model_provider=provider,
            model_version=model_version,
            knowledge_version=knowledge_version,
            trace=trace_details,
        )

    def _refuse(
        self,
        *,
        request: QARequest,
        access: AccessContext,
        conversation: Conversation,
        rewritten: str,
        trace_id: str,
        reason: str,
        warning: str,
    ) -> QAResponse:
        answer = f"暂时不足以作答：{reason}。请补充工点、工况、时间或资料范围后重试。"
        message = self._append_message(
            conversation,
            "assistant",
            answer,
            trace_id=trace_id,
            answer_type=AnswerType.REFUSAL,
            warnings=[warning],
        )
        knowledge_version = self._knowledge_version()
        trace = QATrace(
            trace_id=trace_id,
            user_id=access.user_id,
            conversation_id=conversation.id,
            question=request.question,
            rewritten_question=rewritten,
            retrieval_parameters={
                "top_k": self.settings.retrieval_top_k,
                "filters": request.context_filters.model_dump(mode="json"),
            },
            model_provider="none",
            model_version="none",
            prompt_version=self.settings.prompt_version,
            knowledge_version=knowledge_version,
            refusal_reason=reason,
        )
        self.session.add(trace)
        self.session.commit()
        return QAResponse(
            answer=answer,
            answer_type=AnswerType.REFUSAL,
            confidence_level=ConfidenceLevel.LOW,
            citations=[],
            graph_path=[],
            warnings=[warning],
            follow_up=["请提供具体隧道/工点和施工阶段。", "是否限定规范版本或资料类型？"],
            trace_id=trace_id,
            session_id=conversation.id,
            message_id=message.id,
            model_provider="none",
            model_version="none",
            knowledge_version=knowledge_version,
            trace={"rewritten_question": rewritten, "refusal_reason": reason}
            if request.trace_required
            else None,
        )

    def _get_or_create_conversation(
        self, request: QARequest, access: AccessContext
    ) -> Conversation:
        if request.session_id:
            conversation = self.session.get(Conversation, request.session_id)
            if conversation is None:
                raise NotFoundError("会话", request.session_id)
            if conversation.owner_user_id != access.user_id and access.role.value != "system_admin":
                raise PermissionDeniedError("不能访问其他用户的会话")
            return conversation
        conversation = Conversation(
            owner_user_id=access.user_id,
            title=request.question[:100],
            security_level=access.security_level,
            scenario=request.scenario,
            context_filters=request.context_filters.model_dump(mode="json"),
        )
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def _rewrite_question(self, question: str, conversation: Conversation) -> str:
        previous_users = [message.content for message in conversation.messages if message.role == "user"]
        ambiguous = len(question) <= 18 and bool(
            re.search(r"(这个|那个|它|上述|那|呢|怎么办|如何处理)", question)
        )
        if ambiguous and previous_users:
            return f"上一轮问题：{previous_users[-1]}；本轮追问：{question}"
        return question

    def _append_message(
        self,
        conversation: Conversation,
        role: str,
        content: str,
        *,
        trace_id: str,
        answer_type: AnswerType | None = None,
        citations: list | None = None,
        graph_path: list | None = None,
        warnings: list | None = None,
    ) -> Message:
        sequence = len(conversation.messages) + 1
        message = Message(
            conversation_id=conversation.id,
            sequence=sequence,
            role=role,
            content=content,
            answer_type=answer_type,
            citations=citations or [],
            graph_path=graph_path or [],
            warnings=warnings or [],
            trace_id=trace_id,
        )
        self.session.add(message)
        self.session.flush()
        return message

    @staticmethod
    def _is_injection(question: str) -> bool:
        return any(re.search(pattern, question, re.I) for pattern in INJECTION_PATTERNS)

    @staticmethod
    def _confidence(hits) -> ConfidenceLevel:
        if len(hits) >= 2 and hits[0].score >= 0.65:
            return ConfidenceLevel.HIGH
        if hits[0].score >= 0.3:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    def _knowledge_version(self) -> int:
        return self.session.scalar(select(func.max(KnowledgeRelease.version))) or 0
