from enum import StrEnum


class Role(StrEnum):
    FIELD_ENGINEER = "field_engineer"
    RESEARCHER = "researcher"
    REVIEWER = "reviewer"
    DATA_ADMIN = "data_admin"
    SYSTEM_ADMIN = "system_admin"
    EXTERNAL_SYSTEM = "external_system"


class SecurityLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"


SECURITY_RANK: dict[SecurityLevel, int] = {
    SecurityLevel.PUBLIC: 0,
    SecurityLevel.INTERNAL: 1,
    SecurityLevel.RESTRICTED: 2,
    SecurityLevel.CONFIDENTIAL: 3,
}


class SourceType(StrEnum):
    RESEARCH = "research"
    EXPERT_EXPERIENCE = "expert_experience"
    ENGINEERING_CASE = "engineering_case"
    MONITORING = "monitoring"
    STANDARD = "standard"


class ProcessingStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    QUARANTINED = "quarantined"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class OntologyDomain(StrEnum):
    HUMAN = "human"
    MACHINE = "machine"
    MATERIAL = "material"
    METHOD = "method"
    ENVIRONMENT = "environment"


class AnswerType(StrEnum):
    FACT = "fact"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    DECISION_SUPPORT = "decision_support"
    REFUSAL = "refusal"


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class QualityFlag(StrEnum):
    NORMAL = "normal"
    MISSING = "missing"
    OUTLIER = "outlier"
    INTERPOLATED = "interpolated"
    MANUAL = "manual"


class JobStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUARANTINED = "quarantined"

