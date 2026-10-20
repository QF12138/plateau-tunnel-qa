"""Plan and rehearse versioned re-chunking on an offline database COPY only.

Original bytes, old chunks, question/answer rows and manual facts are retained.
This tool never switches the live application; activation is a separate, stopped-
service operation after reviewing the generated plan and rehearsal report.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3

from sqlalchemy import func, select

from tunnel_qa.app import register_models
from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import ProcessingStatus, ReviewStatus, Role, SecurityLevel
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.content_store import DocumentContentStore
from tunnel_qa.data.knowledge_chunking import PreparedChunk, chunk_source_location
from tunnel_qa.data.preparation import prepare_source_knowledge, isolate_review_pages
from tunnel_qa.data.models import Chunk, SourceDocument, SYSTEM_MANAGED_SOURCE_IDS
from tunnel_qa.data.parsers import parse_document
from tunnel_qa.data.processing_metadata import metadata_object
from tunnel_qa.data.quality import content_quality_checks
from tunnel_qa.data.schemas import ChunkingConfig
from tunnel_qa.db.base import utc_now
from tunnel_qa.db.session import Database
from tunnel_qa.knowledge.models import Evidence, KnowledgeEntity, KnowledgeEvidenceLink, KnowledgeRelation, KnowledgeRelease, KnowledgeReview
from tunnel_qa.knowledge.service import KnowledgeService


def fingerprint(rows):
    payload = [(row.id, row.ordinal, row.processing_version, row.text) for row in sorted(rows, key=lambda r: r.id)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def offline_settings(folder: Path) -> Settings:
    live = Settings()
    folder = folder.resolve(strict=True)
    paths = [folder / name for name in ("tunnel_qa.db", "original_documents.db", "document_chunks.db")]
    live_paths = {Path(url.removeprefix("sqlite:///")).resolve() for url in
                  (live.database_url, live.original_content_database_url, live.chunk_content_database_url)}
    if any(not path.is_file() or path.resolve() in live_paths or path.is_symlink() for path in paths):
        raise ValueError("An offline copy of all three databases is required; live paths are refused")
    return live.model_copy(update=dict(zip(
        ("database_url", "original_content_database_url", "chunk_content_database_url"),
        (f"sqlite:///{path.as_posix()}" for path in paths),
    )))


def make_plan(settings: Settings, config: ChunkingConfig) -> dict:
    register_models()
    db = Database(settings)
    result = {"format": 1, "created_at": utc_now().isoformat(), "chunking": config.model_dump(), "documents": []}
    original_path = Path(settings.original_content_database_url.removeprefix("sqlite:///")).resolve()
    try:
        with db.session_factory() as session, closing(sqlite3.connect(original_path.as_uri() + "?mode=ro", uri=True)) as archive:
            documents = session.scalars(select(SourceDocument).where(
                SourceDocument.source_id.not_in(SYSTEM_MANAGED_SOURCE_IDS),
                SourceDocument.status == ProcessingStatus.PUBLISHED,
            ).order_by(SourceDocument.source_id)).all()
            for document in documents:
                old = session.scalars(select(Chunk).where(Chunk.document_id == document.id,
                    Chunk.processing_version == document.processing_version)).all()
                entry = {"document_id": document.id, "source_id": document.source_id, "title": document.title,
                         "old_version": document.processing_version, "checksum": document.checksum,
                         "old_fingerprint": fingerprint(old), "old_chunks": len(old),
                         "old_chars": sum(chunk.char_count for chunk in old), "accepted": False}
                result["documents"].append(entry)
                try:
                    original = archive.execute("SELECT original_blob FROM original_documents WHERE document_id=?", (document.id,)).fetchone()
                    if not original or hashlib.sha256(original[0]).hexdigest() != document.checksum:
                        raise ValueError("original checksum verification failed")
                    ocr = bool(metadata_object(document.metadata_extra).get("ocr_applied"))
                    parsed_text = document.parsed_text if ocr else parse_document(document.original_filename, original[0])[0]
                    prepared, isolated = prepare_source_knowledge(document.original_filename, parsed_text,
                        config.chunk_size, config.chunk_overlap, source_type=document.source_type, mode="key",
                        content=original[0], reviewed_ocr=ocr)
                    checks = content_quality_checks(parsed_text, [chunk.text for chunk in prepared.chunks])
                    checks["quarantined_pages_excluded"] = not any(
                        chunk.page_start <= item["page"] <= chunk.page_end
                        for chunk in prepared.chunks if chunk.page_start is not None and chunk.page_end is not None
                        for item in isolated)
                    entry.update(accepted=all(checks.values()), checks=checks,
                        text_source="existing_ocr" if ocr else "reparsed_original",
                        quarantined_pages=isolated,
                        report=prepared.report, chunks=[asdict(chunk) for chunk in prepared.chunks],
                        new_chunks=len(prepared.chunks), new_chars=sum(len(chunk.text) for chunk in prepared.chunks))
                    print(json.dumps({"source_id": document.source_id, "old": len(old),
                                      "new": len(prepared.chunks), "accepted": entry["accepted"]}), flush=True)
                except Exception as exc:
                    entry["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        db.engine.dispose()
    result["summary"] = {"documents": len(result["documents"]),
        "accepted": sum(entry["accepted"] for entry in result["documents"]),
        "old_chunks": sum(entry["old_chunks"] for entry in result["documents"]),
        "new_chunks": sum(entry.get("new_chunks", 0) for entry in result["documents"])}
    return result


def reconcile_graph(session, service, new_entities, new_relations, access, blocked_entities=None, blocked_relations=None):
    """Keep manual/current facts and retire only automatically unsupported ones."""
    active_evidence = {e.id: e for e, c, d in session.execute(
        select(Evidence, Chunk, SourceDocument).join(Chunk, Evidence.chunk_id == Chunk.id)
        .join(SourceDocument, Chunk.document_id == SourceDocument.id)
        .where(Chunk.processing_version == SourceDocument.processing_version)
    )}
    linked = defaultdict(list)
    for link in session.scalars(select(KnowledgeEvidenceLink)).all():
        if link.evidence_id in active_evidence:
            linked[(link.target_type, link.target_id)].append(link.evidence_id)
    entities = session.scalars(select(KnowledgeEntity)).all()
    relations = session.scalars(select(KnowledgeRelation)).all()
    # Re-extraction must not resurrect facts previously rejected or removed.
    for fact in entities:
        if fact.id in new_entities and (fact.standard_name, fact.ontology_type_id) in (blocked_entities or set()):
            fact.status = ReviewStatus.ARCHIVED
    for fact in relations:
        key = (fact.source_entity.standard_name, fact.relation_type, fact.target_entity.standard_name)
        if fact.id in new_relations and (key in (blocked_relations or set())
            or fact.source_entity.status == ReviewStatus.ARCHIVED or fact.target_entity.status == ReviewStatus.ARCHIVED):
            fact.status = ReviewStatus.ARCHIVED
    new_entities = [fact.id for fact in entities if fact.id in new_entities and fact.status == ReviewStatus.CANDIDATE]
    new_relations = [fact.id for fact in relations if fact.id in new_relations and fact.status == ReviewStatus.CANDIDATE]
    manual_endpoints = {endpoint for relation in relations
        if relation.status == ReviewStatus.PUBLISHED and any(
            session.get(Chunk, active_evidence[eid].chunk_id).document.source_id in SYSTEM_MANAGED_SOURCE_IDS
            for eid in linked[("relation", relation.id)])
        for endpoint in (relation.source_entity_id, relation.target_entity_id)}
    retired, promoted, retained_manual_endpoints = [], 0, []
    def retire(kind, fact):
        fact.status = ReviewStatus.ARCHIVED
        retired.append({"type": kind, "id": fact.id})
        session.add(KnowledgeReview(target_type=kind, target_id=fact.id, decision=ReviewStatus.ARCHIVED,
            reviewer_id=access.user_id, comment="重切后无当前版本证据，自动退役；新证据可重新提出候选"))

    for kind, facts in (("entity", entities), ("relation", relations)):
        for fact in facts:
            if fact.status not in {ReviewStatus.PUBLISHED, ReviewStatus.APPROVED, ReviewStatus.CANDIDATE}:
                continue
            alternatives = linked[(kind, fact.id)]
            if fact.evidence_id in active_evidence:
                continue
            if alternatives:
                fact.evidence_id = alternatives[0]
                promoted += 1
            elif kind == "entity" and fact.id in manual_endpoints:
                retained_manual_endpoints.append(fact.id)
            else:
                retire(kind, fact)
    session.flush()
    # Existing approvals/rejections are not changed by a bulk migration. Only
    # newly extracted, high-confidence candidates follow the normal auto gate.
    from tunnel_qa.knowledge.schemas import ExtractionResult
    approved_entities, approved_relations = service.auto_review_high_confidence(
        ExtractionResult(document_id="rechunk", entity_ids=new_entities, relation_ids=new_relations, evidence_count=0),
        access, comment="重切后新增高置信知识自动审核",
    )
    version = (session.scalar(select(func.max(KnowledgeRelease.version))) or 0) + 1
    for fact_id in approved_entities:
        fact = session.get(KnowledgeEntity, fact_id)
        fact.status = ReviewStatus.PUBLISHED
        fact.knowledge_version = version
        service._set_fact_evidence_status("entity", fact.id, ReviewStatus.PUBLISHED)
    session.flush()
    published_relations = 0
    for fact_id in approved_relations:
        fact = session.get(KnowledgeRelation, fact_id)
        if fact.source_entity.status == fact.target_entity.status == ReviewStatus.PUBLISHED:
            fact.status = ReviewStatus.PUBLISHED
            fact.knowledge_version = version
            service._set_fact_evidence_status("relation", fact.id, ReviewStatus.PUBLISHED)
            published_relations += 1
    # No active edge may refer to a retired endpoint.
    for relation in relations:
        if relation.status == ReviewStatus.PUBLISHED and (
            relation.source_entity.status != ReviewStatus.PUBLISHED or relation.target_entity.status != ReviewStatus.PUBLISHED
        ):
            retire("relation", relation)
    session.add(KnowledgeRelease(version=version, published_by=access.user_id, published_at=utc_now(),
        entity_count=len(approved_entities), relation_count=published_relations,
        notes="存量资料关键正文重切；历史块保留，当前检索和图谱证据切换至新版本"))
    session.commit()
    return {"knowledge_version": version, "primary_evidence_promoted": promoted, "retired": retired,
            "manual_endpoints_without_current_text": retained_manual_endpoints,
            "new_published_entities": len(approved_entities), "new_published_relations": published_relations}


def apply_plan(settings: Settings, plan: dict) -> dict:
    if plan.get("format") != 1 or not plan.get("documents") or not all(item.get("accepted") for item in plan["documents"]):
        raise ValueError("All documents must pass the reviewed plan before apply")
    register_models()
    config = ChunkingConfig.model_validate(plan["chunking"])
    db = Database(settings)
    store = DocumentContentStore(settings)
    access = AccessContext("rechunk-migration", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL)
    try:
        with db.session_factory() as session:
            current_ids = set(session.scalars(select(SourceDocument.id).where(
                SourceDocument.source_id.not_in(SYSTEM_MANAGED_SOURCE_IDS),
                SourceDocument.status == ProcessingStatus.PUBLISHED,
            )).all())
            planned_ids = [entry["document_id"] for entry in plan["documents"]]
            if len(set(planned_ids)) != len(planned_ids) or set(planned_ids) != current_ids:
                raise ValueError("Published document set changed since planning")
            blocked_entities = {(fact.standard_name, fact.ontology_type_id)
                for fact in session.scalars(select(KnowledgeEntity)).all()
                if fact.status in {ReviewStatus.ARCHIVED, ReviewStatus.REJECTED}}
            blocked_relations = {(fact.source_entity.standard_name, fact.relation_type, fact.target_entity.standard_name)
                for fact in session.scalars(select(KnowledgeRelation)).all()
                if fact.status in {ReviewStatus.ARCHIVED, ReviewStatus.REJECTED}}
            # Validate the whole plan before writing even the first document.
            for entry in plan["documents"]:
                document = session.get(SourceDocument, entry["document_id"])
                if document is None or document.checksum != entry["checksum"] or document.processing_version != entry["old_version"]:
                    raise ValueError("Document version changed since planning")
                old = session.scalars(select(Chunk).where(Chunk.document_id == document.id,
                    Chunk.processing_version == document.processing_version)).all()
                if fingerprint(old) != entry["old_fingerprint"]:
                    raise ValueError("Document chunks changed since planning")
            for entry in plan["documents"]:
                document = session.get(SourceDocument, entry["document_id"])
                version = document.processing_version + 1
                metadata = deepcopy(metadata_object(document.metadata_extra))
                history = metadata.get("processing_history", {})
                history = history if isinstance(history, dict) else {}
                history[str(document.processing_version)] = {key: deepcopy(metadata.get(key))
                    for key in ("chunking", "chunk_processing", "ingestion_quality")}
                metadata.update(processing_history=history, processing_version=version,
                    chunking=config.model_dump(),
                    rechunked_at=utc_now().isoformat(), rechunk_text_source=entry["text_source"],
                    chunk_processing={"report": entry["report"], "chunks": {str(i): {
                        "page_start": c["page_start"], "page_end": c["page_end"], "kind": c["kind"], "warnings": c["warnings"]
                    } for i, c in enumerate(entry["chunks"], 1)}})
                metadata["ingestion_quality"] = {"score": 1.0, "passed": True, "checks": entry["checks"],
                                                  "issues": [], "chunk_count": len(entry["chunks"])}
                new_chunks = [Chunk(document_id=document.id, processing_version=version, ordinal=i,
                    text=c["text"], structure_path=c["heading"], page_number=c["page_start"],
                    content_location=chunk_source_location(document.content_location, PreparedChunk(**c), i),
                    security_level=document.security_level, char_count=len(c["text"]))
                    for i, c in enumerate(entry["chunks"], 1)]
                session.add_all(new_chunks)
                session.flush()
                store.save_revision(document, new_chunks, config)
                document.metadata_extra = metadata
                document.processing_version = version
            session.commit()
            knowledge = KnowledgeService(session, store)
            knowledge.bootstrap_ontology(access, preserve_existing=True)
            new_entities, new_relations = [], []
            for entry in plan["documents"]:
                extracted = knowledge.extract_candidates(entry["document_id"], access,
                    excluded_entities=blocked_entities, excluded_relations=blocked_relations)
                new_entities.extend(extracted.entity_ids)
                new_relations.extend(extracted.relation_ids)
                print(json.dumps({"extracted": entry["source_id"], "new_evidence": extracted.evidence_count}), flush=True)
            graph = reconcile_graph(session, knowledge, new_entities, new_relations, access, blocked_entities, blocked_relations)
            return {"documents_rechunked": len(plan["documents"]), "old_chunks_retained": plan["summary"]["old_chunks"],
                    "new_active_chunks": plan["summary"]["new_chunks"], "graph": graph}
    finally:
        db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-copy", type=Path, required=True)
    parser.add_argument("--plan-to", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--report-to", type=Path)
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--overlap", type=int, default=120)
    args = parser.parse_args()
    if bool(args.plan_to) == bool(args.apply_plan):
        parser.error("Choose exactly one of --plan-to or --apply-plan")
    settings = offline_settings(args.offline_copy)
    if args.plan_to:
        result = make_plan(settings, ChunkingConfig(chunk_size=args.chunk_size, chunk_overlap=args.overlap))
        output = args.plan_to
    else:
        result = apply_plan(settings, json.loads(args.apply_plan.read_text(encoding="utf-8")))
        output = args.report_to
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result.get("summary", result), ensure_ascii=False))


if __name__ == "__main__":
    main()
