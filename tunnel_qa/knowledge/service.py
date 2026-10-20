import hashlib
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypedDict

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from tunnel_qa.core.enums import (
    OntologyDomain,
    ProcessingStatus,
    ReviewStatus,
    SECURITY_RANK,
    SecurityLevel,
    SourceType,
)
from tunnel_qa.core.errors import AppError, ConflictError, NotFoundError
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.models import (
    Chunk,
    DataSource,
    SourceDocument,
    SYSTEM_MANAGED_SOURCE_IDS,
    SYSTEM_MANAGED_SOURCE_NAMES,
)
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.processing_metadata import processing_chunk
from tunnel_qa.db.base import utc_now
from tunnel_qa.knowledge.models import (
    Evidence,
    KnowledgeEntity,
    KnowledgeEvidenceLink,
    KnowledgeRelation,
    KnowledgeRelease,
    KnowledgeReview,
    OntologyType,
)
from tunnel_qa.knowledge.schemas import (
    BatchReviewRequest,
    ExtractionResult,
    GraphEdge,
    GraphNode,
    ManualEntityCreate,
    ManualEntityUpdate,
    ManualRelationCreate,
    ManualRelationUpdate,
    OntologyTypeCreate,
    OntologyTypeRead,
    PublishResult,
    ReviewRequest,
    Subgraph,
)


DEFAULT_ONTOLOGY_VERSION = 2
AUTO_PUBLISH_CONFIDENCE = 0.85

DEFAULT_ONTOLOGY: tuple[OntologyTypeCreate, ...] = (
    OntologyTypeCreate(
        name="人员与组织",
        domain=OntologyDomain.HUMAN,
        aliases=["人员", "组织", "岗位", "专家", "负责人", "作业人员"],
        properties_schema={
            "岗位": "string",
            "所属单位": "string",
            "资质": "string[]",
            "职责": "string[]",
        },
        allowed_relations=["隶属", "负责", "参与", "具备资质", "复核", "采用"],
        version=DEFAULT_ONTOLOGY_VERSION,
    ),
    OntologyTypeCreate(
        name="设备与传感器",
        domain=OntologyDomain.MACHINE,
        aliases=["设备", "施工装备", "传感器", "监测设备", "风机", "部件"],
        properties_schema={
            "型号": "string",
            "参数": "object",
            "安装位置": "string",
            "运行状态": "string",
        },
        allowed_relations=["安装于", "监测", "适用于", "组成", "故障关联", "支撑"],
        version=DEFAULT_ONTOLOGY_VERSION,
    ),
    OntologyTypeCreate(
        name="材料与成果",
        domain=OntologyDomain.MATERIAL,
        aliases=["材料", "科研成果", "报告", "论文", "专利", "样品", "数据资料"],
        properties_schema={
            "规格": "string",
            "性能": "object",
            "成果类型": "string",
            "发布单位": "string",
        },
        allowed_relations=["用于", "符合", "产出", "引用", "支撑", "替代"],
        version=DEFAULT_ONTOLOGY_VERSION,
    ),
    OntologyTypeCreate(
        name="工法与规范",
        domain=OntologyDomain.METHOD,
        aliases=["工法", "规范", "规程", "条款", "预案", "措施", "算法", "施工方案"],
        properties_schema={
            "编号": "string",
            "适用范围": "string",
            "控制指标": "object",
            "实施步骤": "string[]",
        },
        allowed_relations=["规定", "约束", "推荐", "依据", "采用", "替代", "适用于"],
        version=DEFAULT_ONTOLOGY_VERSION,
    ),
    OntologyTypeCreate(
        name="环境与风险",
        domain=OntologyDomain.ENVIRONMENT,
        aliases=["地质体", "断裂", "地下水", "温度", "应力", "工点环境", "风险事件", "隧道"],
        properties_schema={
            "位置": "string",
            "地质特征": "string[]",
            "监测指标": "object",
            "风险等级": "string",
        },
        allowed_relations=["位于", "导致", "影响", "触发", "发生于", "监测", "治理"],
        version=DEFAULT_ONTOLOGY_VERSION,
    ),
)

DOMAIN_TERMS: dict[OntologyDomain, tuple[str, ...]] = {
    OntologyDomain.HUMAN: (
        "作业人员", "施工人员", "监测人员", "试验人员", "科研人员", "现场负责人",
        "技术负责人", "项目经理", "安全员", "监理工程师", "专家", "审核员",
        "施工单位", "建设单位", "设计单位", "监理单位", "应急救援人员",
    ),
    OntologyDomain.MACHINE: (
        "温度传感器", "压力传感器", "瓦斯检测仪", "超前地质预报设备", "隧道掘进机",
        "凿岩台车", "湿喷机械手", "轴流风机", "排水设备", "出渣设备", "装载机",
        "空压机", "位移计", "应力计", "传感器", "风机", "通风设备", "施工装备",
        "监测设备",
    ),
    OntologyDomain.MATERIAL: (
        "喷射混凝土", "衬砌混凝土", "注浆材料", "防水板", "钢拱架", "水泥浆液",
        "监测数据", "试验数据", "科研成果", "标准文件", "科研报告", "施工材料",
        "事故报告", "混凝土", "钢筋", "锚杆", "炸药", "雷管", "论文", "专利",
    ),
    OntologyDomain.METHOD: (
        "铁路隧道设计规范", "施工安全技术规程", "通风降温措施", "超前地质预报",
        "安全技术交底", "超前支护", "锚喷支护", "注浆加固", "排水降压", "通风降温",
        "机械通风", "风险评估", "监控量测", "爆破设计", "施工方案", "专项方案",
        "应急预案", "施工规范", "监测方案", "开挖方法", "支护参数", "钻爆法",
        "TBM法", "新奥法", "工法",
    ),
    OntologyDomain.ENVIRONMENT: (
        "断层破碎带", "掌子面温度", "地下水位", "高地应力", "围岩变形", "高原隧道",
        "隧道洞口", "富水区", "高海拔", "低气压", "地质风险", "高地温", "地下水",
        "地应力", "大变形", "掌子面", "围岩等级", "软岩", "围岩", "岩体", "断层",
        "瓦斯", "冻土", "突水", "突泥", "坍塌", "热害", "缺氧", "岩爆", "隧道",
    ),
}

TERM_ALIASES: dict[str, tuple[str, ...]] = {
    "隧道掘进机": ("TBM", "全断面隧道掘进机"),
    "喷射混凝土": ("喷混凝土",),
    "超前地质预报": ("超前预报",),
    "监控量测": ("监测量测",),
    "通风降温措施": ("通风降温",),
}

PATTERN_TERMS: tuple[tuple[OntologyDomain, re.Pattern[str]], ...] = (
    (
        OntologyDomain.METHOD,
        re.compile(
            r"(?:采用|通过|基于|制定|建立|按照|依据|实施|提出)"
            r"(?P<term>[A-Za-z0-9\-\u4e00-\u9fff]{2,24}(?:技术|方法|方案|工法|规范|规程|预案|措施|算法))"
        ),
    ),
    (OntologyDomain.METHOD, re.compile(r"(?P<term>TB\s*\d{5}(?:-\d{4})?)")),
)

RELATION_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("导致", "导致"),
    ("影响", "影响"),
    ("监测", "监测"),
    ("适用于", "适用于"),
    ("采用", "采用"),
    ("建立", "要求建立"),
    ("复核", "要求复核"),
    ("规定", "规定"),
    ("约束", "约束"),
    ("推荐", "推荐"),
    ("依据", "依据"),
    ("安装", "安装于"),
    ("用于", "用于"),
    ("触发", "触发"),
    ("发生于", "发生于"),
    ("治理", "治理"),
)

SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.MULTILINE)
MEASUREMENT_PATTERN = re.compile(
    r"(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?\s*(?:℃|°C|MPa|kPa|Pa|mm|cm|km|m³/s|m3/s|m/s|%|ppm)"
)


@dataclass(frozen=True)
class EntityMatch:
    start: int
    end: int
    standard_name: str
    observed_name: str
    domain: OntologyDomain
    confidence: float
    method: str


@dataclass(frozen=True)
class _LegacyEvidenceLink:
    evidence: Evidence
    confidence: float
    document_id: None = None
    extraction_method: str = "legacy"


class _ReviewCandidate(TypedDict):
    target_type: str
    target_id: str
    label: str
    domain: OntologyDomain
    confidence: float
    evidence_quote: str
    source_location: str
    source_id: str | None
    source_title: str | None
    extraction_method: str
    source_count: int


class KnowledgeService:
    def __init__(
        self, session: Session, content_store: DocumentContentStore | None = None
    ) -> None:
        self.session = session
        self.content_store = content_store

    def bootstrap_ontology(
        self, access: AccessContext, *, preserve_existing: bool = False
    ) -> list[OntologyType]:
        access.require("knowledge:propose")
        all_types = self.session.scalars(select(OntologyType)).all()
        existing = {(item.name, item.version): item for item in all_types}
        created: list[OntologyType] = []
        for payload in DEFAULT_ONTOLOGY:
            ontology = existing.get((payload.name, payload.version))
            if ontology is None:
                ontology = OntologyType(**payload.model_dump())
                self.session.add(ontology)
            elif not preserve_existing:
                ontology.properties_schema = payload.properties_schema
                ontology.allowed_relations = payload.allowed_relations
                ontology.aliases = payload.aliases
                ontology.active = True
            created.append(ontology)
        active_domains = {item.domain for item in DEFAULT_ONTOLOGY}
        for ontology in all_types:
            if not preserve_existing and ontology.domain in active_domains and ontology.version < DEFAULT_ONTOLOGY_VERSION:
                ontology.active = False
        self.session.commit()
        return created

    def list_ontology_types(self, access: AccessContext) -> list[OntologyTypeRead]:
        access.require("knowledge:read")
        items = self.session.scalars(
            select(OntologyType).order_by(OntologyType.version.desc(), OntologyType.domain)
        ).all()
        return [
            OntologyTypeRead(
                id=item.id,
                name=item.name,
                domain=item.domain,
                properties_schema=item.properties_schema,
                allowed_relations=item.allowed_relations,
                aliases=item.aliases,
                version=item.version,
                active=item.active,
            )
            for item in items
        ]

    def create_ontology_type(
        self, payload: OntologyTypeCreate, access: AccessContext
    ) -> OntologyType:
        access.require("knowledge:propose")
        ontology = OntologyType(**payload.model_dump())
        self.session.add(ontology)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("同版本本体类型已存在", field="name") from exc
        return ontology

    @staticmethod
    def _entity_matches(text: str) -> list[EntityMatch]:
        matches: list[EntityMatch] = []
        for domain, terms in DOMAIN_TERMS.items():
            for standard_name in terms:
                observed_names = (standard_name, *TERM_ALIASES.get(standard_name, ()))
                for observed_name in observed_names:
                    for match in re.finditer(re.escape(observed_name), text, re.IGNORECASE):
                        matches.append(
                            EntityMatch(
                                start=match.start(),
                                end=match.end(),
                                standard_name=standard_name,
                                observed_name=match.group(0),
                                domain=domain,
                                confidence=0.92 if observed_name == standard_name else 0.88,
                                method="dictionary" if observed_name == standard_name else "alias",
                            )
                        )
        for domain, pattern in PATTERN_TERMS:
            for match in pattern.finditer(text):
                term_match = match.group("term")
                term_start, term_end = match.span("term")
                normalized = re.sub(r"\s+", "", term_match).strip("，。；：、 ")
                if len(normalized) < 2:
                    continue
                matches.append(
                    EntityMatch(
                        start=term_start,
                        end=term_end,
                        standard_name=normalized,
                        observed_name=term_match,
                        domain=domain,
                        confidence=0.74,
                        method="pattern",
                    )
                )

        # Prefer the longest, highest-confidence entity when dictionary terms overlap,
        # e.g. “轴流风机” must not also create a separate “风机” occurrence.
        selected: list[EntityMatch] = []
        for candidate in sorted(
            matches,
            key=lambda item: (item.start, -(item.end - item.start), -item.confidence),
        ):
            if any(
                candidate.start < existing.end and candidate.end > existing.start
                for existing in selected
            ):
                continue
            selected.append(candidate)
        return sorted(selected, key=lambda item: item.start)

    def _find_entity(self, match: EntityMatch) -> KnowledgeEntity | None:
        candidates = self.session.scalars(
            select(KnowledgeEntity)
            .join(OntologyType, KnowledgeEntity.ontology_type_id == OntologyType.id)
            .where(
                KnowledgeEntity.standard_name == match.standard_name,
                OntologyType.domain == match.domain,
                KnowledgeEntity.status.in_(
                    [ReviewStatus.CANDIDATE, ReviewStatus.APPROVED, ReviewStatus.PUBLISHED]
                ),
            )
            .order_by(KnowledgeEntity.created_at)
        ).all()
        return next(
            (item for item in candidates if item.status == ReviewStatus.PUBLISHED),
            candidates[0] if candidates else None,
        )

    def _find_relation(
        self, source_id: str, relation_type: str, target_id: str
    ) -> KnowledgeRelation | None:
        candidates = self.session.scalars(
            select(KnowledgeRelation)
            .where(
                KnowledgeRelation.source_entity_id == source_id,
                KnowledgeRelation.target_entity_id == target_id,
                KnowledgeRelation.relation_type == relation_type,
                KnowledgeRelation.status.in_(
                    [ReviewStatus.CANDIDATE, ReviewStatus.APPROVED, ReviewStatus.PUBLISHED]
                ),
            )
            .order_by(KnowledgeRelation.created_at)
        ).all()
        return next(
            (item for item in candidates if item.status == ReviewStatus.PUBLISHED),
            candidates[0] if candidates else None,
        )

    def _fact_has_chunk_evidence(
        self, target_type: str, target_id: str, chunk_id: str
    ) -> bool:
        """Return whether this canonical fact is already supported by one chunk.

        Facts are deduplicated globally, while provenance is intentionally kept
        at chunk granularity.  This lets two chunks in the same document support
        one fact independently and makes a later chunk deletion safe.
        """
        return self.session.scalar(
            select(KnowledgeEvidenceLink.id)
            .join(Evidence, KnowledgeEvidenceLink.evidence_id == Evidence.id)
            .where(
                KnowledgeEvidenceLink.target_type == target_type,
                KnowledgeEvidenceLink.target_id == target_id,
                Evidence.chunk_id == chunk_id,
            )
        ) is not None

    def _create_fact_evidence(
        self,
        *,
        target_type: str,
        target_id: str,
        document: SourceDocument,
        chunk: Chunk,
        quote: str,
        extraction_method: str,
        confidence: float,
        review_status: ReviewStatus,
    ) -> Evidence:
        evidence = Evidence(
            chunk_id=chunk.id,
            quote=quote[:1000],
            source_location=chunk.content_location,
            review_status=review_status,
            valid_from=document.effective_date,
        )
        self.session.add(evidence)
        self.session.flush()
        self.session.add(
            KnowledgeEvidenceLink(
                target_type=target_type,
                target_id=target_id,
                evidence_id=evidence.id,
                document_id=document.id,
                extraction_method=extraction_method,
                confidence=confidence,
            )
        )
        self.session.flush()
        return evidence

    def _link_existing_evidence(
        self,
        *,
        target_type: str,
        target_id: str,
        evidence: Evidence,
        extraction_method: str,
        confidence: float,
    ) -> None:
        document_id = self.session.scalar(
            select(Chunk.document_id).where(Chunk.id == evidence.chunk_id)
        )
        if document_id is None:
            return
        self.session.add(
            KnowledgeEvidenceLink(
                target_type=target_type,
                target_id=target_id,
                evidence_id=evidence.id,
                document_id=document_id,
                extraction_method=extraction_method,
                confidence=confidence,
            )
        )
        self.session.flush()

    @staticmethod
    def _relation_conditions(sentence: str) -> dict:
        measurements = list(dict.fromkeys(
            re.sub(r"\s+", "", match.group(0))
            for match in MEASUREMENT_PATTERN.finditer(sentence)
        ))
        return {"measurements": measurements} if measurements else {}

    def _set_fact_evidence_status(
        self, target_type: str, target_id: str, status: ReviewStatus
    ) -> None:
        links = self.session.scalars(
            select(KnowledgeEvidenceLink).where(
                KnowledgeEvidenceLink.target_type == target_type,
                KnowledgeEvidenceLink.target_id == target_id,
            )
        ).all()
        for link in links:
            link.evidence.review_status = status

    def extract_candidates(
        self, document_id: str, access: AccessContext, *,
        excluded_entities: set[tuple[str, str]] | None = None,
        excluded_relations: set[tuple[str, str, str]] | None = None,
    ) -> ExtractionResult:
        access.require("knowledge:propose")
        document = self.session.get(SourceDocument, document_id)
        if document is None:
            raise NotFoundError("源文档", document_id)
        access.require_access(document.security_level)
        ontologies = self.session.scalars(
            select(OntologyType).where(OntologyType.active.is_(True))
        ).all()
        if not ontologies:
            raise AppError("ONTOLOGY_MISSING", "请先初始化领域本体")
        ontology_by_domain = {item.domain: item for item in ontologies}
        chunks = self.session.scalars(
            select(Chunk).where(
                Chunk.document_id == document_id,
                Chunk.processing_version == document.processing_version,
            ).order_by(Chunk.ordinal)
        ).all()
        stored_texts = (
            self.content_store.get_chunk_texts(chunk.id for chunk in chunks)
            if self.content_store
            else {}
        )

        entity_ids: list[str] = []
        relation_ids: list[str] = []
        evidence_count = 0
        entity_cache: dict[tuple[OntologyDomain, str], KnowledgeEntity] = {}
        for chunk in chunks:
            chunk_text = stored_texts.get(chunk.id, chunk.text)
            for sentence_match in SENTENCE_PATTERN.finditer(chunk_text):
                sentence = sentence_match.group(0).strip()
                if not sentence:
                    continue
                matches = self._entity_matches(sentence)
                entities: list[tuple[EntityMatch, KnowledgeEntity]] = []
                for match in matches:
                    ontology = ontology_by_domain.get(match.domain)
                    if ontology is None:
                        continue
                    if (match.standard_name, ontology.id) in (excluded_entities or set()):
                        continue
                    cache_key = (match.domain, match.standard_name)
                    entity = entity_cache.get(cache_key) or self._find_entity(match)
                    is_new = entity is None
                    if entity is None:
                        primary = Evidence(
                            chunk_id=chunk.id,
                            quote=sentence[:1000],
                            source_location=chunk.content_location,
                            valid_from=document.effective_date,
                        )
                        self.session.add(primary)
                        self.session.flush()
                        aliases = []
                        if match.observed_name.casefold() != match.standard_name.casefold():
                            aliases.append(match.observed_name)
                        entity = KnowledgeEntity(
                            ontology_type_id=ontology.id,
                            standard_name=match.standard_name,
                            aliases=aliases,
                            properties={
                                "extraction_method": match.method,
                                "ontology_version": ontology.version,
                            },
                            confidence=match.confidence,
                            security_level=document.security_level,
                            evidence_id=primary.id,
                            knowledge_version=(self.session.scalar(select(func.max(KnowledgeEntity.knowledge_version)).where(
                                KnowledgeEntity.standard_name == match.standard_name,
                                KnowledgeEntity.ontology_type_id == ontology.id,
                            )) or 0) + 1,
                        )
                        self.session.add(entity)
                        self.session.flush()
                        self.session.add(
                            KnowledgeEvidenceLink(
                                target_type="entity",
                                target_id=entity.id,
                                evidence_id=primary.id,
                                document_id=document.id,
                                extraction_method=match.method,
                                confidence=match.confidence,
                            )
                        )
                        evidence_count += 1
                        entity_ids.append(entity.id)
                    elif not self._fact_has_chunk_evidence("entity", entity.id, chunk.id):
                        self._create_fact_evidence(
                            target_type="entity",
                            target_id=entity.id,
                            document=document,
                            chunk=chunk,
                            quote=sentence,
                            extraction_method=match.method,
                            confidence=match.confidence,
                            review_status=entity.status,
                        )
                        evidence_count += 1
                    if (
                        not is_new
                        and entity.status == ReviewStatus.CANDIDATE
                        and match.observed_name.casefold() != entity.standard_name.casefold()
                    ):
                        entity.aliases = self._clean_aliases([*entity.aliases, match.observed_name])
                    entity_cache[cache_key] = entity
                    entities.append((match, entity))

                for (left_match, left), (right_match, right) in zip(
                    entities, entities[1:]
                ):
                    if left.id == right.id:
                        continue
                    between = sentence[left_match.end:right_match.start]
                    triggered = [
                        (between.find(trigger), label)
                        for trigger, label in RELATION_TRIGGERS
                        if trigger in between
                    ]
                    if not triggered:
                        continue
                    _, relation_type = min(triggered, key=lambda item: item[0])
                    if (left.standard_name, relation_type, right.standard_name) in (excluded_relations or set()):
                        continue
                    relation = self._find_relation(left.id, relation_type, right.id)
                    confidence = min(left_match.confidence, right_match.confidence, 0.9)
                    if relation is None:
                        primary = Evidence(
                            chunk_id=chunk.id,
                            quote=sentence[:1000],
                            source_location=chunk.content_location,
                            valid_from=document.effective_date,
                        )
                        self.session.add(primary)
                        self.session.flush()
                        relation = KnowledgeRelation(
                            source_entity_id=left.id,
                            target_entity_id=right.id,
                            relation_type=relation_type,
                            applicable_conditions=self._relation_conditions(sentence),
                            confidence=confidence,
                            security_level=document.security_level,
                            evidence_id=primary.id,
                            knowledge_version=(self.session.scalar(select(func.max(KnowledgeRelation.knowledge_version)).where(
                                KnowledgeRelation.source_entity_id == left.id,
                                KnowledgeRelation.target_entity_id == right.id,
                                KnowledgeRelation.relation_type == relation_type,
                            )) or 0) + 1,
                        )
                        self.session.add(relation)
                        self.session.flush()
                        self.session.add(
                            KnowledgeEvidenceLink(
                                target_type="relation",
                                target_id=relation.id,
                                evidence_id=primary.id,
                                document_id=document.id,
                                extraction_method="relation_trigger",
                                confidence=confidence,
                            )
                        )
                        evidence_count += 1
                        relation_ids.append(relation.id)
                    elif not self._fact_has_chunk_evidence(
                        "relation", relation.id, chunk.id
                    ):
                        self._create_fact_evidence(
                            target_type="relation",
                            target_id=relation.id,
                            document=document,
                            chunk=chunk,
                            quote=sentence,
                            extraction_method="relation_trigger",
                            confidence=confidence,
                            review_status=relation.status,
                        )
                        evidence_count += 1
        self.session.commit()
        return ExtractionResult(
            document_id=document_id,
            entity_ids=entity_ids,
            relation_ids=relation_ids,
            evidence_count=evidence_count,
        )

    def review(self, payload: ReviewRequest, access: AccessContext) -> KnowledgeReview:
        access.require("knowledge:review")
        if payload.decision not in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}:
            raise AppError("INVALID_REVIEW_DECISION", "审核决定只能为 approved 或 rejected")
        if payload.target_type not in {"entity", "relation"}:
            raise AppError("INVALID_TARGET_TYPE", "target_type 必须是 entity 或 relation")
        target: KnowledgeEntity | KnowledgeRelation | None = (
            self.session.get(KnowledgeEntity, payload.target_id)
            if payload.target_type == "entity"
            else self.session.get(KnowledgeRelation, payload.target_id)
        )
        if target is None:
            raise NotFoundError("待审核知识", payload.target_id)
        access.require_access(target.security_level)
        if target.status != ReviewStatus.CANDIDATE:
            raise ConflictError("该知识已完成审核，不能重复审核")
        target.status = payload.decision
        target.evidence.review_status = payload.decision
        self._set_fact_evidence_status(payload.target_type, target.id, payload.decision)
        review = KnowledgeReview(
            target_type=payload.target_type,
            target_id=payload.target_id,
            decision=payload.decision,
            reviewer_id=access.user_id,
            comment=payload.comment,
        )
        self.session.add(review)
        self.session.commit()
        return review

    def auto_review_high_confidence(
        self,
        extraction: ExtractionResult,
        access: AccessContext,
        *,
        comment: str,
        endpoint_entity_ids: set[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Approve deterministic high-confidence facts and leave patterns for experts."""
        approved_entities: list[str] = []
        for entity_id in extraction.entity_ids:
            entity = self.session.get(KnowledgeEntity, entity_id)
            if entity is None or entity.status != ReviewStatus.CANDIDATE or entity.confidence < AUTO_PUBLISH_CONFIDENCE:
                continue
            self.review(
                ReviewRequest(
                    target_type="entity",
                    target_id=entity_id,
                    decision=ReviewStatus.APPROVED,
                    comment=comment,
                ),
                access,
            )
            approved_entities.append(entity_id)

        approved_relations: list[str] = []
        for relation_id in extraction.relation_ids:
            relation = self.session.get(KnowledgeRelation, relation_id)
            if relation is None or relation.status != ReviewStatus.CANDIDATE or relation.confidence < AUTO_PUBLISH_CONFIDENCE:
                continue
            if endpoint_entity_ids is not None and any(
                endpoint.status != ReviewStatus.PUBLISHED and endpoint.id not in endpoint_entity_ids
                for endpoint in (relation.source_entity, relation.target_entity)
            ):
                continue
            if relation.source_entity.status not in {
                ReviewStatus.APPROVED,
                ReviewStatus.PUBLISHED,
            } or relation.target_entity.status not in {
                ReviewStatus.APPROVED,
                ReviewStatus.PUBLISHED,
            }:
                continue
            self.review(
                ReviewRequest(
                    target_type="relation",
                    target_id=relation_id,
                    decision=ReviewStatus.APPROVED,
                    comment=comment,
                ),
                access,
            )
            approved_relations.append(relation_id)
        return approved_entities, approved_relations

    def review_batch(
        self, payload: BatchReviewRequest, access: AccessContext
    ) -> dict[str, int]:
        access.require("knowledge:review")
        prepared: list[tuple[ReviewRequest, KnowledgeEntity | KnowledgeRelation]] = []
        seen: set[tuple[str, str]] = set()
        decisions: dict[tuple[str, str], ReviewStatus] = {}
        for item in payload.items:
            key = (item.target_type, item.target_id)
            if key in seen:
                raise ConflictError("同一候选知识不能在一次批量审核中重复提交")
            seen.add(key)
            if item.decision not in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}:
                raise AppError("INVALID_REVIEW_DECISION", "审核决定只能为 approved 或 rejected")
            if item.target_type not in {"entity", "relation"}:
                raise AppError("INVALID_TARGET_TYPE", "target_type 必须是 entity 或 relation")
            target: KnowledgeEntity | KnowledgeRelation | None = (
                self.session.get(KnowledgeEntity, item.target_id)
                if item.target_type == "entity"
                else self.session.get(KnowledgeRelation, item.target_id)
            )
            if target is None:
                raise NotFoundError("待审核知识", item.target_id)
            access.require_access(target.security_level)
            if target.status != ReviewStatus.CANDIDATE:
                raise ConflictError("批量审核包含已完成审核的知识，请刷新后重试")
            prepared.append((item, target))
            decisions[key] = item.decision

        auto_approved_endpoints = 0
        for item, target in list(prepared):
            if item.target_type != "relation" or item.decision != ReviewStatus.APPROVED:
                continue
            assert isinstance(target, KnowledgeRelation)
            for endpoint in (target.source_entity, target.target_entity):
                endpoint_key = ("entity", endpoint.id)
                selected_decision = decisions.get(endpoint_key)
                if selected_decision == ReviewStatus.REJECTED:
                    raise ConflictError("不能在加入关系的同时驳回该关系的起点或终点实体")
                if endpoint.status in {ReviewStatus.APPROVED, ReviewStatus.PUBLISHED}:
                    continue
                if endpoint.status != ReviewStatus.CANDIDATE:
                    raise ConflictError("关系端点实体当前不可发布，请先处理实体状态")
                if selected_decision == ReviewStatus.APPROVED:
                    continue
                access.require_access(endpoint.security_level)
                endpoint_review = ReviewRequest(
                    target_type="entity",
                    target_id=endpoint.id,
                    decision=ReviewStatus.APPROVED,
                    comment="批量加入关系时自动通过其候选端点实体",
                )
                prepared.append((endpoint_review, endpoint))
                decisions[endpoint_key] = ReviewStatus.APPROVED
                auto_approved_endpoints += 1

        approved = 0
        rejected = 0
        for item, target in prepared:
            target.status = item.decision
            target.evidence.review_status = item.decision
            self._set_fact_evidence_status(item.target_type, target.id, item.decision)
            self.session.add(
                KnowledgeReview(
                    target_type=item.target_type,
                    target_id=item.target_id,
                    decision=item.decision,
                    reviewer_id=access.user_id,
                    comment=item.comment,
                )
            )
            if item.decision == ReviewStatus.APPROVED:
                approved += 1
            else:
                rejected += 1
        self.session.commit()
        return {
            "processed": len(prepared),
            "approved": approved,
            "rejected": rejected,
            "auto_approved_endpoints": auto_approved_endpoints,
        }

    def publish(
        self, access: AccessContext, notes: str = "", *,
        entity_ids: Sequence[str] | None = None,
        relation_ids: Sequence[str] | None = None,
    ) -> PublishResult:
        access.require("knowledge:publish")
        entity_query = select(KnowledgeEntity).where(KnowledgeEntity.status == ReviewStatus.APPROVED)
        relation_query = select(KnowledgeRelation).where(KnowledgeRelation.status == ReviewStatus.APPROVED)
        # Supplying either scope makes the omitted scope empty, never global.
        if entity_ids is not None or relation_ids is not None:
            entity_query = entity_query.where(KnowledgeEntity.id.in_(entity_ids or []))
            relation_query = relation_query.where(KnowledgeRelation.id.in_(relation_ids or []))
        approved_entities = self.session.scalars(entity_query).all()
        approved_relations = self.session.scalars(relation_query).all()
        if not approved_entities and not approved_relations:
            raise AppError("NOTHING_TO_PUBLISH", "没有已审核通过的知识")
        for fact in [*approved_entities, *approved_relations]:
            access.require_access(fact.security_level)
        selected_entity_ids = {entity.id for entity in approved_entities}
        for relation in approved_relations:
            for endpoint in (relation.source_entity, relation.target_entity):
                access.require_access(endpoint.security_level)
                if endpoint.status != ReviewStatus.PUBLISHED and endpoint.id not in selected_entity_ids:
                    raise AppError("RELATION_ENDPOINT_UNPUBLISHED", "关系端点尚未发布且不在本次发布范围内")

        last_version = self.session.scalar(select(func.max(KnowledgeRelease.version))) or 0
        # Retired candidates can have versions without a release. Publishing a
        # replacement must not collide with their retained unique fact keys.
        for model in (KnowledgeEntity, KnowledgeRelation):
            retired_version = self.session.scalar(select(func.max(model.knowledge_version)).where(
                model.status.in_([ReviewStatus.ARCHIVED, ReviewStatus.REJECTED])
            )) or 0
            last_version = max(last_version, retired_version)
        version = last_version + 1
        for entity in approved_entities:
            entity.status = ReviewStatus.PUBLISHED
            entity.knowledge_version = version
            entity.evidence.review_status = ReviewStatus.PUBLISHED
            self._set_fact_evidence_status("entity", entity.id, ReviewStatus.PUBLISHED)
        for relation in approved_relations:
            relation.status = ReviewStatus.PUBLISHED
            relation.knowledge_version = version
            relation.evidence.review_status = ReviewStatus.PUBLISHED
            self._set_fact_evidence_status("relation", relation.id, ReviewStatus.PUBLISHED)
        release = KnowledgeRelease(
            version=version,
            entity_count=len(approved_entities),
            relation_count=len(approved_relations),
            published_by=access.user_id,
            published_at=utc_now(),
            notes=notes,
        )
        self.session.add(release)

        chunk_ids = {entity.evidence.chunk_id for entity in approved_entities}
        document_ids = set(
            self.session.scalars(select(Chunk.document_id).where(Chunk.id.in_(chunk_ids))).all()
        )
        if document_ids:
            documents = self.session.scalars(
                select(SourceDocument).where(SourceDocument.id.in_(document_ids))
            ).all()
            for document in documents:
                document.status = ProcessingStatus.PUBLISHED
        self.session.commit()
        return PublishResult(
            version=version,
            entity_count=len(approved_entities),
            relation_count=len(approved_relations),
            release_id=release.id,
        )

    @staticmethod
    def _node_read(entity: KnowledgeEntity) -> GraphNode:
        return GraphNode(
            id=entity.id,
            name=entity.standard_name,
            type=entity.ontology_type.name,
            domain=entity.ontology_type.domain,
            properties=entity.properties,
            evidence_id=entity.evidence_id,
            aliases=entity.aliases,
            confidence=entity.confidence,
            security_level=entity.security_level,
        )

    @staticmethod
    def _edge_read(relation: KnowledgeRelation) -> GraphEdge:
        return GraphEdge(
            id=relation.id,
            source=relation.source_entity_id,
            target=relation.target_entity_id,
            relation_type=relation.relation_type,
            applicable_conditions=relation.applicable_conditions,
            evidence_id=relation.evidence_id,
            confidence=relation.confidence,
            security_level=relation.security_level,
        )

    def _ontology_for_domain(self, domain: OntologyDomain) -> OntologyType:
        ontology = self.session.scalar(
            select(OntologyType).where(
                OntologyType.domain == domain,
                OntologyType.active.is_(True),
            ).order_by(OntologyType.version.desc())
        )
        if ontology:
            return ontology
        payload = next(item for item in DEFAULT_ONTOLOGY if item.domain == domain)
        ontology = OntologyType(**payload.model_dump())
        self.session.add(ontology)
        self.session.flush()
        return ontology

    def _manual_evidence(self, quote: str, access: AccessContext) -> Evidence:
        source_name = next(iter(SYSTEM_MANAGED_SOURCE_NAMES))
        source_id = next(iter(SYSTEM_MANAGED_SOURCE_IDS))
        source = self.session.scalar(select(DataSource).where(DataSource.name == source_name))
        if source is None:
            source = DataSource(
                name=source_name,
                source_type=SourceType.EXPERT_EXPERIENCE,
                connection_type="system",
                owner="知识图谱人工维护",
                security_level=SecurityLevel.INTERNAL,
                sync_policy={"mode": "manual_graph"},
            )
            self.session.add(source)
            self.session.flush()
        document = self.session.scalar(
            select(SourceDocument).where(SourceDocument.source_id == source_id)
        )
        if document is None:
            document = SourceDocument(
                source_id=source_id,
                data_source_id=source.id,
                title="知识图谱人工维护记录",
                source_type=SourceType.EXPERT_EXPERIENCE,
                version="SYSTEM",
                effective_date=date.today().isoformat(),
                security_level=SecurityLevel.INTERNAL,
                owner_org="知识图谱人工维护",
                checksum=hashlib.sha256(b"tunnel-qa-manual-graph-records").hexdigest(),
                content_location="系统/知识图谱/人工维护",
                original_filename="manual-graph-records.system",
                mime_type="application/x-tunnel-graph-record",
                storage_uri="system://knowledge-graph/manual",
                parsed_text="人工维护的知识图谱实体与关系审计载体。",
                status=ProcessingStatus.ARCHIVED,
                metadata_extra={"system_managed": True, "hidden": True},
            )
            self.session.add(document)
            self.session.flush()
        chunk = self.session.scalar(
            select(Chunk).where(Chunk.document_id == document.id, Chunk.ordinal == 1)
        )
        if chunk is None:
            chunk_text = "人工维护的知识图谱实体与关系审计载体。"
            chunk = Chunk(
                document_id=document.id,
                processing_version=1,
                ordinal=1,
                text=chunk_text,
                structure_path="知识图谱/人工维护",
                content_location="系统/知识图谱/人工维护",
                security_level=SecurityLevel.INTERNAL,
                quality_status="manual",
                char_count=len(chunk_text),
            )
            self.session.add(chunk)
            self.session.flush()
        evidence = Evidence(
            chunk_id=chunk.id,
            quote=quote[:1000],
            source_location=f"知识图谱/人工维护/{access.user_id}",
            review_status=ReviewStatus.PUBLISHED,
            valid_from=date.today().isoformat(),
        )
        self.session.add(evidence)
        self.session.flush()
        return evidence

    def _next_knowledge_version(self) -> int:
        return (self.session.scalar(select(func.max(KnowledgeRelease.version))) or 0) + 1

    def _record_manual_change(
        self,
        *,
        version: int,
        entity_count: int,
        relation_count: int,
        target_type: str,
        target_id: str,
        note: str,
        access: AccessContext,
        decision: ReviewStatus = ReviewStatus.PUBLISHED,
    ) -> None:
        self.session.add(
            KnowledgeReview(
                target_type=target_type,
                target_id=target_id,
                decision=decision,
                reviewer_id=access.user_id,
                comment=note,
            )
        )
        self.session.add(
            KnowledgeRelease(
                version=version,
                status="published",
                entity_count=entity_count,
                relation_count=relation_count,
                published_by=access.user_id,
                published_at=utc_now(),
                notes=note,
            )
        )

    @staticmethod
    def _clean_aliases(aliases: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in aliases if item.strip()))

    def list_entities(
        self, query: str | None, limit: int, access: AccessContext
    ) -> list[GraphNode]:
        access.require("knowledge:read")
        statement = (
            select(KnowledgeEntity)
            .where(KnowledgeEntity.status == ReviewStatus.PUBLISHED)
            .order_by(KnowledgeEntity.standard_name)
        )
        if query and query.strip():
            statement = statement.where(
                KnowledgeEntity.standard_name.contains(query.strip())
            )
        entities = [
            entity
            for entity in self.session.scalars(statement).all()
            if access.can_access(entity.security_level)
        ]
        return [self._node_read(entity) for entity in entities[:limit]]

    def create_manual_entity(
        self, payload: ManualEntityCreate, access: AccessContext
    ) -> GraphNode:
        access.require("knowledge:publish")
        name = payload.standard_name.strip()
        ontology = self._ontology_for_domain(payload.domain)
        duplicate = self.session.scalar(
            select(KnowledgeEntity).join(
                OntologyType, KnowledgeEntity.ontology_type_id == OntologyType.id
            ).where(
                KnowledgeEntity.standard_name == name,
                OntologyType.domain == payload.domain,
                KnowledgeEntity.status == ReviewStatus.PUBLISHED,
            )
        )
        if duplicate:
            raise ConflictError("同名、同类型的已发布实体已经存在", field="standard_name")
        evidence = self._manual_evidence(f"人工新增实体：{name}", access)
        version = self._next_knowledge_version()
        entity = KnowledgeEntity(
            ontology_type_id=ontology.id,
            standard_name=name,
            aliases=self._clean_aliases(payload.aliases),
            properties=payload.properties,
            status=ReviewStatus.PUBLISHED,
            confidence=payload.confidence,
            knowledge_version=version,
            security_level=payload.security_level,
            evidence_id=evidence.id,
        )
        self.session.add(entity)
        self.session.flush()
        self._link_existing_evidence(
            target_type="entity",
            target_id=entity.id,
            evidence=evidence,
            extraction_method="manual",
            confidence=payload.confidence,
        )
        self._record_manual_change(
            version=version,
            entity_count=1,
            relation_count=0,
            target_type="entity",
            target_id=entity.id,
            note=f"人工新增实体：{name}",
            access=access,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("实体保存冲突，请刷新图谱后重试") from exc
        return self._node_read(entity)

    def update_manual_entity(
        self, entity_id: str, payload: ManualEntityUpdate, access: AccessContext
    ) -> GraphNode:
        access.require("knowledge:publish")
        entity = self.session.get(KnowledgeEntity, entity_id)
        if entity is None or entity.status != ReviewStatus.PUBLISHED:
            raise NotFoundError("已发布实体", entity_id)
        access.require_access(entity.security_level)
        name = payload.standard_name.strip() if payload.standard_name is not None else entity.standard_name
        ontology = (
            self._ontology_for_domain(payload.domain)
            if payload.domain is not None
            else entity.ontology_type
        )
        duplicate = self.session.scalar(
            select(KnowledgeEntity).join(
                OntologyType, KnowledgeEntity.ontology_type_id == OntologyType.id
            ).where(
                KnowledgeEntity.id != entity.id,
                KnowledgeEntity.standard_name == name,
                OntologyType.domain == ontology.domain,
                KnowledgeEntity.status == ReviewStatus.PUBLISHED,
            )
        )
        if duplicate:
            raise ConflictError("同名、同类型的已发布实体已经存在", field="standard_name")
        entity.standard_name = name
        entity.ontology_type_id = ontology.id
        if payload.aliases is not None:
            entity.aliases = self._clean_aliases(payload.aliases)
        if payload.properties is not None:
            entity.properties = payload.properties
        if payload.confidence is not None:
            entity.confidence = payload.confidence
        if payload.security_level is not None:
            entity.security_level = payload.security_level
        version = self._next_knowledge_version()
        entity.knowledge_version = version
        change_evidence = self._manual_evidence(f"人工修改实体：{name}", access)
        self._link_existing_evidence(
            target_type="entity",
            target_id=entity.id,
            evidence=change_evidence,
            extraction_method="manual_update",
            confidence=entity.confidence,
        )
        self._record_manual_change(
            version=version,
            entity_count=1,
            relation_count=0,
            target_type="entity",
            target_id=entity.id,
            note=f"人工修改实体：{name}",
            access=access,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("实体修改冲突，请刷新图谱后重试") from exc
        return self._node_read(entity)

    def delete_manual_entity(
        self, entity_id: str, access: AccessContext
    ) -> dict[str, str | int]:
        access.require("knowledge:publish")
        entity = self.session.get(KnowledgeEntity, entity_id)
        if entity is None or entity.status != ReviewStatus.PUBLISHED:
            raise NotFoundError("已发布实体", entity_id)
        access.require_access(entity.security_level)
        relations = self.session.scalars(
            select(KnowledgeRelation).where(
                KnowledgeRelation.status == ReviewStatus.PUBLISHED,
                or_(
                    KnowledgeRelation.source_entity_id == entity.id,
                    KnowledgeRelation.target_entity_id == entity.id,
                ),
            )
        ).all()
        for relation in relations:
            access.require_access(relation.security_level)
        version = self._next_knowledge_version()
        entity.status = ReviewStatus.ARCHIVED
        entity.knowledge_version = version
        for relation in relations:
            relation.status = ReviewStatus.ARCHIVED
            relation.knowledge_version = version
        self._record_manual_change(
            version=version,
            entity_count=1,
            relation_count=len(relations),
            target_type="entity",
            target_id=entity.id,
            note=f"人工删除实体及关联关系：{entity.standard_name}",
            access=access,
            decision=ReviewStatus.ARCHIVED,
        )
        self.session.commit()
        return {
            "entity_id": entity.id,
            "deleted_relations": len(relations),
            "knowledge_version": version,
        }

    def _relation_entities(
        self, source_id: str, target_id: str, access: AccessContext
    ) -> tuple[KnowledgeEntity, KnowledgeEntity]:
        if source_id == target_id:
            raise AppError("INVALID_RELATION_ENDPOINTS", "关系起点和终点不能相同")
        source = self.session.get(KnowledgeEntity, source_id)
        target = self.session.get(KnowledgeEntity, target_id)
        if source is None or source.status != ReviewStatus.PUBLISHED:
            raise NotFoundError("关系起点实体", source_id)
        if target is None or target.status != ReviewStatus.PUBLISHED:
            raise NotFoundError("关系终点实体", target_id)
        access.require_access(source.security_level)
        access.require_access(target.security_level)
        return source, target

    def create_manual_relation(
        self, payload: ManualRelationCreate, access: AccessContext
    ) -> GraphEdge:
        access.require("knowledge:publish")
        source, target = self._relation_entities(
            payload.source_entity_id, payload.target_entity_id, access
        )
        relation_type = payload.relation_type.strip()
        duplicate = self.session.scalar(
            select(KnowledgeRelation).where(
                KnowledgeRelation.source_entity_id == source.id,
                KnowledgeRelation.target_entity_id == target.id,
                KnowledgeRelation.relation_type == relation_type,
                KnowledgeRelation.status == ReviewStatus.PUBLISHED,
            )
        )
        if duplicate:
            raise ConflictError("相同起点、关系和终点的已发布关系已经存在")
        evidence = self._manual_evidence(
            f"人工新增关系：{source.standard_name} -[{relation_type}]-> {target.standard_name}",
            access,
        )
        version = self._next_knowledge_version()
        security_level = max(
            (source.security_level, target.security_level), key=SECURITY_RANK.__getitem__
        )
        relation = KnowledgeRelation(
            source_entity_id=source.id,
            target_entity_id=target.id,
            relation_type=relation_type,
            applicable_conditions=payload.applicable_conditions,
            confidence=payload.confidence,
            status=ReviewStatus.PUBLISHED,
            knowledge_version=version,
            security_level=security_level,
            evidence_id=evidence.id,
        )
        self.session.add(relation)
        self.session.flush()
        self._link_existing_evidence(
            target_type="relation",
            target_id=relation.id,
            evidence=evidence,
            extraction_method="manual",
            confidence=payload.confidence,
        )
        self._record_manual_change(
            version=version,
            entity_count=0,
            relation_count=1,
            target_type="relation",
            target_id=relation.id,
            note=f"人工新增关系：{source.standard_name} → {target.standard_name}",
            access=access,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("关系保存冲突，请刷新图谱后重试") from exc
        return self._edge_read(relation)

    def update_manual_relation(
        self, relation_id: str, payload: ManualRelationUpdate, access: AccessContext
    ) -> GraphEdge:
        access.require("knowledge:publish")
        relation = self.session.get(KnowledgeRelation, relation_id)
        if relation is None or relation.status != ReviewStatus.PUBLISHED:
            raise NotFoundError("已发布关系", relation_id)
        access.require_access(relation.security_level)
        source_id = payload.source_entity_id or relation.source_entity_id
        target_id = payload.target_entity_id or relation.target_entity_id
        source, target = self._relation_entities(source_id, target_id, access)
        relation_type = (
            payload.relation_type.strip()
            if payload.relation_type is not None
            else relation.relation_type
        )
        duplicate = self.session.scalar(
            select(KnowledgeRelation).where(
                KnowledgeRelation.id != relation.id,
                KnowledgeRelation.source_entity_id == source.id,
                KnowledgeRelation.target_entity_id == target.id,
                KnowledgeRelation.relation_type == relation_type,
                KnowledgeRelation.status == ReviewStatus.PUBLISHED,
            )
        )
        if duplicate:
            raise ConflictError("相同起点、关系和终点的已发布关系已经存在")
        relation.source_entity_id = source.id
        relation.target_entity_id = target.id
        relation.relation_type = relation_type
        if payload.applicable_conditions is not None:
            relation.applicable_conditions = payload.applicable_conditions
        if payload.confidence is not None:
            relation.confidence = payload.confidence
        relation.security_level = max(
            (source.security_level, target.security_level), key=SECURITY_RANK.__getitem__
        )
        version = self._next_knowledge_version()
        relation.knowledge_version = version
        change_evidence = self._manual_evidence(
            f"人工修改关系：{source.standard_name} -[{relation_type}]-> {target.standard_name}",
            access,
        )
        self._link_existing_evidence(
            target_type="relation",
            target_id=relation.id,
            evidence=change_evidence,
            extraction_method="manual_update",
            confidence=relation.confidence,
        )
        self._record_manual_change(
            version=version,
            entity_count=0,
            relation_count=1,
            target_type="relation",
            target_id=relation.id,
            note=f"人工修改关系：{source.standard_name} → {target.standard_name}",
            access=access,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("关系修改冲突，请刷新图谱后重试") from exc
        return self._edge_read(relation)

    def delete_manual_relation(
        self, relation_id: str, access: AccessContext
    ) -> dict[str, str | int]:
        access.require("knowledge:publish")
        relation = self.session.get(KnowledgeRelation, relation_id)
        if relation is None or relation.status != ReviewStatus.PUBLISHED:
            raise NotFoundError("已发布关系", relation_id)
        access.require_access(relation.security_level)
        version = self._next_knowledge_version()
        relation.status = ReviewStatus.ARCHIVED
        relation.knowledge_version = version
        self._record_manual_change(
            version=version,
            entity_count=0,
            relation_count=1,
            target_type="relation",
            target_id=relation.id,
            note=(
                "人工删除关系："
                f"{relation.source_entity.standard_name} → {relation.target_entity.standard_name}"
            ),
            access=access,
            decision=ReviewStatus.ARCHIVED,
        )
        self.session.commit()
        return {
            "relation_id": relation.id,
            "knowledge_version": version,
        }

    def get_workspace(self, limit: int, access: AccessContext) -> Subgraph:
        """Return a stable manual-maintenance canvas without discarding unrelated nodes."""
        access.require("knowledge:read")
        entities = [
            item
            for item in self.session.scalars(
                select(KnowledgeEntity)
                .where(KnowledgeEntity.status == ReviewStatus.PUBLISHED)
                .order_by(KnowledgeEntity.standard_name)
                .limit(limit)
            ).all()
            if access.can_access(item.security_level)
        ]
        node_ids = {item.id for item in entities}
        relations = (
            [
                item
                for item in self.session.scalars(
                    select(KnowledgeRelation).where(
                        KnowledgeRelation.status == ReviewStatus.PUBLISHED,
                        KnowledgeRelation.source_entity_id.in_(node_ids),
                        KnowledgeRelation.target_entity_id.in_(node_ids),
                    )
                ).all()
                if access.can_access(item.security_level)
            ]
            if node_ids
            else []
        )
        return Subgraph(
            nodes=[self._node_read(item) for item in entities],
            edges=[self._edge_read(item) for item in relations],
        )

    def get_subgraph(
        self, entity_query: str, depth: int, access: AccessContext
    ) -> Subgraph:
        access.require("knowledge:read")
        if depth < 0 or depth > 5:
            raise AppError("INVALID_DEPTH", "子图深度必须在 0 到 5 之间", field="depth")
        candidates = self.session.scalars(
            select(KnowledgeEntity).where(
                KnowledgeEntity.status == ReviewStatus.PUBLISHED,
                KnowledgeEntity.standard_name.contains(entity_query),
            )
        ).all()
        visible = [entity for entity in candidates if access.can_access(entity.security_level)]
        if not visible:
            return Subgraph(nodes=[], edges=[])

        node_map: dict[str, KnowledgeEntity] = {entity.id: entity for entity in visible}
        edge_map: dict[str, KnowledgeRelation] = {}
        queue = deque((entity.id, 0) for entity in visible)
        traversed: set[str] = set()
        while queue:
            entity_id, level = queue.popleft()
            if entity_id in traversed or level >= depth:
                continue
            traversed.add(entity_id)
            relations = self.session.scalars(
                select(KnowledgeRelation).where(
                    KnowledgeRelation.status == ReviewStatus.PUBLISHED,
                    or_(
                        KnowledgeRelation.source_entity_id == entity_id,
                        KnowledgeRelation.target_entity_id == entity_id,
                    ),
                )
            ).all()
            for relation in relations:
                if not access.can_access(relation.security_level):
                    continue
                other_id = (
                    relation.target_entity_id
                    if relation.source_entity_id == entity_id
                    else relation.source_entity_id
                )
                other = self.session.get(KnowledgeEntity, other_id)
                if other and access.can_access(other.security_level):
                    node_map[other.id] = other
                    edge_map[relation.id] = relation
                    queue.append((other.id, level + 1))

        nodes = [self._node_read(entity) for entity in node_map.values()]
        edges = [self._edge_read(relation) for relation in edge_map.values()]
        return Subgraph(nodes=nodes, edges=edges)

    def list_review_candidates(
        self, limit: int, access: AccessContext
    ) -> list[dict[str, object]]:
        access.require("knowledge:review")
        entities = [
            item
            for item in self.session.scalars(
                select(KnowledgeEntity)
                .where(KnowledgeEntity.status == ReviewStatus.CANDIDATE)
                .order_by(KnowledgeEntity.confidence.desc(), KnowledgeEntity.created_at)
            ).all()
            if access.can_access(item.security_level)
        ]
        relations = [
            item
            for item in self.session.scalars(
                select(KnowledgeRelation)
                .where(KnowledgeRelation.status == ReviewStatus.CANDIDATE)
                .order_by(KnowledgeRelation.confidence.desc(), KnowledgeRelation.created_at)
            ).all()
            if access.can_access(item.security_level)
        ]
        candidates: list[_ReviewCandidate] = []
        for target_type, items in (("entity", entities), ("relation", relations)):
            for item in items:
                if isinstance(item, KnowledgeRelation) and (
                    not access.can_access(item.source_entity.security_level)
                    or not access.can_access(item.target_entity.security_level)
                ):
                    continue
                # Use the same source permissions as the evidence drawer. A
                # promoted primary may now belong to a higher-clearance source.
                visible = self.list_fact_evidence(target_type, item.id, access)
                if not visible:
                    continue
                evidence = next((row for row in visible if row["evidence_id"] == item.evidence_id), visible[0])
                source_count = len({row["document_id"] for row in visible})
                if isinstance(item, KnowledgeEntity):
                    label = item.standard_name
                    domain = item.ontology_type.domain
                else:
                    label = (
                        f"{item.source_entity.standard_name} -[{item.relation_type}]-> "
                        f"{item.target_entity.standard_name}"
                    )
                    domain = item.source_entity.ontology_type.domain
                candidates.append(
                    {
                        "target_type": target_type,
                        "target_id": item.id,
                        "label": label,
                        "domain": domain,
                        "confidence": item.confidence,
                        "evidence_quote": evidence["quote"],
                        "source_location": evidence["source_location"],
                        "source_id": evidence["source_id"],
                        "source_title": evidence["source_title"],
                        "extraction_method": evidence["extraction_method"],
                        "source_count": source_count,
                    }
                )
        candidates.sort(key=lambda item: (-item["confidence"], item["label"]))
        return [dict(item) for item in candidates[:limit]]

    def list_fact_evidence(
        self, target_type: str, target_id: str, access: AccessContext
    ) -> list[dict[str, object]]:
        access.require("knowledge:read")
        if target_type not in {"entity", "relation"}:
            raise AppError("INVALID_TARGET_TYPE", "target_type 必须是 entity 或 relation")
        target: KnowledgeEntity | KnowledgeRelation | None = (
            self.session.get(KnowledgeEntity, target_id)
            if target_type == "entity"
            else self.session.get(KnowledgeRelation, target_id)
        )
        if target is None:
            raise NotFoundError("知识事实", target_id)
        access.require_access(target.security_level)
        if isinstance(target, KnowledgeRelation):
            access.require_access(target.source_entity.security_level)
            access.require_access(target.target_entity.security_level)
        links: Sequence[KnowledgeEvidenceLink | _LegacyEvidenceLink] = self.session.scalars(
            select(KnowledgeEvidenceLink)
            .options(selectinload(KnowledgeEvidenceLink.evidence))
            .where(
                KnowledgeEvidenceLink.target_type == target_type,
                KnowledgeEvidenceLink.target_id == target_id,
            )
            .order_by(KnowledgeEvidenceLink.created_at)
        ).all()
        if target.evidence is not None and not any(
            link.evidence_id == target.evidence_id for link in links
        ):
            links = [*links, _LegacyEvidenceLink(evidence=target.evidence, confidence=target.confidence)]
        evidence_items = [link.evidence for link in links if link.evidence is not None]
        chunk_ids = {evidence.chunk_id for evidence in evidence_items}
        chunks = (
            self.session.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))).all()
            if chunk_ids
            else []
        )
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        document_ids = {chunk.document_id for chunk in chunks}
        documents = (
            self.session.scalars(
                select(SourceDocument).where(SourceDocument.id.in_(document_ids))
            ).all()
            if document_ids
            else []
        )
        documents_by_id = {document.id: document for document in documents}
        # A canonical fact can have evidence from documents of different
        # clearances. Filter each source before fetching payloads or quotes.
        visible_chunk_ids = {
            chunk.id for chunk in chunks
            if access.can_access(chunk.security_level)
            and (document := documents_by_id.get(chunk.document_id)) is not None
            and access.can_access(document.security_level)
            and chunk.processing_version == document.processing_version
        }
        links = [link for link in links if link.evidence is not None
                 and link.evidence.chunk_id in visible_chunk_ids]
        result: list[dict[str, object]] = []
        chunk_texts = (
            self.content_store.get_chunk_texts(
                visible_chunk_ids
            )
            if self.content_store
            else {}
        )
        for link in links:
            evidence = link.evidence
            chunk = chunks_by_id.get(evidence.chunk_id)
            document = documents_by_id.get(chunk.document_id) if chunk else None
            result.append(
                {
                    "evidence_id": evidence.id,
                    "chunk_id": chunk.id if chunk else evidence.chunk_id,
                    "chunk_ordinal": chunk.ordinal if chunk else None,
                    "chunk_text": (
                        chunk_texts.get(chunk.id, chunk.text) if chunk else evidence.quote
                    ),
                    "chunk_char_count": chunk.char_count if chunk else len(evidence.quote),
                    "page_number": chunk.page_number if chunk else None,
                    "page_end": (processing_chunk(document.metadata_extra, chunk.ordinal).get("page_end", chunk.page_number)
                                 if document and chunk else None),
                    "structure_path": chunk.structure_path if chunk else None,
                    "quote": evidence.quote,
                    "source_location": evidence.source_location,
                    "review_status": evidence.review_status,
                    "valid_from": evidence.valid_from,
                    "source_id": document.source_id if document else None,
                    "source_title": document.title if document else None,
                    "document_id": document.id if document else link.document_id,
                    "extraction_method": link.extraction_method,
                    "confidence": link.confidence,
                }
            )
        return result

    def list_releases(self, limit: int, access: AccessContext) -> list[dict[str, object]]:
        access.require("knowledge:read")
        releases = self.session.scalars(
            select(KnowledgeRelease).order_by(KnowledgeRelease.version.desc()).limit(limit)
        ).all()
        return [
            {
                "version": item.version,
                "status": item.status,
                "entity_count": item.entity_count,
                "relation_count": item.relation_count,
                "published_by": item.published_by,
                "published_at": item.published_at,
                "notes": item.notes,
            }
            for item in releases
        ]

    def requeue_unsupported_legacy_relations(self, access: AccessContext) -> dict[str, int]:
        """Move legacy adjacency relations back to review without deleting evidence."""
        access.require("knowledge:publish")
        low_confidence = self.session.scalars(
            select(KnowledgeRelation).where(
                KnowledgeRelation.status == ReviewStatus.PUBLISHED,
                KnowledgeRelation.confidence < 0.9,
            )
        ).all()
        trusted_methods = {"relation_trigger", "manual", "manual_update"}
        requeued: list[KnowledgeRelation] = []
        for relation in low_confidence:
            methods = set(
                self.session.scalars(
                    select(KnowledgeEvidenceLink.extraction_method).where(
                        KnowledgeEvidenceLink.target_type == "relation",
                        KnowledgeEvidenceLink.target_id == relation.id,
                    )
                ).all()
            )
            if methods & trusted_methods:
                continue
            relation.status = ReviewStatus.CANDIDATE
            relation.evidence.review_status = ReviewStatus.CANDIDATE
            self._set_fact_evidence_status("relation", relation.id, ReviewStatus.CANDIDATE)
            requeued.append(relation)
        if not requeued:
            return {"requeued_relations": 0, "knowledge_version": self._next_knowledge_version() - 1}
        version = self._next_knowledge_version()
        for relation in requeued:
            relation.knowledge_version = version
            self.session.add(
                KnowledgeReview(
                    target_type="relation",
                    target_id=relation.id,
                    decision=ReviewStatus.CANDIDATE,
                    reviewer_id=access.user_id,
                    comment="V2质量迁移：缺少句内触发证据，移回人工审核",
                )
            )
        self.session.add(
            KnowledgeRelease(
                version=version,
                status="published",
                entity_count=0,
                relation_count=len(requeued),
                published_by=access.user_id,
                published_at=utc_now(),
                notes="V2质量迁移：低置信旧关系移回审核队列",
            )
        )
        self.session.commit()
        return {"requeued_relations": len(requeued), "knowledge_version": version}

    def graph_statistics(self, access: AccessContext) -> dict[str, object]:
        access.require("knowledge:read")
        entities = self.session.scalars(
            select(KnowledgeEntity).where(KnowledgeEntity.status == ReviewStatus.PUBLISHED)
        ).all()
        visible_entities = [item for item in entities if access.can_access(item.security_level)]
        visible_ids = {item.id for item in visible_entities}
        relations = self.session.scalars(
            select(KnowledgeRelation).where(KnowledgeRelation.status == ReviewStatus.PUBLISHED)
        ).all()
        visible_relations = [
            item
            for item in relations
            if access.can_access(item.security_level)
            and item.source_entity_id in visible_ids
            and item.target_entity_id in visible_ids
        ]
        visible_count = len(visible_entities)
        relation_count = len(visible_relations)
        by_domain = {domain.value: 0 for domain in OntologyDomain}
        for entity in visible_entities:
            domain_value = (
                entity.ontology_type.domain.value
                if isinstance(entity.ontology_type.domain, OntologyDomain)
                else str(entity.ontology_type.domain)
            )
            by_domain[domain_value] += 1
        pending_entities = sum(
            1
            for item in self.session.scalars(
                select(KnowledgeEntity).where(KnowledgeEntity.status == ReviewStatus.CANDIDATE)
            ).all()
            if access.can_access(item.security_level)
        )
        pending_relations = sum(
            1
            for item in self.session.scalars(
                select(KnowledgeRelation).where(KnowledgeRelation.status == ReviewStatus.CANDIDATE)
            ).all()
            if access.can_access(item.security_level)
        )
        fact_ids = [("entity", item.id) for item in visible_entities] + [
            ("relation", item.id) for item in visible_relations
        ]
        multi_source_facts = 0
        for target_type, target_id in fact_ids:
            source_count = self.session.scalar(
                select(func.count(func.distinct(KnowledgeEvidenceLink.document_id))).where(
                    KnowledgeEvidenceLink.target_type == target_type,
                    KnowledgeEvidenceLink.target_id == target_id,
                )
            ) or 0
            if source_count > 1:
                multi_source_facts += 1
        active_ontology_version = self.session.scalar(
            select(func.max(OntologyType.version)).where(OntologyType.active.is_(True))
        ) or 0
        average_confidence = (
            sum(item.confidence for item in visible_entities) / visible_count
            if visible_count
            else 0.0
        )
        return {
            "published_nodes": visible_count,
            "published_relations": relation_count,
            "nodes_by_domain": by_domain,
            "pending_entities": pending_entities,
            "pending_relations": pending_relations,
            "multi_source_facts": multi_source_facts,
            "average_entity_confidence": round(average_confidence, 4),
            "active_ontology_version": active_ontology_version,
            "target_nodes": 500,
            "remaining_nodes": max(0, 500 - visible_count),
            "target_progress": round(min(1.0, visible_count / 500), 4),
            "meets_500_node_target": visible_count >= 500,
        }
