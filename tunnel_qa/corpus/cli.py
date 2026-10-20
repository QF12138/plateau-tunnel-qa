import argparse
import json
from pathlib import Path

from tunnel_qa.corpus.processor import CorpusProcessor
from tunnel_qa.corpus.ocr import PDFOCRProcessor
from tunnel_qa.corpus.importer import CorpusDatabaseImporter
from tunnel_qa.core.config import Settings
from tunnel_qa.corpus.smoke_runner import run_corpus_smoke
from tunnel_qa.corpus.test_runner import run_test_data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="高原铁路隧道领域语料处理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="盘点语料并输出 JSONL")
    inventory.add_argument("--input", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)

    process = subparsers.add_parser("process", help="提取可解析语料并生成切片和质量报告")
    process.add_argument("--input", type=Path, required=True)
    process.add_argument("--output", type=Path, required=True)
    process.add_argument("--chunk-size", type=int, default=800)
    process.add_argument("--chunk-overlap", type=int, default=120)

    test = subparsers.add_parser("run-test-data", help="运行 test_data 端到端测试")
    test.add_argument("--input", type=Path, required=True)
    test.add_argument("--output", type=Path, required=True)
    test.add_argument("--use-deepseek", action="store_true")

    smoke = subparsers.add_parser("run-corpus-smoke", help="使用选定真实语料运行独立问答冒烟测试")
    smoke.add_argument("--input", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--file", dest="files", action="append", required=True)
    smoke.add_argument("--question", required=True)
    smoke.add_argument("--use-deepseek", action="store_true")

    ocr = subparsers.add_parser("ocr-pdf", help="OCR 扫描版 PDF 并保留页码检查点")
    ocr.add_argument("--file", type=Path, required=True)
    ocr.add_argument("--output", type=Path, required=True)
    ocr.add_argument("--scale", type=float, default=1.5)

    import_corpus = subparsers.add_parser("import-corpus", help="将全部处理后语料导入应用数据库")
    import_corpus.add_argument("--input", type=Path, required=True)
    import_corpus.add_argument("--processed", type=Path, required=True)
    import_corpus.add_argument("--database", type=Path, required=True)
    import_corpus.add_argument("--storage", type=Path, required=True)
    import_corpus.add_argument("--skip-knowledge", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "inventory":
        processor = CorpusProcessor(args.input)
        items = processor.inventory(progress=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as stream:
            for item in items:
                stream.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
        print(json.dumps({"files": len(items), "output": str(args.output.resolve())}, ensure_ascii=False))
    elif args.command == "process":
        processor = CorpusProcessor(
            args.input, chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap
        )
        summary = processor.process(args.output, progress=True)
        print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False))
    elif args.command == "run-test-data":
        report = run_test_data(args.input, args.output, use_deepseek=args.use_deepseek)
        print(json.dumps(report, ensure_ascii=False))
    elif args.command == "run-corpus-smoke":
        report = run_corpus_smoke(
            args.input,
            args.files,
            args.output,
            args.question,
            use_deepseek=args.use_deepseek,
        )
        print(json.dumps(report, ensure_ascii=False))
    elif args.command == "ocr-pdf":
        ocr_report = PDFOCRProcessor(render_scale=args.scale).process(
            args.file, args.output, progress=True
        )
        print(json.dumps(ocr_report.model_dump(), ensure_ascii=False))
    else:
        settings = Settings(
            database_url=f"sqlite:///{args.database.resolve().as_posix()}",
            storage_path=args.storage.resolve(),
        )
        report = CorpusDatabaseImporter(args.input, args.processed, settings).run(
            publish_knowledge=not args.skip_knowledge
        )
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
