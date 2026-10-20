import json
from pathlib import Path

from pypdf import PdfWriter

from tunnel_qa.corpus.processor import CorpusProcessor, infer_metadata, sha256_file
from tunnel_qa.corpus.test_runner import run_test_data
from tunnel_qa.corpus.smoke_runner import run_corpus_smoke


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_metadata_inference_distinguishes_standard_and_research(tmp_path):
    root = tmp_path / "语料"
    standard = root / "标准" / "TB 99999-2026 测试规范.txt"
    research = root / "论文" / "高原隧道测试研究_测试作者.txt"
    standard.parent.mkdir(parents=True)
    research.parent.mkdir(parents=True)
    standard.write_text("测试规范正文内容足够用于元数据推断。", encoding="utf-8")
    research.write_text("测试论文正文内容足够用于元数据推断。", encoding="utf-8")

    standard_meta = infer_metadata(standard, root, sha256_file(standard))
    research_meta = infer_metadata(research, root, sha256_file(research))

    assert standard_meta["source_type"].value == "standard"
    assert standard_meta["version"] == "TB 99999-2026"
    assert research_meta["source_type"].value == "research"
    assert research_meta["author"] == "测试作者"


def test_blank_pdf_is_classified_as_requiring_ocr(tmp_path):
    root = tmp_path / "语料"
    pdf_path = root / "标准" / "扫描测试规范.pdf"
    pdf_path.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    item = CorpusProcessor(root).inspect_file(pdf_path)

    assert item.extraction_status == "requires_ocr"
    assert "insufficient_embedded_text_requires_ocr" in item.issues


def test_processor_creates_manifest_chunks_and_isolates_invalid_json(tmp_path):
    source = tmp_path / "input"
    (source / "标准").mkdir(parents=True)
    (source / "异常").mkdir(parents=True)
    (source / "标准" / "测试规范.md").write_text(
        "# 总则\n高地温隧道应监测掌子面温度，并检查通风降温措施。" * 5,
        encoding="utf-8",
    )
    (source / "异常" / "损坏.json").write_text('{"bad": }', encoding="utf-8")
    output = tmp_path / "processed"

    summary = CorpusProcessor(source, chunk_size=120, chunk_overlap=20).process(output)
    manifest = [json.loads(line) for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    quality = json.loads((output / "quality_report.json").read_text(encoding="utf-8"))

    assert summary.total_files == 2
    assert summary.processed_files == 1
    assert summary.failed_files == 1
    assert summary.chunk_count >= 2
    assert (output / "chunks.jsonl").stat().st_size > 0
    assert {item["processing"]["status"] for item in manifest} == {"processed", "failed"}
    assert quality["quality_issues"][0]["processing_status"] == "failed"
    assert quality["quality_issues"][0]["processing_errors"]

    second_summary = CorpusProcessor(source, chunk_size=120, chunk_overlap=20).process(output)
    assert second_summary.total_files == 2
    assert second_summary.chunk_count == summary.chunk_count


def test_synthetic_test_data_runs_end_to_end(tmp_path):
    report = run_test_data(PROJECT_ROOT / "test_data", tmp_path / "run")

    assert all(item["accepted"] for item in report["imports"])
    assert report["monitoring_records"] == 4
    assert report["knowledge_release"]["entity_count"] >= 8
    assert report["evaluation"]["case_count"] == 3
    assert report["evaluation"]["metrics"]["refusal_accuracy"] == 1.0
    assert report["risk"]["risk_level"] == "high"
    assert report["risk"]["review_required"] is True


def test_corpus_smoke_runner_imports_selected_files_and_answers(tmp_path):
    corpus = tmp_path / "corpus"
    (corpus / "标准").mkdir(parents=True)
    (corpus / "论文").mkdir(parents=True)
    (corpus / "标准" / "TB 99999-2026 高地温测试规范.md").write_text(
        "高地温影响作业人员。传感器应监测掌子面温度，并采用通风降温措施。" * 4,
        encoding="utf-8",
    )
    (corpus / "论文" / "岩爆测试研究_测试作者.txt").write_text(
        "高地应力可能导致岩爆风险。监测设备应采集应力数据，并由专家复核。" * 4,
        encoding="utf-8",
    )

    report = run_corpus_smoke(
        corpus,
        ["标准/TB 99999-2026 高地温测试规范.md", "论文/岩爆测试研究_测试作者.txt"],
        tmp_path / "smoke",
        "高地温时如何通风降温？",
    )

    assert all(item["accepted"] for item in report["imports"])
    assert report["model_provider"] == "local_fallback"
    assert report["answer_type"] != "refusal"
    assert report["citations"]
