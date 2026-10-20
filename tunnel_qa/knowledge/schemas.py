from pydantic import BaseModel, Field, model_validator

from tunnel_qa.core.enums import OntologyDomain, ReviewStatus, SecurityLevel


class OntologyTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    domain: OntologyDomain
    properties_schema: dict = Field(default_factory=dict)
    allowed_relations: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    version: int = Field(default=2, ge=1)
    active: bool = True


class OntologyTypeRead(BaseModel):
    id: str
    name: str
    domain: OntologyDomain
    properties_schema: dict
    allowed_relations: list[str]
    aliases: list[str]
    version: int
    active: bool


class ExtractionResult(BaseModel):
    document_id: str
    entity_ids: list[str]
    relation_ids: list[str]
    evidence_count: int


class ReviewRequest(BaseModel):
    target_type: str
    target_id: str
    decision: ReviewStatus
    comment: str = ""


class BatchReviewRequest(BaseModel):
    items: list[ReviewRequest] = Field(min_length=1, max_length=100)


class PublishResult(BaseModel):
    version: int
    entity_count: int
    relation_count: int
    release_id: str


class GraphNode(BaseModel):
    id: str
    name: str
    type: str
    domain: OntologyDomain
    properties: dict
    evidence_id: str
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 0.7
    security_level: SecurityLevel = SecurityLevel.INTERNAL


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation_type: str
    applicable_conditions: dict
    evidence_id: str
    confidence: float = 0.7
    security_level: SecurityLevel = SecurityLevel.INTERNAL


class Subgraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ManualEntityCreate(BaseModel):
    standard_name: str = Field(min_length=1, max_length=300)
    domain: OntologyDomain
    aliases: list[str] = Field(default_factory=list, max_length=50)
    properties: dict = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)
    security_level: SecurityLevel = SecurityLevel.INTERNAL


class ManualEntityUpdate(BaseModel):
    standard_name: str | None = Field(default=None, min_length=1, max_length=300)
    domain: OntologyDomain | None = None
    aliases: list[str] | None = Field(default=None, max_length=50)
    properties: dict | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    security_level: SecurityLevel | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个需要修改的实体字段")
        return self


class ManualRelationCreate(BaseModel):
    source_entity_id: str = Field(min_length=1, max_length=36)
    target_entity_id: str = Field(min_length=1, max_length=36)
    relation_type: str = Field(min_length=1, max_length=100)
    applicable_conditions: dict = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def endpoints_must_differ(self):
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("关系起点和终点不能相同")
        return self


class ManualRelationUpdate(BaseModel):
    source_entity_id: str | None = Field(default=None, min_length=1, max_length=36)
    target_entity_id: str | None = Field(default=None, min_length=1, max_length=36)
    relation_type: str | None = Field(default=None, min_length=1, max_length=100)
    applicable_conditions: dict | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个需要修改的关系字段")
        if (
            self.source_entity_id is not None
            and self.target_entity_id is not None
            and self.source_entity_id == self.target_entity_id
        ):
            raise ValueError("关系起点和终点不能相同")
        return self
