"""Re-extract the published corpus with the active ontology without touching source content."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from tunnel_qa.app import register_models
from tunnel_qa.core.config import get_settings
from tunnel_qa.core.enums import ProcessingStatus, Role, SecurityLevel
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.models import SourceDocument, SYSTEM_MANAGED_SOURCE_IDS
from tunnel_qa.db.session import Database
from tunnel_qa.knowledge.service import KnowledgeService


def rebuild(
    *, publish_high_confidence: bool, requeue_legacy_relations: bool = False
) -> dict[str, object]:
    settings = get_settings()
    register_models()
    database = Database(settings)
    database.create_schema()
    session = database.session_factory()
    access = AccessContext(
        user_id="knowledge-rebuild",
        role=Role.SYSTEM_ADMIN,
        security_level=SecurityLevel.CONFIDENTIAL,
    )
    try:
        service = KnowledgeService(session, DocumentContentStore(settings))
        ontologies = service.bootstrap_ontology(access)
        documents = session.scalars(
            select(SourceDocument)
            .where(
                SourceDocument.status == ProcessingStatus.PUBLISHED,
                SourceDocument.source_id.not_in(SYSTEM_MANAGED_SOURCE_IDS),
            )
            .order_by(SourceDocument.created_at)
        ).all()
        extracted_entities: list[str] = []
        extracted_relations: list[str] = []
        approved_entities: list[str] = []
        approved_relations: list[str] = []
        evidence_count = 0
        processed = []
        for document in documents:
            extraction = service.extract_candidates(document.id, access)
            extracted_entities.extend(extraction.entity_ids)
            extracted_relations.extend(extraction.relation_ids)
            evidence_count += extraction.evidence_count
            approved_for_document: tuple[list[str], list[str]] = ([], [])
            if publish_high_confidence:
                approved_for_document = service.auto_review_high_confidence(
                    extraction,
                    access,
                    comment="知识图谱V2重建高置信知识自动审核",
                )
                approved_entities.extend(approved_for_document[0])
                approved_relations.extend(approved_for_document[1])
            processed.append(
                {
                    "document_id": document.id,
                    "source_id": document.source_id,
                    "new_entities": len(extraction.entity_ids),
                    "new_relations": len(extraction.relation_ids),
                    "new_evidence": extraction.evidence_count,
                    "approved_entities": len(approved_for_document[0]),
                    "approved_relations": len(approved_for_document[1]),
                }
            )

        release = None
        if publish_high_confidence and (approved_entities or approved_relations):
            release = service.publish(access, notes="知识图谱V2存量语料增量发布")
        legacy_migration = (
            service.requeue_unsupported_legacy_relations(access)
            if requeue_legacy_relations
            else None
        )
        statistics = service.graph_statistics(access)
        return {
            "ontology_version": max(item.version for item in ontologies),
            "documents_processed": len(documents),
            "new_entities": len(extracted_entities),
            "new_relations": len(extracted_relations),
            "new_evidence": evidence_count,
            "approved_entities": len(approved_entities),
            "approved_relations": len(approved_relations),
            "release": release.model_dump(mode="json") if release else None,
            "legacy_relation_migration": legacy_migration,
            "statistics": statistics,
            "documents": processed,
        }
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish-high-confidence",
        action="store_true",
        help="Automatically approve and publish only facts with confidence >= 0.85.",
    )
    parser.add_argument(
        "--requeue-legacy-relations",
        action="store_true",
        help="Move low-confidence legacy relations without V2 evidence back to review.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            rebuild(
                publish_high_confidence=args.publish_high_confidence,
                requeue_legacy_relations=args.requeue_legacy_relations,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
