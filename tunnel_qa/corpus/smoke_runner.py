import json
from pathlib import Path

from tunnel_qa.app import register_models
from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import ReviewStatus, Role, SecurityLevel
from tunnel_qa.core.security import AccessContext
from tunnel_qa.corpus.processor import CorpusProcessor
from tunnel_qa.data.schemas import DataSourceCreate, DocumentMetadata
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.db.session import Database
from tunnel_qa.knowledge.schemas import ReviewRequest
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.qa.model_gateway import DeepSeekGateway, ExtractiveFallbackGateway
from tunnel_qa.qa.schemas import QARequest
from tunnel_qa.qa.service import QAService
from tunnel_qa.retrieval.service import HybridRetrievalService


def run_corpus_smoke(
    corpus_root: Path,
    relative_files: list[str],
    output_root: Path,
    question: str,
    *,
    use_deepseek: bool = False,
) -> dict:
    corpus_root = corpus_root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    database_path = output_root / "corpus_smoke.db"
    if database_path.exists():
        raise FileExistsError(f"冒烟数据库已存在，请使用新的输出目录: {database_path}")
    settings = Settings(
        database_url=f"sqlite:///{database_path.as_posix()}",
        original_content_database_url=f"sqlite:///{(output_root / 'original_documents.db').as_posix()}",
        chunk_content_database_url=f"sqlite:///{(output_root / 'document_chunks.db').as_posix()}",
        storage_path=output_root / "storage",
        max_upload_bytes=50 * 1024 * 1024,
        chunk_size=800,
        chunk_overlap=120,
        retrieval_top_k=6,
    )
    register_models()
    database = Database(settings)
    database.create_schema()
    session = database.session_factory()
    access = AccessContext("corpus-smoke", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL)
    ingestion = DataIngestionService(session, settings)
    knowledge = KnowledgeService(session, ingestion.content_store)
    knowledge.bootstrap_ontology(access)
    inspector = CorpusProcessor(corpus_root)

    imports = []
    entity_ids: list[str] = []
    relation_ids: list[str] = []
    for index, relative_file in enumerate(relative_files, 1):
        path = (corpus_root / relative_file).resolve()
        if not path.is_relative_to(corpus_root):
            raise ValueError(f"文件不在语料根目录内: {relative_file}")
        item = inspector.inspect_file(path)
        if item.extraction_status != "ready":
            raise ValueError(f"文件不可直接提取: {relative_file} ({item.extraction_status})")
        source = ingestion.create_data_source(
            DataSourceCreate(
                name=f"真实语料冒烟-{index}-{item.source_id}",
                source_type=item.source_type,
                owner="真实语料冒烟测试",
                security_level=item.security_level,
            ),
            access,
        )
        result = ingestion.import_document(
            data_source_id=source.id,
            filename=path.name,
            content=path.read_bytes(),
            metadata=DocumentMetadata(
                source_id=item.source_id,
                title=item.title,
                source_type=item.source_type,
                project_site=None,
                version=item.version,
                effective_date=item.effective_date,
                security_level=item.security_level,
                owner_org=item.owner_org,
                content_location="PDF全文（按页定位）",
                extra={"original_path": item.path, "author": item.author},
            ),
            idempotency_key=f"corpus-smoke-{index}-{item.checksum_sha256[:16]}",
            access=access,
        )
        imports.append(
            {
                "relative_path": relative_file,
                "source_id": item.source_id,
                "accepted": result.accepted,
                "document_id": result.document_id,
                "chunk_count": result.quality.chunk_count if result.quality else 0,
            }
        )
        if not result.accepted:
            continue
        if result.document_id is None:
            raise ValueError("已接受的导入结果缺少文档 ID")
        extraction = knowledge.extract_candidates(result.document_id, access)
        entity_ids.extend(extraction.entity_ids)
        relation_ids.extend(extraction.relation_ids)

    for entity_id in dict.fromkeys(entity_ids):
        knowledge.review(
            ReviewRequest(
                target_type="entity",
                target_id=entity_id,
                decision=ReviewStatus.APPROVED,
                comment="真实语料冒烟测试自动审核；不用于生产发布",
            ),
            access,
        )
    for relation_id in dict.fromkeys(relation_ids):
        knowledge.review(
            ReviewRequest(
                target_type="relation",
                target_id=relation_id,
                decision=ReviewStatus.APPROVED,
                comment="真实语料冒烟测试自动审核；不用于生产发布",
            ),
            access,
        )
    release = knowledge.publish(access, notes="真实语料独立冒烟环境")
    gateway = DeepSeekGateway(settings) if use_deepseek else ExtractiveFallbackGateway()
    qa = QAService(
        session,
        settings,
        HybridRetrievalService(session, ingestion.content_store),
        gateway,
    )
    response = qa.ask(QARequest(question=question, scenario="risk_analysis"), access)
    report = {
        "database": str(database_path),
        "model_provider": gateway.provider,
        "model_version": gateway.model,
        "imports": imports,
        "knowledge_release": release.model_dump(mode="json"),
        "question": question,
        "answer": response.answer,
        "answer_type": response.answer_type.value,
        "confidence_level": response.confidence_level.value,
        "citations": [citation.model_dump(mode="json") for citation in response.citations],
        "graph_path": response.graph_path,
        "warnings": response.warnings,
        "trace_id": response.trace_id,
    }
    (output_root / "smoke_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    session.close()
    return report
