import csv
import json
from datetime import datetime
from pathlib import Path

from tunnel_qa.core.config import Settings
from tunnel_qa.core.enums import QualityFlag, ReviewStatus, Role, SecurityLevel, SourceType
from tunnel_qa.core.security import AccessContext
from tunnel_qa.data.schemas import DataSourceCreate, DocumentMetadata, MonitoringInput
from tunnel_qa.data.service import DataIngestionService
from tunnel_qa.db.session import Database
from tunnel_qa.knowledge.schemas import ReviewRequest
from tunnel_qa.knowledge.service import KnowledgeService
from tunnel_qa.qa.model_gateway import DeepSeekGateway, ExtractiveFallbackGateway
from tunnel_qa.qa.service import QAService
from tunnel_qa.quality.schemas import EvaluationCaseCreate, MonitoringSnapshot, RiskAnalysisRequest
from tunnel_qa.quality.service import EvaluationService, RiskService
from tunnel_qa.retrieval.service import HybridRetrievalService


def run_test_data(
    test_data_root: Path,
    output_root: Path,
    *,
    use_deepseek: bool = False,
) -> dict:
    test_data_root = test_data_root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    database_path = output_root / "test_run.db"
    if database_path.exists():
        raise FileExistsError(f"测试输出数据库已存在，请使用新的输出目录: {database_path}")
    settings = Settings(
        database_url=f"sqlite:///{database_path.as_posix()}",
        original_content_database_url=f"sqlite:///{(output_root / 'original_documents.db').as_posix()}",
        chunk_content_database_url=f"sqlite:///{(output_root / 'document_chunks.db').as_posix()}",
        storage_path=output_root / "storage",
        chunk_size=400,
        chunk_overlap=60,
        retrieval_top_k=6,
    )
    from tunnel_qa.app import register_models

    register_models()
    database = Database(settings)
    database.create_schema()
    session = database.session_factory()
    access = AccessContext("test-data-runner", Role.SYSTEM_ADMIN, SecurityLevel.CONFIDENTIAL)
    ingestion = DataIngestionService(session, settings)
    knowledge = KnowledgeService(session, ingestion.content_store)
    knowledge.bootstrap_ontology(access)

    manifest = json.loads((test_data_root / "manifest.json").read_text(encoding="utf-8"))
    imports = []
    entity_ids: list[str] = []
    relation_ids: list[str] = []
    monitoring_count = 0
    for index, item in enumerate(manifest["documents"], 1):
        relative_path = item["path"]
        path = test_data_root / relative_path
        source_type = SourceType(item["source_type"])
        source = ingestion.create_data_source(
            DataSourceCreate(
                name=f"测试数据源-{index}-{source_type.value}",
                source_type=source_type,
                owner="自动化测试",
                security_level=SecurityLevel(item.get("security_level", "internal")),
            ),
            access,
        )
        result = ingestion.import_document(
            data_source_id=source.id,
            filename=path.name,
            content=path.read_bytes(),
            metadata=DocumentMetadata(
                source_id=item["source_id"],
                title=item["title"],
                source_type=source_type,
                project_site=item.get("project_site"),
                version=item["version"],
                effective_date=item["effective_date"],
                security_level=SecurityLevel(item.get("security_level", "internal")),
                owner_org=item["owner_org"],
                content_location=item["content_location"],
                extra={"synthetic": True},
            ),
            idempotency_key=f"test-data-{index}",
            access=access,
        )
        imports.append(result.model_dump(mode="json"))
        if not result.accepted:
            continue
        if result.document_id is None:
            raise ValueError("已接受的导入结果缺少文档 ID")
        extraction = knowledge.extract_candidates(result.document_id, access)
        entity_ids.extend(extraction.entity_ids)
        relation_ids.extend(extraction.relation_ids)

    monitoring_path = test_data_root / manifest["monitoring_file"]
    with monitoring_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            value = float(row["value"]) if row["value"].strip() else None
            ingestion.ingest_monitoring(
                MonitoringInput(
                    sensor_id=row["sensor_id"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    metric_code=row["metric_code"],
                    value=value,
                    unit=row["unit"],
                    quality_flag=QualityFlag(row["quality_flag"]),
                    location={
                        "tunnel": row["tunnel"],
                        "mileage": row["mileage"],
                        "section": row["section"],
                    },
                    source_system=row["source_system"],
                ),
                access,
            )
            monitoring_count += 1

    for entity_id in dict.fromkeys(entity_ids):
        knowledge.review(
            ReviewRequest(
                target_type="entity",
                target_id=entity_id,
                decision=ReviewStatus.APPROVED,
                comment="合成测试数据自动审核",
            ),
            access,
        )
    for relation_id in dict.fromkeys(relation_ids):
        knowledge.review(
            ReviewRequest(
                target_type="relation",
                target_id=relation_id,
                decision=ReviewStatus.APPROVED,
                comment="合成测试数据自动审核",
            ),
            access,
        )
    release = knowledge.publish(access, notes="test_data 合成语料发布")

    gateway = DeepSeekGateway(settings) if use_deepseek else ExtractiveFallbackGateway()
    qa = QAService(
        session,
        settings,
        HybridRetrievalService(session, ingestion.content_store),
        gateway,
    )
    evaluation = EvaluationService(session, qa)
    cases = json.loads((test_data_root / "evaluation_cases.json").read_text(encoding="utf-8"))
    for case in cases:
        evaluation.create_case(EvaluationCaseCreate(**case), access)
    evaluation_run = evaluation.run(access)

    risk = RiskService(session, qa)
    risk.bootstrap_rules(access)
    risk_result = risk.analyze(
        RiskAnalysisRequest(
            description="测试隧道掌子面出现高地温，需核查通风和人员防护",
            construction_stage="钻爆法开挖",
            monitoring=[
                MonitoringSnapshot(
                    metric_code="temperature",
                    value=39.2,
                    unit="℃",
                    location={"tunnel": "测试隧道", "mileage": "DK100+200"},
                )
            ],
        ),
        access,
    )
    report = {
        "database": str(database_path),
        "model_provider": gateway.provider,
        "imports": imports,
        "monitoring_records": monitoring_count,
        "knowledge_release": release.model_dump(mode="json"),
        "evaluation": evaluation_run.model_dump(mode="json"),
        "risk": {
            "risk_level": risk_result.risk_level,
            "review_required": risk_result.review_required,
            "factor_count": len(risk_result.risk_factors),
            "citation_count": len(risk_result.qa.citations),
        },
    }
    (output_root / "run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    session.close()
    return report
