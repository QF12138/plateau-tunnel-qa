import hashlib
import json
import mimetypes
import re
from collections import Counter
from pathlib import Path
from typing import TextIO

from pypdf import PdfReader

from tunnel_qa.core.enums import SecurityLevel, SourceType
from tunnel_qa.data.parsers import parse_document, split_text
from tunnel_qa.corpus.schemas import CorpusItem, CorpusSummary, ProcessedDocument


SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".html", ".htm", ".json", ".csv", ".xlsx"}
CONTROL_FILES = {"manifest.json", "evaluation_cases.json", "run_report.json", "quality_report.json"}
IGNORED_DIRECTORY_NAMES = {
    "processed",
    "run_output",
    "storage",
    "__pycache__",
    ".git",
    ".pytest_cache",
}
CATEGORY_TYPES = {
    "标准": SourceType.STANDARD,
    "论文": SourceType.RESEARCH,
    "科研成果": SourceType.RESEARCH,
    "专家经验": SourceType.EXPERT_EXPERIENCE,
    "案例": SourceType.ENGINEERING_CASE,
    "工程案例": SourceType.ENGINEERING_CASE,
    "监测": SourceType.MONITORING,
}


def sha256_file(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def normalize_pdf_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def infer_metadata(path: Path, root: Path, checksum: str) -> dict:
    relative = path.relative_to(root)
    category = relative.parts[0] if len(relative.parts) > 1 else "未分类"
    source_type = CATEGORY_TYPES.get(category, SourceType.RESEARCH)
    stem = path.stem.strip()
    author = None
    title = stem
    if source_type == SourceType.RESEARCH and "_" in stem:
        candidate_title, candidate_author = stem.rsplit("_", 1)
        if candidate_author and len(candidate_author) <= 30:
            title, author = candidate_title, candidate_author
    standard_match = re.search(r"TB\s*\d+(?:\.\d+)?\s*[-—]\s*\d{4}", stem, re.I)
    year_match = re.search(r"(?:19|20)\d{2}", stem)
    version = re.sub(r"\s+", " ", standard_match.group(0)).upper() if standard_match else (
        f"V{year_match.group(0)}" if year_match else "V1"
    )
    effective_date = f"{year_match.group(0)}-01-01" if year_match else "2026-01-01"
    category_code = {
        SourceType.STANDARD: "STD",
        SourceType.RESEARCH: "RES",
        SourceType.EXPERT_EXPERIENCE: "EXP",
        SourceType.ENGINEERING_CASE: "CASE",
        SourceType.MONITORING: "MON",
    }[source_type]
    return {
        "source_id": f"SRC-{category_code}-{checksum[:12].upper()}",
        "relative_path": relative.as_posix(),
        "category": category,
        "source_type": source_type,
        "title": title,
        "author": author,
        "version": version,
        "effective_date": effective_date,
    }


def detect_sensitive_flags(text: str) -> list[str]:
    flags = []
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text):
        flags.append("possible_phone_number")
    if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        flags.append("possible_email")
    if re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", text):
        flags.append("possible_id_number")
    return flags


class CorpusProcessor:
    def __init__(self, input_root: Path, *, chunk_size: int = 800, chunk_overlap: int = 120) -> None:
        self.input_root = input_root.resolve()
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def discover(self) -> list[Path]:
        return sorted(
            path
            for path in self.input_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and path.name not in CONTROL_FILES
            and not any(part in IGNORED_DIRECTORY_NAMES for part in path.relative_to(self.input_root).parts[:-1])
        )

    def inspect_file(self, path: Path) -> CorpusItem:
        checksum = sha256_file(path)
        inferred = infer_metadata(path, self.input_root, checksum)
        issues: list[str] = []
        sensitive_flags: list[str] = []
        page_count = None
        encrypted = False
        sample_counts: list[int] = []
        status = "ready"
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        if path.suffix.lower() == ".pdf":
            try:
                reader = PdfReader(path, strict=False)
                encrypted = reader.is_encrypted
                if encrypted:
                    try:
                        reader.decrypt("")
                    except Exception:
                        pass
                    issues.append("pdf_encrypted_flag_requires_authorization_review")
                page_count = len(reader.pages)
                indices = sorted(
                    {
                        index
                        for index in (0, 1, 2, page_count // 4, page_count // 2, 3 * page_count // 4, page_count - 1)
                        if 0 <= index < page_count
                    }
                )
                samples = []
                for index in indices:
                    try:
                        text = normalize_pdf_text(reader.pages[index].extract_text() or "")
                    except Exception:
                        text = ""
                    samples.append(text)
                    sample_counts.append(len(text))
                sensitive_flags = detect_sensitive_flags("\n".join(samples))
                informative_pages = sum(count >= 80 for count in sample_counts)
                if not sample_counts or informative_pages < max(1, len(sample_counts) // 2):
                    status = "requires_ocr"
                    issues.append("insufficient_embedded_text_requires_ocr")
            except Exception as exc:
                status = "failed"
                issues.append(f"pdf_inspection_failed:{type(exc).__name__}")
        else:
            try:
                sample = path.read_bytes()[:2 * 1024 * 1024]
                if path.stat().st_size == 0:
                    status = "failed"
                    issues.append("empty_file")
                elif path.suffix.lower() in {".txt", ".md", ".html", ".htm", ".json", ".csv"}:
                    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
                        try:
                            sensitive_flags = detect_sensitive_flags(sample.decode(encoding))
                            break
                        except UnicodeDecodeError:
                            continue
            except Exception as exc:
                status = "failed"
                issues.append(f"file_inspection_failed:{type(exc).__name__}")

        score = 1.0
        if status == "requires_ocr":
            score -= 0.55
        elif status == "failed":
            score = 0.0
        if encrypted:
            score -= 0.1
        if sensitive_flags:
            score -= 0.1
            issues.append("sensitive_content_requires_review")
        return CorpusItem(
            **inferred,
            path=str(path),
            owner_org="国铁川藏科创中心（成都）有限公司",
            security_level=SecurityLevel.INTERNAL,
            size_bytes=path.stat().st_size,
            checksum_sha256=checksum,
            media_type=media_type,
            page_count=page_count,
            encrypted=encrypted,
            sample_page_chars=sample_counts,
            extraction_status=status,
            quality_score=max(0.0, round(score, 2)),
            issues=issues,
            sensitive_flags=sensitive_flags,
        )

    def inventory(self, progress: bool = False) -> list[CorpusItem]:
        items = []
        paths = self.discover()
        for index, path in enumerate(paths, 1):
            item = self.inspect_file(path)
            items.append(item)
            if progress:
                print(
                    json.dumps(
                        {
                            "stage": "inventory",
                            "index": index,
                            "total": len(paths),
                            "file": item.relative_path,
                            "status": item.extraction_status,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        return items

    def process(self, output_root: Path, progress: bool = False) -> CorpusSummary:
        output_root = output_root.resolve()
        documents_dir = output_root / "documents"
        output_root.mkdir(parents=True, exist_ok=True)
        documents_dir.mkdir(parents=True, exist_ok=True)
        items = self.inventory(progress=progress)
        results: list[ProcessedDocument] = []
        with (output_root / "chunks.jsonl").open("w", encoding="utf-8", newline="\n") as chunk_stream:
            for index, item in enumerate(items, 1):
                if item.extraction_status != "ready":
                    result = ProcessedDocument(source_id=item.source_id, status=item.extraction_status)
                else:
                    result = self._process_item(item, documents_dir, chunk_stream)
                results.append(result)
                if progress:
                    print(
                        json.dumps(
                            {
                                "stage": "process",
                                "index": index,
                                "total": len(items),
                                "source_id": item.source_id,
                                "status": result.status,
                                "chunks": result.chunk_count,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        with (output_root / "manifest.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for item, result in zip(items, results):
                payload = item.model_dump(mode="json")
                payload["processing"] = result.model_dump(mode="json")
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

        status_counts = Counter(result.status for result in results)
        category_counts = Counter(item.category for item in items)
        summary = CorpusSummary(
            input_root=str(self.input_root),
            output_root=str(output_root),
            total_files=len(items),
            ready_files=sum(item.extraction_status == "ready" for item in items),
            processed_files=status_counts["processed"],
            requires_ocr_files=status_counts["requires_ocr"],
            failed_files=status_counts["failed"],
            total_pages=sum(item.page_count or 0 for item in items),
            text_chars=sum(result.text_chars for result in results),
            chunk_count=sum(result.chunk_count for result in results),
            status_counts=dict(status_counts),
            category_counts=dict(category_counts),
        )
        report = {
            "summary": summary.model_dump(mode="json"),
            "quality_issues": [
                {
                    "source_id": item.source_id,
                    "relative_path": item.relative_path,
                    "inventory_status": item.extraction_status,
                    "processing_status": result.status,
                    "quality_score": item.quality_score,
                    "issues": item.issues,
                    "processing_errors": result.errors,
                    "sensitive_flags": item.sensitive_flags,
                }
                for item, result in zip(items, results)
                if item.issues or item.sensitive_flags or result.errors or result.status == "failed"
            ],
        }
        (output_root / "quality_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return summary

    def _process_item(
        self, item: CorpusItem, documents_dir: Path, chunk_stream: TextIO
    ) -> ProcessedDocument:
        destination = documents_dir / f"{item.source_id}.txt"
        result = ProcessedDocument(source_id=item.source_id, status="processed", text_path=str(destination))
        try:
            if Path(item.path).suffix.lower() == ".pdf":
                self._process_pdf(item, destination, chunk_stream, result)
            else:
                content = Path(item.path).read_bytes()
                text, _ = parse_document(Path(item.path).name, content)
                destination.write_text(text, encoding="utf-8")
                result.text_chars = len(text)
                for ordinal, (heading, body) in enumerate(
                    split_text(text, self.chunk_size, self.chunk_overlap), 1
                ):
                    self._write_chunk(
                        chunk_stream,
                        item,
                        page_number=None,
                        ordinal=ordinal,
                        heading=heading,
                        text=body,
                    )
                    result.chunk_count += 1
                if not text.strip():
                    raise ValueError("no_text_extracted")
        except Exception as exc:
            result.status = "failed"
            result.errors.append(f"{type(exc).__name__}:{exc}")
        return result

    def _process_pdf(
        self,
        item: CorpusItem,
        destination: Path,
        chunk_stream: TextIO,
        result: ProcessedDocument,
    ) -> None:
        reader = PdfReader(item.path, strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        with destination.open("w", encoding="utf-8", newline="\n") as text_stream:
            for page_number, page in enumerate(reader.pages, 1):
                try:
                    text = normalize_pdf_text(page.extract_text() or "")
                except Exception as exc:
                    result.errors.append(f"page_{page_number}:{type(exc).__name__}")
                    text = ""
                text_stream.write(f"[第{page_number}页]\n{text}\n\n")
                if not text:
                    result.empty_pages += 1
                    continue
                result.extracted_pages += 1
                result.text_chars += len(text)
                chunks = split_text(text, self.chunk_size, self.chunk_overlap)
                for ordinal, (heading, body) in enumerate(chunks, 1):
                    self._write_chunk(
                        chunk_stream,
                        item,
                        page_number=page_number,
                        ordinal=ordinal,
                        heading=heading,
                        text=body,
                    )
                    result.chunk_count += 1
        if result.text_chars < 100:
            raise ValueError("insufficient_text_after_full_extraction")

    @staticmethod
    def _write_chunk(
        stream: TextIO,
        item: CorpusItem,
        *,
        page_number: int | None,
        ordinal: int,
        heading: str,
        text: str,
    ) -> None:
        location = f"第{page_number}页/{heading}" if page_number else heading
        payload = {
            "chunk_id": f"CHK-{item.source_id}-{page_number or 0:04d}-{ordinal:04d}",
            "source_id": item.source_id,
            "title": item.title,
            "source_type": item.source_type.value,
            "version": item.version,
            "effective_date": item.effective_date,
            "security_level": item.security_level.value,
            "page_number": page_number,
            "structure_path": heading,
            "content_location": location,
            "text": text,
            "char_count": len(text),
        }
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
