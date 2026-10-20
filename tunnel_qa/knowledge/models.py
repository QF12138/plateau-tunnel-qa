from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tunnel_qa.core.enums import OntologyDomain, ReviewStatus, SecurityLevel
from tunnel_qa.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OntologyType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ontology_types"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_ontology_name_version"),)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    domain: Mapped[OntologyDomain] = mapped_column(String(30), nullable=False)
    properties_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    allowed_relations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class Evidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evidence"

    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    source_location: Mapped[str] = mapped_column(String(1000), nullable=False)
    review_status: Mapped[ReviewStatus] = mapped_column(
        String(20), default=ReviewStatus.CANDIDATE, nullable=False
    )
    valid_from: Mapped[str | None] = mapped_column(String(30))
    valid_to: Mapped[str | None] = mapped_column(String(30))


class KnowledgeEvidenceLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Additional provenance for a fact that may be supported by many documents."""

    __tablename__ = "knowledge_evidence_links"
    __table_args__ = (
        UniqueConstraint(
            "target_type", "target_id", "evidence_id", name="uq_knowledge_evidence_link"
        ),
    )

    target_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id"), nullable=False, index=True
    )
    extraction_method: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)

    evidence: Mapped[Evidence] = relationship()


class KnowledgeEntity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_entities"
    __table_args__ = (
        UniqueConstraint(
            "standard_name", "ontology_type_id", "knowledge_version", name="uq_entity_name_type_version"
        ),
    )

    ontology_type_id: Mapped[str] = mapped_column(ForeignKey("ontology_types.id"), nullable=False)
    standard_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    properties: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(
        String(20), default=ReviewStatus.CANDIDATE, nullable=False, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    knowledge_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(String(20), nullable=False)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), nullable=False)

    ontology_type: Mapped[OntologyType] = relationship()
    evidence: Mapped[Evidence] = relationship()


class KnowledgeRelation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_entity_id",
            "relation_type",
            "target_entity_id",
            "knowledge_version",
            name="uq_relation_fact_version",
        ),
    )

    source_entity_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_entities.id"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_entities.id"), nullable=False, index=True
    )
    applicable_conditions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(
        String(20), default=ReviewStatus.CANDIDATE, nullable=False, index=True
    )
    knowledge_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    security_level: Mapped[SecurityLevel] = mapped_column(String(20), nullable=False)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), nullable=False)

    source_entity: Mapped[KnowledgeEntity] = relationship(foreign_keys=[source_entity_id])
    target_entity: Mapped[KnowledgeEntity] = relationship(foreign_keys=[target_entity_id])
    evidence: Mapped[Evidence] = relationship()


class KnowledgeReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_reviews"

    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision: Mapped[ReviewStatus] = mapped_column(String(20), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)


class KnowledgeRelease(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_releases"

    version: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="published", nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    published_by: Mapped[str] = mapped_column(String(100), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
